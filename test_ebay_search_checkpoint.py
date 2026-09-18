from __future__ import annotations
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import ebay_api
import ebay_endgame as endgame
import ebay_private_recall_monitor as recall
import ebay_private_seller_monitor as private
import ebay_search_checkpoint as search

START = "2026-09-18T01:00:00Z"
END = "2026-09-18T02:00:00Z"
NEXT = "https://api.ebay.com/buy/browse/v1/item_summary/search?offset=200"

def rows(start, stop):
    return [{"itemId": f"v1|{i:012d}|0", "title": f"Photo book {i}",
             "price": {"currency": "GBP", "value": "10"},
             "buyingOptions": ["FIXED_PRICE"]} for i in range(start, stop)]

class CheckpointTests(unittest.TestCase):
    def test_350_results_survive_restart_with_one_call_per_run(self):
        checkpoint = search.new_checkpoint(START, END)
        first, calls, complete = search.drain(checkpoint,
            lambda *a: {"itemSummaries": rows(0, 200), "next": NEXT, "total": 350},
            lambda url: self.fail("Budget exceeded"), max_calls=1)
        self.assertEqual((len(first), calls, complete), (200, 1, False))
        saved = json.loads(json.dumps(checkpoint))
        second, calls, complete = search.drain(saved,
            lambda *a: self.fail("Must not restart at page one"),
            lambda url: {"itemSummaries": rows(200, 350), "offset": 200, "total": 350}, max_calls=1)
        self.assertEqual((len(second), calls, complete), (150, 1, True))
        self.assertEqual(len({r['itemId'] for r in first + second}), 350)
        self.assertEqual(saved['end'], END)

    def test_failed_next_keeps_cursor_and_preceding_rows(self):
        checkpoint = search.new_checkpoint(START, END)
        def fail(url):
            raise RuntimeError("temporary failure")
        found, calls, complete = search.drain(checkpoint,
            lambda *a: {"itemSummaries": rows(0, 200), "next": NEXT}, fail, max_calls=3)
        self.assertEqual((len(found), calls, complete), (200, 2, False))
        self.assertEqual(checkpoint['pending'][0]['next'], NEXT)
        self.assertIn('temporary', checkpoint['last_error'])

    def test_zero_budget_does_not_consume_work(self):
        checkpoint = search.new_checkpoint(START, END)
        before = copy.deepcopy(checkpoint)
        self.assertEqual(search.drain(checkpoint, None, None, max_calls=0), ([], 0, False))
        self.assertEqual(checkpoint, before)

    def test_result_ceiling_splits_even_without_next(self):
        checkpoint = search.new_checkpoint(START, END)
        found, calls, complete = search.drain(checkpoint,
            lambda *a: {"itemSummaries": rows(0, 200), "total": 14000}, None, max_calls=1)
        self.assertEqual(len(found), 200)
        self.assertFalse(complete)
        self.assertEqual(len(checkpoint['pending']), 2)
        self.assertEqual(checkpoint['pending'][0]['start'], START)
        self.assertEqual(checkpoint['pending'][0]['end'], checkpoint['pending'][1]['start'])
        self.assertEqual(checkpoint['pending'][1]['end'], END)

    def test_unsplittable_result_ceiling_is_not_success(self):
        checkpoint = search.new_checkpoint(START, START)
        _, _, complete = search.drain(checkpoint,
            lambda *a: {"itemSummaries": rows(0, 200), "total": 12000}, None, max_calls=1)
        self.assertFalse(complete)
        self.assertIn('Unsplittable', checkpoint['last_error'])
        self.assertTrue(checkpoint['pending'])

    def test_repeated_next_url_cannot_report_complete(self):
        checkpoint = search.new_checkpoint(START, END)
        _, calls, complete = search.drain(checkpoint,
            lambda *a: {"itemSummaries": rows(0, 200), "next": NEXT},
            lambda u: {"itemSummaries": rows(0, 200), "next": NEXT}, max_calls=9)
        self.assertEqual(calls, 2)
        self.assertFalse(complete)
        self.assertIn('Repeated', checkpoint['last_error'])

    def test_fully_drained_cursor_does_not_issue_more_calls(self):
        checkpoint = search.new_checkpoint(START, END)
        search.drain(checkpoint, lambda *a: {"itemSummaries": []}, None, max_calls=1)
        self.assertEqual(search.drain(checkpoint, None, None, max_calls=10), ([], 0, True))

    def test_duplicate_ids_are_deduplicated_within_a_run(self):
        checkpoint = search.new_checkpoint(START, END)
        found, _, complete = search.drain(checkpoint,
            lambda *a: {"itemSummaries": rows(0, 200), "next": NEXT},
            lambda u: {"itemSummaries": rows(199, 300)}, max_calls=2)
        self.assertTrue(complete)
        self.assertEqual(len(found), 300)

    def test_invalid_window_rejected(self):
        with self.assertRaises(ValueError):
            search.new_checkpoint(END, START)
        with self.assertRaises(ValueError):
            search.new_checkpoint("2026-09-18T01:00:00", END)

    def test_resume_share_is_bounded_and_oldest_attempt_first(self):
        state = {"query_windows": {str(i): {'pending': [{}], 'step': {'query': str(i)},
                    'last_attempt_at': f'2026-09-18T01:{i:02d}:00Z'} for i in range(12)}}
        selected = search.pending_steps(state, 17)
        self.assertEqual([s['query'] for s in selected], ['0', '1', '2', '3'])
        self.assertTrue(all(s['resume_only'] for s in selected))
        self.assertEqual(search.pending_steps(state, 0), [])

    def test_resume_and_new_work_share_existing_17_call_budget(self):
        config = recall.recall_config(private.load_config(Path('data/ebay_private_recall_searches.json')))
        state = private.load_state(Path('/no/such/state'))
        state['query_windows'] = {str(i): {'pending': [{}], 'step': {'query': str(i), 'lane': 'broad'}}
                                  for i in range(7)}
        plan = recall.build_budgeted_search_plan(config, state, search.parsed(END), 17)
        self.assertEqual(len(plan), 17)
        self.assertEqual(sum(bool(s.get('resume_only')) for s in plan), 4)
        self.assertTrue(any(s['lane'] == 'core_target_3' for s in plan))

    def test_private_watermark_moves_only_after_last_page(self):
        class Client:
            def search_page(self, query, **kwargs):
                self.window = kwargs
                return {'itemSummaries': rows(0, 200), 'next': NEXT, 'total': 350}
            def search_next(self, url):
                return {'itemSummaries': rows(200, 350), 'total': 350, 'offset': 200}
        client = Client()
        state = {'query_last_checked': {'broad:FIXED_PRICE:photobook': START}}
        kwargs = dict(lane='broad', query='photobook', category_ids='261186',
                      buying_options=['FIXED_PRICE'], search_in_description=True, limit=200,
                      delivery_country='GB', max_price_gbp=750, incremental=True,
                      ending_start_date=None, ending_end_date=None)
        first = private.run_query(client, state, detected_at=END, **kwargs)
        self.assertEqual(len(first), 200)
        self.assertEqual(state['query_last_checked']['broad:FIXED_PRICE:photobook'], START)
        second = private.run_query(client, state, detected_at='2026-09-18T04:00:00Z', **kwargs)
        self.assertEqual(len(second), 150)
        self.assertEqual(state['query_last_checked']['broad:FIXED_PRICE:photobook'], END)
        self.assertEqual(state['query_windows'], {})
        self.assertEqual(client.window['item_end_date'], END)

    def test_endgame_resume_does_not_start_new_window(self):
        class Client:
            def search_page(self, query, **kwargs):
                return {'itemSummaries': rows(0, 200), 'next': NEXT}
            def search_next(self, url):
                return {'itemSummaries': rows(200, 350)}
        checkpoint = search.new_checkpoint(START, END)
        config = endgame.load_config()
        task = endgame.build_tasks(config)[0]
        now = search.parsed(START)
        first, _, complete = endgame.search_task(Client(), task, config, now, 0, checkpoint)
        self.assertFalse(complete)
        with patch.object(Client, 'search_page', side_effect=AssertionError('Do not restart')):
            second, _, complete = endgame.search_task(Client(), task, config, now+timedelta(hours=1), 0, checkpoint)
        self.assertTrue(complete)
        self.assertEqual(len(first)+len(second), 350)

    def test_all_failed_discovery_does_not_advance_success_timestamp(self):
        from test_ebay_endgame import FakeSearchClient, FakePool
        class Failing(FakeSearchClient):
            def search_page(self, *a, **kw):
                self.browse_calls += 1
                raise ebay_api.EbayApiError('HTTP 503')
        config = endgame.load_config()
        state = endgame.blank_state()
        state['last_discovery_at'] = START
        result = endgame.discover(config, state, search.parsed(END), FakePool(Failing()), 2)
        self.assertEqual(state['last_discovery_at'], START)
        self.assertEqual(state['last_discovery_attempt_at'], END)
        self.assertEqual(result['successful_tasks'], 0)
        self.assertEqual(len(result['errors']), 2)
        self.assertEqual(len(state['search_windows']), 2)


