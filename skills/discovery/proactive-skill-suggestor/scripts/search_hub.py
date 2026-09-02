#!/usr/bin/env python3
"""Run Hermes Skills Hub searches from miner JSON. Suggest-only. No installs."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mine-json", type=Path, required=True)
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--max-queries", type=int, default=24)
    return p.parse_args()


def search(query: str, limit: int) -> list[dict]:
    r = subprocess.run(
        ["hermes", "skills", "search", "--json", "--limit", str(limit), query],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return []
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def local_name_set(mine: dict) -> set[str]:
    names = {n.lower() for n in mine.get("local_skill_names") or []}
    for item in mine.get("local_skills_near_themes") or []:
        names.add(str(item.get("name") or "").lower())
    return names


def collect_queries(mine: dict, max_queries: int) -> list[tuple[str, str]]:
    """Round-robin queries across work classes so later themes still get searched."""
    queues: list[tuple[str, list[str]]] = []
    for cls in mine.get("work_classes") or []:
        label = cls.get("label") or ""
        qs = [q.strip() for q in (cls.get("hub_queries") or []) if q.strip()]
        if qs:
            queues.append((label, qs))
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    i = 0
    while queues and len(out) < max_queries:
        label, qs = queues[i]
        q = qs.pop(0)
        key = q.lower()
        if key not in seen:
            seen.add(key)
            out.append((q, label))
        if qs:
            i = (i + 1) % len(queues)
        else:
            queues.pop(i)
            if queues:
                i %= len(queues)
    return out


def relevant(hit: dict, query: str) -> bool:
    token = (query.split() or [""])[0].lower()
    if len(token) < 4:
        return False
    ident = str(hit.get("identifier") or "").lower()
    name = str(hit.get("name") or "").lower()
    desc = str(hit.get("description") or "").lower()
    pat = re.compile(rf"(^|[^a-z0-9]){re.escape(token)}([^a-z0-9]|$)")
    if pat.search(name) or pat.search(ident):
        return True
    # Description-only hits must still contain the token as a word.
    return bool(pat.search(desc) and token in name.split() + ident.replace("/", " ").split())


def classify_hit(hit: dict, local_names: set[str]) -> str:
    name = str(hit.get("name") or "").lower()
    ident = str(hit.get("identifier") or "").lower()
    slug = ident.rsplit("/", 1)[-1]
    if name in local_names or slug in local_names:
        return "upgrade-candidate"
    return "net-new-candidate"


def main() -> int:
    args = parse_args()
    mine = json.loads(args.mine_json.read_text(encoding="utf-8"))
    local_names = local_name_set(mine)
    queries = collect_queries(mine, args.max_queries)
    hits_by_id: dict[str, dict] = {}
    for query, label in queries:
        for hit in search(query, args.limit):
            if not relevant(hit, query):
                continue
            ident = hit.get("identifier") or hit.get("name")
            if not ident:
                continue
            rec = hits_by_id.setdefault(
                ident,
                {
                    "identifier": ident,
                    "name": hit.get("name"),
                    "source": hit.get("source"),
                    "trust_level": hit.get("trust_level"),
                    "description": (hit.get("description") or "")[:240],
                    "queries": [],
                    "work_classes": [],
                    "lane": classify_hit(hit, local_names),
                },
            )
            if query not in rec["queries"]:
                rec["queries"].append(query)
            if label and label not in rec["work_classes"]:
                rec["work_classes"].append(label)

    def rank(rec: dict) -> tuple:
        official = 0 if rec.get("trust_level") == "official" else 1
        lane = 0 if rec.get("lane") == "upgrade-candidate" else 1
        return (official, lane, -len(rec.get("queries") or []))

    ranked = sorted(hits_by_id.values(), key=rank)
    payload = {
        "query_count": len(queries),
        "hit_count": len(ranked),
        "queries": [q for q, _ in queries],
        "hits": ranked[:40],
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
