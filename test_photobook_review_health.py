```python
import copy
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
import photobook_review_health as health

NOW = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)

def issue(n, title='EBAY_PRIVATE_NEW: test', comments=0):
    return {'number': n, 'title': title, 'comments': comments,
            'created_at': '2026-09-17T05:00:00Z', 'updated_at': '2026-09-17T05:00:00Z',
            'html_url': f'https://github.com/owner/repo/issues/{n}',
            'body': '### An unusual photography book\n- **Observed price:** £24.50\n- **Listing:** https://www.ebay.co.uk/itm/123456789012\n'}

def getter(issues, comments=None, recent=None):
    def get(endpoint):
        page = int(parse_qs(urlparse(endpoint).query)['page'][0])
        if '/issues/comments?' in endpoint:
            values = recent or []
        elif '/comments?' in endpoint:
            values = comments or []
        else:
            values = issues
        return copy.deepcopy(values[(page-1)*100:page*100])
    return get

class HealthTests(unittest.TestCase):
    def test_list_paginates_beyond_1000_issues(self):
        data = health.collect('owner/repo', getter([issue(i) for i in range(1005)]))
        self.assertEqual(len(data['queues']['EBAY_PRIVATE_NEW']), 1005)
        self.assertTrue(data['index_scan_complete'])

    def test_only_exact_source_prefixes_are_accepted(self):
        issues = [issue(1), issue(2, 'BACKFILL: EBAY_PRIVATE_NEW: test'),
                  issue(3, 'EXTERNAL_NEW: test'), issue(4, 'ENDGAME_EARLY: test')]
        data = health.collect('owner/repo', getter(issues))
        self.assertEqual(sum(map(len, data['queues'].values())), 2)

    def test_pr_is_not_an_issue(self):
        row = issue(1); row['pull_request'] = {}
        data = health.collect('owner/repo', getter([row]))
        self.assertEqual(sum(map(len, data['queues'].values())), 0)

    def test_comment_marker_inside_issue_body_is_not_a_receipt(self):
        row = issue(1); row['body'] += '\nCHATGPT_GEM_REVIEWED:\n'
        data = health.collect('owner/repo', getter([row]))
        self.assertEqual(len(data['queues']['EBAY_PRIVATE_NEW']), 1)
        self.assertIsNone(data['last_completed_review_at'])

    def test_untrusted_comment_cannot_suppress_candidate(self):
        comment = {'body': 'CHATGPT_GEM_REVIEWED: PASS', 'user': {'login': 'stranger'},
                   'created_at': '2026-09-18T05:00:00Z'}
        data = health.collect('owner/repo', getter([issue(1, comments=1)], [comment]))
        self.assertEqual(len(data['queues']['EBAY_PRIVATE_NEW']), 1)

    def test_completed_marker_on_later_comment_page_is_found(self):
        comments = [{'body': 'ordinary comment'} for i in range(100)]
        comments.append({'body': 'CHATGPT_GEM_REVIEWED: PASS', 'user': {'login': 'owner'},
                         'created_at': '2026-09-18T05:00:00Z'})
        data = health.collect('owner/repo', getter([issue(1, comments=101)], comments))
        self.assertEqual(data['completed_but_open'], [1])
        self.assertEqual(len(data['queues']['EBAY_PRIVATE_NEW']), 0)

    def test_closed_issue_receipt_is_observed_from_recent_comments(self):
        comment = {'body': 'CHATGPT_GEM_REVIEWED: PASS', 'user': {'login': 'owner'},
                   'created_at': '2026-09-18T05:00:00Z'}
        data = health.collect('owner/repo', getter([], recent=[comment]))
        self.assertEqual(data['last_completed_review_at'], comment['created_at'])

    def test_stale_or_missing_receipt_cannot_be_green_with_waiting_work(self):
        data = health.collect('owner/repo', getter([issue(1)]))
        report = health.health(data, {'last_discovery_at': '2026-09-18T05:55:00Z'}, NOW)
        self.assertEqual(report['status'], 'ATTENTION')
        self.assertTrue(any('completed review' in w for w in report['warnings']))

    def test_elapsed_and_urgent_deadlines_are_reported_separately(self):
        row = issue(1, 'ENDGAME_EARLY: test')
        row['body'] += '\n- **Deadline:** 2026-09-18T05:00:00Z\n'
        data = health.collect('owner/repo', getter([row]))
        report = health.health(data, {'last_discovery_at': '2026-09-18T05:55:00Z'}, NOW)
        self.assertIn('deadlines', report)
```
import copy
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
import photobook_review_health as health

