# Digest workflow boundary

This package implements a deterministic digest-edition gate and crash-safe
claim/render state on top of `hermes-workflow-state`.

The pre-check emits Hermes' native `{"wakeAgent":false}` control object for
not-due, disabled, empty, already-processed, and in-flight states. Exactly one
successful claim emits `{"wakeAgent":true,"context":...}` with a bounded
candidate envelope. This makes no-change ticks zero-token without adding a
second scheduler.

Native Hermes cron remains the only scheduler and Telegram delivery owner. The
package does not write `jobs.json`, call a messaging API, carry Telegram
credentials, read Hermes' private database, change Miniflux read state, infer
delivery, or implement automatic delivery retries. Deployment configuration owns
profile selection, schedules, private paths, and concrete delivery destinations.

Edition preferences use the shared bounded preference CLI and this package's
immutable contract; routes and credentials cannot be added through preferences:

```console
hermes-workflow-preferences \
  --state-dir /var/lib/hermes-digest/PROFILE/preferences \
  --contract /run/current-system/sw/share/hermes-digest-workflow/preferences.json \
  preview --input requested-edition.json
hermes-workflow-preferences \
  --state-dir /var/lib/hermes-digest/PROFILE/preferences \
  --contract /run/current-system/sw/share/hermes-digest-workflow/preferences.json \
  apply --input requested-edition.json --actor operator --reason 'update digest edition'
```

The managed cron pre-check is `hermes-digest-workflow live-gate`; it reads only
the current profile's `0400` Miniflux endpoint and credential, verifies a
non-admin account, pages unread entries with a hard 500-row/10 MiB response
bound, preserves entry/feed provenance, and never mutates Miniflux.

## State transition

```text
claimed -> rendered
   |
   +-> failed
```

Rendering durably stores the exact edition body and SHA-256. Rendering or an
explicit editorial failure marks the selected Miniflux entry identities
processed, so a failed newest candidate cannot starve older candidates and
batch-record eviction cannot replay terminal entries. Claim expiry marks the
claim failed but leaves its candidate identities reclaimable; a later gate may
issue a fresh claim for that same deterministic batch. There is no automatic
native-delivery retry state machine.

The selected state retains at most 128 recent batch records, at most 2,048
canonical processed-entry ID ranges, and at most the selected plus two prior
immutable generations. Adjacent IDs coalesce. If the safe range bound would be
exceeded, publication fails closed rather than tombstoning unknown IDs or
forgetting prior terminal identities.

Candidate packing is deterministic. It preserves canonical identity and source
provenance, truncates only optional summary text when needed, and skips rows that
cannot fit the configured envelope rather than rejecting a contract-valid
prompt limit.

## Native delivery semantics

Telegram is a non-authoritative, best-effort projection. Native Hermes execution history and known delivery
errors are useful observability, but they are not successful-provider or
human-read receipts. The workflow therefore has no `delivery_requested`,
`delivered`, acknowledgement, sender, or automatic replay state.

Ambiguous native delivery does not discard the durable batch identity, selected
entry IDs, or rendered content. An operator may explicitly replay retained
content through native Hermes and must accept a possible duplicate Telegram
notification.

## Verification

```console
PYTHONPATH=pkgs/common/hermes-digest-workflow/src:pkgs/common/hermes-workflow-state/src \
  python3 -m unittest discover -s pkgs/common/hermes-digest-workflow/tests -v
python3 -m py_compile pkgs/common/hermes-digest-workflow/src/hermes_digest_workflow.py
nix build .#nixosConfigurations.<host>.pkgs.hermes-digest-workflow --no-link
```
