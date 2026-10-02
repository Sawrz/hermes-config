# Managed Repository Synchronization

Use this pattern when several agent profiles need repositories refreshed without copied profile-specific cron prompts or model calls.

## Ownership split

### Declarative/review layer

Owns:

- generic clone/fetch/materialization implementation;
- authorized repository origin, repository identity, ref, destination role/path, and credential;
- script-only scheduler template, locks, timeouts, retries, hard frequency bounds, metrics, and failure routing;
- host-key verification and profile-scoped credential materialization;
- safe first-clone and update semantics;
- tests and migration from legacy pull jobs.

A mutable repository name is not authorization. Runtime configuration may select only from declaratively authorized repositories and destinations. Never accept arbitrary URLs, shell commands, refs, paths, hooks, or credential choices from mutable policy.

### Dynamic profile/human policy

May control, within bounds:

- enabled/disabled state;
- cadence;
- selection among already authorized repositories;
- optional analysis wake policy after a proven content change.

Adding a new origin/repository/ref/destination or widening credentials remains reviewed configuration.

## Deterministic runtime contract

Repository synchronization is infrastructure. Use a script-only job or another managed deterministic service; do not instantiate an agent merely to clone, fetch, or report no change.

No-op contract:

```text
not due
or disabled
or unauthorized
or already at selected revision
  -> no Hermes session
  -> no model call
  -> no token use
  -> no success notification
```

One repository failure must not replay or block successful resources. Maintain per-repository locks, due state, revision/status, retry/backoff, and error classification.

## Safe first clone

Automatic clone-on-enrollment is acceptable when enrollment itself is the reviewed authorization:

1. Validate logical repository name against the declarative allowlist.
2. Validate destination is inside the managed root and absent.
3. Clone into a temporary sibling with bounded time and expected credentials.
4. Resolve and verify expected origin, ref, and checked-out revision.
5. Atomically promote to the managed destination.
6. Persist status only after promotion proof.

Do not clone into an existing non-empty path. An explicit bootstrap command is useful only when first materialization has different authority or side effects than normal enrolled synchronization.

## Safe subsequent update

For an existing destination:

1. Lock the repository.
2. Verify it is the expected repository and path.
3. Verify ref/branch policy and current worktree state.
4. Fetch with bounded timeout/retry.
5. Determine whether the selected ref advanced.
6. Advance only by a verified fast-forward when the worktree contract allows it.
7. Read back origin/ref/revision and release the lock.

Never perform an unqualified blind `git pull`, automatic merge, destructive reset, clean, overwrite, or checkout deletion.

Dirty, detached, mismatched, unrelated, or diverged state fails closed with actionable status. Removing a repository from configuration disables synchronization; it does not delete the checkout.

## Writable versus read-only consumers

Choose explicitly:

- **read-only reference consumer:** prefer a managed cache plus atomic materialized snapshot or read-only checkout;
- **writable work repository:** use locking and clean fast-forward-only updates, and fail rather than overwrite local work.

Do not let a background synchronizer and an editing agent race over one worktree without a shared lock and documented writer contract.

## Credential and observability rules

- Read credentials at command time from profile-scoped runtime files.
- Do not place secrets in URLs, arguments, logs, status files, Nix store paths, or metrics.
- Report authentication, authorization, connectivity, ref-not-found, dirty/diverged, timeout, and local-path mismatch separately.
- Successful clone/update/no-change stays quiet.
- Export bounded status/metrics; route actionable failures through the accepted operational incident authority rather than creating ad-hoc Kanban work from the sync script.

## Migration and acceptance

Keep legacy pull jobs until the generic replacement proves:

- absent-path clone and atomic promotion;
- no-change silence with zero sessions/tokens;
- clean fast-forward update;
- dirty/diverged/mismatched fail-closed behavior;
- concurrency locking;
- timeout/retry and restart recovery;
- profile/repository authorization isolation;
- credential non-disclosure;
- scheduler persistence across rebuild/redeploy.

After cutover, remove copied job prompts/scripts only through reviewed migration. Preserve repository data; retirement is not deletion.
