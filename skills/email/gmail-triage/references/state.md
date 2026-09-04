# State, audit, and honest health

## Where state lives

`--state-dir` (default `~/.gmail-triage`) holds `gmail-triage-state.sqlite3`. The
directory is created `0700` and the database `0600`. Keep it outside any
repository or synced public folder; the repository `.gitignore` also blocks the
usual runtime artifacts.

Stored: preferences, protected senders, exact-sender overrides, action receipts,
monitoring observations. Never stored: message bodies, subjects, unsubscribe URLs
or tokens, OAuth tokens, API keys, connected-account identifiers.

## Account scoping

Every row carries the exact connected Gmail address, and every read filters by it.
Two accounts can share one state directory without seeing each other's
preferences, protections, receipts, undo targets, or health numbers. Commands
refuse a plan whose `account` does not match `--account`.

## Preferences

| Preference | Default | Meaning |
|---|---|---|
| `window_days` | 60 | review window the agent should fetch |
| `min_count` | 2 | minimum messages before a sender is a candidate |
| `max_senders` | 25 | hard cap on candidates returned |
| `sample_limit` | 3 | sample message IDs shown per sender |
| `batch_size` | 25 | actions per execution batch |
| `archive_label` | `Triage/Bulk` | label used by archive-and-label filters |
| `default_action` | `archive_label` | action used when a selection line omits one |
| `monitor_days` | 14 | default post-decision monitoring window |

Out-of-range, non-integer, unknown, or comma-containing values are refused rather
than silently clamped.

## Protections and overrides

- `prefs protect --address … --reason …` adds one exact sender to the account's
  protected list. `rank` marks it protected and `plan` blocks it.
- Heuristic protection also applies: account-critical words in the address or
  display name, and Gmail `IMPORTANT` or `STARRED` labels.
- `prefs allow --address … --reason …` records an override for that one exact
  address, with a required reason. Wildcards, bare domains, `@domain`, and
  comma-separated lists are refused, so an override can never become blanket
  permission. `prefs revoke` removes it.

## Receipts and resume

A receipt is keyed by (account, batch, exact sender, selected action), so
replaying the same results updates one row instead of inflating counts. Each
receipt records the unsubscribe method and outcome, the filter outcome and Gmail
filter ID, whether a readback verified it, how many existing messages were
affected, and a status of `pending`, `completed`, `partial`, or `failed`.

`resume` splits a plan into `completed`, `pending`, and `retry_after_readback`.
Retry entries carry `read_back_first: true` because a partial action may already
exist in Gmail. Nothing is retried blindly.

`handled` lists senders already acted on within a window; feed it back with
`rank --exclude-handled` so a second review does not re-offer them.

## Undo

`undo --address` returns the exact filter IDs recorded for that sender, the tool
to remove them (`GMAIL_DELETE_FILTER`), and the explicit list of what undo does
not do: it does not withdraw an unsubscribe request already sent, does not restore
messages already in Trash, and does not relabel existing mail. After deleting,
read `GMAIL_LIST_FILTERS` back and confirm the filter is gone.

## Monitoring outcomes

`monitor` classifies post-decision mail for one exact sender:

- `quiet` — nothing arrived after the decision; suggestive, not proof;
- `still_sending` — mail arrived after the decision, possibly hidden by a filter;
- `insufficient_evidence` — the search did not include spam and trash, so silence
  proves nothing.

Each observation is stored with its window and the include-spam-trash flag.

## Health honesty

`health` derives every number from recorded receipts and readback flags for that
account in the requested period. It never infers success from a Gmail state
snapshot, and it reports `inconsistent_records` when stored data contradicts
itself, for example a filter marked verified with no readback, a created filter
with no filter ID, an `unsubscribe_only` receipt carrying a filter outcome, or
existing messages affected while the action is still pending. Fix the record or
re-verify against Gmail; do not report the cleaner number.
