# Operating policy

## Default categories

These are starter categories, not universal truth. Preserve the user's existing
labels and policy when present.

| Category | Default action |
|---|---|
| Security or authentication | Keep in Inbox; mark for immediate attention |
| Important human correspondence | Keep in Inbox |
| Meetings, travel, legal, medical, financial | Keep in Inbox |
| Receipts and active account notices | Label; archive only if user opted in |
| Newsletters and marketing | Label; archive only if user opted in |
| Social or product notifications | Label; archive only if user opted in |
| Obvious spam or malicious mail | Leave Gmail spam controls in charge |
| Uncertain | Keep in Inbox |

Never use the unsubscribe or cleanup workflow for transactional and
account-critical mail merely because it is frequent.

## Classification input

Use sender address, sender display name, subject, selected safe headers, Gmail
labels, and a short decoded text excerpt only when needed. Do not send complete
threads or attachments to a model by default. Strip authentication codes and
obvious secrets from model input.

Return a structured decision:

```json
{
  "category": "newsletter",
  "confidence": 0.91,
  "keep_in_inbox": false,
  "labels": ["Triage/Newsletter"],
  "reason": "Recurring newsletter with List-Unsubscribe header"
}
```

The reason must be short and must not quote private content.

## Required protections

Always keep in Inbox when any of these apply unless the user has an explicit
sender-specific override:

- Gmail `IMPORTANT` or `STARRED` label.
- Authentication, password reset, security alert, login, suspicious activity,
  device approval, or verification code.
- Direct reply in an active human conversation.
- Calendar/meeting change, travel disruption, legal notice, medical message,
  bank/payment warning, tax message, or active purchase/account receipt.
- The sender is on the user's protected list.
- The classifier is uncertain or fails.

## Idempotence and retries

Persist the message ID plus a hash of the labels/policy inputs that affect the
decision. Skip unchanged successful messages. Re-evaluate when relevant labels
or policy change. After classification failure, use bounded exponential retry
cooldowns and keep the message in Inbox.

Do not retry Gmail mutations blindly. First read back the message, label, or
filter state and determine whether the prior request succeeded.

## Health evidence

For each reporting period calculate:

- unique incoming messages;
- retained in Inbox and protected count;
- archived/labeled/trash counts attributable to this system;
- classifications, unchanged-state skips, failures, retries, and unresolved
  cooldowns;
- unsubscribe attempts by method and verified result;
- filters created, verified, removed, or missing.

Provider mailbox state is not proof of causation. Report an action as performed
by Gmail Triage only when its audit receipt and Gmail readback agree.