NOW = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)

def issue(n, title='EBAY_PRIVATE_NEW: test', comments=0):
    return {'number': n, 'title': title, 'comments': comments,
            'created_at': '2026-09-17T05:00:00Z', 'updated_at': '2026-09-17T05:00:00Z',
            'html_url': f'https://github.com/owner/repo/issues/{n}',
            'body': '### An unusual photography book\n- **Observed price:** £24.50\n- **Listing:** https://www.ebay.co.uk/itm/123456789012\n'}

def getter(issues, comments=None, recent=None):
    def get(endpoint):
        page = int(parse_qs(urlparse(endpoint).query)['page'][0])
        if '/issues/comments?' in endpoint:
            values = recent or []
        elif '/comments?' in endpoint:
            values = comments or []
        else:
            values = issues
        return copy.deepcopy(values[(page-1)*100:page*100])
    return get

class HealthTests(unittest.TestCase):
    def test_list_paginates_beyond_1000_issues(self):
        data = health.collect('owner/repo', getter([issue(i) for i in range(1005)]))
        self.assertEqual(len(data['queues']['EBAY_PRIVATE_NEW']), 1005)
        self.assertTrue(data['index_scan_complete'])

    def test_only_exact_source_prefixes_are_accepted(self):
        issues = [issue(1), issue(2, 'BACKFILL: EBAY_PRIVATE_NEW: test'),
                  issue(3, 'EXTERNAL_NEW: test'), issue(4, 'ENDGAME_EARLY: test')]
        data = health.collect('owner/repo', getter(issues))
        self.assertEqual(sum(map(len, data['queues'].values())), 2)

    def test_pr_is_not_an_issue(self):
        row = issue(1); row['pull_request'] = {}
        data = health.collect('owner/repo', getter([row]))
        self.assertEqual(sum(map(len, data['queues'].values())), 0)

    def test_comment_marker_inside_issue_body_is_not_a_receipt(self):
        row = issue(1); row['body'] += '\nCHATGPT_GEM_REVIEWED:\n'
        data = health.collect('owner/repo', getter([row]))
        self.assertEqual(len(data['queues']['EBAY_PRIVATE_NEW']), 1)
        self.assertIsNone(data['last_completed_review_at'])

    def test_untrusted_comment_cannot_suppress_candidate(self):
        comment = {'body': 'CHATGPT_GEM_REVIEWED: PASS', 'user': {'login': 'stranger'},
                   'created_at': '2026-09-18T05:00:00Z'}
        data = health.collect('owner/repo', getter([issue(1, comments=1)], [comment]))
        self.assertEqual(len(data['queues']['EBAY_PRIVATE_NEW']), 1)

    def test_completed_marker_on_later_comment_page_is_found(self):
        comments = [{'body': 'ordinary comment'} for i in range(100)]
        comments.append({'body': 'CHATGPT_GEM_REVIEWED: PASS', 'user': {'login': 'owner'},
                         'created_at': '2026-09-18T05:00:00Z'})
        data = health.collect('owner/repo', getter([issue(1, comments=101)], comments))
        self.assertEqual(data['completed_but_open'], [1])
        self.assertEqual(len(data['queues']['EBAY_PRIVATE_NEW']), 0)

    def test_closed_issue_receipt_is_observed_from_recent_comments(self):
        comment = {'body': 'CHATGPT_GEM_REVIEWED: PASS', 'user': {'login': 'owner'},
                   'created_at': '2026-09-18T05:00:00Z'}
        data = health.collect('owner/repo', getter([], recent=[comment]))
        self.assertEqual(data['last_completed_review_at'], comment['created_at'])

    def test_stale_or_missing_receipt_cannot_be_green_with_waiting_work(self):
        data = health.collect('owner/repo', getter([issue(1)]))
        report = health.health(data, {'last_discovery_at': '2026-09-18T05:55:00Z'}, NOW)
        self.assertEqual(report['status'], 'ATTENTION')
        self.assertTrue(any('completed review' in w for w in report['warnings']))

    def test_elapsed_and_urgent_deadlines_are_reported_separately(self):
        row = issue(1, 'ENDGAME_EARLY: test')
        row['body'] += '\n- **Deadline:** 2026-09-18T05:00:00Z\n'
        data = health.collect('owner/repo', getter([row]))
        report = health.health(data, {'last_discovery_at': '2026-09-18T05:55:00Z'}, NOW)
        self.assertIn('deadlines', report)
