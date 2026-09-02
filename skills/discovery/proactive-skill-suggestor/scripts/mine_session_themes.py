#!/usr/bin/env python3
"""Compact 72h Hermes work-class miner for proactive-skill-suggestor.

Reads state.db read-only. Emits work classes, hub query lists, and a local
catalog index. Stores tokens and titles only — never message bodies.

Aggressive by default: heavy single sessions still become classes, and each
class gets several hub queries (task + synonyms + upgrade-of-incumbent).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.S)
NAME_RE = re.compile(r"^name:\s*[\"']?([^\"'\n]+)", re.M)
DESC_RE = re.compile(r"^description:\s*[\"']?(.*?)[\"']?\s*$", re.M)
SKILL_VIEW_RE = re.compile(r"name['\"]?\s*[:=]\s*['\"]([a-z0-9][a-z0-9_-]{1,64})")
TOKEN_RE = re.compile(r"[a-z][a-z0-9-]{3,}")
STOP = {
    "this", "that", "with", "from", "have", "just", "want", "need", "make",
    "please", "what", "when", "where", "your", "about", "then", "them", "they",
    "also", "into", "over", "than", "some", "more", "like", "help", "here",
    "there", "could", "would", "should", "using", "hermes", "skill", "skills",
    "agent", "session", "exactly", "ready", "reply", "lane", "create", "draft",
    "compile", "group", "message", "access", "note", "master", "speed",
    "response", "ensure", "verify", "check", "update", "add", "live", "work",
    "task", "todo", "file", "files", "code", "test", "tests", "true", "false",
    "untitled", "operations", "operation",
}
GENERIC_SKILL_PREFIX = {
    "agent", "hermes", "mia", "dest", "andromeda", "system", "session",
    "verification", "constraint", "long", "recent",
}
QUERY_SUFFIXES = ("", " cli", " api", " workflow", " admin")


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hours", type=int, default=72)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--skills-root", type=Path, action="append", default=None)
    p.add_argument("--max-classes", type=int, default=8)
    p.add_argument("--json", action="store_true", default=True)
    return p.parse_args()


def connect_ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def unix_cutoff(hours: int) -> float:
    return time.time() - (hours * 3600)


def load_sessions(con: sqlite3.Connection, cutoff: float) -> list[dict[str, Any]]:
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, title, source, cwd, last_activity_at, started_at,
               message_count, tool_call_count
        FROM sessions
        WHERE COALESCE(archived, 0) = 0
          AND COALESCE(hidden, 0) = 0
          AND CAST(COALESCE(last_activity_at, started_at) AS REAL) >= ?
        ORDER BY last_activity_at DESC
        """,
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def loaded_skills_for_sessions(
    con: sqlite3.Connection, session_ids: list[str]
) -> dict[str, list[str]]:
    if not session_ids:
        return {}
    con.row_factory = sqlite3.Row
    qmarks = ",".join("?" * len(session_ids))
    rows = con.execute(
        f"""
        SELECT session_id, tool_name, content, tool_calls
        FROM messages
        WHERE session_id IN ({qmarks})
          AND role = 'tool'
          AND tool_name IN ('skill_view', 'skills_list')
        """,
        session_ids,
    ).fetchall()
    by_session: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        blob = " ".join(
            str(x or "") for x in (r["tool_name"], r["content"], r["tool_calls"])
        )
        names = SKILL_VIEW_RE.findall(blob.lower())
        names += re.findall(r'"name":\s*"([a-z0-9][a-z0-9_-]{1,64})"', blob.lower())
        for n in names:
            if n not in by_session[r["session_id"]]:
                by_session[r["session_id"]].append(n)
    return by_session


