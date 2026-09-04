"""Behavioral tests for account-scoped state, receipts, resume, and health."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import FIXTURES, triage_core as core, triage_state as st

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)
ALICE = "alice@example.com"
BOB = "bob@example.com"


def receipt(**overrides):
    base = {
        "batch_id": "b1",
        "address": "news@example.com",
        "selected_action": "archive_label",
        "unsubscribe_method": "one_click",
        "unsubscribe_outcome": "not_attempted",
        "filter_outcome": "verified",
        "filter_id": "filter-1",
        "readback_verified": True,
        "status": "completed",
    }
    base.update(overrides)
    return base


class AccountScopeTests(unittest.TestCase):
    def test_invalid_accounts_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            for bad in ("", "   ", "not-an-address", "a@b", "two addr@example.com"):
                with self.assertRaises(st.StateError):
                    st.TriageState(Path(directory), bad)

    def test_two_accounts_sharing_a_directory_stay_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with st.TriageState(root, ALICE) as alice, st.TriageState(root, BOB) as bob:
                alice.protect("boss@example.com", "manager")
                alice.allow_override("news@example.com", "newsletter only")
                alice.set_preferences({"archive_label": "Alice/Bulk"})
                alice.upsert_receipt(receipt(), now=NOW)
                alice.record_observation("news@example.com", 14, 0, True, "quiet", now=NOW)

                self.assertEqual(bob.protected_senders(), {})
                self.assertEqual(bob.overrides(), {})
                self.assertEqual(bob.preferences()["archive_label"], st.DEFAULT_PREFERENCES["archive_label"])
                self.assertEqual(bob.receipts(), [])
                self.assertEqual(bob.observations(), [])
                self.assertEqual(bob.health(24, now=NOW)["receipts_recorded"], 0)
                self.assertEqual(bob.undo_targets("news@example.com"), [])

                self.assertEqual(alice.health(24, now=NOW)["receipts_recorded"], 1)
                self.assertEqual(len(alice.undo_targets("news@example.com")), 1)

    def test_state_files_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with st.TriageState(root, ALICE) as state:
                directory_mode = stat.S_IMODE(os.stat(root).st_mode)
                file_mode = stat.S_IMODE(os.stat(state.path).st_mode)
            self.assertEqual(directory_mode, 0o700)
            self.assertEqual(file_mode, 0o600)


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = st.TriageState(Path(self.directory.name), ALICE)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_defaults_then_persistence(self):
        self.assertEqual(self.state.preferences()["default_action"], "archive_label")
        self.state.set_preferences({"max_senders": 40, "default_action": "unsubscribe_and_archive_label"})
        with st.TriageState(Path(self.directory.name), ALICE) as reopened:
            prefs = reopened.preferences()
            self.assertEqual(prefs["max_senders"], 40)
            self.assertEqual(prefs["default_action"], "unsubscribe_and_archive_label")

    def test_out_of_range_and_unknown_preferences_are_refused(self):
        for updates in (
            {"max_senders": 0},
            {"max_senders": 10_000},
            {"window_days": "sixty"},
            {"default_action": "delete_all"},
            {"archive_label": ""},
            {"archive_label": "bad,label"},
            {"nonsense": 1},
        ):
            with self.assertRaises(st.StateError):
                self.state.set_preferences(updates)


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = st.TriageState(Path(self.directory.name), ALICE)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_wildcard_and_domain_overrides_are_refused(self):
        for bad in ("*@example.com", "@example.com", "example.com", "a@example.com,b@example.com", "*"):
            with self.assertRaises(st.StateError):
                self.state.allow_override(bad, "blanket permission attempt")
            with self.assertRaises(st.StateError):
                self.state.protect(bad, "blanket protection attempt")

    def test_override_requires_a_reason(self):
        with self.assertRaises(st.StateError):
            self.state.allow_override("news@example.com", "   ")

    def test_override_is_revocable_and_scoped_to_one_sender(self):
        self.state.allow_override("news@example.com", "newsletter only")
        self.assertEqual(list(self.state.overrides()), ["news@example.com"])
        self.assertTrue(self.state.revoke_override("NEWS@example.com"))
        self.assertEqual(self.state.overrides(), {})
        self.assertFalse(self.state.revoke_override("news@example.com"))

    def test_protection_is_case_insensitive(self):
        self.state.protect("Boss@Example.com", "manager")
        self.assertIn("boss@example.com", self.state.protected_senders())
        self.assertTrue(self.state.unprotect("boss@example.com"))


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = st.TriageState(Path(self.directory.name), ALICE)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_recording_twice_updates_instead_of_duplicating(self):
        self.assertEqual(self.state.upsert_receipt(receipt(), now=NOW), "inserted")
        self.assertEqual(self.state.upsert_receipt(receipt(), now=NOW), "updated")
        self.assertEqual(len(self.state.receipts()), 1)

    def test_invalid_receipt_values_are_refused(self):
        for bad in (
            {"batch_id": ""},
            {"address": "*@example.com"},
            {"selected_action": "delete"},
            {"unsubscribe_outcome": "probably_worked"},
            {"filter_outcome": "kind_of"},
            {"status": "finished"},
        ):
            with self.assertRaises(st.StateError):
                self.state.upsert_receipt(receipt(**bad), now=NOW)

    def test_handled_lookup_respects_the_window(self):
        self.state.upsert_receipt(receipt(), now=NOW - timedelta(days=120))
        self.assertEqual(self.state.handled_addresses(90, now=NOW), {})
        self.state.upsert_receipt(receipt(batch_id="b2"), now=NOW - timedelta(days=5))
        self.assertIn("news@example.com", self.state.handled_addresses(90, now=NOW))

    def test_pending_receipts_are_not_treated_as_handled(self):
        self.state.upsert_receipt(receipt(status="pending", filter_outcome="not_requested",
                                          filter_id=None, readback_verified=False), now=NOW)
        self.assertEqual(self.state.handled_addresses(90, now=NOW), {})

    def test_undo_lookup_returns_only_recorded_filter_ids(self):
        self.state.upsert_receipt(receipt(), now=NOW)
        self.state.upsert_receipt(receipt(batch_id="b2", selected_action="unsubscribe_only",
                                          filter_outcome="not_requested", filter_id=None,
                                          readback_verified=False), now=NOW)
        targets = self.state.undo_targets("news@example.com")
        self.assertEqual([item["filter_id"] for item in targets], ["filter-1"])


class MonitorAndHealthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = st.TriageState(Path(self.directory.name), ALICE)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_unknown_monitor_outcome_is_refused(self):
        with self.assertRaises(st.StateError):
            self.state.record_observation("news@example.com", 14, 0, True, "probably_quiet", now=NOW)

    def test_health_counts_only_recorded_receipts(self):
        self.state.upsert_receipt(receipt(), now=NOW)
        self.state.upsert_receipt(receipt(
            batch_id="b2", address="deals@shop.example.net", selected_action="unsubscribe_only",
            unsubscribe_outcome="submitted", filter_outcome="not_requested",
            filter_id=None, readback_verified=False,
        ), now=NOW)
        self.state.record_observation("deals@shop.example.net", 14, 0, True, "quiet", now=NOW)
        report = self.state.health(24, now=NOW)
        self.assertEqual(report["receipts_recorded"], 2)
        self.assertEqual(report["filters_verified_by_readback"], 1)
        self.assertEqual(report["actions_by_type"], {"archive_label": 1, "unsubscribe_only": 1})
        self.assertEqual(report["unsubscribe_outcomes"], {"not_attempted": 1, "submitted": 1})
        self.assertEqual(report["monitor_outcomes"], {"quiet": 1})
        self.assertEqual(report["inconsistent_records"], [])
        self.assertIn("not proof of causation", report["evidence_basis"])

    def test_health_ignores_receipts_outside_the_period(self):
        self.state.upsert_receipt(receipt(), now=NOW - timedelta(days=3))
        self.assertEqual(self.state.health(24, now=NOW)["receipts_recorded"], 0)
        self.assertEqual(self.state.health(24 * 7, now=NOW)["receipts_recorded"], 1)

    def test_health_flags_verified_without_readback(self):
        self.state.connection.execute(
            """INSERT INTO receipts (account, batch_id, address, selected_action,
                   unsubscribe_method, unsubscribe_outcome, filter_outcome, filter_id,
                   readback_verified, existing_messages_affected, status, note,
                   first_recorded_at, updated_at)
               VALUES (?, 'b9', 'news@example.com', 'archive_label', 'one_click',
                   'not_attempted', 'verified', 'filter-9', 0, 0, 'completed', '', ?, ?)""",
            (ALICE, NOW.isoformat(), NOW.isoformat()),
        )
        self.state.connection.commit()
        report = self.state.health(24, now=NOW)
        self.assertEqual(report["filters_verified_by_readback"], 0)
        self.assertIn("without a Gmail readback", report["inconsistent_records"][0]["problem"])


class RecordAndResumeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.records = core.load_records(FIXTURES / "sample-messages.jsonl")
        aggregated = core.aggregate(self.records, NOW, 1, 100, 3)
        self.plan = core.build_plan(
            aggregated,
            [
                {"target": "news@example.com", "selected_action": "unsubscribe_and_archive_label"},
                {"target": "deals@shop.example.net", "selected_action": "trash"},
                {"target": "updates@app.example.org", "selected_action": "unsubscribe_only"},
            ],
            ALICE, "Triage/Bulk", 25, NOW,
        )

    def tearDown(self):
        self.directory.cleanup()

    def results_for(self, *addresses, status="completed"):
        rows = []
        for _, action in core.plan_actions(self.plan):
            if action["address"] not in addresses:
                continue
            has_filter = bool(action["gmail_filter"])
            rows.append({
                "address": action["address"],
                "selected_action": action["selected_action"],
                "unsubscribe_outcome": "submitted" if action["unsubscribe"]["requested"] else "not_attempted",
                "filter_outcome": "verified" if has_filter else "not_requested",
                "filter_id": f"filter-{action['address']}" if has_filter else None,
                "readback_verified": has_filter,
                "status": status,
            })
        return {"results": rows}

    def test_partial_batch_then_resume_completes_the_rest(self):
        with st.TriageState(self.root, ALICE) as state:
            first = core.record_results(state, self.plan, self.results_for("news@example.com"), NOW)
            self.assertEqual((first["inserted"], first["updated"]), (1, 0))
            resumed = core.resume_plan(state, self.plan)
            self.assertEqual([item["address"] for item in resumed["completed"]], ["news@example.com"])
            self.assertEqual(
                sorted(item["address"] for item in resumed["pending"]),
                ["deals@shop.example.net", "updates@app.example.org"],
            )
            second = core.record_results(
                state, self.plan,
                self.results_for("deals@shop.example.net", "updates@app.example.org"), NOW,
            )
            self.assertEqual((second["inserted"], second["updated"]), (2, 0))
            final = core.resume_plan(state, self.plan)
            self.assertEqual(final["pending"], [])
            self.assertEqual(len(final["completed"]), 3)

    def test_replaying_the_same_results_does_not_double_count(self):
        with st.TriageState(self.root, ALICE) as state:
            core.record_results(state, self.plan, self.results_for("news@example.com"), NOW)
            again = core.record_results(state, self.plan, self.results_for("news@example.com"), NOW)
            self.assertEqual((again["inserted"], again["updated"]), (0, 1))
            self.assertEqual(state.health(24, now=NOW)["receipts_recorded"], 1)

    def test_failed_action_is_offered_for_retry_after_readback(self):
        with st.TriageState(self.root, ALICE) as state:
            core.record_results(state, self.plan, self.results_for("news@example.com", status="failed"), NOW)
            resumed = core.resume_plan(state, self.plan)
            retry = resumed["retry_after_readback"]
            self.assertEqual([item["address"] for item in retry], ["news@example.com"])
            self.assertTrue(retry[0]["read_back_first"])
            self.assertIn("read current Gmail state", resumed["note"])

    def test_results_outside_the_plan_are_rejected(self):
        with st.TriageState(self.root, ALICE) as state:
            outcome = core.record_results(state, self.plan, {"results": [{
                "address": "stranger@example.com",
                "selected_action": "trash",
                "status": "completed",
            }]}, NOW)
            self.assertEqual(outcome["inserted"], 0)
            self.assertIn("not present in this plan", outcome["rejected"][0]["reason"])

    def test_created_filter_without_an_id_is_rejected(self):
        with st.TriageState(self.root, ALICE) as state:
            outcome = core.record_results(state, self.plan, {"results": [{
                "address": "deals@shop.example.net",
                "selected_action": "trash",
                "filter_outcome": "created",
                "status": "completed",
            }]}, NOW)
            self.assertEqual(outcome["inserted"], 0)
            self.assertIn("filter id from readback", outcome["rejected"][0]["reason"])

    def test_readback_claim_without_verification_is_rejected(self):
        with st.TriageState(self.root, ALICE) as state:
            outcome = core.record_results(state, self.plan, {"results": [{
                "address": "deals@shop.example.net",
                "selected_action": "trash",
                "filter_outcome": "created",
                "filter_id": "filter-1",
                "readback_verified": True,
                "status": "completed",
            }]}, NOW)
            self.assertIn("requires filter_outcome 'verified'", outcome["rejected"][0]["reason"])

    def test_unsubscribe_only_receipt_cannot_carry_a_filter(self):
        with st.TriageState(self.root, ALICE) as state:
            outcome = core.record_results(state, self.plan, {"results": [{
                "address": "updates@app.example.org",
                "selected_action": "unsubscribe_only",
                "filter_outcome": "verified",
                "filter_id": "filter-x",
                "readback_verified": True,
                "status": "completed",
            }]}, NOW)
            self.assertEqual(outcome["inserted"], 0)
            self.assertIn("must not report a filter outcome", outcome["rejected"][0]["reason"])

    def test_malformed_results_payloads_are_reported(self):
        with st.TriageState(self.root, ALICE) as state:
            with self.assertRaises(core.InputError):
                core.record_results(state, self.plan, {"results": "everything worked"}, NOW)
            outcome = core.record_results(state, self.plan, {"results": ["oops", {"address": "x"}]}, NOW)
            self.assertEqual(outcome["inserted"], 0)
            self.assertEqual(len(outcome["rejected"]), 2)

    def test_plan_from_another_account_is_refused(self):
        with st.TriageState(self.root, BOB) as bob:
            with self.assertRaises(core.InputError) as caught:
                core.record_results(bob, self.plan, self.results_for("news@example.com"), NOW)
            self.assertIn("scoped to", str(caught.exception))
            with self.assertRaises(core.InputError):
                core.resume_plan(bob, self.plan)


class MonitorAndDiagnoseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.messages = [
            {"id": "n1", "from_address": "news@example.com", "date": "2026-09-05T00:00:00Z", "labels": ["TRASH"]},
            {"id": "n2", "from_address": "other@example.com", "date": "2026-09-06T00:00:00Z", "labels": ["INBOX"]},
        ]

    def tearDown(self):
        self.directory.cleanup()

    def test_search_without_spam_and_trash_cannot_prove_quiet(self):
        with st.TriageState(self.root, ALICE) as state:
            result = core.monitor(state, "news@example.com", [], NOW, 14, False, NOW)
            self.assertEqual(result["outcome"], "insufficient_evidence")
            self.assertIn("spam and trash", result["interpretation"])

    def test_post_decision_message_means_still_sending(self):
        with st.TriageState(self.root, ALICE) as state:
            result = core.monitor(state, "news@example.com", self.messages, NOW, 14, True, NOW)
            self.assertEqual(result["outcome"], "still_sending")
            self.assertEqual(result["messages_after_decision"], 1)
            self.assertEqual(len(state.observations()), 1)

    def test_quiet_when_nothing_arrived_after_the_decision(self):
        older = [{"id": "n0", "from_address": "news@example.com", "date": "2026-08-01T00:00:00Z", "labels": ["INBOX"]}]
        with st.TriageState(self.root, ALICE) as state:
            result = core.monitor(state, "news@example.com", older, NOW, 14, True, NOW)
            self.assertEqual(result["outcome"], "quiet")
            self.assertIn("not proof", result["interpretation"])

    def test_diagnose_explains_filter_and_location(self):
        live = [{"id": "f-1", "criteria": {"from": "news@example.com"}, "action": {"addLabelIds": ["TRASH"]}}]
        with st.TriageState(self.root, ALICE) as state:
            state.upsert_receipt(receipt(selected_action="trash", filter_id="f-1"), now=NOW)
            result = core.diagnose(state, "news@example.com", self.messages, live)
            self.assertEqual(result["messages_found"], 1)
            self.assertEqual(result["locations"]["trash"], 1)
            self.assertTrue(any("sends new mail from this sender to Trash" in item for item in result["explanations"]))
            self.assertEqual(result["recorded_actions"][0]["filter_id"], "f-1")

    def test_diagnose_reports_when_no_filter_or_message_matches(self):
        with st.TriageState(self.root, ALICE) as state:
            result = core.diagnose(state, "ghost@example.com", self.messages, [])
            self.assertEqual(result["messages_found"], 0)
            self.assertTrue(any("no Gmail filter" in item for item in result["explanations"]))
            self.assertTrue(any("include_spam_trash" in item for item in result["explanations"]))


if __name__ == "__main__":
    unittest.main()
