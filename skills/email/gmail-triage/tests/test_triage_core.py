import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "triage_core.py"
SPEC = importlib.util.spec_from_file_location("triage_core", SCRIPT)
CORE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(CORE)


class TriageCoreTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 4, tzinfo=timezone.utc)
        self.messages = [
            {
                "id": "m1",
                "from_address": "Example News <news@example.com>",
                "date": "2026-09-03T12:00:00Z",
                "labels": ["INBOX", "UNREAD"],
                "list_unsubscribe": ["https://example.com/u/token"],
                "list_unsubscribe_post": "List-Unsubscribe=One-Click",
            },
            {
                "id": "m2",
                "from_address": "news@example.com",
                "from_name": "Example News",
                "date": "2026-08-20T12:00:00Z",
                "labels": ["INBOX"],
            },
            {
                "id": "m3",
                "from_address": "security@example.net",
                "date": "2026-09-03T12:00:00Z",
                "labels": ["IMPORTANT", "INBOX"],
            },
        ]

    def test_rank_groups_exact_sender_and_detects_one_click(self):
        candidates = CORE.rank(self.messages, self.now, min_count=2)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["address"], "news@example.com")
        self.assertEqual(candidates[0]["unsubscribe_method"], "one_click")
        self.assertFalse(candidates[0]["protected"])

    def test_protected_sender_is_flagged(self):
        candidates = CORE.rank(self.messages, self.now, min_count=1)
        security = next(item for item in candidates if item["address"] == "security@example.net")
        self.assertTrue(security["protected"])

    def test_plan_is_future_only_and_reports_missing(self):
        plan = CORE.build_plan(self.messages, {"news@example.com", "missing@example.org"}, self.now)
        self.assertEqual(len(plan["actions"]), 1)
        self.assertTrue(plan["actions"][0]["gmail_filter"]["future_only"])
        self.assertFalse(plan["actions"][0]["affect_existing_messages"])
        self.assertEqual(plan["missing_selected"], ["missing@example.org"])

    def test_record_and_health_are_privacy_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = CORE.build_plan(self.messages, {"news@example.com"}, self.now)
            plan_path = root / "plan.json"
            results_path = root / "results.json"
            db_path = root / "state.sqlite3"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            results_path.write_text(json.dumps({"results": [{
                "address": "news@example.com",
                "unsubscribe_result": "submitted",
                "filter_result": "verified",
                "filter_id": "filter-1",
                "existing_messages_affected": False,
            }]}), encoding="utf-8")
            CORE.record_results(db_path, plan_path, results_path, self.now)
            report = CORE.health(db_path, 24, self.now)
            self.assertEqual(report["actions"], 1)
            self.assertEqual(report["unsubscribed"], 1)
            self.assertEqual(report["filters_verified"], 1)
            self.assertEqual(report["existing_messages_affected"], 0)


if __name__ == "__main__":
    unittest.main()
