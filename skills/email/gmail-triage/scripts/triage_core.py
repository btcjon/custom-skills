#!/usr/bin/env python3
"""Deterministic, offline helpers for the Gmail Triage skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable


PROTECTED_TERMS = {
    "account", "admin", "alert", "auth", "billing", "bank", "calendar",
    "doctor", "fraud", "invoice", "legal", "login", "medical", "meeting",
    "order", "password", "payment", "receipt", "security", "support", "tax",
    "ticket", "travel", "verification", "verify",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = parsedate_to_datetime(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_address(value: str) -> tuple[str, str]:
    name, address = parseaddr(value or "")
    address = address.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        return "", name.strip()
    return address, name.strip()


def load_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        value = json.loads(text)
        if not isinstance(value, list):
            raise ValueError("JSON input must be an array or JSON Lines")
        return [item for item in value if isinstance(item, dict)]
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def dump_json(value: Any, path: Path | None = None) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path:
        path.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def labels_of(message: dict[str, Any]) -> set[str]:
    raw = message.get("labels") or message.get("label_ids") or []
    return {str(label).upper() for label in raw}


def unsub_values(message: dict[str, Any]) -> tuple[list[str], str]:
    raw = message.get("list_unsubscribe", message.get("List-Unsubscribe", []))
    if isinstance(raw, str):
        raw = [part.strip().strip("<>") for part in raw.split(",")]
    values = [str(item).strip().strip("<>") for item in (raw or [])]
    post = str(message.get("list_unsubscribe_post", message.get("List-Unsubscribe-Post", "")))
    return values, post


def unsubscribe_method(messages: Iterable[dict[str, Any]]) -> str:
    has_https = has_mailto = has_one_click = False
    for message in messages:
        values, post = unsub_values(message)
        has_https = has_https or any(value.lower().startswith("https://") for value in values)
        has_mailto = has_mailto or any(value.lower().startswith("mailto:") for value in values)
        has_one_click = has_one_click or "one-click" in post.lower()
    if has_https and has_one_click:
        return "one_click"
    if has_mailto:
        return "mailto"
    if has_https:
        return "web_review"
    return "unavailable"


def is_protected(address: str, name: str, messages: list[dict[str, Any]]) -> bool:
    haystack = f"{address} {name}".lower()
    if any(term in haystack for term in PROTECTED_TERMS):
        return True
    return any(labels_of(message) & {"IMPORTANT", "STARRED"} for message in messages)


def candidate_id(address: str) -> str:
    return "c_" + hashlib.sha256(address.encode("utf-8")).hexdigest()[:10]


def rank(records: list[dict[str, Any]], now: datetime, min_count: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    names: dict[str, str] = {}
    for message in records:
        raw_address = str(message.get("from_address") or message.get("from") or "")
        address, parsed_name = normalize_address(raw_address)
        if not address:
            continue
        grouped[address].append(message)
        names[address] = str(message.get("from_name") or parsed_name or names.get(address, ""))

    candidates = []
    recent_cutoff = now - timedelta(days=30)
    for address, messages in grouped.items():
        if len(messages) < min_count:
            continue
        dates = [parse_time(message.get("date")) for message in messages]
        label_sets = [labels_of(message) for message in messages]
        unread = sum("UNREAD" in labels for labels in label_sets)
        inbox = sum("INBOX" in labels for labels in label_sets)
        recent = sum(date >= recent_cutoff for date in dates)
        method = unsubscribe_method(messages)
        protected = is_protected(address, names[address], messages)
        score = len(messages) + (recent * 2) + unread + inbox
        reasons = []
        if len(messages) >= 10:
            reasons.append("high volume")
        if recent >= 3:
            reasons.append("frequent recently")
        if unread >= max(2, len(messages) // 2):
            reasons.append("often unread")
        if method == "one_click":
            reasons.append("one-click available")
        elif method == "mailto":
            reasons.append("mailto unsubscribe available")
        if protected:
            reasons.append("protected; manual review")
        candidates.append({
            "candidate_id": candidate_id(address),
            "address": address,
            "display_name": names[address],
            "total": len(messages),
            "last_30_days": recent,
            "unread": unread,
            "inbox": inbox,
            "last_seen": max(dates).isoformat(),
            "unsubscribe_method": method,
            "score": score,
            "protected": protected,
            "reason": "; ".join(reasons) or "recurring sender",
        })
    return sorted(candidates, key=lambda item: (item["protected"], -item["score"], item["address"]))


def read_selected(path: Path) -> set[str]:
    selected = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        selected.add(value.lower())
    return selected


def build_plan(records: list[dict[str, Any]], selected: set[str], now: datetime) -> dict[str, Any]:
    ranked = rank(records, now, min_count=1)
    by_address = {item["address"]: item for item in ranked}
    missing = sorted(selected - set(by_address))
    actions = []
    for address in sorted(selected & set(by_address)):
        item = by_address[address]
        actions.append({
            "candidate_id": item["candidate_id"],
            "address": address,
            "unsubscribe_method": item["unsubscribe_method"],
            "unsubscribe_action": "attempt" if item["unsubscribe_method"] != "unavailable" else "unavailable",
            "gmail_filter": {"from": address, "action": "trash", "future_only": True},
            "affect_existing_messages": False,
            "protected": item["protected"],
        })
    return {
        "created_at": now.isoformat(),
        "actions": actions,
        "missing_selected": missing,
        "requires_manual_review": [action["address"] for action in actions if action["protected"]],
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS action_receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recorded_at TEXT NOT NULL,
  address TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  unsubscribe_method TEXT NOT NULL,
  unsubscribe_result TEXT NOT NULL,
  filter_result TEXT NOT NULL,
  filter_id TEXT,
  existing_messages_affected INTEGER NOT NULL DEFAULT 0
);
"""


