#!/usr/bin/env python3
"""Account-scoped local state for the Gmail Triage skill.

Stores preferences, protected senders, exact-sender overrides, action receipts,
and monitoring observations. Every row is keyed by the connected Gmail account so
two accounts sharing one state directory never read or mutate each other's data.
No message bodies, subjects, unsubscribe URLs, tokens, or credentials are stored.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ACTIONS = (
    "unsubscribe_only",
    "archive_label",
    "trash",
    "unsubscribe_and_archive_label",
    "unsubscribe_and_trash",
)

DEFAULT_PREFERENCES: dict[str, Any] = {
    "window_days": 60,
    "min_count": 2,
    "max_senders": 25,
    "sample_limit": 3,
    "batch_size": 25,
    "archive_label": "Triage/Bulk",
    "default_action": "archive_label",
    "monitor_days": 14,
}

INTEGER_PREFERENCES = {
    "window_days": (1, 3650),
    "min_count": (1, 1000),
    "max_senders": (1, 500),
    "sample_limit": (0, 25),
    "batch_size": (1, 500),
    "monitor_days": (1, 365),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  account TEXT PRIMARY KEY,
  first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
  account TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (account, key)
);
CREATE TABLE IF NOT EXISTS protected_senders (
  account TEXT NOT NULL,
  address TEXT NOT NULL,
  reason TEXT NOT NULL,
  added_at TEXT NOT NULL,
  PRIMARY KEY (account, address)
);
CREATE TABLE IF NOT EXISTS sender_overrides (
  account TEXT NOT NULL,
  address TEXT NOT NULL,
  reason TEXT NOT NULL,
  added_at TEXT NOT NULL,
  PRIMARY KEY (account, address)
);
CREATE TABLE IF NOT EXISTS receipts (
  account TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  address TEXT NOT NULL,
  selected_action TEXT NOT NULL,
  unsubscribe_method TEXT NOT NULL DEFAULT 'unavailable',
  unsubscribe_outcome TEXT NOT NULL DEFAULT 'not_attempted',
  filter_outcome TEXT NOT NULL DEFAULT 'not_requested',
  filter_id TEXT,
  readback_verified INTEGER NOT NULL DEFAULT 0,
  existing_messages_affected INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  note TEXT NOT NULL DEFAULT '',
  first_recorded_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (account, batch_id, address, selected_action)
);
CREATE TABLE IF NOT EXISTS monitor_observations (
  account TEXT NOT NULL,
  address TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  window_days INTEGER NOT NULL,
  messages_after_decision INTEGER NOT NULL,
  include_spam_trash INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  PRIMARY KEY (account, address, observed_at)
);
"""

UNSUBSCRIBE_OUTCOMES = {
    "not_attempted",
    "submitted",
    "mailto_sent",
    "review_required",
    "failed",
    "unavailable",
}

FILTER_OUTCOMES = {
    "not_requested",
    "created",
    "verified",
    "mismatch",
    "missing",
    "duplicate_skipped",
    "failed",
}

MONITOR_OUTCOMES = {
    "quiet",
    "still_sending",
    "insufficient_evidence",
}

STATUSES = {"pending", "completed", "partial", "failed"}


