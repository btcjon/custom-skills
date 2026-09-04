# Composio setup

## Detect the access path first

A fresh agent should branch on what is actually available instead of assuming:

```bash
command -v composio && composio --help | head -20   # CLI path
composio whoami                                     # signed in?
```

- **CLI available and signed in:** use `composio search`, `composio tools list
  gmail`, `composio execute <SLUG> --get-schema`, and `composio execute <SLUG> -d
  '{ … }'`. `composio --help` is the authority on the installed version's commands.
- **MCP or native plugin only:** use the harness's Composio tool surface with an
  explicit Gmail tool allowlist. Composio Connect and per-user sessions over MCP
  are documented at <https://docs.composio.dev/docs/composio-connect> and
  <https://docs.composio.dev/docs/sessions-via-mcp>.
- **No Composio access:** this skill cannot operate Gmail. The offline helpers and
  `selftest` still run, and the review workflow can be explained, but no mailbox
  read or mutation is possible. Say so instead of implying a connection exists.
- **Degraded scope:** if filter tools or label creation are missing, run
  `preflight` and report the `degraded_capabilities` it names. Unsubscribe-only
  cleanup can still proceed; future-only filters cannot.

Connect once with `composio link gmail`; Composio stores and refreshes the OAuth
connection. Never paste API keys, OAuth codes, connection URLs, or tool output
containing message content into files, chats, or logs.

## Confirm the account

```bash
composio execute GMAIL_GET_PROFILE -d '{ user_id: "me" }'
```

`GMAIL_GET_PROFILE` returns the authenticated `emailAddress` (sometimes wrapped in
a `data` field). Use it to identify the mailbox dynamically; never hard-code or
assume a default when several accounts are connected. Feed the output to
`triage_core.py preflight`.

## Tools by mode

Discover current schemas before the first call with `composio tools list gmail`
or `composio execute <SLUG> --get-schema`; tool versions change.

| Mode | Tools |
|---|---|
| inspect | `GMAIL_GET_PROFILE`, `GMAIL_FETCH_EMAILS`, `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`, `GMAIL_LIST_LABELS`, `GMAIL_LIST_FILTERS` |
| triage | inspect set plus `GMAIL_CREATE_LABEL`, `GMAIL_ADD_LABEL_TO_EMAIL` |
| cleanup_review | `GMAIL_GET_PROFILE`, `GMAIL_FETCH_EMAILS`, `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`, `GMAIL_LIST_FILTERS` |
| cleanup_execute | adds `GMAIL_CREATE_FILTER`, `GMAIL_GET_FILTER`, `GMAIL_CREATE_LABEL` |
| undo | `GMAIL_LIST_FILTERS`, `GMAIL_DELETE_FILTER` |
| cleanup_existing | `GMAIL_FETCH_EMAILS`, `GMAIL_BATCH_MODIFY_MESSAGES`, `GMAIL_MOVE_TO_TRASH` |

Never enable `GMAIL_BATCH_DELETE_MESSAGES`, `GMAIL_DELETE_MESSAGE`, or
`GMAIL_DELETE_THREAD` for this skill; `preflight` reports them if they appear in
the allowlist.

## Useful parameter facts

These come from the current Gmail toolkit documentation
(<https://docs.composio.dev/toolkits/gmail>); re-check with `--get-schema` rather
than inventing fields.

- `GMAIL_FETCH_EMAILS` takes `query`, `max_results` (cap 500 per page),
  `page_token`, `label_ids`, `include_payload`, `include_spam_trash`, `ids_only`,
  `verbose`. Results are not sorted by recency, and `messages` may be absent.
  Loop on `nextPageToken`; `resultSizeEstimate` is not a stop condition.
- `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID` takes `message_id` and `format`
  (`minimal`, `metadata`, `full`, `raw`). `metadata` is enough for headers.
- `GMAIL_CREATE_FILTER` takes `criteria` and `action` objects.
- Label tools need label IDs (`Label_42`), not display names.
- For precise Gmail search windows use Unix epoch seconds with `after:` and
  `before:`, then check message timestamps against the requested timezone window.
  Do not assume date-string searches use the user's timezone.

## Scopes

Read-only review needs `gmail.readonly`. Labeling and archiving need
`gmail.modify`. Creating or deleting filters needs `gmail.settings.basic`. Resolve
the exact list with Composio's current scope resolver
(<https://docs.composio.dev/docs/authenticating-tools>).

Composio-managed OAuth removes the need for a personal Google Cloud project. It
does not remove Google's consent screen, Workspace admin restrictions, sensitive
scope review, or app verification. A managed organization may need an
administrator to approve the Composio app and its scopes.

## Preflight checklist

- Confirm the intended Gmail account with `GMAIL_GET_PROFILE`.
- Run `triage_core.py preflight` for the mode and stop unless `ready` is true.
- Start read-only and fetch no more content than the question needs.
- Create or locate the archive label only after the user authorizes execution.
- Test with one synthetic fixture or one harmless message before any batch.
- Read Gmail state back after every mutation.

## Failure classification

- **401 / missing connection:** reconnect once; do not retry stale tokens.
- **403 / scope blocked:** name the missing scope or admin restriction.
- **404 / tool missing:** search the toolkit and inspect the replacement schema.
  Never guess parameters.
- **409 on label creation:** the label exists; list labels and reuse its ID.
- **429 / rate limit:** stop the batch, keep completed receipts, resume with
  `resume` after the provider's interval.
- **Malformed or partial response:** read Gmail state back before retrying.
- **Ambiguous account:** stop and ask the user to choose.
