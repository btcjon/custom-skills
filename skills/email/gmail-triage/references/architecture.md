# Architecture and portability

## What the package provides

Gmail Triage separates policy from connectivity:

```text
person <-> skill-aware agent <-> Composio Gmail tools <-> Gmail
                    |
                    +-> local policy, candidate ranking, audit state
                    |
                    +-> optional scheduler or trigger worker
```

The skill tells an agent how to inspect, decide, ask for selection, execute, and
verify. Composio manages the user's OAuth connection and executes Gmail API
operations. The included Python helper ranks candidates and stores privacy-safe
receipts without requiring a particular model provider.

## Deployment profiles

### Chat-only community profile

Best first release. A user connects Gmail to Composio and invokes the skill when
they want an Inbox review or cleanup batch. The agent fetches a bounded window,
runs the helper, presents candidates, and executes only selected actions.

Benefits: no server, database service, Google Cloud project, or dedicated model
API key. Limitation: nothing happens when the user is not running the agent.

### Scheduled local profile

A local scheduler invokes the user's skill-capable agent or a wrapper command at
fixed intervals. The local SQLite file preserves dedupe and audit state.

Benefits: simple and private. Limitations: the computer must be awake, and the
agent runtime must support unattended tool use safely.

### Hosted multi-user profile

A small service creates one Composio session per stable application user ID,
receives Gmail trigger events, invokes a model against the shared policy, calls
allowlisted Gmail tools, and stores per-user state. Never reuse one user's
connected account or state for another user.

Benefits: continuous operation and an easy connect flow. Limitations: the
operator now owns hosting, tenant isolation, model cost, audit logging, incident
response, and privacy disclosures. Composio-managed Gmail triggers are polling
based and may not be immediate.

## What is not portable automatically

- The user's Gmail authorization and OAuth scopes.
- A particular agent's scheduling, interactive checkbox, or notification UI.
- A background model runtime.
- Private policies, VIP lists, credentials, historical audit records, or message
  state from another installation.
- Reliable unsubscribe for senders that omit standards-based headers or require
  an authenticated preference center.

## Capability statement

The package is cross-agent, not universal. A compatible installation needs an
Agent Skills reader plus one approved Composio access path: native plugin, CLI,
SDK session, or MCP session. The agent must also support the relevant mutation
and confirmation model. Read-only use remains possible with fewer scopes.

## Current authoritative documentation

- Composio overview: <https://docs.composio.dev/docs>
- Composio Connect MCP: <https://docs.composio.dev/docs/composio-connect>
- Sessions via MCP: <https://docs.composio.dev/docs/sessions-via-mcp>
- Gmail toolkit: <https://docs.composio.dev/toolkits/gmail>
- Gmail triggers: <https://docs.composio.dev/docs/triggers>
- OpenAI apps overview: <https://help.openai.com/en/articles/11487775-connectors-in>

Tool names, scopes, plans, and trigger latency can change. Discover current tool
schemas and required scopes during setup instead of treating this document as a
permanent API contract.