def user_tokens_for_sessions(
    con: sqlite3.Connection, session_ids: list[str]
) -> dict[str, list[str]]:
    """First user message per session → tokens only. Never emit the text."""
    if not session_ids:
        return {}
    con.row_factory = sqlite3.Row
    qmarks = ",".join("?" * len(session_ids))
    rows = con.execute(
        f"""
        SELECT session_id, substr(content, 1, 400) AS snippet
        FROM messages
        WHERE session_id IN ({qmarks})
          AND role = 'user'
          AND COALESCE(active, 1) = 1
        ORDER BY id ASC
        """,
        session_ids,
    ).fetchall()
    by_session: dict[str, list[str]] = {}
    for r in rows:
        sid = r["session_id"]
        if sid in by_session:
            continue
        by_session[sid] = title_tokens(r["snippet"] or "")[:12]
    return by_session


def title_tokens(title: str) -> list[str]:
    words = TOKEN_RE.findall((title or "").lower())
    return [w for w in words if w not in STOP]


def skill_stems(name: str) -> list[str]:
    parts = [p for p in (name or "").lower().split("-") if p and p not in STOP]
    stems = []
    if parts and parts[0] not in GENERIC_SKILL_PREFIX:
        stems.append(parts[0])
    if len(parts) >= 2 and parts[1] not in GENERIC_SKILL_PREFIX:
        stems.append(parts[1])
    joined = " ".join(parts[:3])
    if joined:
        stems.append(joined)
    return stems


def cwd_token(cwd: str | None) -> str | None:
    if not cwd:
        return None
    base = Path(cwd).name.lower()
    base = re.sub(r"[^a-z0-9-]+", "-", base).strip("-")
    if not base or base in STOP or len(base) < 4:
        return None
    return base


def session_domains(
    session: dict[str, Any],
    skills: list[str],
    user_tokens: list[str],
) -> list[str]:
    title = session.get("title") or ""
    toks = title_tokens(title) + user_tokens
    cwd = cwd_token(session.get("cwd"))
    if cwd:
        toks.append(cwd)
    for sk in skills:
        toks.extend(skill_stems(sk)[:2])
    counts = Counter(t for t in toks if t not in STOP and len(t) >= 4)
    return [t for t, _ in counts.most_common(4)]


def hub_queries_for_class(label: str, domains: list[str], loaded: list[str]) -> list[str]:
    seeds: list[str] = []
    for d in domains:
        if d and d not in seeds:
            seeds.append(d)
    for sk in loaded[:6]:
        for stem in skill_stems(sk):
            if stem not in seeds and stem not in STOP:
                seeds.append(stem)
    for t in title_tokens(label):
        if t not in seeds:
            seeds.append(t)
    queries: list[str] = []
    for seed in seeds[:6]:
        for suffix in QUERY_SUFFIXES:
            q = f"{seed}{suffix}".strip()
            if q not in queries:
                queries.append(q)
            if len(queries) >= 8:
                return queries
    return queries[:8]


