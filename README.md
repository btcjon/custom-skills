# custom-skills

Public home for custom Hermes Agent skills that are not part of the bundled
Hermes tree.

Skills live under `skills/<category>/<skill-name>/` so later additions stay
grouped (discovery, devops, media, …). Add a category folder when the first
skill in that class lands. Do not dump new skills at the repo root.

## Skills

| Skill | Category | Purpose |
|---|---|---|
| [proactive-skill-suggestor](skills/discovery/proactive-skill-suggestor/) | discovery | High-bar Skills Hub scout from recent Hermes work. Suggest-only. Upgrade similar local skills instead of installing duplicates. |

## Install (Hermes)

```bash
git clone https://github.com/btcjon/custom-skills.git
ln -s "$(pwd)/custom-skills/skills/discovery/proactive-skill-suggestor" \
  ~/.hermes/skills/proactive-skill-suggestor
```

Load with `skill_view(name="proactive-skill-suggestor")` in a new session.

## Scout once (no cron)

```bash
python3 skills/discovery/proactive-skill-suggestor/scripts/mine_session_themes.py --hours 72
# Agent follows skills/discovery/proactive-skill-suggestor/SKILL.md against that JSON.
```

Empty output means silence. Suggestions are a proposal, not a mutation.

## Adding a skill

1. Pick or create `skills/<category>/`.
2. Put a complete package there: `SKILL.md`, then `scripts/`, `references/`, `tests/` as needed.
3. Symlink the package directory into `~/.hermes/skills/<skill-name>` (not the whole repo).

## Privacy

The miner emits compact work-class JSON (titles, skill names, query seeds).
Do not commit `receipts/` or raw transcripts. This repo is public.
