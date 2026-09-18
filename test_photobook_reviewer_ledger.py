import copy
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import photobook_reviewer_ledger as ledger

NOW = datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc)
TASK = "Endgame Urgent Review"


def comment(body, created="2026-09-18T07:50:00Z", login="owner", cid=1):
    return {
        "id": cid,
        "body": body,
        "created_at": created,
        "html_url": f"https://github.com/owner/repo/issues/99#issuecomment-{cid}",
        "user": {"login": login},
    }


def marker(kind, payload, **kwargs):
    prefix = ledger.START_MARKER if kind == "start" else ledger.FINISH_MARKER
    import json
    return comment(prefix + " " + json.dumps(payload), **kwargs)


class FakeGitHub:
    def __init__(self, ledger_comments=None, issues=None, issue_comments=None):
        self.ledger_comments = ledger_comments or []
        self.issues = issues or {}
        self.issue_comments = issue_comments or {}

    def get_list(self, endpoint):
        parsed = urlparse(endpoint)
        page = int(parse_qs(parsed.query).get("page", ["1"])[0])
        path = parsed.path
        if path.endswith("/issues/99/comments"):
            values = self.ledger_comments
        else:
            parts = path.strip("/").split("/")
            issue_number = int(parts[-2]) if len(parts) >= 2 and parts[-1] == "comments" else None
            values = self.issue_comments.get(issue_number, [])
        return copy.deepcopy(values[(page - 1) * 100: page * 100])

    def get_object(self, endpoint):
        number = int(endpoint.rstrip("/").split("/")[-1])
        return copy.deepcopy(self.issues[number])


class ReviewerLedgerTests(unittest.TestCase):
    def test_valid_run_is_independently_verified(self):
        run_id = "urgent|2026-09-18T07:45:00Z"
        start = marker("start", {
            "run_id": run_id, "task": TASK, "started_at": "2026-09-18T07:45:00Z",
        }, created="2026-09-18T07:45:01Z", cid=1)
        finish = marker("finish", {
            "run_id": run_id, "task": TASK, "started_at": "2026-09-18T07:45:00Z",
            "finished_at": "2026-09-18T07:50:00Z", "status": "COMPLETED",
            "completed_issue_numbers": [12], "unique_listings_reviewed": 8,
            "reportable_listing_count": 1, "errors": [],
        }, cid=2)
        gh = FakeGitHub(
            [start, finish],
            issues={12: {"state": "closed"}},
            issue_comments={12: [comment("CHATGPT_GEM_REVIEWED: PASS", login="owner")]},
        )
        receipt = ledger.derive_receipt(
            "owner/repo", 99, gh.get_list, gh.get_object, NOW,
            required_tasks=(TASK,),
        )
        self.assertEqual(receipt["status"], "OK")
        task = receipt["tasks"][TASK]
        self.assertEqual(task["status"], "COMPLETED")
        self.assertEqual(task["verified_completed_issue_numbers"], [12])
        self.assertEqual(receipt["latest_verified_completion_at"], "2026-09-18T07:50:00Z")

    def test_claimed_completion_must_be_closed_and_marked(self):
        run_id = "urgent|x"
        gh = FakeGitHub(
            [
                marker("start", {"run_id": run_id, "task": TASK, "started_at": "2026-09-18T07:45:00Z"}),
                marker("finish", {
                    "run_id": run_id, "task": TASK, "finished_at": "2026-09-18T07:50:00Z",
                    "status": "COMPLETED", "completed_issue_numbers": [12],
                }, cid=2),
            ],
            issues={12: {"state": "open"}},
        )
        receipt = ledger.derive_receipt(
            "owner/repo", 99, gh.get_list, gh.get_object, NOW,
            required_tasks=(TASK,),
        )
        self.assertEqual(receipt["status"], "ATTENTION")
        self.assertEqual(receipt["tasks"][TASK]["status"], "UNVERIFIED")
        self.assertIn("12", receipt["tasks"][TASK]["invalid_completed_issue_claims"])

    def test_empty_completed_list_can_still_prove_a_real_run(self):
        run_id = "fixed|x"
        task = "eBay Fixed Price Review"
        gh = FakeGitHub([
            marker("start", {"run_id": run_id, "task": task, "started_at": "2026-09-18T07:45:00Z"}),
            marker("finish", {
                "run_id": run_id, "task": task, "finished_at": "2026-09-18T07:49:00Z",
                "status": "COMPLETED", "completed_issue_numbers": [],
                "unique_listings_reviewed": 0, "reportable_listing_count": 0, "errors": [],
            }, cid=2),
        ])
        receipt = ledger.derive_receipt(
            "owner/repo", 99, gh.get_list, gh.get_object, NOW,
            required_tasks=(task,),
        )
        self.assertEqual(receipt["status"], "OK")
        self.assertEqual(receipt["tasks"][task]["status"], "COMPLETED")

    def test_stale_start_without_finish_is_attention(self):
        gh = FakeGitHub([
            marker("start", {
                "run_id": "urgent|stuck", "task": TASK,
                "started_at": "2026-09-18T06:30:00Z",
            }, created="2026-09-18T06:30:00Z"),
        ])
        receipt = ledger.derive_receipt(
            "owner/repo", 99, gh.get_list, gh.get_object, NOW,
            required_tasks=(TASK,), stale_start_minutes=30,
        )
        self.assertEqual(receipt["status"], "ATTENTION")
        self.assertTrue(any("no matching FINISH" in w for w in receipt["warnings"]))

    def test_untrusted_or_malformed_markers_are_not_completion_evidence(self):
        bad = comment(
            ledger.FINISH_MARKER + ' {"run_id":"x","task":"' + TASK + '"}',
            login="stranger",
        )
        malformed = comment(ledger.START_MARKER + " not-json", login="owner", cid=2)
        gh = FakeGitHub([bad, malformed])
        receipt = ledger.derive_receipt(
            "owner/repo", 99, gh.get_list, gh.get_object, NOW,
            required_tasks=(TASK,),
        )
        self.assertEqual(receipt["tasks"][TASK]["status"], "NO_VERIFIED_FINISH")
        self.assertTrue(any("malformed" in w for w in receipt["warnings"]))

    def test_finish_without_matching_start_is_not_accepted(self):
        gh = FakeGitHub([
            marker("finish", {
                "run_id": "orphan", "task": TASK,
                "finished_at": "2026-09-18T07:50:00Z",
                "status": "COMPLETED", "completed_issue_numbers": [],
            }),
        ])
        receipt = ledger.derive_receipt(
            "owner/repo", 99, gh.get_list, gh.get_object, NOW,
            required_tasks=(TASK,),
        )
        self.assertEqual(receipt["tasks"][TASK]["status"], "NO_VERIFIED_FINISH")
        self.assertTrue(any("no matching START" in w for w in receipt["warnings"]))


if __name__ == "__main__":
    unittest.main()
