#!/usr/bin/env python3
"""Compact 72h Hermes work-class miner for proactive-skill-suggestor.

Reads state.db read-only. Emits JSON with work-class seeds, loaded skill
names, and a local catalog index. Does not print message bodies.

Public-repo safe: default output is themes and names only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.S)
NAME_RE = re.compile(r"^name:\s*[\"']?([^\"'\n]+)", re.M)
DESC_RE = re.compile(r"^description:\s*[\"']?(.*?)[\"']?\s*$", re.M)
SKILL_VIEW_RE = re.compile(r"name['\"]?\s*[:=]\s*['\"]([a-z0-9][a-z0-9_-]{1,64})")
TOKEN_RE = re.compile(r"[a-z][a-z0-9-]{3,}")
STOP = {
    "this",
    "that",
    "with",
    "from",
    "have",
    "just",
    "want",
    "need",
    "make",
    "please",
    "what",
    "when",
    "where",
    "your",
    "about",
    "then",
    "them",
    "they",
    "also",
    "into",
    "over",
    "than",
    "some",
    "more",
    "like",
    "help",
    "here",
    "there",
    "could",
    "would",
    "should",
    "using",
    "hermes",
    "skill",
    "skills",
    "agent",
    "session",
}


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hours", type=int, default=72)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--skills-root", type=Path, action="append", default=None)
    p.add_argument("--max-classes", type=int, default=5)
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
        # skill_view results often include "name": "foo" in JSON
        names += re.findall(r'"name":\s*"([a-z0-9][a-z0-9_-]{1,64})"', blob.lower())
        for n in names:
            if n not in by_session[r["session_id"]]:
                by_session[r["session_id"]].append(n)
    return by_session


def title_tokens(title: str) -> list[str]:
    words = TOKEN_RE.findall((title or "").lower())
    return [w for w in words if w not in STOP]


def cluster_classes(
    sessions: list[dict[str, Any]],
    skills_by_session: dict[str, list[str]],
    max_classes: int,
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for s in sessions:
        title = (s.get("title") or "").strip() or "(untitled)"
        toks = title_tokens(title)
        key = " ".join(toks[:3]) if toks else title.lower()[:40]
        if key not in buckets:
            buckets[key] = {
                "label": title[:80],
                "session_count": 0,
                "titles": [],
                "loaded_skills": [],
                "query_seeds": set(),
            }
        b = buckets[key]
        b["session_count"] += 1
        if title not in b["titles"] and len(b["titles"]) < 6:
            b["titles"].append(title[:80])
        for sk in skills_by_session.get(s["id"], []):
            if sk not in b["loaded_skills"]:
                b["loaded_skills"].append(sk)
            b["query_seeds"].add(sk.replace("-", " "))
        for t in toks[:4]:
            b["query_seeds"].add(t)

    ranked = sorted(
        buckets.values(),
        key=lambda x: (x["session_count"], len(x["loaded_skills"])),
        reverse=True,
    )
    out = []
    for b in ranked[:max_classes]:
        if b["session_count"] < 1:
            continue
        seeds = [x for x in sorted(b["query_seeds"]) if x][:6]
        out.append(
            {
                "label": b["label"],
                "session_count": b["session_count"],
                "titles": b["titles"],
                "loaded_skills": b["loaded_skills"][:12],
                "query_seeds": seeds,
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
        skills_by_session = loaded_skills_for_sessions(
            con, [s["id"] for s in sessions]
        )
    finally:
        con.close()

    classes = cluster_classes(sessions, skills_by_session, args.max_classes)
    roots = args.skills_root or default_skill_roots()
    catalog = local_catalog(roots)
    loaded = sorted({n for v in skills_by_session.values() for n in v})
    seed_tokens = set()
    for c in classes:
        for s in c["query_seeds"] + c["loaded_skills"]:
            seed_tokens.update(TOKEN_RE.findall(s.lower()))
    nearby = []
    for item in catalog:
        blob = f"{item['name']} {item['description']}".lower()
        if seed_tokens and any(t in blob for t in seed_tokens if len(t) >= 4):
            nearby.append(item)
            if len(nearby) >= 40:
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
    }
    if not classes:
        payload["stop"] = "no_work_classes"
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
