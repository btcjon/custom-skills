#!/usr/bin/env python3
"""Offline helpers for the Gmail Triage skill.

Every subcommand is deterministic, uses only the Python standard library, and
never contacts Gmail or the network. Gmail reads and mutations stay visible
Composio tool calls made by the agent. Message-derived text is treated as
untrusted data: it is sanitized for display and never interpreted as instruction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from triage_state import (  # noqa: E402
    ACTIONS,
    StateError,
    TriageState,
    normalize_account,
    normalize_exact_address,
    utcnow,
    validate_action,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

PROTECTED_TERMS = {
    "account", "admin", "alert", "auth", "billing", "bank", "calendar",
    "doctor", "fraud", "invoice", "legal", "login", "medical", "meeting",
    "order", "password", "payment", "receipt", "security", "support", "tax",
    "ticket", "travel", "verification", "verify",
}

TOOLS_BY_MODE: dict[str, tuple[str, ...]] = {
    "inspect": (
        "GMAIL_GET_PROFILE",
        "GMAIL_FETCH_EMAILS",
        "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
        "GMAIL_LIST_LABELS",
        "GMAIL_LIST_FILTERS",
    ),
    "triage": (
        "GMAIL_GET_PROFILE",
        "GMAIL_FETCH_EMAILS",
        "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
        "GMAIL_LIST_LABELS",
        "GMAIL_CREATE_LABEL",
        "GMAIL_ADD_LABEL_TO_EMAIL",
    ),
    "cleanup_review": (
        "GMAIL_GET_PROFILE",
        "GMAIL_FETCH_EMAILS",
        "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
        "GMAIL_LIST_FILTERS",
    ),
    "cleanup_execute": (
        "GMAIL_GET_PROFILE",
        "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
        "GMAIL_LIST_LABELS",
        "GMAIL_CREATE_LABEL",
        "GMAIL_LIST_FILTERS",
        "GMAIL_CREATE_FILTER",
        "GMAIL_GET_FILTER",
    ),
    "undo": ("GMAIL_GET_PROFILE", "GMAIL_LIST_FILTERS", "GMAIL_DELETE_FILTER"),
    "cleanup_existing": (
        "GMAIL_GET_PROFILE",
        "GMAIL_FETCH_EMAILS",
        "GMAIL_BATCH_MODIFY_MESSAGES",
        "GMAIL_MOVE_TO_TRASH",
    ),
}

SCOPES_BY_MODE: dict[str, tuple[str, ...]] = {
    "inspect": ("gmail.readonly",),
    "triage": ("gmail.readonly", "gmail.modify"),
    "cleanup_review": ("gmail.readonly",),
    "cleanup_execute": ("gmail.readonly", "gmail.settings.basic"),
    "undo": ("gmail.settings.basic",),
    "cleanup_existing": ("gmail.modify",),
}

FORBIDDEN_TOOLS = ("GMAIL_BATCH_DELETE_MESSAGES", "GMAIL_DELETE_MESSAGE", "GMAIL_DELETE_THREAD")

LABEL_PLACEHOLDER = "${archive_label_id}"


class InputError(ValueError):
    """Raised for malformed or unsafe helper input."""


# --------------------------------------------------------------------------
# parsing and normalization
# --------------------------------------------------------------------------
def parse_time(value: Any) -> datetime:
    if isinstance(value, bool):
        raise InputError("date must be a timestamp or date string")
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if re.fullmatch(r"\d{10,13}", text):
        return parse_time(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError) as error:
            raise InputError(f"unparsable date: {text!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_address(value: str) -> tuple[str, str]:
    name, address = parseaddr(value or "")
    address = address.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        return "", name.strip()
    return address, name.strip()


def safe_text(value: Any, limit: int = 80) -> str:
    """Sanitize untrusted sender-supplied text for display only."""
    text = str(value or "")
    text = "".join(
        " " if unicodedata.category(char)[0] == "C" else char for char in text
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def split_header_list(raw: str) -> list[str]:
    """Split an RFC 2369 header value on commas that sit outside angle brackets."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in raw:
        if char == "<":
            depth += 1
            continue
        if char == ">":
            depth = max(0, depth - 1)
            continue
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def unsubscribe_values(message: dict[str, Any]) -> tuple[list[str], str]:
    raw = message.get("list_unsubscribe", message.get("List-Unsubscribe", []))
    if isinstance(raw, str):
        values = split_header_list(raw)
    else:
        values = []
        for item in raw or []:
            values.extend(split_header_list(str(item)))
    post = str(message.get("list_unsubscribe_post", message.get("List-Unsubscribe-Post", "")) or "")
    return values, post


def message_evidence(message: dict[str, Any]) -> dict[str, Any]:
    """Derive the unsubscribe method from one message only."""
    values, post = unsubscribe_values(message)
    https = [value for value in values if value.lower().startswith("https://")]
    mailto = [value for value in values if value.lower().startswith("mailto:")]
    one_click = "one-click" in post.lower()
    if https and one_click:
        method = "one_click"
    elif mailto:
        method = "mailto"
    elif https:
        method = "web_review"
    else:
        method = "unavailable"
    return {
        "message_id": str(message.get("id") or ""),
        "date": parse_time(message.get("date")).isoformat(),
        "has_https_target": bool(https),
        "has_mailto_target": bool(mailto),
        "one_click_post_header": one_click,
        "method": method,
    }


def labels_of(message: dict[str, Any]) -> set[str]:
    raw = message.get("labels") or message.get("label_ids") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(label).strip().upper() for label in raw if str(label).strip()}


