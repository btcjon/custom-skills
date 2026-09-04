# Selectable actions and their exact Gmail filters

## The five actions

Each selected sender gets exactly one action. Nothing is bundled implicitly.

| Action | Unsubscribe | Filter criteria | Filter action | Existing mail |
|---|---|---|---|---|
| `unsubscribe_only` | attempted | none | none | untouched |
| `archive_label` | no | `from` = exact address | `removeLabelIds: ["INBOX"]`, `addLabelIds: [<label id>]` | untouched |
| `trash` | no | `from` = exact address | `addLabelIds: ["TRASH"]` | untouched |
| `unsubscribe_and_archive_label` | attempted | `from` = exact address | archive and label | untouched |
| `unsubscribe_and_trash` | attempted | `from` = exact address | `addLabelIds: ["TRASH"]` | untouched |

Defaults: when the user chooses filtering without naming a disposition, use
archive-and-label, not Trash. Trash requires an explicit selection. Choosing
unsubscribe alone creates no filter at all.

The Gmail filter resource is `{id, criteria, action}` with `criteria.from` and
`action.addLabelIds` / `action.removeLabelIds`
(<https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.filters>).
Gmail's `criteria.from` accepts a display name or an address, so always pass the
complete normalized mailbox and never a name or bare domain.

## Selection file format

One line per sender, `address [action]`, comments after `#`:

```text
news@example.com unsubscribe_and_archive_label
deals@shop.example.net trash
updates@app.example.org unsubscribe_only
quiet@example.com            # uses the account's default_action
```

A candidate ID from `rank` may replace the address. A duplicate target, an extra
field, an unknown action, or an empty file is an error, not a guess.

## Label ids, not label names

`archive_label` filters need a Gmail label ID such as `Label_42`. The plan carries
the placeholder `${archive_label_id}` plus `requires_label_id: true`. Resolve it
with `GMAIL_LIST_LABELS`, or create the label once with `GMAIL_CREATE_LABEL`, then
pass `--label-id` to `filters-check` and `filters-verify`. Both commands refuse to
proceed with an unresolved placeholder.

## Duplicate-safe creation

Run `filters-check` against a live `GMAIL_LIST_FILTERS` readback before creating
anything. It sorts planned filters into three buckets:

- `to_create` — no live filter targets this sender; create it.
- `duplicate_skip` — a live filter already has the identical criterion and action;
  make no call and record `duplicate_skipped`.
- `conflicts` — a live filter targets this sender with a different action, or aims
  at the sender through a `criteria.query` such as `from:news@example.com`; stop and
  let the user decide. Never silently replace or stack a second filter.

A query-based filter is treated as a conflict rather than a duplicate because its
scope cannot be compared exactly, and it never counts as verification.

## Verification

After creation, read the filter list again and run `filters-verify`. Only
`readback_verified: true` entries may be recorded or reported as verified.
`mismatch` means a filter for that sender exists with different effects;
`missing` means creation did not take effect. A filter carrying a `forward`
action never counts as verification.

## What a plan is not

A plan is a record of the user's selection. It is not authorization, not proof,
and not a promise that anything ran. `record` enforces this boundary: results
must correspond to planned actions, created or verified filters must carry the
Gmail filter ID from readback, and `unsubscribe_only` receipts must not report a
filter outcome.