def cluster_classes(
    sessions: list[dict[str, Any]],
    skills_by_session: dict[str, list[str]],
    user_tokens_by_session: dict[str, list[str]],
    max_classes: int,
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for s in sessions:
        sid = s["id"]
        skills = skills_by_session.get(sid, [])
        user_toks = user_tokens_by_session.get(sid, [])
        domains = session_domains(s, skills, user_toks)
        title = (s.get("title") or "").strip() or "(untitled)"
        tool_calls = int(s.get("tool_call_count") or 0)
        heavy = tool_calls >= 20 or int(s.get("message_count") or 0) >= 12
        keys = domains[:2] or title_tokens(title)[:1] or ["misc"]
        for key in keys:
            if key not in buckets:
                buckets[key] = {
                    "label": key,
                    "session_count": 0,
                    "heavy_sessions": 0,
                    "tool_calls": 0,
                    "titles": [],
                    "loaded_skills": [],
                    "domains": set(),
                }
            b = buckets[key]
            b["session_count"] += 1
            b["tool_calls"] += tool_calls
            if heavy:
                b["heavy_sessions"] += 1
            b["domains"].add(key)
            if title not in b["titles"] and len(b["titles"]) < 8:
                b["titles"].append(title[:80])
            for sk in skills:
                if sk not in b["loaded_skills"]:
                    b["loaded_skills"].append(sk)

    ranked = sorted(
        buckets.values(),
        key=lambda x: (x["session_count"], x["heavy_sessions"], x["tool_calls"]),
        reverse=True,
    )
    out = []
    for b in ranked:
        if len(out) >= max_classes:
            break
        if b["session_count"] < 1:
            continue
        # Drop empty greeting buckets with no skills and no heavy work.
        if b["session_count"] == 1 and not b["loaded_skills"] and not b["heavy_sessions"]:
            continue
        domains = sorted(b["domains"])
        queries = hub_queries_for_class(b["label"], domains, b["loaded_skills"])
        out.append(
            {
                "label": b["label"],
                "session_count": b["session_count"],
                "heavy_sessions": b["heavy_sessions"],
                "tool_calls": b["tool_calls"],
                "titles": b["titles"],
                "loaded_skills": b["loaded_skills"][:16],
                "query_seeds": domains[:6],
                "hub_queries": queries,
                "qualify": "recurring"
                if b["session_count"] >= 2
                else ("heavy" if b["heavy_sessions"] else "single"),
            }
        )
    return out


def parse_skill_md(path: Path) -> dict[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return None
    m = FRONTMATTER_RE.match(text)
    block = m.group(1) if m else text[:800]
    name_m = NAME_RE.search(block)
    desc_m = DESC_RE.search(block)
    name = (name_m.group(1) if name_m else path.parent.name).strip()
    desc = (desc_m.group(1) if desc_m else "").strip()
    return {"name": name, "description": desc[:180], "path": str(path)}


def local_catalog(roots: list[Path]) -> list[dict[str, str]]:
    seen: set[str] = set()
    items: list[dict[str, str]] = []
    for root in roots:
        if not root.exists():
            continue
        for skill_md in root.rglob("SKILL.md"):
            parsed = parse_skill_md(skill_md)
            if not parsed:
                continue
            key = parsed["name"].lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(parsed)
    items.sort(key=lambda x: x["name"])
    return items


def default_skill_roots() -> list[Path]:
    home = hermes_home()
    roots = [home / "skills"]
    extra = Path.home() / "Dropbox" / "AI-Control-Plane" / "skills" / "exported"
    if extra.exists():
        roots.append(extra)
    return roots


def main() -> int:
    args = parse_args()
    db = args.db or (hermes_home() / "state.db")
    if not db.exists():
        print(json.dumps({"error": f"missing db: {db}"}), file=sys.stderr)
        return 2
    cutoff = unix_cutoff(args.hours)
    con = connect_ro(db)
    try:
        sessions = load_sessions(con, cutoff)
        ids = [s["id"] for s in sessions]
        skills_by_session = loaded_skills_for_sessions(con, ids)
        user_tokens_by_session = user_tokens_for_sessions(con, ids)
    finally:
        con.close()

    classes = cluster_classes(
        sessions, skills_by_session, user_tokens_by_session, args.max_classes
    )
    roots = args.skills_root or default_skill_roots()
    catalog = local_catalog(roots)
    loaded = sorted({n for v in skills_by_session.values() for n in v})
    seed_tokens = set()
    for c in classes:
        for s in c["query_seeds"] + c["loaded_skills"] + c["hub_queries"]:
            seed_tokens.update(TOKEN_RE.findall(s.lower()))
    nearby = []
    for item in catalog:
        blob = f"{item['name']} {item['description']}".lower()
        if seed_tokens and any(t in blob for t in seed_tokens if len(t) >= 4):
            nearby.append(item)
            if len(nearby) >= 60:
                break
    payload = {
        "window_hours": args.hours,
        "cutoff_unix": cutoff,
        "db": str(db),
        "session_count": len(sessions),
        "work_classes": classes,
        "loaded_skills_in_window": loaded,
        "local_skill_count": len(catalog),
        "local_skill_names": [x["name"] for x in catalog],
        "local_skills_near_themes": nearby,
        "search_mode": "aggressive",
    }
    if not classes:
        payload["stop"] = "no_work_classes"
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
