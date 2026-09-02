# Decision rubric

Use this after hub inspect, not before.

## Recurring vs one-off

Scout it when any of these hold:

- same job class in ≥2 sessions (`qualify: recurring`)
- one session with ≥20 tool calls or ≥12 messages (`qualify: heavy`)
- a loaded local skill whose job the hub might cover better (upgrade pass)

One-off (skip): greeting, format test, or a tiny finished lookup with no
loaded skills and no heavy tool use.

Do **not** skip Shopify-class work, Discord ops, or other real product jobs
just because they appeared once.

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
