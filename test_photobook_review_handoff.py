from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from photobook_review_handoff import publish


def entry(i, price=20):
    return {'number': i, 'url': f'https://github.com/example/repo/issues/{i}',
            'created_at': f'2026-09-{i % 28 + 1:02d}T00:00:00Z',
            'lowest_observed_gbp': price, 'candidate_summaries': [{'title': f'Book {i}'}]}


class HandoffTests(unittest.TestCase):
    def run_publish(self, entries):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        path = Path(root.name)
        return publish({'queues': {'EBAY_PRIVATE_NEW': entries}, 'checked_at': 'now',
                        'index_scan_complete': True}, path), path

    def test_all_1005_issues_survive_in_small_pages(self):
        manifest, path = self.run_publish([entry(i) for i in range(1005)])
        pages = manifest['queues']['EBAY_PRIVATE_NEW']['pages']
        self.assertEqual(len(pages), 101)
        read = []
        for p in pages:
            data = json.loads((path / 'queue' / Path(p['path']).name).read_text())
            self.assertLessEqual(len(data['issues']), 10)
            read.extend(e['number'] for e in data['issues'])
        self.assertEqual(read, list(range(1005)))

    def test_oldest_affordable_has_direct_page_reference(self):
        manifest, _ = self.run_publish([entry(4, 200), entry(1, 10), entry(3, None)])
        queue = manifest['queues']['EBAY_PRIVATE_NEW']
        self.assertEqual(queue['oldest_affordable']['number'], 1)
        self.assertTrue(queue['oldest_affordable']['page'].endswith('0001.json'))
        self.assertTrue(queue['pages'][0]['contains_unknown_price'])

    def test_unknown_and_over_budget_issues_are_not_removed(self):
        manifest, _ = self.run_publish([entry(1, None), entry(2, 1000)])
        self.assertEqual(manifest['queues']['EBAY_PRIVATE_NEW']['issue_count'], 2)
        self.assertIsNone(manifest['queues']['EBAY_PRIVATE_NEW']['oldest_affordable'])

    def test_empty_queues_and_invalid_page_size(self):
        manifest, path = self.run_publish([])
        self.assertEqual(manifest['queues']['ENDGAME_4H']['pages'], [])
        with self.assertRaises(ValueError):
            publish({}, path, page_size=0)


if __name__ == '__main__':
    unittest.main()
