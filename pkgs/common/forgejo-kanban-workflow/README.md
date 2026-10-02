# Forgejo–Kanban workflow boundary

This F11 package consumes the F10 immutable Forgejo journal and projects supported work into native Hermes Kanban. It does not read Kanban SQLite, create a second dispatcher, call a model, project to Vikunja, or infer merge/deploy authority.

## Identity and authority

Three identities remain separate:

- source event: the F10 `fj-<sha256>` event ID and source key;
- logical workflow: `forgejo:nixos/nix-config:<issue|pull_request>:<number>`;
- execution epoch: the source event that creates a continuation after a completed workflow.

The first task uses the logical identity as native `--idempotency-key`. A reopening after completion uses `<logical>:epoch:<event-id>`. The crash-safe mapping is a validated cache of the native task IDs; `hermes kanban --board ... show ... --json` is read back before every update.

Forgejo remains technical authority. Kanban remains execution authority. Evidence comments explicitly do not grant merge, deploy, rebuild, verification, closure, or human authorization.

## Deterministic gate

`forgejo-kanban-workflow reconcile`:

1. reads the bounded F10 pending envelope;
2. refetches the exact Forgejo target;
3. for PRs, captures one head, lists every formal review, fetches each review's inline comments, reads exact-head statuses, and confirms the head did not move;
4. performs no Kanban operation for evidence without an existing logical route;
5. creates actionable work with native board-scoped idempotency, or appends one marker-bound evidence comment to the mapped task;
6. reopens blocked/review work only for actionable events;
7. acknowledges F10 only after native readback and durable mapping publication.

Healthy empty and evidence-only/no-route runs make no model calls, create no agent attempts, and perform no Kanban mutation.

## Guarded mutations

`reply` validates the exact issue/PR before posting, requires the exact source-event marker in a bounded body, then lists comments and verifies the returned ID, exact body, and canonical URL.

`submit-review` only publishes an existing `PENDING` review on the current exact head. It refetches the PR, all formal reviews, every review's inline comments, and statuses before and after publication. A head change fails closed.

Both commands are capabilities for an authorized profile. The timer does not call them automatically.

## Native CLI contract

The adapter invokes the configured Nix store Hermes executable directly, never through a shell. It always supplies `--board`, uses `create --json` and `show --json`, preserves the full show envelope, and reconciles response loss through native idempotency plus authenticated exact-body readback. Because the pinned Hermes idempotency lookup and insert are not one transaction, one process lock serializes F11's lookup/create/map/ack boundary; F11 is the sole creator for this keyspace. The systemd service receives the default dispatcher's Hermes home; model workers continue to use injected `kanban_*` tools rather than shelling out.

## Verification

```console
nix build .#checks.x86_64-linux.hermes-forgejo-kanban-workflow --no-link
nix build .#checks.x86_64-linux.hermes-forgejo-kanban-eval --no-link
```

Fixtures cover issue/PR/comment/review/inline/status/head-change/replay/continuation, exact-target replies, pending-review publication, restart/concurrency, profile package closure, and evidence-only zero-wake behavior.

### Human-wait lifecycle boundary

```console
nix build .#checks.x86_64-linux.hermes-forgejo-native-intake --no-link
nix build .#checks.x86_64-linux.hermes-foundation-skills --no-link
```

`tests/integration/test_native_human_wait.py` runs the unchanged pinned Hermes
DB lifecycle and gateway dispatcher against disposable data, including fresh
processes over the same DB. Only auxiliary inference and worker process creation
are fixtures; no real model/worker or production board is used.

- A genuine contract prerequisite prevents consumer dispatch while unresolved.
  After explicit resolution and completion, the implementation becomes claimable;
  its first unexpected human question remains blocked across watcher restarts.
- On the same execution card, unblock preserves recurrence history. A second,
  different `needs_input` question becomes `triage`; a valid single-task auxiliary
  response then permits automatic dispatch without a second human answer or
  child creation. This is a **known limitation characterization**, not a passing
  loop-prevention acceptance test. A future native behavior change must trigger
  re-evaluation of this test and the published guidance, not restoration of the bug.

The foundation test checks instruction publication only. Neither green test
suite proves model compliance or safe arbitrary repeated human waits. Correct
planning, native dependencies and status readback reduce avoidable ambiguity;
they do not enforce containment against the dispatcher. Do not deploy or label
these guidance changes as a complete restart-loop fix. Runtime settings and any
operational containment remain separately authorized.
