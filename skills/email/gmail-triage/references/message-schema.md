# Normalized message schema

The deterministic helper accepts either a JSON array or JSON Lines. Normalize
Composio results to one object per Gmail message:

```json
{
  "id": "provider-message-id",
  "thread_id": "provider-thread-id",
  "from_address": "news@example.com",
  "from_name": "Example News",
  "date": "2026-09-04T12:00:00Z",
  "labels": ["INBOX", "UNREAD"],
  "list_unsubscribe": ["https://example.com/unsubscribe/token", "mailto:leave@example.com"],
  "list_unsubscribe_post": "List-Unsubscribe=One-Click"
}
```

Only `id`, `from_address`, and `date` are required. `labels` defaults to an
empty list. Header names may use snake case or their original hyphenated form.

Do not put subjects, bodies, authentication codes, OAuth tokens, or raw
unsubscribe URLs in committed fixtures or public receipts. Runtime normalized
files should be private and temporary.

## Candidate output

`rank` produces one record per normalized exact sender address:

```json
{
  "candidate_id": "c_1a2b3c4d5e",
  "address": "news@example.com",
  "display_name": "Example News",
  "total": 14,
  "last_30_days": 6,
  "unread": 5,
  "inbox": 9,
  "last_seen": "2026-09-04T12:00:00+00:00",
  "unsubscribe_method": "one_click",
  "score": 29,
  "protected": false,
  "reason": "high volume; often unread; one-click available"
}
```

Candidate IDs are stable hashes of the normalized address and are safe to show
in a text-only selection interface. They are not authorization until the user
explicitly selects them.
