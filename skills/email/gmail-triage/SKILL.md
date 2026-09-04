---
name: gmail-triage
description: Review, classify, clean up, and unsubscribe Gmail through a connected Composio Gmail toolkit. Use for inbox triage, sender cleanup batches, exact-sender future filters, unsubscribe review, and mailbox health checks; do not use when Gmail or Composio is unavailable.
license: MIT
metadata:
  version: 0.1.0
  author: btcjon
  tags: [gmail, email, composio, triage, unsubscribe, cleanup]
---

# Gmail Triage

Operate Gmail through Composio without requiring the mailbox owner to build a
Google Cloud project. The skill supports two complementary jobs:

- **Live triage:** inspect new mail, classify it under the user's policy, apply
  labels, and remove only clearly safe categories from the Inbox.
- **Mailbox hygiene:** surface high-volume or low-value senders, let the user
  select exact addresses, attempt standards-based unsubscribe, and install
  future-only Gmail filters.

The connected agent supplies judgment. Composio supplies Gmail authentication
and actions. Continuous unattended operation additionally requires a scheduler
or webhook worker; installing this skill alone does not create one.

## Before operating

1. Confirm a Composio Gmail connection exists for the intended Google account.
   If not, create one through the agent's Composio connection flow.
2. Discover the current Gmail tool schemas before the first call. Prefer the
   current equivalents of `GMAIL_FETCH_EMAILS`, `GMAIL_ADD_LABEL_TO_EMAIL`,
   `GMAIL_MODIFY_THREAD_LABELS`, `GMAIL_CREATE_LABEL`, `GMAIL_LIST_FILTERS`,
   `GMAIL_CREATE_FILTER`, and `GMAIL_MOVE_TO_TRASH`.
3. Use only the minimum tool allowlist and OAuth scopes needed for the requested
   mode. Read-only review should not load mutation tools.
4. Identify the exact connected account when more than one Gmail account is
   present. Never rely on an ambiguous default account.

For connection options, scopes, and current Composio commands, read
[Composio setup](references/composio-setup.md).

## Choose a mode

### Inspect or explain

Read Gmail metadata and the minimum content needed to answer the question.
Summarize observed state separately from recommendations. Do not mutate Gmail.

### Live triage

Fetch new or unread Inbox mail, preserve account-critical messages, classify
the remainder using the user's declared categories, then apply only the policy's
allowed label/archive actions. Never infer permission to send, delete, create a
filter, or unsubscribe from a general request to "triage" or "review."

Read [operating policy](references/operating-policy.md) before configuring live
triage or changing a classification policy.

### Cleanup review

1. Fetch a bounded window, normally 60–90 days, using metadata plus headers.
2. Normalize messages to the schema in
   [message schema](references/message-schema.md).
3. Run `scripts/triage_core.py rank` to aggregate exact sender addresses.
4. Exclude protected and previously handled senders.
5. Present one large, reviewable batch with exact address, display name, message
   count, recent count, unread count, last seen date, and recommendation reason.
6. Do not treat a recommendation, checked-by-default item, or candidate list as
   authorization.

Use real interactive controls when the host supports them. Otherwise use stable
candidate IDs and ask the user to return the IDs or exact addresses.

### Execute selected cleanup

The user's explicit selection is authorization only for the exact addresses
selected and the action described beside the selection. Do not add a redundant
confirmation round unless the selection is ambiguous or the action has changed.

For each selected exact sender:

1. Re-read a recent representative message and its unsubscribe headers.
2. Prefer RFC one-click unsubscribe when both a supported HTTPS URI and the
   required one-click indication are present.
3. Otherwise surface a mailto or web-review method; do not browse through login,
   payment, or preference changes silently.
4. Create an exact-sender, future-only Trash filter only when the user selected
   that action. Do not run a search-and-apply operation against existing mail.
5. Read back the live Gmail filter list and verify the exact criterion/action.
6. Record the attempt and result without storing message bodies, unsubscribe
   tokens, credentials, or authentication codes.

Read [unsubscribe workflow](references/unsubscribe.md) for method selection,
failure handling, monitoring, and undo behavior.

### Health report

Report a bounded period with counts for received, retained in Inbox, archived,
spam, trash, protected, classification failures, retries, and unresolved errors.
Do not include subjects, bodies, authentication codes, unsubscribe URLs, or
credentials. Distinguish Gmail state from actions proven to have been performed
by this skill.

## Safety invariants

- Default to read-only until the user explicitly selects a mailbox mutation.
- Preserve Important, starred, VIP, direct human correspondence, receipts,
  security alerts, authentication codes, legal, medical, financial, travel,
  meeting, and active account messages unless the user creates a narrower rule.
- Never permanently delete mail. Use Trash only when explicitly selected.
- Cleanup filters match the exact normalized sender address, not a display name,
  broad domain, subject fragment, or guessed organization.
- New filters affect future mail only unless the user separately requests an
  existing-message cleanup and sees its exact scope.
- An unsubscribe failure does not authorize browser automation or broader
  filtering. Record the failure and keep it reviewable.
- Verify mutations by reading Gmail state back. A planned action, tool success
  string, or local receipt is not sufficient proof.
- Keep API keys, OAuth tokens, connected-account IDs, message bodies, and
  unsubscribe URLs out of skill files, prompts, logs, and public receipts.
- Stop after an ambiguous account, missing required scope, partial batch, rate
  limit, or malformed tool response. Read current state before retrying.

## Portability boundary

This skill can be used by Codex, Claude Code, Hermes, Cursor, or another agent
that can load Agent Skills and call a Composio Gmail connection. Compatibility
requires all three layers:

1. The harness can load this `SKILL.md` and its referenced files.
2. The harness can use Composio through its native plugin, CLI, SDK, or MCP.
3. The connected Gmail account grants the scopes required by the chosen tools.

An agent that merely understands skill files but cannot call Composio cannot
operate Gmail. A chat session can run on-demand cleanup; continuous triage needs
an external scheduler or Composio trigger consumer. See
[architecture](references/architecture.md).

## Local deterministic helpers

The helpers use only the Python standard library and never contact Gmail:

```bash
python3 scripts/triage_core.py rank --input normalized-messages.jsonl --output candidates.json
python3 scripts/triage_core.py plan --input normalized-messages.jsonl --selected selected.txt --output plan.json
python3 scripts/triage_core.py record --db state.sqlite3 --plan plan.json --results results.json
python3 scripts/triage_core.py health --db state.sqlite3 --hours 24
```

They accept normalized metadata, produce deterministic candidate/action plans,
and keep privacy-safe local audit state. Gmail reads and mutations remain visible
Composio tool calls performed by the agent or optional worker.
