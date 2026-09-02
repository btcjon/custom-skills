---
name: proactive-skill-suggestor
description: Use when scouting Skills Hub from recent Hermes work. High-bar search; upgrade similar local skills instead of installing duplicates.
version: 1.0.0
author: btcjon
license: MIT
metadata:
  hermes:
    tags: [skills, hub, discovery, upgrade, cron-ready]
    related_skills: [find-skills, skill-developer, capability-evolution-operations, clawhub]
---

# Proactive Skill Suggestor

Suggest-only scout. Mine recent Hermes work, search Hermes Skills Hub, then
either recommend a **net-new** skill or an **upgrade/merge** of an incumbent
local skill. Never install, patch, enable, or publish without an explicit ask.

## When to Use

- User asks to scout the hub from recent sessions / last 1–3 days of work.
- A dry-run or scheduled job should propose skills that would actually help.
- A hub hit overlaps a local skill and the question is upgrade vs skip.

## Do Not Use When

- The user wants a skill installed right now for a named task — use `find-skills`.
- The user wants to author a brand-new local skill from a workflow — use `skill-developer`.
- The user wants evolutionary GEPA/DSPy optimization — use `hermes-agent-self-evolution`.
- You would need to mutate skills, cron, or the keep-list to "finish."

## Defaults

| Knob | Default | Why |
|---|---|---|
| Window | **72 hours**, recency-weighted | 24h is one-off noise; 7d re-litigates old themes |
| Delivery | Silence unless ≥1 high-bar match | Empty cron ticks must send nothing |
| Cap | **3 suggestions** per run | Prompt-budget and keep-list already hurt |
| Mutation | **None** | Suggest; wait for an explicit install/patch ask |
| Similar local skill | **Upgrade/merge incumbent** | Do not add a second skill for the same job |

Scheduled runs must assume Hermes cron's **3-minute interrupt**. Run the miner
script first. Only spend model time on the work-class JSON, not raw transcripts.

## Search depth (this is the quality bar)

Shallow name-match against 80k+ hub skills will always "find something." That
is a fail. A recommendation is only valid if you did all of this:

1. **Work classes, not keywords.** Run
   `python3 scripts/mine_session_themes.py --hours 72`.
   Use at most **5** classes. Drop one-off greetings, status pings, and
   already-solved lookups.
2. **Two or three hub queries per class**, specific then synonym
   (`hermes skills search --json "<query>"`). Do not stop at the first page hit.
3. **Inspect 2–3 finalists** with `hermes skills inspect <id>`. Name similarity
   is not evidence.
4. **Subtract the local catalog** using miner `local_skills` plus
   `skill_view` / `hermes skills list` for close names. Match on name, slug,
   and description job — not exact string only.
5. **Classify** each finalist (see rubric). Inspect the incumbent `SKILL.md`
   before calling something net-new.
6. **Stop** if nothing clears the bar. Output nothing (cron) or `no_suggestions`
   (interactive).

Hub is the install source of truth (`hermes skills search/inspect/install`).
Do not bypass it with skills.sh, SkillsMP, or `npx skills`. ClawHub is
fallback/reference only.

## Classification

| Class | When | Action |
|---|---|---|
| `upgrade-incumbent` | Local skill already owns the job; hub has a **material delta** | Propose merge into the incumbent. Quote the missing commands, APIs, pitfalls, or current docs. Do **not** install a sibling. |
| `net-new` | No local skill covers the job, inspect passed, recurring work class | Propose `hermes skills inspect` + `install` as a command, unexecuted. |
| `reject-duplicate` | Local skill already covers it; hub adds branding or restates the same steps | Skip. Name the incumbent. |
| `skip-one-off` | Single session, curiosity, or already finished | Skip. |
| `skip-bloat` | Would grow the keep-list for a job the catalog already routes | Skip. |

**Material delta** means at least one of: newer API/CLI than the incumbent,
commands the incumbent lacks, failure modes the incumbent omits, or verified
current docs the incumbent contradicts. "Better written" is not a delta.

Upgrade does **not** mean auto-patch. Output a proposed diff/plan. Apply only
when the user says to patch that named skill.

## Hard constraints

- Never `hermes skills install/update/uninstall` unless the current user turn
  names the exact skill id and asks to install.
- Never patch a live `SKILL.md` from this scout unless asked.
- Never copy raw transcripts, emails, Discord ids, or secrets into the public
  repo, receipts that might be shared, or a suggested skill body.
- Never recommend a skill you did not inspect this run.
- Prefer class-level umbrellas over micro-skills.

## Workflow

1. Mine themes (`scripts/mine_session_themes.py`). If `work_classes` is empty, stop.
2. For each class, search hub (2–3 queries), inspect 2–3 finalists.
3. Load close local incumbents with `skill_view`.
4. Classify. Keep at most 3 suggestions, upgrades first when the job is already owned.
5. Write a local receipt under `receipts/` (gitignored) if the user asked for a dry-run.
6. Deliver only if there is at least one `upgrade-incumbent` or `net-new`.

## Required output

For each suggestion:

1. Work class and why it is recurring (session titles/skill names, not quotes of private content).
2. Hub id, search queries used, inspect evidence (what the skill actually does).
3. Classification.
4. If upgrade: incumbent path/name, material delta, proposed merge (not a new install).
5. If net-new: why no incumbent, inspect proof, proposed `hermes skills inspect/install` commands **unexecuted**.
6. Risks (supply chain, overlap, keep-list cost).

## Verification

- [ ] Miner JSON exists and was the only session source
- [ ] Every recommendation has an inspect from this run
- [ ] Every close local skill was opened before `net-new`
- [ ] Similar job → `upgrade-incumbent` or `reject-duplicate`, never a second install
- [ ] No install/patch/cron create ran unless the user asked in this turn
- [ ] Public git has no receipts, transcripts, tokens, or `.env`
