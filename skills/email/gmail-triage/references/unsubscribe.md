# Unsubscribe and filter workflow

## Method selection

Email may advertise unsubscribe mechanisms through `List-Unsubscribe` and
`List-Unsubscribe-Post` headers.

1. **One-click HTTPS:** Use only when an HTTPS URI is present and
   `List-Unsubscribe-Post` indicates one-click. Submit according to the standard,
   without following unrelated redirects or exposing the URL in logs.
2. **Mailto:** Offer or send the prescribed unsubscribe email only when the user
   selected that exact sender and the action is clearly represented.
3. **Web review:** Open for user review when no compliant one-click method exists.
   Do not log in, accept an offer, change unrelated preferences, or solve a
   challenge without a new explicit instruction.
4. **No method:** Record `unavailable`; a user-selected future-only exact sender
   filter can still stop future Inbox delivery.

Primary standards:

- RFC 2369, list command headers: <https://www.rfc-editor.org/rfc/rfc2369>
- RFC 8058, one-click unsubscribe: <https://www.rfc-editor.org/rfc/rfc8058>

## Exact-sender filter

Create a Gmail filter criterion matching the complete normalized mailbox in the
`From` field and an action that sends future matching messages to Trash. Avoid
display names, domains, plus-address simplification, or organization aliases.

After creation:

1. List or fetch the filter from Gmail.
2. Verify the criterion and Trash action exactly.
3. Record the Gmail filter ID privately so it can be removed later.
4. Confirm that no existing-message search or mutation was run.

## Monitoring

For an unsubscribe request, monitor new messages from the exact address for a
bounded period. A quiet sender suggests success but is not definitive proof.
Classify outcomes as:

- `submitted`: endpoint accepted the request;
- `quiet`: no post-decision messages observed in the period;
- `still_sending`: a post-decision message arrived;
- `review_required`: redirect, authentication, challenge, or preference center;
- `failed`: endpoint or mailto action returned a definite error;
- `unavailable`: no supported method was advertised.

The future-only filter may hide a continuing sender from Inbox, so monitoring
must search all mail including Trash for that exact sender.

## Undo

Undo removes only the exact Gmail filter recorded for that sender. It does not
reverse an unsubscribe request or restore previously trashed messages unless the
user separately requests and scopes those actions.

## Privacy-safe receipts

Store exact sender address, candidate ID, selected action, method class, result
class, timestamp, and Gmail filter ID. Never store unsubscribe URLs, message
bodies, authentication codes, API keys, or OAuth tokens.