class StateError(ValueError):
    """Raised for invalid account, preference, or receipt input."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_account(value: str | None) -> str:
    account = (value or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", account):
        raise StateError(
            "account must be the exact connected Gmail address, e.g. you@example.com"
        )
    return account


def normalize_exact_address(value: str | None) -> str:
    """Accept one exact mailbox only. Wildcards and domain patterns are refused."""
    address = (value or "").strip().lower().strip("<>")
    if any(char in address for char in "*?%,; ") or address.startswith("@"):
        raise StateError(
            "only one exact sender address is accepted; wildcard, list, and "
            "domain-wide patterns are refused"
        )
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        raise StateError(f"not an exact sender address: {value!r}")
    return address


def validate_action(value: str | None) -> str:
    action = (value or "").strip().lower()
    if action not in ACTIONS:
        raise StateError(f"unknown action {value!r}; expected one of {', '.join(ACTIONS)}")
    return action


class TriageState:
    """SQLite-backed store scoped to one Gmail account."""

    def __init__(self, state_dir: Path, account: str) -> None:
        self.account = normalize_account(account)
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.path = self.state_dir / "gmail-triage-state.sqlite3"
        existed = self.path.exists()
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        if not existed:
            os.chmod(self.path, 0o600)
        self.connection.execute(
            "INSERT OR IGNORE INTO accounts (account, first_seen_at) VALUES (?, ?)",
            (self.account, utcnow().isoformat()),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "TriageState":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ----- preferences -------------------------------------------------
    def preferences(self) -> dict[str, Any]:
        values = dict(DEFAULT_PREFERENCES)
        rows = self.connection.execute(
            "SELECT key, value FROM preferences WHERE account = ?", (self.account,)
        ).fetchall()
        for row in rows:
            values[row["key"]] = json.loads(row["value"])
        return values

    def set_preferences(self, updates: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        stamp = (now or utcnow()).isoformat()
        for key, raw in updates.items():
            if key not in DEFAULT_PREFERENCES:
                raise StateError(f"unknown preference {key!r}")
            value = self._coerce_preference(key, raw)
            self.connection.execute(
                """INSERT INTO preferences (account, key, value, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account, key) DO UPDATE SET value = excluded.value,
                       updated_at = excluded.updated_at""",
                (self.account, key, json.dumps(value), stamp),
            )
        self.connection.commit()
        return self.preferences()

    @staticmethod
    def _coerce_preference(key: str, raw: Any) -> Any:
        if key in INTEGER_PREFERENCES:
            low, high = INTEGER_PREFERENCES[key]
            try:
                value = int(raw)
            except (TypeError, ValueError) as error:
                raise StateError(f"{key} must be an integer") from error
            if not low <= value <= high:
                raise StateError(f"{key} must be between {low} and {high}")
            return value
        if key == "default_action":
            return validate_action(str(raw))
        if key == "archive_label":
            label = str(raw).strip()
            if not label or "," in label:
                raise StateError("archive_label must be non-empty and contain no comma")
            return label
        return str(raw)

    # ----- protections and overrides ----------------------------------
    def protect(self, address: str, reason: str, now: datetime | None = None) -> str:
        exact = normalize_exact_address(address)
        self.connection.execute(
            """INSERT INTO protected_senders (account, address, reason, added_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(account, address) DO UPDATE SET reason = excluded.reason""",
            (self.account, exact, reason.strip() or "user protected", (now or utcnow()).isoformat()),
        )
        self.connection.commit()
        return exact

    def unprotect(self, address: str) -> bool:
        exact = normalize_exact_address(address)
        cursor = self.connection.execute(
            "DELETE FROM protected_senders WHERE account = ? AND address = ?",
            (self.account, exact),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def protected_senders(self) -> dict[str, str]:
        rows = self.connection.execute(
            "SELECT address, reason FROM protected_senders WHERE account = ? ORDER BY address",
            (self.account,),
        ).fetchall()
        return {row["address"]: row["reason"] for row in rows}

    def allow_override(self, address: str, reason: str, now: datetime | None = None) -> str:
        """Permit action on one exact protected sender. Never a blanket permission."""
        exact = normalize_exact_address(address)
        if not reason.strip():
            raise StateError("an override requires a reason naming why this exact sender is safe")
        self.connection.execute(
            """INSERT INTO sender_overrides (account, address, reason, added_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(account, address) DO UPDATE SET reason = excluded.reason""",
            (self.account, exact, reason.strip(), (now or utcnow()).isoformat()),
        )
        self.connection.commit()
        return exact

    def revoke_override(self, address: str) -> bool:
        exact = normalize_exact_address(address)
        cursor = self.connection.execute(
            "DELETE FROM sender_overrides WHERE account = ? AND address = ?",
            (self.account, exact),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def overrides(self) -> dict[str, str]:
        rows = self.connection.execute(
            "SELECT address, reason FROM sender_overrides WHERE account = ? ORDER BY address",
            (self.account,),
        ).fetchall()
        return {row["address"]: row["reason"] for row in rows}

    # ----- receipts ---------------------------------------------------
    def upsert_receipt(self, receipt: dict[str, Any], now: datetime | None = None) -> str:
        """Idempotently record one (batch, sender, action) outcome.

        Returns "inserted" or "updated" so a resumed batch never double counts.
        """
        stamp = (now or utcnow()).isoformat()
        address = normalize_exact_address(receipt.get("address"))
        action = validate_action(receipt.get("selected_action"))
        batch_id = str(receipt.get("batch_id") or "").strip()
        if not batch_id:
            raise StateError("receipt requires batch_id")
        unsub_outcome = str(receipt.get("unsubscribe_outcome", "not_attempted"))
        if unsub_outcome not in UNSUBSCRIBE_OUTCOMES:
            raise StateError(f"unknown unsubscribe outcome {unsub_outcome!r}")
        filter_outcome = str(receipt.get("filter_outcome", "not_requested"))
        if filter_outcome not in FILTER_OUTCOMES:
            raise StateError(f"unknown filter outcome {filter_outcome!r}")
        status = str(receipt.get("status", "pending"))
        if status not in STATUSES:
            raise StateError(f"unknown status {status!r}")
        existing = self.connection.execute(
            """SELECT 1 FROM receipts
               WHERE account = ? AND batch_id = ? AND address = ? AND selected_action = ?""",
            (self.account, batch_id, address, action),
        ).fetchone()
        self.connection.execute(
            """INSERT INTO receipts (account, batch_id, address, selected_action,
                   unsubscribe_method, unsubscribe_outcome, filter_outcome, filter_id,
                   readback_verified, existing_messages_affected, status, note,
                   first_recorded_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account, batch_id, address, selected_action) DO UPDATE SET
                   unsubscribe_method = excluded.unsubscribe_method,
                   unsubscribe_outcome = excluded.unsubscribe_outcome,
                   filter_outcome = excluded.filter_outcome,
                   filter_id = excluded.filter_id,
                   readback_verified = excluded.readback_verified,
                   existing_messages_affected = excluded.existing_messages_affected,
                   status = excluded.status,
                   note = excluded.note,
                   updated_at = excluded.updated_at""",
            (
                self.account, batch_id, address, action,
                str(receipt.get("unsubscribe_method", "unavailable")),
                unsub_outcome, filter_outcome,
                (str(receipt["filter_id"]) if receipt.get("filter_id") else None),
                int(bool(receipt.get("readback_verified", False))),
                int(receipt.get("existing_messages_affected", 0) or 0),
                status, str(receipt.get("note", ""))[:200],
                stamp, stamp,
            ),
        )
        self.connection.commit()
        return "updated" if existing else "inserted"

    def receipts(
        self,
        address: str | None = None,
        batch_id: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM receipts WHERE account = ?"
        params: list[Any] = [self.account]
        if address:
            query += " AND address = ?"
            params.append(normalize_exact_address(address))
        if batch_id:
            query += " AND batch_id = ?"
            params.append(batch_id)
        if since:
            query += " AND updated_at >= ?"
            params.append(since.isoformat())
        query += " ORDER BY updated_at, address"
        return [dict(row) for row in self.connection.execute(query, params).fetchall()]

    def completed_keys(self, batch_id: str | None = None) -> set[tuple[str, str]]:
        rows = self.receipts(batch_id=batch_id)
        return {
            (row["address"], row["selected_action"])
            for row in rows
            if row["status"] == "completed"
        }

    def handled_addresses(self, days: int, now: datetime | None = None) -> dict[str, str]:
        cutoff = ((now or utcnow()) - timedelta(days=days)).isoformat()
        rows = self.connection.execute(
            """SELECT address, MAX(updated_at) AS last_at FROM receipts
               WHERE account = ? AND updated_at >= ? AND status IN ('completed', 'partial')
               GROUP BY address ORDER BY address""",
            (self.account, cutoff),
        ).fetchall()
        return {row["address"]: row["last_at"] for row in rows}

    def undo_targets(self, address: str) -> list[dict[str, Any]]:
        rows = self.receipts(address=address)
        return [
            {
                "batch_id": row["batch_id"],
                "address": row["address"],
                "selected_action": row["selected_action"],
                "filter_id": row["filter_id"],
                "filter_outcome": row["filter_outcome"],
                "recorded_at": row["first_recorded_at"],
            }
            for row in rows
            if row["filter_id"]
        ]

    # ----- monitoring -------------------------------------------------
    def record_observation(
        self,
        address: str,
        window_days: int,
        messages_after_decision: int,
        include_spam_trash: bool,
        outcome: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if outcome not in MONITOR_OUTCOMES:
            raise StateError(f"unknown monitor outcome {outcome!r}")
        exact = normalize_exact_address(address)
        stamp = (now or utcnow()).isoformat()
        self.connection.execute(
            """INSERT INTO monitor_observations (account, address, observed_at, window_days,
                   messages_after_decision, include_spam_trash, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account, address, observed_at) DO UPDATE SET
                   window_days = excluded.window_days,
                   messages_after_decision = excluded.messages_after_decision,
                   include_spam_trash = excluded.include_spam_trash,
                   outcome = excluded.outcome""",
            (self.account, exact, stamp, int(window_days),
             int(messages_after_decision), int(bool(include_spam_trash)), outcome),
        )
        self.connection.commit()
        return {"address": exact, "observed_at": stamp, "outcome": outcome}

    def observations(self, address: str | None = None, since: datetime | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM monitor_observations WHERE account = ?"
        params: list[Any] = [self.account]
        if address:
            query += " AND address = ?"
            params.append(normalize_exact_address(address))
        if since:
            query += " AND observed_at >= ?"
            params.append(since.isoformat())
        query += " ORDER BY observed_at, address"
        return [dict(row) for row in self.connection.execute(query, params).fetchall()]

    # ----- health -----------------------------------------------------
    def health(self, hours: int, now: datetime | None = None) -> dict[str, Any]:
        moment = now or utcnow()
        since = moment - timedelta(hours=hours)
        rows = self.receipts(since=since)
        by_action: dict[str, int] = {}
        unsubscribe: dict[str, int] = {}
        filters: dict[str, int] = {}
        inconsistent: list[dict[str, str]] = []
        verified_filters = 0
        existing_affected = 0
        for row in rows:
            by_action[row["selected_action"]] = by_action.get(row["selected_action"], 0) + 1
            unsubscribe[row["unsubscribe_outcome"]] = unsubscribe.get(row["unsubscribe_outcome"], 0) + 1
            filters[row["filter_outcome"]] = filters.get(row["filter_outcome"], 0) + 1
            existing_affected += row["existing_messages_affected"]
            if row["filter_outcome"] == "verified" and row["readback_verified"]:
                verified_filters += 1
            elif row["filter_outcome"] == "verified":
                inconsistent.append({
                    "address": row["address"],
                    "problem": "filter marked verified without a Gmail readback",
                })
            if row["filter_outcome"] in {"created", "verified"} and not row["filter_id"]:
                inconsistent.append({
                    "address": row["address"],
                    "problem": "filter reported created without a recorded filter id",
                })
            if row["selected_action"] == "unsubscribe_only" and row["filter_outcome"] != "not_requested":
                inconsistent.append({
                    "address": row["address"],
                    "problem": "unsubscribe_only receipt carries a filter outcome",
                })
            if row["existing_messages_affected"] and row["status"] == "pending":
                inconsistent.append({
                    "address": row["address"],
                    "problem": "existing messages affected while the action is still pending",
                })
        observations = self.observations(since=since)
        monitor_counts: dict[str, int] = {}
        for row in observations:
            monitor_counts[row["outcome"]] = monitor_counts.get(row["outcome"], 0) + 1
        return {
            "account": self.account,
            "period_hours": hours,
            "generated_at": moment.isoformat(),
            "receipts_recorded": len(rows),
            "actions_by_type": dict(sorted(by_action.items())),
            "status_counts": _count(rows, "status"),
            "unsubscribe_outcomes": dict(sorted(unsubscribe.items())),
            "filter_outcomes": dict(sorted(filters.items())),
            "filters_verified_by_readback": verified_filters,
            "existing_messages_affected": existing_affected,
            "monitor_outcomes": dict(sorted(monitor_counts.items())),
            "protected_senders": len(self.protected_senders()),
            "active_overrides": len(self.overrides()),
            "inconsistent_records": inconsistent,
            "evidence_basis": (
                "counts come only from recorded receipts and readback flags in this "
                "account's state; Gmail mailbox state alone is not proof of causation"
            ),
        }


def _count(rows: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row[key]] = counts.get(row[key], 0) + 1
    return dict(sorted(counts.items()))