class ShippingTests(unittest.TestCase):
    def test_missing_shipping_is_not_free(self):
        item = ebay_api.listing_from_summary(rows(0, 1)[0], {'id': 'test', 'name': 'test'})
        self.assertEqual(item['price_gbp'], 10)
        self.assertIsNone(item['shipping_value'])
        self.assertIsNone(item['landed_price_gbp'])

    def test_explicit_free_shipping_keeps_delivered_total(self):
        raw = rows(0, 1)[0]
        raw['shippingOptions'] = [{'shippingCost': {'value': '0', 'currency': 'GBP'}}]
        item = ebay_api.listing_from_summary(raw, {'id': 'test', 'name': 'test'})
        self.assertEqual(item['landed_price_gbp'], 10)

    def test_detail_currency_change_clears_stale_gbp(self):
        item = private._merge_live_detail({'price_gbp': 20, 'shipping_value': 3, 'landed_price_gbp': 23},
                                         {'price': {'value': '40', 'currency': 'USD'}})
        self.assertIsNone(item['price_gbp'])
        self.assertIsNone(item['landed_price_gbp'])
        self.assertIsNone(item['shipping_value'])

    def test_unknown_shipping_remains_unknown_after_detail_fetch(self):
        item = private._merge_live_detail({'shipping_value': 3, 'landed_price_gbp': 13},
                                         {'price': {'value': '10', 'currency': 'GBP'}})
        self.assertIsNone(item['landed_price_gbp'])
        self.assertEqual(item['price_gbp'], 10)

if __name__ == '__main__':
    unittest.main()
