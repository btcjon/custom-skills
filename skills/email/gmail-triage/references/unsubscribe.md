# Unsubscribe workflow

## Method selection from one message

Senders advertise unsubscribe mechanisms through `List-Unsubscribe` (RFC 2369,
<https://www.rfc-editor.org/rfc/rfc2369>) and `List-Unsubscribe-Post` (RFC 8058,
<https://www.rfc-editor.org/rfc/rfc8058>).

The method is derived from **one representative message**, and both the target URL
and the one-click header must come from that same message. `rank` picks the
representative deterministically (preferring a message that advertises one-click,
then the most recent) and reports it as `evidence.message_id`. Mixing a URL from
one message with a `List-Unsubscribe-Post` header from another would fabricate
one-click support that the sender never advertised.

Header values are split on commas that sit outside angle brackets, so
`<https://host/u?id=1,2>, <mailto:leave@example.com>` yields two targets and not
three fragments.

| Method | Condition | Handling |
|---|---|---|
| `one_click` | HTTPS target and `List-Unsubscribe=One-Click` in the same message | run the helper below |
| `mailto` | a mailto target and no compliant one-click | send or offer the prescribed message for that exact sender only |
| `web_review` | HTTPS target without one-click | open for the user to review; do not log in, pay, accept an offer, or solve a challenge |
| `unavailable` | no advertised method | record `unavailable`; a user-selected future-only filter can still stop Inbox delivery |

## Secure one-click helper

Re-read the representative message with `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`, take
the HTTPS target from it, write it to a file so it stays out of shell history, and
run:

```bash
python3 scripts/unsubscribe_oneclick.py \
  --account you@example.com \
  --address news@example.com \
  --selected-action unsubscribe_only \
  --evidence-message-id 19b11732c1b578fd \
  --url-file /tmp/target.url
```

Add `--dry-run` to validate the target without sending anything. The helper treats
the URL as hostile input from mail:

- HTTPS only, port 443 only, no embedded credentials, ASCII only, no control
  characters, 2048-character ceiling.
- Rejects `localhost`, bare hostnames, and `.local`, `.localhost`, `.internal`,
  `.intranet`, `.home.arpa` names.
- Rejects any target that resolves to a non-public address: loopback, private,
  link-local (including `169.254.169.254`), carrier NAT, multicast, reserved,
  documentation ranges, IPv6 unique-local, and IPv4-mapped IPv6 forms.
- Resolves the name once, rejects the request unless **every** answer is public,
  then connects to that pinned address with SNI and `Host` set to the original
  hostname. A later DNS answer cannot move the connection to an internal target.
- Sends exactly `List-Unsubscribe=One-Click` as
  `application/x-www-form-urlencoded`, never follows redirects, and bounds both
  the timeout (default 10s, max 30s) and the response read (default 64 KiB).
- Requires an explicit `--selected-action` from the unsubscribe family and an
  `--evidence-message-id`; it refuses to run for `archive_label` or `trash`.
- Prints the host, a digest of the URL, the pinned address, and the outcome. It
  never prints or logs the URL, its tokens, or the response body.

Outcomes: `submitted`, `validated_not_sent` (dry run), `review_required`
(redirect, 401, 403, 405, 429), `failed`, or a top-level `rejected` with the
reason a target was refused. A rejection or failure never authorizes browser
automation or a broader filter.

## Monitoring after the request

Monitor the exact sender for a bounded period (default 14 days) with a search that
includes spam and trash, because a future-only filter can hide a sender that is
still sending:

```bash
python3 scripts/triage_core.py monitor --account you@example.com \
  --address news@example.com --input post-decision.jsonl \
  --decision-at 2026-09-04T00:00:00Z --include-spam-trash
```

Without `--include-spam-trash` the outcome is `insufficient_evidence`. `quiet` is
suggestive, never proof of a completed unsubscribe.

## Undo

`triage_core.py undo --account … --address …` returns the exact Gmail filter IDs
recorded for that sender. Deleting them stops the filter. It does not withdraw an
unsubscribe request already sent, restore messages already in Trash, or relabel
existing mail. Confirm with a fresh `GMAIL_LIST_FILTERS` readback.

## Privacy-safe receipts

Record the exact sender, candidate ID, selected action, method class, outcome
class, timestamp, and Gmail filter ID. Never record unsubscribe URLs or tokens,
message bodies, subjects, authentication codes, API keys, or OAuth tokens.
