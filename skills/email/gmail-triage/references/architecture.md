# Architecture and portability

## What the package provides

```text
person <-> skill-aware agent <-> Composio Gmail tools <-> Gmail
                  |
                  +-> scripts/triage_core.py       aggregation, plans, filter safety,
                  |                                receipts, resume, monitoring, health
                  +-> scripts/unsubscribe_oneclick.py  hardened RFC 8058 POST
                  +-> account-scoped SQLite state (outside the repository)
```

The skill tells an agent how to inspect, decide, ask for a selection, execute,
verify, and record. Composio owns the user's OAuth connection and executes Gmail
API calls. The helpers own everything deterministic, need only the Python 3.10+
standard library, and never contact Gmail. `unsubscribe_oneclick.py` is the single
component that opens a network connection, and only to a validated public HTTPS
target the user selected.

## Deployment profiles

### Chat-only community profile (default)

The user connects Gmail to Composio and invokes the skill when they want a review
or a cleanup batch. The agent fetches a bounded window, runs the helpers, presents
candidates, executes only the selected actions, verifies them, and records
receipts.

No server, database service, Google Cloud project, or model API key. Nothing runs
between sessions.

### Scheduled local profile

The user's own scheduler (cron, launchd, CI) invokes their agent or a wrapper on
an interval with the same `--state-dir`, so dedupe, receipts, and undo history
persist across runs. This package ships no scheduler, service, or unit file; the
user creates it. Requirements: the machine is awake and the agent runtime can use
tools unattended within the same safety rules.

### Hosted multi-user profile

A service creates one Composio session per stable application user, consumes Gmail
trigger events, applies the shared policy, calls allowlisted tools, and stores
per-user state. Never reuse one user's connected account or state for another.
Composio-managed Gmail triggers are polling based, so delivery is not immediate.
The operator then owns hosting, tenant isolation, model cost, audit logging,
incident response, and privacy disclosure. Nothing hosted ships here.

## What is not portable automatically

- The user's Gmail authorization and granted scopes.
- A particular harness's scheduling, checkbox UI, or notification surface.
- A background model runtime.
- Private policies, protected-sender lists, credentials, receipts, or undo history
  from another installation.
- Reliable unsubscribe for senders that omit standards-based headers or hide
  behind an authenticated preference center.

## Capability statement

Cross-agent, not universal. A working installation needs an Agent Skills reader
plus one Composio access path (native plugin, CLI, SDK, or MCP session) and an
account whose scopes cover the chosen mode. An agent that can read skill files but
cannot call Composio cannot operate Gmail; say so rather than implying otherwise.

## Current authoritative documentation

- Composio docs: <https://docs.composio.dev/docs>
- Composio Connect: <https://docs.composio.dev/docs/composio-connect>
- Sessions via MCP: <https://docs.composio.dev/docs/sessions-via-mcp>
- Single toolkit MCP: <https://docs.composio.dev/docs/mcp-overview>
- Authenticating tools and scopes: <https://docs.composio.dev/docs/authenticating-tools>
- Gmail toolkit tools and parameters: <https://docs.composio.dev/toolkits/gmail>
- Triggers: <https://docs.composio.dev/docs/triggers>
- Gmail filter resource: <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.filters>

Tool names, scopes, plans, and trigger latency change. Discover current schemas
during setup instead of treating this file as a permanent API contract.