def record_results(db_path: Path, plan_path: Path, results_path: Path, now: datetime) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    results = json.loads(results_path.read_text(encoding="utf-8"))
    result_map = {str(item["address"]).lower(): item for item in results.get("results", [])}
    inserted = 0
    with sqlite3.connect(db_path) as connection:
        connection.executescript(SCHEMA)
        for action in plan.get("actions", []):
            address = str(action["address"]).lower()
            result = result_map.get(address, {})
            connection.execute(
                """INSERT INTO action_receipts
                (recorded_at, address, candidate_id, unsubscribe_method,
                 unsubscribe_result, filter_result, filter_id, existing_messages_affected)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now.isoformat(), address, action["candidate_id"],
                    action["unsubscribe_method"], result.get("unsubscribe_result", "unknown"),
                    result.get("filter_result", "unknown"), result.get("filter_id"),
                    int(bool(result.get("existing_messages_affected", False))),
                ),
            )
            inserted += 1
    return {"recorded": inserted, "database": str(db_path)}


def health(db_path: Path, hours: int, now: datetime) -> dict[str, Any]:
    cutoff = (now - timedelta(hours=hours)).isoformat()
    if not db_path.exists():
        return {"period_hours": hours, "actions": 0, "unsubscribed": 0, "filters_verified": 0, "existing_messages_affected": 0}
    with sqlite3.connect(db_path) as connection:
        connection.executescript(SCHEMA)
        row = connection.execute(
            """SELECT COUNT(*),
               SUM(CASE WHEN unsubscribe_result IN ('submitted', 'success') THEN 1 ELSE 0 END),
               SUM(CASE WHEN filter_result IN ('verified', 'success') THEN 1 ELSE 0 END),
               SUM(existing_messages_affected)
               FROM action_receipts WHERE recorded_at >= ?""",
            (cutoff,),
        ).fetchone()
    return {
        "period_hours": hours,
        "actions": row[0] or 0,
        "unsubscribed": row[1] or 0,
        "filters_verified": row[2] or 0,
        "existing_messages_affected": row[3] or 0,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    rank_parser = sub.add_parser("rank")
    rank_parser.add_argument("--input", type=Path, required=True)
    rank_parser.add_argument("--output", type=Path)
    rank_parser.add_argument("--min-count", type=int, default=2)
    rank_parser.add_argument("--now")

    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--input", type=Path, required=True)
    plan_parser.add_argument("--selected", type=Path, required=True)
    plan_parser.add_argument("--output", type=Path)
    plan_parser.add_argument("--now")

    record_parser = sub.add_parser("record")
    record_parser.add_argument("--db", type=Path, required=True)
    record_parser.add_argument("--plan", type=Path, required=True)
    record_parser.add_argument("--results", type=Path, required=True)
    record_parser.add_argument("--now")

    health_parser = sub.add_parser("health")
    health_parser.add_argument("--db", type=Path, required=True)
    health_parser.add_argument("--hours", type=int, default=24)
    health_parser.add_argument("--now")
    return root


def chosen_now(value: str | None) -> datetime:
    return parse_time(value) if value else utcnow()


def main() -> int:
    args = parser().parse_args()
    if args.command == "rank":
        if args.min_count < 1:
            raise SystemExit("--min-count must be at least 1")
        dump_json(rank(load_records(args.input), chosen_now(args.now), args.min_count), args.output)
    elif args.command == "plan":
        dump_json(build_plan(load_records(args.input), read_selected(args.selected), chosen_now(args.now)), args.output)
    elif args.command == "record":
        dump_json(record_results(args.db, args.plan, args.results, chosen_now(args.now)))
    elif args.command == "health":
        if args.hours < 1:
            raise SystemExit("--hours must be at least 1")
        dump_json(health(args.db, args.hours, chosen_now(args.now)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
