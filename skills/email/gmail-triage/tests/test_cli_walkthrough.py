"""End-to-end CLI walkthrough: the commands a fresh agent runs, in order.

Each test shells out to scripts/triage_core.py the same way the SKILL.md steps do,
so argparse wiring, exit codes, and file outputs are covered, not just functions.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import FIXTURES, SCRIPTS

CLI = SCRIPTS / "triage_core.py"
ACCOUNT = "you@example.com"
OTHER = "work@example.com"
NOW = "2026-09-04T00:00:00Z"


def run(*args, expect: int = 0):
    process = subprocess.run(
        [sys.executable, str(CLI), *[str(arg) for arg in args]],
        capture_output=True, text=True, timeout=120,
    )
    assert process.returncode == expect, (
        f"expected exit {expect}, got {process.returncode}\nstdout={process.stdout}\nstderr={process.stderr}"
    )
    payload = json.loads(process.stdout) if process.stdout.strip() and expect == 0 else {}
    return payload, process


class CliWalkthroughTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state = self.root / "state"

    def tearDown(self):
        self.directory.cleanup()

    def base(self, account=ACCOUNT):
        return ["--account", account, "--state-dir", str(self.state)]

    def write(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_first_use_walkthrough_from_preflight_to_health(self):
        profile = self.write("profile.json", {"data": {"emailAddress": ACCOUNT}})
        tools = self.write("tools.json", [
            "GMAIL_GET_PROFILE", "GMAIL_FETCH_EMAILS", "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
            "GMAIL_LIST_LABELS", "GMAIL_CREATE_LABEL", "GMAIL_LIST_FILTERS",
            "GMAIL_CREATE_FILTER", "GMAIL_GET_FILTER",
        ])
        preflight, _ = run("preflight", "--account", ACCOUNT, "--mode", "cleanup_execute",
                           "--profile", profile, "--tools", tools)
        self.assertTrue(preflight["ready"])

        prefs, _ = run("prefs", *self.base(), "set", "--max-senders", "10",
                       "--archive-label", "Triage/Bulk", "--default-action", "archive_label")
        self.assertEqual(prefs["preferences"]["max_senders"], 10)

        run("prefs", *self.base(), "protect", "--address", "lead@example.org", "--reason", "manager")

        candidates_path = self.root / "candidates.json"
        stdout_payload, _ = run("rank", *self.base(), "--input", FIXTURES / "sample-messages.jsonl",
                                "--output", candidates_path, "--now", NOW)
        self.assertEqual(stdout_payload, {}, "--output writes the file instead of stdout")
        candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
        self.assertEqual(candidates["account"], ACCOUNT)
        self.assertEqual(candidates["duplicates_removed"], 1)
        by_address = {item["address"]: item for item in candidates["candidates"]}
        self.assertTrue(by_address["lead@example.org"]["protected"])
        self.assertIn("protected list", " ".join(by_address["lead@example.org"]["protected_reasons"]))

        plan_path = self.root / "plan.json"
        run("plan", *self.base(), "--input", FIXTURES / "sample-messages.jsonl",
            "--selected", FIXTURES / "sample-selection.txt", "--output", plan_path, "--now", NOW)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["totals"]["planned"], 3)
        self.assertEqual([item["address"] for item in plan["blocked"]], ["billing@shop.example.com"])
        self.assertEqual(plan["unknown_selected"], ["ghost@example.com"])

        empty_filters = self.write("live-empty.json", {"filter": []})
        check, _ = run("filters-check", "--plan", plan_path, "--live-filters", empty_filters,
                       "--label-id", "Label_42")
        self.assertEqual(len(check["to_create"]), 2)
        self.assertEqual(check["conflicts"], [])

        live = self.write("live-after.json", {"filter": [
            {"id": f"filter-{index}", **entry["filter"]}
            for index, entry in enumerate(check["to_create"], start=1)
        ]})
        verify, _ = run("filters-verify", "--plan", plan_path, "--live-filters", live,
                        "--label-id", "Label_42")
        self.assertEqual(verify["verified"], 2)

        results = self.write("results.json", {"results": [
            {
                "address": item["address"],
                "selected_action": item["selected_action"],
                "unsubscribe_outcome": "submitted" if item["selected_action"].startswith("unsubscribe") else "not_attempted",
                "filter_outcome": item["filter_outcome"],
                "filter_id": item["filter_id"],
                "readback_verified": item["readback_verified"],
                "status": "completed",
            }
            for item in verify["results"]
        ]})
        recorded, _ = run("record", *self.base(), "--plan", plan_path, "--results", results, "--now", NOW)
        self.assertEqual(recorded["inserted"], 3)
        self.assertEqual(recorded["rejected"], [])

        resumed, _ = run("resume", *self.base(), "--plan", plan_path)
        self.assertEqual(resumed["pending"], [])

        handled, _ = run("handled", *self.base(), "--days", "90", "--now", NOW)
        self.assertIn("news@example.com", handled["handled"])

        second_pass, _ = run("rank", *self.base(), "--input", FIXTURES / "sample-messages.jsonl",
                             "--exclude-handled", "--now", NOW)
        self.assertNotIn("news@example.com", [item["address"] for item in second_pass["candidates"]])
        self.assertEqual(second_pass["excluded_counts"]["already_handled"], 3)

        history, _ = run("history", *self.base(), "--address", "news@example.com", "--now", NOW)
        self.assertEqual(len(history["receipts"]), 1)

        undo, _ = run("undo", *self.base(), "--address", "news@example.com")
        self.assertEqual(undo["delete_filter_ids"], ["filter-1"])
        self.assertEqual(undo["tool"], "GMAIL_DELETE_FILTER")

        health, _ = run("health", *self.base(), "--hours", "24", "--now", NOW)
        self.assertEqual(health["receipts_recorded"], 3)
        self.assertEqual(health["filters_verified_by_readback"], 2)
        self.assertEqual(health["inconsistent_records"], [])
        self.assertEqual(health["protected_senders"], 1)

    def test_monitor_and_diagnose_commands(self):
        run("prefs", *self.base(), "show")
        followup = self.root / "followup.jsonl"
        followup.write_text(json.dumps({
            "id": "n9", "from_address": "news@example.com",
            "date": "2026-09-10T00:00:00Z", "labels": ["TRASH"],
        }) + "\n", encoding="utf-8")

        without_flag, _ = run("monitor", *self.base(), "--address", "news@example.com",
                              "--input", followup, "--decision-at", NOW, "--now", "2026-09-11T00:00:00Z")
        self.assertEqual(without_flag["outcome"], "insufficient_evidence")

        with_flag, _ = run("monitor", *self.base(), "--address", "news@example.com",
                           "--input", followup, "--decision-at", NOW, "--include-spam-trash",
                           "--now", "2026-09-11T00:00:01Z")
        self.assertEqual(with_flag["outcome"], "still_sending")

        live = self.write("live.json", {"filter": [
            {"id": "f-1", "criteria": {"from": "news@example.com"}, "action": {"addLabelIds": ["TRASH"]}}
        ]})
        diagnosis, _ = run("diagnose", *self.base(), "--address", "news@example.com",
                           "--input", followup, "--live-filters", live)
        self.assertEqual(diagnosis["locations"]["trash"], 1)
        self.assertTrue(any("Trash" in item for item in diagnosis["explanations"]))

    def test_existing_cleanup_requires_explicit_scope_flag(self):
        _, refused = run("cleanup-existing", "--address", "news@example.com",
                         "--input", FIXTURES / "sample-messages.jsonl",
                         "--action", "trash", "--max", "5", expect=2)
        self.assertIn("separate authorization", refused.stderr)

        allowed, _ = run("cleanup-existing", "--address", "news@example.com",
                         "--input", FIXTURES / "sample-messages.jsonl",
                         "--action", "trash", "--max", "2", "--confirm-existing-scope")
        self.assertEqual(allowed["selected_count"], 2)
        self.assertTrue(allowed["truncated"])
        self.assertEqual(allowed["tool"], "GMAIL_MOVE_TO_TRASH")

    def test_cross_account_state_is_isolated_on_the_command_line(self):
        run("prefs", *self.base(), "protect", "--address", "lead@example.org", "--reason", "manager")
        plan_path = self.root / "plan.json"
        run("plan", *self.base(), "--input", FIXTURES / "sample-messages.jsonl",
            "--selected", FIXTURES / "sample-selection.txt", "--output", plan_path, "--now", NOW)

        other_prefs, _ = run("prefs", *self.base(OTHER), "show")
        self.assertEqual(other_prefs["protected_senders"], {})

        results = self.write("results.json", {"results": [{
            "address": "deals@shop.example.net", "selected_action": "trash",
            "filter_outcome": "verified", "filter_id": "filter-1",
            "readback_verified": True, "status": "completed",
        }]})
        _, refused = run("record", *self.base(OTHER), "--plan", plan_path,
                         "--results", results, expect=2)
        self.assertIn("scoped to", refused.stderr)

        run("record", *self.base(), "--plan", plan_path, "--results", results, "--now", NOW)
        other_health, _ = run("health", *self.base(OTHER), "--now", NOW)
        mine_health, _ = run("health", *self.base(), "--now", NOW)
        self.assertEqual(other_health["receipts_recorded"], 0)
        self.assertEqual(mine_health["receipts_recorded"], 1)

    def test_invalid_cli_input_exits_with_a_named_reason(self):
        cases = [
            (["prefs", "--account", "nope", "--state-dir", str(self.state), "show"], "exact connected Gmail address"),
            (["prefs", *self.base(), "set"], "at least one preference"),
            (["prefs", *self.base(), "allow", "--address", "*@example.com", "--reason", "x"], "wildcard"),
            (["prefs", *self.base(), "allow", "--address", "news@example.com"], "reason"),
            (["health", *self.base(), "--hours", "0"], "--hours"),
            (["rank", *self.base(), "--input", str(self.root / "missing.jsonl")], "cannot read"),
            (["rank", *self.base(), "--input", str(FIXTURES / "sample-messages.jsonl"), "--min-count", "0"], "min_count"),
        ]
        for argv, expected in cases:
            _, process = run(*argv, expect=2)
            self.assertIn(expected, process.stderr, argv)

    def test_remedy_command_from_a_blocked_plan_runs_as_printed(self):
        run("prefs", *self.base(), "protect", "--address", "billing@shop.example.com", "--reason", "orders")
        plan, _ = run("plan", *self.base(), "--input", FIXTURES / "sample-messages.jsonl",
                      "--selected", FIXTURES / "sample-selection.txt", "--now", NOW)
        remedy = next(item for item in plan["blocked"] if item["address"] == "billing@shop.example.com")["remedy"]
        self.assertIn("prefs allow --account you@example.com --address billing@shop.example.com", remedy)

        # The printed order puts the subcommand before its options; argparse must accept it.
        run("prefs", "allow", "--account", ACCOUNT, "--state-dir", str(self.state),
            "--address", "billing@shop.example.com", "--reason", "shop promotions only")
        after, _ = run("plan", *self.base(), "--input", FIXTURES / "sample-messages.jsonl",
                       "--selected", FIXTURES / "sample-selection.txt", "--now", NOW)
        self.assertIn("billing@shop.example.com", [action["address"] for batch in after["batches"] for action in batch["actions"]])
        self.assertEqual([item["address"] for item in after["blocked"]], [])

    def test_selftest_command_passes(self):
        payload, _ = run("selftest")
        self.assertTrue(payload["ok"])
        self.assertTrue(all(payload["checks"].values()))


if __name__ == "__main__":
    unittest.main()
