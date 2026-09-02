# Decision rubric

Use this after hub inspect, not before.

## Recurring vs one-off

Recurring: same job class appears in ≥2 sessions, or one long session that
clearly continues. One-off: a single question that already completed.

## Incumbent match

Treat as the same job when any of these hold:

- Normalized names share a distinctive token (`gmail`, `stripe`, `obsidian`).
- Descriptions describe the same user outcome.
- The candidate's README/SKILL would load instead of the local skill for the
  same "Use when" trigger.

Then classify `upgrade-incumbent` or `reject-duplicate`. Never `net-new`.

## Material delta (upgrade)

Count a delta only with inspect evidence:

- Commands or flags the incumbent lacks
- APIs/endpoints that changed
- Pitfalls that would have prevented a recent failure
- Docs that contradict the incumbent

Do not count: longer prose, more emoji, extra registries, popularity.

## Hub ranking (from find-skills, tightened)

1. Problem-fit after inspect
2. Hub metadata actually shown (source, description, freshness if present)
3. Publisher reputation
4. Source repo/docs health for high-stakes finalists
5. Keep-list cost — a fourth overlapping skill is a cost, not a win

Unknown install counts or stars: say unknown. Do not invent them.
