---
name: proactive-skill-suggestor
description: Use when scouting Skills Hub from recent Hermes work. Aggressive search; upgrade similar local skills instead of installing duplicates.
version: 1.1.0
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
| Delivery | Silence unless ≥1 inspect-backed match | Empty cron ticks must send nothing |
| Cap | **5 suggestions** per run | Aggressive hunt, still bounded |
| Mutation | **None** | Suggest; wait for an explicit install/patch ask |
| Similar local skill | **Upgrade/merge incumbent** | Do not add a second skill for the same job |
| Single heavy session | **Still scout it** | High tool-call work is not "one-off curiosity" |

Scheduled runs must assume Hermes cron's **3-minute interrupt**. Run the miner
script first. Only spend model time on the work-class JSON, not raw transcripts.

## Search depth (this is the quality bar)

Shallow name-match against 80k+ hub skills will always "find something." That
is a fail. A recommendation is only valid if you did all of this:

1. **Work classes from titles, cwd, first-prompt tokens, and loaded skills.**
   Run `python3 scripts/mine_session_themes.py --hours 72` (default 8 classes).
   Drop greetings and format tests. Keep a **single heavy session**
   (`qualify: heavy`) — do not skip it as one-off.
2. **Search the hub hard.** Prefer
   `python3 scripts/search_hub.py --mine-json <miner.json>` which fires the
   generated `hub_queries` (task, `cli`, `api`, `workflow`, `admin`). If a
   query returns nothing, the next synonym still runs. Do not stop at the
   first empty search.
3. **Inspect 2–3 finalists per live class** with `hermes skills inspect <id>`.
   Prefer `trust_level: official`, then community with a real SKILL.md body.
   Name similarity is not evidence.
4. **Subtract the local catalog**, then hunt **upgrades** of skills already
   loaded this window. Same job + material delta → `upgrade-incumbent`.
5. **Classify** (see rubric). Inspect the incumbent `SKILL.md` before
   `net-new`.
6. **Stop** only if every inspected hit is a restatement of a local skill
   with no delta. Output nothing (cron) or `no_suggestions` (interactive).

Hub is the install source of truth (`hermes skills search/inspect/install`).
Do not bypass it with skills.sh, SkillsMP, or `npx skills`. ClawHub is
fallback/reference only.

## Classification

| Class | When | Action |
|---|---|---|
| `upgrade-incumbent` | Local skill already owns the job; hub has a **material delta** | Propose merge into the incumbent. Quote the missing commands, APIs, pitfalls, or current docs. Do **not** install a sibling. |
| `net-new` | No local skill covers the job; inspect passed; class is recurring **or heavy** | Propose `hermes skills inspect` + `install` unexecuted. |
| `reject-duplicate` | Local skill already covers it; hub adds branding or restates the same steps | Skip. Name the incumbent. |
| `skip-one-off` | Tiny session, greeting, or format test. Not heavy work. | Skip. |
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
2. Run `scripts/search_hub.py --mine-json ...` (or equivalent hub searches).
3. Inspect 2–3 finalists per live class. Load close incumbents with `skill_view`.
4. Classify. Keep at most 5 suggestions, upgrades first when the job is already owned.
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
