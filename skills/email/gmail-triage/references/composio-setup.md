# Composio setup

## Fast path for an individual

Use Composio's native agent plugin or connect an MCP-compatible client to
Composio Connect. The user completes the Gmail OAuth consent flow once; Composio
stores and refreshes that connection.

Typical CLI discovery flow:

```bash
composio link gmail
composio search "fetch recent Gmail messages with full headers"
composio execute GMAIL_FETCH_EMAILS --get-schema
composio execute GMAIL_FETCH_EMAILS -d '{ query: "newer_than:30d", max_results: 10, include_payload: true }'
```

Do not paste API keys, OAuth codes, connection URLs, or tool output containing
message bodies into public files or support chats.

## MCP path

Composio Connect exposes a shared MCP endpoint at:

```text
https://connect.composio.dev/mcp
```

For an application, create a user-scoped Composio session with Gmail enabled and
expose that session through MCP. Prefer an explicit tool allowlist. The current
Composio session API can create an MCP endpoint that any compatible client can
consume.

## Recommended tool groups

Discover schemas before use because tool versions evolve.

Read-only review:

- `GMAIL_FETCH_EMAILS`
- `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`
- `GMAIL_LIST_LABELS`
- `GMAIL_LIST_FILTERS`

Triage mutations:

- `GMAIL_ADD_LABEL_TO_EMAIL`
- `GMAIL_MODIFY_THREAD_LABELS`
- `GMAIL_CREATE_LABEL`

Cleanup mutations:

- `GMAIL_CREATE_FILTER`
- `GMAIL_GET_FILTER`
- `GMAIL_DELETE_FILTER` for a user-requested undo
- `GMAIL_MOVE_TO_TRASH` only for explicitly scoped existing messages

Never enable permanent-delete tools for this skill.

## Scopes

Use Composio's current scope resolver for the exact allowlist. Common Gmail
capabilities use scopes such as `gmail.readonly`, `gmail.modify`, `gmail.labels`,
and `gmail.settings.basic`. Gmail filter creation specifically requires
`gmail.settings.basic` in current Composio documentation.

Composio-managed OAuth usually removes the need for an individual user to build
a Google Cloud project. It does not remove Google's consent, Workspace-admin,
sensitive-scope, or app-verification rules. A managed organization may need its
administrator to approve the Composio application and scopes.

## Preflight checklist

- Confirm the intended Gmail account, especially when multiple accounts exist.
- Confirm connection status without exposing connection identifiers.
- Resolve required scopes for the current tool version.
- Start with read-only tools and fetch no more content than needed.
- Create or locate a dedicated Gmail label only after the user authorizes live
  triage.
- Test with one harmless message or one synthetic fixture before enabling a
  recurring worker.
- Read Gmail state back after every test mutation and undo the test if requested.

## Failure classification

- **401/connection missing:** reconnect once; do not keep retrying stale tokens.
- **403/scope blocked:** identify the missing scope or administrator restriction.
- **404/tool missing:** search the current toolkit and inspect the replacement
  schema; do not guess parameters.
- **429/rate limit:** stop the batch, preserve completed receipts, and resume only
  after the provider's retry interval.
- **Malformed/partial response:** read back Gmail state before retrying any
  mutation.
- **Ambiguous account:** stop and ask the user to select the account.