def load_records(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise InputError(f"cannot read {path}: {error}") from error
    if not text:
        return []
    try:
        if text.startswith("["):
            value = json.loads(text)
            if not isinstance(value, list):
                raise InputError("JSON input must be an array or JSON Lines")
            records = value
        else:
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
    except json.JSONDecodeError as error:
        raise InputError(f"malformed JSON in {path}: {error}") from error
    bad = [index for index, item in enumerate(records) if not isinstance(item, dict)]
    if bad:
        raise InputError(f"records must be JSON objects; bad entries at {bad[:5]}")
    return records


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise InputError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise InputError(f"malformed JSON in {path}: {error}") from error


def dump_json(value: Any, path: Path | None = None) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path:
        path.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def candidate_id(address: str) -> str:
    return "c_" + hashlib.sha256(address.encode("utf-8")).hexdigest()[:10]


def plan_id(account: str, addresses: Iterable[str], now: datetime) -> str:
    seed = account + "|" + "|".join(sorted(addresses)) + "|" + now.isoformat()
    return "p_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10]


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------
def dedupe_messages(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    duplicates = 0
    for message in records:
        key = str(message.get("id") or "").strip()
        rfc = str(message.get("rfc822_message_id") or message.get("Message-ID") or "").strip().lower()
        marker = f"id:{key}" if key else (f"rfc:{rfc}" if rfc else "")
        if rfc and key:
            if f"rfc:{rfc}" in seen:
                duplicates += 1
                continue
            seen.add(f"rfc:{rfc}")
        if marker and marker in seen:
            duplicates += 1
            continue
        if marker:
            seen.add(marker)
        unique.append(message)
    return unique, duplicates


def heuristic_protection(address: str, display_name: str, messages: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    haystack = f"{address} {display_name}".lower()
    hits = sorted(term for term in PROTECTED_TERMS if term in haystack)
    if hits:
        reasons.append("sender name suggests account-critical mail: " + ", ".join(hits[:3]))
    flagged = {label for message in messages for label in labels_of(message) & {"IMPORTANT", "STARRED"}}
    if flagged:
        reasons.append("Gmail marked messages " + ", ".join(sorted(flagged)))
    return reasons


def aggregate(
    records: list[dict[str, Any]],
    now: datetime,
    min_count: int,
    max_senders: int,
    sample_limit: int,
    protected: dict[str, str] | None = None,
    overrides: dict[str, str] | None = None,
    handled: dict[str, str] | None = None,
    excluded: set[str] | None = None,
) -> dict[str, Any]:
    if min_count < 1:
        raise InputError("min_count must be at least 1")
    if max_senders < 1:
        raise InputError("max_senders must be at least 1")
    if sample_limit < 0:
        raise InputError("sample_limit cannot be negative")
    protected = protected or {}
    overrides = overrides or {}
    handled = handled or {}
    excluded = excluded or set()

    unique, duplicates = dedupe_messages(records)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    names: dict[str, str] = {}
    unparsable = 0
    for message in unique:
        raw = str(message.get("from_address") or message.get("from") or "")
        address, parsed_name = normalize_address(raw)
        if not address:
            unparsable += 1
            continue
        grouped[address].append(message)
        name = safe_text(message.get("from_name") or parsed_name)
        if name and not names.get(address):
            names[address] = name

    exclusions = {"below_min_count": 0, "already_handled": 0, "user_excluded": 0}
    candidates: list[dict[str, Any]] = []
    recent_cutoff = now - timedelta(days=30)
    for address, messages in grouped.items():
        if len(messages) < min_count:
            exclusions["below_min_count"] += 1
            continue
        if address in excluded:
            exclusions["user_excluded"] += 1
            continue
        if address in handled:
            exclusions["already_handled"] += 1
            continue
        dated = sorted(messages, key=lambda item: parse_time(item.get("date")), reverse=True)
        dates = [parse_time(item.get("date")) for item in dated]
        label_sets = [labels_of(item) for item in dated]
        unread = sum("UNREAD" in labels for labels in label_sets)
        inbox = sum("INBOX" in labels for labels in label_sets)
        recent = sum(date >= recent_cutoff for date in dates)
        representative = pick_representative(dated)
        evidence = message_evidence(representative)
        reasons = list(heuristic_protection(address, names.get(address, ""), dated))
        if address in protected:
            reasons.insert(0, f"on this account's protected list: {safe_text(protected[address], 120)}")
        note_reasons: list[str] = []
        if len(messages) >= 10:
            note_reasons.append("high volume")
        if recent >= 3:
            note_reasons.append("frequent in last 30 days")
        if unread >= max(2, len(messages) // 2):
            note_reasons.append("often unread")
        if evidence["method"] == "one_click":
            note_reasons.append("one-click unsubscribe advertised")
        elif evidence["method"] == "mailto":
            note_reasons.append("mailto unsubscribe advertised")
        elif evidence["method"] == "web_review":
            note_reasons.append("unsubscribe link needs manual review")
        candidates.append({
            "candidate_id": candidate_id(address),
            "address": address,
            "display_name": names.get(address, ""),
            "total": len(messages),
            "last_30_days": recent,
            "unread": unread,
            "in_inbox": inbox,
            "first_seen": min(dates).isoformat(),
            "last_seen": max(dates).isoformat(),
            "unsubscribe_method": evidence["method"],
            "evidence": evidence,
            "samples": [str(item.get("id") or "") for item in dated[:sample_limit]],
            "samples_available": len(messages),
            "samples_truncated": len(messages) > sample_limit,
            "protected": bool(reasons),
            "protected_reasons": reasons,
            "override_recorded": address in overrides,
            "score": len(messages) + (recent * 2) + unread + inbox,
            "reason": "; ".join(note_reasons) or "recurring sender",
        })

    ordered = sorted(
        candidates,
        key=lambda item: (item["protected"], -item["score"], item["address"]),
    )
    returned = ordered[:max_senders]
    return {
        "generated_at": now.isoformat(),
        "input_messages": len(records),
        "unique_messages": len(unique),
        "duplicates_removed": duplicates,
        "unparsable_senders": unparsable,
        "senders_seen": len(grouped),
        "senders_eligible": len(ordered),
        "senders_returned": len(returned),
        "truncated": len(ordered) > len(returned),
        "excluded_counts": exclusions,
        "sample_limit": sample_limit,
        "untrusted_fields": ["display_name"],
        "candidates": returned,
    }


def pick_representative(dated_desc: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick one message that supplies both the header and URL evidence."""
    ranked = sorted(
        dated_desc,
        key=lambda item: (
            {"one_click": 0, "mailto": 1, "web_review": 2, "unavailable": 3}[
                message_evidence(item)["method"]
            ],
            -parse_time(item.get("date")).timestamp(),
            str(item.get("id") or ""),
        ),
    )
    return ranked[0]


# --------------------------------------------------------------------------
# selection and planning
# --------------------------------------------------------------------------
def read_selection(path: Path, default_action: str) -> list[dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise InputError(f"cannot read selection file {path}: {error}") from error
    selection: list[dict[str, str]] = []
    seen: set[str] = set()
    for number, line in enumerate(lines, start=1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        parts = [part for part in re.split(r"[\s,]+", text) if part]
        if len(parts) > 2:
            raise InputError(f"line {number}: expected 'target [action]', got {text!r}")
        target = parts[0].lower()
        action = validate_action(parts[1]) if len(parts) == 2 else validate_action(default_action)
        if target in seen:
            raise InputError(f"line {number}: {target} selected more than once")
        seen.add(target)
        selection.append({"target": target, "selected_action": action})
    if not selection:
        raise InputError("selection file contains no selected senders")
    return selection


def filter_spec(action: str, address: str, archive_label: str) -> dict[str, Any] | None:
    if action == "unsubscribe_only":
        return None
    if action in {"trash", "unsubscribe_and_trash"}:
        return {
            "criteria": {"from": address},
            "action": {"addLabelIds": ["TRASH"]},
            "future_only": True,
            "requires_label_id": False,
            "readback_tool": "GMAIL_LIST_FILTERS",
        }
    return {
        "criteria": {"from": address},
        "action": {"addLabelIds": [LABEL_PLACEHOLDER], "removeLabelIds": ["INBOX"]},
        "future_only": True,
        "requires_label_id": True,
        "label_name": archive_label,
        "label_placeholder": LABEL_PLACEHOLDER,
        "readback_tool": "GMAIL_LIST_FILTERS",
    }


def build_plan(
    aggregated: dict[str, Any],
    selection: list[dict[str, str]],
    account: str,
    archive_label: str,
    batch_size: int,
    now: datetime,
) -> dict[str, Any]:
    if batch_size < 1:
        raise InputError("batch_size must be at least 1")
    by_address = {item["address"]: item for item in aggregated["candidates"]}
    by_candidate = {item["candidate_id"]: item for item in aggregated["candidates"]}

    actions: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    unknown: list[str] = []
    for entry in selection:
        target = entry["target"]
        candidate = by_address.get(target) or by_candidate.get(target)
        if not candidate:
            unknown.append(target)
            continue
        action = entry["selected_action"]
        if candidate["protected"] and not candidate["override_recorded"]:
            blocked.append({
                "address": candidate["address"],
                "selected_action": action,
                "reason": "; ".join(candidate["protected_reasons"]),
                "remedy": (
                    "record an override for this exact address only: "
                    f"triage_core.py prefs allow --account {account} "
                    f"--address {candidate['address']} --reason '<why this sender is safe>'"
                ),
            })
            continue
        wants_unsubscribe = action.startswith("unsubscribe")
        method = candidate["unsubscribe_method"]
        spec = filter_spec(action, candidate["address"], archive_label)
        verification = ["read back GMAIL_LIST_FILTERS and compare criteria and action exactly"] if spec else []
        actions.append({
            "candidate_id": candidate["candidate_id"],
            "address": candidate["address"],
            "display_name": candidate["display_name"],
            "selected_action": action,
            "unsubscribe": {
                "requested": wants_unsubscribe,
                "method": method if wants_unsubscribe else "not_requested",
                "evidence_message_id": candidate["evidence"]["message_id"],
                "note": (
                    "re-read this exact message id and take the target URL and the "
                    "List-Unsubscribe-Post header from that same message"
                ) if wants_unsubscribe else "no unsubscribe was selected for this sender",
            },
            "gmail_filter": spec,
            "no_filter_reason": None if spec else "unsubscribe-only selection creates no Gmail filter",
            "affects_existing_messages": False,
            "verification": verification,
        })

    batches = []
    for index in range(0, len(actions), batch_size):
        chunk = actions[index:index + batch_size]
        batches.append({
            "batch_id": f"b{index // batch_size + 1}",
            "count": len(chunk),
            "actions_by_type": _tally(chunk, "selected_action"),
            "actions": chunk,
        })

    identifier = plan_id(account, [item["address"] for item in actions], now)
    return {
        "plan_id": identifier,
        "account": account,
        "created_at": now.isoformat(),
        "archive_label": archive_label,
        "authorization": {
            "source": "user selection file",
            "note": (
                "this plan records what the user selected; the plan itself is not "
                "authorization and does not permit any action beyond the exact "
                "addresses and actions listed"
            ),
        },
        "totals": {
            "selected": len(selection),
            "planned": len(actions),
            "blocked": len(blocked),
            "unknown": len(unknown),
        },
        "planned_by_action": _tally(actions, "selected_action"),
        "batches": batches,
        "blocked": blocked,
        "unknown_selected": sorted(unknown),
        "existing_messages": (
            "no existing message is touched by this plan; existing cleanup is a "
            "separate cleanup-existing run with its own confirmation"
        ),
    }


def _tally(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row[key]] = counts.get(row[key], 0) + 1
    return dict(sorted(counts.items()))


def plan_actions(plan: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    pairs: list[tuple[str, dict[str, Any]]] = []
    for batch in plan.get("batches", []):
        for action in batch.get("actions", []):
            pairs.append((str(batch.get("batch_id")), action))
    return pairs


# --------------------------------------------------------------------------
# filter duplicate safety and verification
# --------------------------------------------------------------------------
def existing_filters(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("filter", "filters", "data", "items"):
            if key in payload:
                return existing_filters(payload[key])
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def normalized_filter(entry: dict[str, Any]) -> dict[str, Any]:
    criteria = entry.get("criteria") or {}
    action = entry.get("action") or {}
    sender, _ = normalize_address(str(criteria.get("from") or ""))
    return {
        "id": str(entry.get("id") or ""),
        "from": sender or str(criteria.get("from") or "").strip().lower(),
        "query": str(criteria.get("query") or "").strip().lower(),
        "add": sorted({str(item) for item in (action.get("addLabelIds") or [])}),
        "remove": sorted({str(item) for item in (action.get("removeLabelIds") or [])}),
        "forward": str(action.get("forward") or ""),
    }


def targets_sender(entry: dict[str, Any], address: str) -> bool:
    """True when a live filter aims at this sender, by From or by query."""
    if entry["from"] == address:
        return True
    return bool(re.search(rf"\bfrom:\s*<?{re.escape(address)}>?", entry["query"]))


def resolve_spec(spec: dict[str, Any], label_id: str | None) -> dict[str, Any]:
    resolved = json.loads(json.dumps(spec))
    add = resolved["action"].get("addLabelIds") or []
    if LABEL_PLACEHOLDER in add:
        if not label_id:
            raise InputError(
                "this plan needs the Gmail label id; run GMAIL_LIST_LABELS or "
                "GMAIL_CREATE_LABEL and pass --label-id"
            )
        resolved["action"]["addLabelIds"] = [label_id if item == LABEL_PLACEHOLDER else item for item in add]
    return resolved


def check_filters(plan: dict[str, Any], live: list[dict[str, Any]], label_id: str | None) -> dict[str, Any]:
    normalized = [normalized_filter(item) for item in live]
    to_create: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for batch_id, action in plan_actions(plan):
        spec = action.get("gmail_filter")
        if not spec:
            continue
        resolved = resolve_spec(spec, label_id)
        want = normalized_filter({"criteria": resolved["criteria"], "action": resolved["action"]})
        matches = [item for item in normalized if targets_sender(item, want["from"])]
        identical = [
            item for item in matches
            if item["from"] == want["from"]
            and item["add"] == want["add"]
            and item["remove"] == want["remove"]
            and not item["forward"]
        ]
        record = {
            "batch_id": batch_id,
            "address": action["address"],
            "selected_action": action["selected_action"],
            "filter": resolved,
        }
        if identical:
            duplicates.append({**record, "existing_filter_ids": [item["id"] for item in identical]})
        elif matches:
            by_query = all(item["from"] != want["from"] for item in matches)
            conflicts.append({
                **record,
                "existing": [
                    {"id": item["id"], "add": item["add"], "remove": item["remove"],
                     "forward": item["forward"], "query": item["query"]}
                    for item in matches
                ],
                "reason": (
                    "an existing filter targets this sender through a query criterion; "
                    "review it by hand before adding another"
                    if by_query else
                    "a filter already targets this sender with a different action"
                ),
            })
        else:
            to_create.append(record)
    return {
        "plan_id": plan.get("plan_id"),
        "account": plan.get("account"),
        "live_filters_seen": len(normalized),
        "to_create": to_create,
        "duplicate_skip": duplicates,
        "conflicts": conflicts,
        "note": (
            "create only the entries under to_create; duplicate_skip needs no call "
            "and conflicts need a user decision before anything is created"
        ),
    }


def verify_filters(plan: dict[str, Any], live: list[dict[str, Any]], label_id: str | None) -> dict[str, Any]:
    normalized = [normalized_filter(item) for item in live]
    results: list[dict[str, Any]] = []
    for batch_id, action in plan_actions(plan):
        spec = action.get("gmail_filter")
        if not spec:
            results.append({
                "batch_id": batch_id,
                "address": action["address"],
                "selected_action": action["selected_action"],
                "filter_outcome": "not_requested",
                "filter_id": None,
                "readback_verified": False,
            })
            continue
        resolved = resolve_spec(spec, label_id)
        want = normalized_filter({"criteria": resolved["criteria"], "action": resolved["action"]})
        matches = [item for item in normalized if targets_sender(item, want["from"])]
        identical = [
            item for item in matches
            if item["from"] == want["from"]
            and item["add"] == want["add"]
            and item["remove"] == want["remove"]
            and not item["forward"]
        ]
        if identical:
            outcome, filter_id, verified = "verified", identical[0]["id"] or None, True
        elif matches:
            outcome, filter_id, verified = "mismatch", matches[0]["id"] or None, False
        else:
            outcome, filter_id, verified = "missing", None, False
        results.append({
            "batch_id": batch_id,
            "address": action["address"],
            "selected_action": action["selected_action"],
            "filter_outcome": outcome,
            "filter_id": filter_id,
            "readback_verified": verified,
        })
    return {
        "plan_id": plan.get("plan_id"),
        "account": plan.get("account"),
        "verified": sum(1 for item in results if item["readback_verified"]),
        "mismatch": sum(1 for item in results if item["filter_outcome"] == "mismatch"),
        "missing": sum(1 for item in results if item["filter_outcome"] == "missing"),
        "results": results,
        "note": "only readback_verified entries may be reported as verified filters",
    }


# --------------------------------------------------------------------------
# receipts, resume, monitoring, diagnosis
# --------------------------------------------------------------------------
def record_results(state: TriageState, plan: dict[str, Any], results: Any, now: datetime) -> dict[str, Any]:
    if normalize_account(plan.get("account")) != state.account:
        raise InputError(
            f"plan belongs to {plan.get('account')!r} but state is scoped to {state.account!r}"
        )
    rows = results.get("results") if isinstance(results, dict) else results
    if not isinstance(rows, list):
        raise InputError("results must be a list or an object with a 'results' list")
    planned = {
        (action["address"], action["selected_action"]): batch_id
        for batch_id, action in plan_actions(plan)
    }
    method_by_address = {
        action["address"]: action["unsubscribe"]["method"]
        for _, action in plan_actions(plan)
    }
    inserted = updated = 0
    rejected: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            rejected.append({"entry": str(row)[:60], "reason": "result entries must be objects"})
            continue
        try:
            address = normalize_exact_address(row.get("address"))
            action = validate_action(row.get("selected_action"))
        except StateError as error:
            rejected.append({"entry": str(row.get("address"))[:60], "reason": str(error)})
            continue
        key = (address, action)
        if key not in planned:
            rejected.append({
                "entry": f"{address} {action}",
                "reason": "not present in this plan; only selected actions can be recorded",
            })
            continue
        if row.get("filter_outcome") in {"created", "verified"} and not row.get("filter_id"):
            rejected.append({
                "entry": f"{address} {action}",
                "reason": "a created or verified filter must carry the Gmail filter id from readback",
            })
            continue
        if row.get("readback_verified") and row.get("filter_outcome") != "verified":
            rejected.append({
                "entry": f"{address} {action}",
                "reason": "readback_verified requires filter_outcome 'verified'",
            })
            continue
        if action == "unsubscribe_only" and row.get("filter_outcome", "not_requested") != "not_requested":
            rejected.append({
                "entry": f"{address} {action}",
                "reason": "unsubscribe_only must not report a filter outcome",
            })
            continue
        receipt = dict(row)
        receipt["address"] = address
        receipt["selected_action"] = action
        receipt["batch_id"] = str(row.get("batch_id") or planned[key])
        receipt.setdefault("unsubscribe_method", method_by_address.get(address, "unavailable"))
        try:
            outcome = state.upsert_receipt(receipt, now=now)
        except StateError as error:
            rejected.append({"entry": f"{address} {action}", "reason": str(error)})
            continue
        inserted += outcome == "inserted"
        updated += outcome == "updated"
    return {
        "account": state.account,
        "plan_id": plan.get("plan_id"),
        "inserted": inserted,
        "updated": updated,
        "rejected": rejected,
        "state_path": str(state.path),
        "note": "receipts are idempotent per account, batch, sender, and action",
    }


def resume_plan(state: TriageState, plan: dict[str, Any]) -> dict[str, Any]:
    if normalize_account(plan.get("account")) != state.account:
        raise InputError(
            f"plan belongs to {plan.get('account')!r} but state is scoped to {state.account!r}"
        )
    by_key = {
        (row["address"], row["selected_action"]): row
        for row in state.receipts()
    }
    pending: list[dict[str, Any]] = []
    retry: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    for batch_id, action in plan_actions(plan):
        key = (action["address"], action["selected_action"])
        receipt = by_key.get(key)
        entry = {
            "batch_id": batch_id,
            "address": action["address"],
            "selected_action": action["selected_action"],
        }
        if not receipt:
            pending.append(entry)
        elif receipt["status"] == "completed":
            completed.append({**entry, "recorded_at": receipt["first_recorded_at"]})
        else:
            retry.append({
                **entry,
                "status": receipt["status"],
                "filter_outcome": receipt["filter_outcome"],
                "unsubscribe_outcome": receipt["unsubscribe_outcome"],
                "read_back_first": True,
            })
    return {
        "account": state.account,
        "plan_id": plan.get("plan_id"),
        "completed": completed,
        "pending": pending,
        "retry_after_readback": retry,
        "note": (
            "read current Gmail state for each retry entry before repeating any "
            "mutation; a recorded partial action may already exist in Gmail"
        ),
    }


def monitor(
    state: TriageState,
    address: str,
    records: list[dict[str, Any]],
    decision_at: datetime,
    window_days: int,
    include_spam_trash: bool,
    now: datetime,
) -> dict[str, Any]:
    exact = normalize_exact_address(address)
    unique, duplicates = dedupe_messages(records)
    after = []
    for message in unique:
        sender, _ = normalize_address(str(message.get("from_address") or message.get("from") or ""))
        if sender != exact:
            continue
        if parse_time(message.get("date")) >= decision_at:
            after.append(message)
    if not include_spam_trash:
        outcome = "insufficient_evidence"
    elif after:
        outcome = "still_sending"
    else:
        outcome = "quiet"
    state.record_observation(exact, window_days, len(after), include_spam_trash, outcome, now=now)
    receipts = state.receipts(address=exact)
    return {
        "account": state.account,
        "address": exact,
        "decision_at": decision_at.isoformat(),
        "window_days": window_days,
        "include_spam_trash": include_spam_trash,
        "messages_after_decision": len(after),
        "duplicates_removed": duplicates,
        "outcome": outcome,
        "recorded_unsubscribe_outcomes": sorted({row["unsubscribe_outcome"] for row in receipts}),
        "interpretation": {
            "quiet": "no message from this exact sender arrived after the decision; quiet is suggestive, not proof",
            "still_sending": "the sender is still delivering mail; a future-only filter may be hiding it",
            "insufficient_evidence": "the search did not include spam and trash, so silence cannot be trusted",
        }[outcome],
    }


def diagnose(
    state: TriageState,
    address: str,
    records: list[dict[str, Any]],
    live: list[dict[str, Any]],
) -> dict[str, Any]:
    exact = normalize_exact_address(address)
    unique, duplicates = dedupe_messages(records)
    mine = []
    for message in unique:
        sender, _ = normalize_address(str(message.get("from_address") or message.get("from") or ""))
        if sender == exact:
            mine.append(message)
    locations = {"inbox": 0, "trash": 0, "spam": 0, "archived": 0}
    labels_seen: dict[str, int] = {}
    for message in mine:
        labels = labels_of(message)
        for label in labels:
            labels_seen[label] = labels_seen.get(label, 0) + 1
        if "TRASH" in labels:
            locations["trash"] += 1
        elif "SPAM" in labels:
            locations["spam"] += 1
        elif "INBOX" in labels:
            locations["inbox"] += 1
        else:
            locations["archived"] += 1

    matching = [item for item in (normalized_filter(entry) for entry in live) if item["from"] == exact]
    explanations: list[str] = []
    for item in matching:
        if "TRASH" in item["add"]:
            explanations.append(f"filter {item['id']} sends new mail from this sender to Trash")
        elif "INBOX" in item["remove"]:
            explanations.append(
                f"filter {item['id']} removes new mail from the Inbox and labels it {', '.join(item['add']) or '(no label)'}"
            )
        else:
            explanations.append(f"filter {item['id']} matches this sender with add={item['add']} remove={item['remove']}")
    if not matching:
        explanations.append("no Gmail filter in the readback targets this exact sender")
    if locations["trash"]:
        explanations.append(f"{locations['trash']} message(s) from this sender are in Trash")
    if locations["archived"]:
        explanations.append(f"{locations['archived']} message(s) are archived outside the Inbox")
    if locations["spam"]:
        explanations.append(f"{locations['spam']} message(s) are in Spam, which this skill does not control")
    if not mine:
        explanations.append(
            "the supplied search returned no message from this exact sender; widen the window "
            "and set include_spam_trash true before concluding anything"
        )
    receipts = state.receipts(address=exact)
    return {
        "account": state.account,
        "address": exact,
        "messages_found": len(mine),
        "duplicates_removed": duplicates,
        "locations": locations,
        "labels_seen": dict(sorted(labels_seen.items())),
        "matching_filters": matching,
        "recorded_actions": [
            {
                "batch_id": row["batch_id"],
                "selected_action": row["selected_action"],
                "filter_outcome": row["filter_outcome"],
                "filter_id": row["filter_id"],
                "unsubscribe_outcome": row["unsubscribe_outcome"],
                "recorded_at": row["first_recorded_at"],
            }
            for row in receipts
        ],
        "explanations": explanations,
        "next_steps": [
            "search this exact sender with include_spam_trash true over a wider window",
            "list Gmail filters and compare the criteria to the exact address",
            f"undo lookup: triage_core.py undo --account {state.account} --address {exact}",
        ],
    }


def cleanup_existing(
    address: str,
    records: list[dict[str, Any]],
    action: str,
    limit: int,
    confirmed: bool,
) -> dict[str, Any]:
    if action not in {"archive_label", "trash"}:
        raise InputError("existing cleanup supports archive_label or trash only")
    if limit < 1:
        raise InputError("max must be at least 1")
    if not confirmed:
        raise InputError(
            "existing-message cleanup is a separate authorization from future "
            "filters; re-run with --confirm-existing-scope after the user sees the "
            "exact message count"
        )
    exact = normalize_exact_address(address)
    unique, duplicates = dedupe_messages(records)
    matched: list[dict[str, Any]] = []
    skipped: list[str] = []
    for message in unique:
        sender, _ = normalize_address(str(message.get("from_address") or message.get("from") or ""))
        if sender != exact:
            continue
        labels = labels_of(message)
        if labels & {"IMPORTANT", "STARRED"}:
            skipped.append(str(message.get("id") or ""))
            continue
        if "TRASH" in labels:
            skipped.append(str(message.get("id") or ""))
            continue
        matched.append(message)
    matched.sort(key=lambda item: parse_time(item.get("date")), reverse=True)
    selected = matched[:limit]
    tool = "GMAIL_BATCH_MODIFY_MESSAGES" if action == "archive_label" else "GMAIL_MOVE_TO_TRASH"
    return {
        "address": exact,
        "action": action,
        "tool": tool,
        "message_ids": [str(item.get("id") or "") for item in selected],
        "matched_total": len(matched),
        "selected_count": len(selected),
        "truncated": len(matched) > len(selected),
        "skipped_protected_or_trashed": skipped,
        "duplicates_removed": duplicates,
        "scope_statement": (
            f"this run touches exactly {len(selected)} existing message(s) from {exact}; "
            "it creates no filter and changes nothing for other senders"
        ),
        "authorization": (
            "separate from any future-only filter; confirm this count with the user "
            "and read back the affected messages afterwards"
        ),
        "record_hint": (
            "after Gmail confirms the change, re-run record for this sender's planned "
            "action with existing_messages_affected set to the verified count; the "
            "receipt update is idempotent"
        ),
    }


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------
def preflight(
    account: str,
    mode: str,
    profile: Any,
    available_tools: list[str],
    connected_accounts: list[str],
) -> dict[str, Any]:
    if mode not in TOOLS_BY_MODE:
        raise InputError(f"unknown mode {mode!r}; expected one of {', '.join(sorted(TOOLS_BY_MODE))}")
    expected = normalize_account(account)
    payload = profile
    for _ in range(4):
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                break
        elif isinstance(payload, dict) and "data" in payload and not payload.get("emailAddress"):
            payload = payload["data"]
        else:
            break
    observed_raw = ""
    if isinstance(payload, dict):
        observed_raw = str(payload.get("emailAddress") or payload.get("email_address") or "")
    observed, _ = normalize_address(observed_raw)

    available = {str(tool).strip().upper() for tool in available_tools if str(tool).strip()}
    required = TOOLS_BY_MODE[mode]
    missing = [tool for tool in required if tool not in available]
    forbidden = sorted(tool for tool in FORBIDDEN_TOOLS if tool in available)
    others = sorted({normalize_address(item)[0] for item in connected_accounts} - {"", expected})

    problems: list[str] = []
    if not observed:
        problems.append(
            "GMAIL_GET_PROFILE did not report an emailAddress; confirm the connection "
            "before any read or mutation"
        )
    elif observed != expected:
        problems.append(
            f"connected mailbox is {observed} but the requested account is {expected}; "
            "stop and let the user pick the account"
        )
    if others:
        problems.append(
            "more than one Gmail account is connected (" + ", ".join(others) +
            "); pass the exact account for every call instead of a default"
        )
    if missing:
        problems.append("missing tools for this mode: " + ", ".join(missing))

    degraded = []
    if mode in {"cleanup_execute", "undo"} and "GMAIL_CREATE_FILTER" not in available:
        degraded.append(
            "no filter tool is available; unsubscribe-only cleanup still works, "
            "future-only filters do not"
        )
    if mode == "triage" and "GMAIL_CREATE_LABEL" not in available:
        degraded.append("label creation is unavailable; reuse an existing label id or stay read-only")

    return {
        "account": expected,
        "mode": mode,
        "observed_account": observed,
        "account_matches": bool(observed) and observed == expected,
        "required_tools": list(required),
        "missing_tools": missing,
        "forbidden_tools_present": forbidden,
        "required_scopes": list(SCOPES_BY_MODE[mode]),
        "other_connected_accounts": others,
        "degraded_capabilities": degraded,
        "ready": not problems,
        "problems": problems,
        "reminder": (
            "never enable permanent-delete tools for this skill; keep the allowlist "
            "to the tools this mode needs"
        ),
    }


# --------------------------------------------------------------------------
# self test walkthrough
# --------------------------------------------------------------------------
def selftest(now: datetime) -> dict[str, Any]:
    account = "demo@example.com"
    messages = load_records(FIXTURES / "sample-messages.jsonl")
    steps: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as directory:
        state_dir = Path(directory)
        with TriageState(state_dir, account) as state:
            prefs = state.preferences()
            state.protect("billing@shop.example.com", "order and payment mail", now=now)
            aggregated = aggregate(
                messages,
                now,
                min_count=prefs["min_count"],
                max_senders=prefs["max_senders"],
                sample_limit=prefs["sample_limit"],
                protected=state.protected_senders(),
                overrides=state.overrides(),
                handled=state.handled_addresses(90, now=now),
            )
            steps.append({
                "step": "rank",
                "candidates": aggregated["senders_returned"],
                "duplicates_removed": aggregated["duplicates_removed"],
                "addresses": [item["address"] for item in aggregated["candidates"]],
            })
            selection = read_selection(FIXTURES / "sample-selection.txt", prefs["default_action"])
            plan = build_plan(
                aggregated, selection, account, prefs["archive_label"], prefs["batch_size"], now
            )
            steps.append({
                "step": "plan",
                "planned": plan["totals"]["planned"],
                "blocked": [item["address"] for item in plan["blocked"]],
                "by_action": plan["planned_by_action"],
            })
            expected_filters = sum(1 for _, action in plan_actions(plan) if action["gmail_filter"])
            check = check_filters(plan, [], label_id="Label_42")
            steps.append({
                "step": "filters-check",
                "to_create": len(check["to_create"]),
                "duplicate_skip": len(check["duplicate_skip"]),
                "conflicts": len(check["conflicts"]),
                "expected_filters": expected_filters,
            })
            live = [
                {"id": f"filter_demo_{index}", **resolve_spec(entry["filter"], "Label_42")}
                for index, entry in enumerate(check["to_create"], start=1)
            ]
            verified = verify_filters(plan, live, label_id="Label_42")
            steps.append({"step": "filters-verify", "verified": verified["verified"]})
            results = {"results": [
                {
                    "address": item["address"],
                    "selected_action": item["selected_action"],
                    "unsubscribe_outcome": "submitted" if item["selected_action"].startswith("unsubscribe") else "not_attempted",
                    "filter_outcome": item["filter_outcome"],
                    "filter_id": item["filter_id"],
                    "readback_verified": item["readback_verified"],
                    "status": "completed",
                }
                for item in verified["results"]
            ]}
            recorded = record_results(state, plan, results, now)
            steps.append({"step": "record", "inserted": recorded["inserted"], "rejected": recorded["rejected"]})
            resumed = resume_plan(state, plan)
            steps.append({
                "step": "resume",
                "pending": len(resumed["pending"]),
                "completed": len(resumed["completed"]),
            })
            report = state.health(24, now=now)
            steps.append({
                "step": "health",
                "receipts_recorded": report["receipts_recorded"],
                "filters_verified_by_readback": report["filters_verified_by_readback"],
                "inconsistent_records": report["inconsistent_records"],
            })
    by_step = {entry["step"]: entry for entry in steps}
    checks = {
        "candidates_found": by_step["rank"]["candidates"] > 0,
        "duplicate_message_removed": by_step["rank"]["duplicates_removed"] == 1,
        "actions_planned": by_step["plan"]["planned"] > 0,
        "protected_sender_blocked": bool(by_step["plan"]["blocked"]),
        "filters_planned_once": by_step["filters-check"]["to_create"] == by_step["filters-check"]["expected_filters"],
        "filters_verified_by_readback": by_step["filters-verify"]["verified"] == by_step["filters-check"]["expected_filters"],
        "receipts_recorded": by_step["record"]["inserted"] == by_step["plan"]["planned"],
        "nothing_rejected": not by_step["record"]["rejected"],
        "no_work_left": by_step["resume"]["pending"] == 0,
        "health_consistent": not by_step["health"]["inconsistent_records"],
    }
    return {
        "walkthrough": "synthetic fixtures only; no Gmail call and no network access",
        "account": account,
        "steps": steps,
        "checks": checks,
        "ok": all(checks.values()),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    def account_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--account", required=True, help="exact connected Gmail address")
        parser.add_argument("--state-dir", type=Path, default=Path.home() / ".gmail-triage")

    pre = sub.add_parser("preflight", help="check account identity and tool capability")
    pre.add_argument("--account", required=True)
    pre.add_argument("--mode", required=True, choices=sorted(TOOLS_BY_MODE))
    pre.add_argument("--profile", type=Path, required=True, help="GMAIL_GET_PROFILE output")
    pre.add_argument("--tools", type=Path, required=True, help="JSON array of available tool slugs")
    pre.add_argument("--connected-accounts", type=Path, help="JSON array of connected Gmail addresses")

    prefs = sub.add_parser("prefs", help="read or change account preferences and protections")
    account_args(prefs)
    prefs.add_argument("action", choices=["show", "set", "protect", "unprotect", "allow", "revoke"])
    prefs.add_argument("--address")
    prefs.add_argument("--reason", default="")
    prefs.add_argument("--window-days", type=int)
    prefs.add_argument("--min-count", type=int)
    prefs.add_argument("--max-senders", type=int)
    prefs.add_argument("--sample-limit", type=int)
    prefs.add_argument("--batch-size", type=int)
    prefs.add_argument("--monitor-days", type=int)
    prefs.add_argument("--archive-label")
    prefs.add_argument("--default-action", choices=list(ACTIONS))

    rank = sub.add_parser("rank", help="aggregate exact senders from normalized messages")
    account_args(rank)
    rank.add_argument("--input", type=Path, required=True)
    rank.add_argument("--output", type=Path)
    rank.add_argument("--min-count", type=int)
    rank.add_argument("--max-senders", type=int)
    rank.add_argument("--sample-limit", type=int)
    rank.add_argument("--exclude", type=Path, help="file of addresses to skip")
    rank.add_argument("--exclude-handled", action="store_true")
    rank.add_argument("--handled-days", type=int, default=90)
    rank.add_argument("--now")

    plan = sub.add_parser("plan", help="turn a user selection into an explicit action plan")
    account_args(plan)
    plan.add_argument("--input", type=Path, required=True)
    plan.add_argument("--selected", type=Path, required=True)
    plan.add_argument("--output", type=Path)
    plan.add_argument("--default-action", choices=list(ACTIONS))
    plan.add_argument("--batch-size", type=int)
    plan.add_argument("--min-count", type=int, default=1)
    plan.add_argument("--now")

    check = sub.add_parser("filters-check", help="compare a plan with live filters before creating any")
    check.add_argument("--plan", type=Path, required=True)
    check.add_argument("--live-filters", type=Path, required=True)
    check.add_argument("--label-id")
    check.add_argument("--output", type=Path)

    verify = sub.add_parser("filters-verify", help="verify created filters against a live readback")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--live-filters", type=Path, required=True)
    verify.add_argument("--label-id")
    verify.add_argument("--output", type=Path)

    record = sub.add_parser("record", help="record verified outcomes idempotently")
    account_args(record)
    record.add_argument("--plan", type=Path, required=True)
    record.add_argument("--results", type=Path, required=True)
    record.add_argument("--now")

    resume = sub.add_parser("resume", help="list plan actions that still need work")
    account_args(resume)
    resume.add_argument("--plan", type=Path, required=True)

    history = sub.add_parser("history", help="show recorded receipts for this account")
    account_args(history)
    history.add_argument("--address")
    history.add_argument("--days", type=int, default=365)
    history.add_argument("--now")

    handled = sub.add_parser("handled", help="list senders already handled for this account")
    account_args(handled)
    handled.add_argument("--days", type=int, default=90)
    handled.add_argument("--now")

    undo = sub.add_parser("undo", help="look up the exact filter ids to delete for one sender")
    account_args(undo)
    undo.add_argument("--address", required=True)

    watch = sub.add_parser("monitor", help="classify post-decision mail for one sender")
    account_args(watch)
    watch.add_argument("--address", required=True)
    watch.add_argument("--input", type=Path, required=True)
    watch.add_argument("--decision-at", required=True)
    watch.add_argument("--window-days", type=int)
    watch.add_argument("--include-spam-trash", action="store_true")
    watch.add_argument("--now")

    diag = sub.add_parser("diagnose", help="explain where mail from one sender went")
    account_args(diag)
    diag.add_argument("--address", required=True)
    diag.add_argument("--input", type=Path, required=True)
    diag.add_argument("--live-filters", type=Path, required=True)

    existing = sub.add_parser("cleanup-existing", help="bound an existing-message cleanup scope")
    existing.add_argument("--address", required=True)
    existing.add_argument("--input", type=Path, required=True)
    existing.add_argument("--action", required=True, choices=["archive_label", "trash"])
    existing.add_argument("--max", type=int, default=50)
    existing.add_argument("--confirm-existing-scope", action="store_true")
    existing.add_argument("--output", type=Path)

    health = sub.add_parser("health", help="report outcomes recorded for this account")
    account_args(health)
    health.add_argument("--hours", type=int, default=24)
    health.add_argument("--now")

    sub.add_parser("selftest", help="run the whole offline pipeline on bundled fixtures")
    return root


def chosen_now(value: str | None) -> datetime:
    return parse_time(value) if value else utcnow()


def read_address_file(path: Path | None) -> set[str]:
    if not path:
        return set()
    addresses = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip()
        if text:
            addresses.add(normalize_exact_address(text))
    return addresses


def prefs_command(args: argparse.Namespace) -> dict[str, Any]:
    with TriageState(args.state_dir, args.account) as state:
        if args.action == "show":
            return {
                "account": state.account,
                "state_path": str(state.path),
                "preferences": state.preferences(),
                "protected_senders": state.protected_senders(),
                "overrides": state.overrides(),
            }
        if args.action == "set":
            updates = {
                key: getattr(args, key)
                for key in (
                    "window_days", "min_count", "max_senders", "sample_limit",
                    "batch_size", "monitor_days", "archive_label", "default_action",
                )
                if getattr(args, key) is not None
            }
            if not updates:
                raise InputError("prefs set needs at least one preference flag")
            return {"account": state.account, "preferences": state.set_preferences(updates)}
        if not args.address:
            raise InputError(f"prefs {args.action} needs --address")
        if args.action == "protect":
            return {"account": state.account, "protected": state.protect(args.address, args.reason)}
        if args.action == "unprotect":
            return {"account": state.account, "removed": state.unprotect(args.address)}
        if args.action == "allow":
            address = state.allow_override(args.address, args.reason)
            return {
                "account": state.account,
                "override_for": address,
                "scope": "this exact sender only; no other sender gains permission",
            }
        return {"account": state.account, "revoked": state.revoke_override(args.address)}


def rank_command(args: argparse.Namespace) -> dict[str, Any]:
    now = chosen_now(args.now)
    with TriageState(args.state_dir, args.account) as state:
        prefs = state.preferences()
        handled = state.handled_addresses(args.handled_days, now=now) if args.exclude_handled else {}
        aggregated = aggregate(
            load_records(args.input),
            now,
            min_count=args.min_count if args.min_count is not None else prefs["min_count"],
            max_senders=args.max_senders if args.max_senders is not None else prefs["max_senders"],
            sample_limit=args.sample_limit if args.sample_limit is not None else prefs["sample_limit"],
            protected=state.protected_senders(),
            overrides=state.overrides(),
            handled=handled,
            excluded=read_address_file(args.exclude),
        )
    aggregated["account"] = args.account.strip().lower()
    aggregated["window_days_expected"] = prefs["window_days"]
    return aggregated


def plan_command(args: argparse.Namespace) -> dict[str, Any]:
    now = chosen_now(args.now)
    with TriageState(args.state_dir, args.account) as state:
        prefs = state.preferences()
        default_action = args.default_action or prefs["default_action"]
        aggregated = aggregate(
            load_records(args.input),
            now,
            min_count=args.min_count,
            max_senders=10_000,
            sample_limit=prefs["sample_limit"],
            protected=state.protected_senders(),
            overrides=state.overrides(),
        )
        return build_plan(
            aggregated,
            read_selection(args.selected, default_action),
            state.account,
            prefs["archive_label"],
            args.batch_size if args.batch_size is not None else prefs["batch_size"],
            now,
        )


def history_command(args: argparse.Namespace) -> dict[str, Any]:
    now = chosen_now(args.now)
    with TriageState(args.state_dir, args.account) as state:
        rows = state.receipts(address=args.address, since=now - timedelta(days=args.days))
        return {
            "account": state.account,
            "address": args.address,
            "days": args.days,
            "receipts": rows,
            "observations": state.observations(address=args.address, since=now - timedelta(days=args.days)),
        }


def undo_command(args: argparse.Namespace) -> dict[str, Any]:
    with TriageState(args.state_dir, args.account) as state:
        targets = state.undo_targets(args.address)
        return {
            "account": state.account,
            "address": normalize_exact_address(args.address),
            "delete_filter_ids": [item["filter_id"] for item in targets],
            "targets": targets,
            "tool": "GMAIL_DELETE_FILTER",
            "undo_does_not": [
                "reverse an unsubscribe request already sent to the sender",
                "restore messages already in Trash",
                "change any existing message label",
            ],
            "after_delete": "read back GMAIL_LIST_FILTERS and confirm the filter is gone",
        }


def dispatch(args: argparse.Namespace) -> Any:
    if args.command == "preflight":
        return preflight(
            args.account,
            args.mode,
            load_json(args.profile),
            load_json(args.tools) or [],
            load_json(args.connected_accounts) if args.connected_accounts else [],
        )
    if args.command == "prefs":
        return prefs_command(args)
    if args.command == "rank":
        return rank_command(args)
    if args.command == "plan":
        return plan_command(args)
    if args.command == "filters-check":
        return check_filters(
            load_json(args.plan), existing_filters(load_json(args.live_filters)), args.label_id
        )
    if args.command == "filters-verify":
        return verify_filters(
            load_json(args.plan), existing_filters(load_json(args.live_filters)), args.label_id
        )
    if args.command == "record":
        with TriageState(args.state_dir, args.account) as state:
            return record_results(state, load_json(args.plan), load_json(args.results), chosen_now(args.now))
    if args.command == "resume":
        with TriageState(args.state_dir, args.account) as state:
            return resume_plan(state, load_json(args.plan))
    if args.command == "history":
        return history_command(args)
    if args.command == "handled":
        now = chosen_now(args.now)
        with TriageState(args.state_dir, args.account) as state:
            return {
                "account": state.account,
                "days": args.days,
                "handled": state.handled_addresses(args.days, now=now),
            }
    if args.command == "undo":
        return undo_command(args)
    if args.command == "monitor":
        now = chosen_now(args.now)
        with TriageState(args.state_dir, args.account) as state:
            window = args.window_days if args.window_days is not None else state.preferences()["monitor_days"]
            return monitor(
                state, args.address, load_records(args.input), parse_time(args.decision_at),
                window, args.include_spam_trash, now,
            )
    if args.command == "diagnose":
        with TriageState(args.state_dir, args.account) as state:
            return diagnose(
                state, args.address, load_records(args.input),
                existing_filters(load_json(args.live_filters)),
            )
    if args.command == "cleanup-existing":
        return cleanup_existing(
            args.address, load_records(args.input), args.action, args.max, args.confirm_existing_scope
        )
    if args.command == "health":
        if args.hours < 1:
            raise InputError("--hours must be at least 1")
        with TriageState(args.state_dir, args.account) as state:
            return state.health(args.hours, now=chosen_now(args.now))
    return selftest(utcnow())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = dispatch(args)
    except (InputError, StateError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 2
    output = getattr(args, "output", None)
    dump_json(result, output)
    if args.command == "selftest" and not result.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
