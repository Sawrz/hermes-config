# Read-only concept-conformance audit

Use this when a PR must be judged against an accepted concept, plan, amendments, and explicit ownership clarifications rather than merely reviewed for local code quality.

## Evidence order

1. Read the normative sources before code. Later amendments and explicit audit clarifications override older plan text only within their stated scope.
2. If a supplied accepted-source file is only a pointer, excerpt, or suspiciously short copy, locate the complete accepted artifact before deriving requirements. Record which copy supplied the complete text.
3. Freeze repository identity and the effective artifact before review: branch, HEAD, merge base, staged/unstaged status, changed-file inventories, and a digest or durable copy of the staged plus unstaged diff.
4. Pause implementation edits for the audit or give the reviewer an immutable worktree/snapshot. A moving dirty tree invalidates line evidence.
5. Review both `merge-base..HEAD` and every staged/unstaged change. Re-check status and the frozen digest at the end to prove the reviewer modified nothing and reviewed the intended artifact.
6. Do not treat prior reviews as authority. They may provide reproduction leads, but every reported finding needs current source and exact line evidence.
7. Treat delegated reviewer output as a self-report. Before action, the parent independently spot-checks every blocking finding's decisive citation against the frozen effective tree.

## Late concurrent-mutation recovery

A clean kickoff does not guarantee a clean finish. Re-run `git status --short` before publishing. If files changed during the audit:

1. Record the dirty file list and a digest of the uncommitted diff; do not reset, stash, or treat it as part of the requested committed head.
2. Re-read every decisive citation from immutable objects (`git show <head>:<path>`), not ordinary worktree files.
3. Re-run any test or reproduction claimed as exact-head evidence from a disposable `git archive <head>` extraction, with bytecode/build outputs disabled or confined outside the repository. Tests run before the mutation was noticed are temporally ambiguous and must not be represented as exact-head proof without this rerun.
4. Remove the disposable extraction afterward, report that the worktree changed concurrently, and state explicitly that the dirty diff was excluded from the verdict.
5. If the concurrent diff appears to address a finding, do not downgrade the exact-head finding. Mention the dirty correction only when the requested scope includes effective-tree classification.

When a focused probe imports modules, import them from the immutable extraction too, so sibling imports cannot resolve to dirty worktree code.

## Finding-state matrix for dirty worktrees

For every candidate finding, classify all three relevant views:

| View | Question | Allowed label |
|---|---|---|
| Merge base | Was this behavior already outside the PR? | context only |
| Committed PR head | Does `HEAD` contain the divergence? | `present at HEAD` |
| Effective worktree | After staged and unstaged changes, does it still exist? | `introduced by dirty diff`, `fixed by dirty diff`, or `still present in effective tree` |

Never quote a committed-head option, assertion, service, or test as evidence about the current implementation after the dirty diff removes it. Conversely, removing one layer does not clear the feature class if executable code, checks, docs, secrets, contracts, or evidence dependencies remain elsewhere.

Before publishing:

1. Re-read each blocking citation from the effective tree, not from `git show HEAD` alone.
2. Search the dirty diff for additions/removals of every cited symbol.
3. Run a contradiction pass: finding prose, state label, line evidence, and runtime consequence must describe the same artifact state.
4. Narrow or withdraw any subclaim whose cited symbol is absent. Preserve only independently evidenced residue.
5. Report uncertainty explicitly when line numbers differ between HEAD and the dirty tree.

## Traceability and runtime materialization

Build two separate matrices:

- **Schedule ownership:** classify every cadence as profile-owned native Hermes cron, justified deterministic systemd collector/reconciler/watchdog, or improper duplicate/central scheduling.
- **Runtime activation:** distinguish package defined in an overlay, package present in a closure, option selected, service/timer instantiated, native job reconciled, and live behavior verified.

A JSON manifest, schedule template, capability artifact string, package check, or test fixture is not runtime activation. Search for the consumer that materializes the declaration through the supported owner interface. Absence of such a consumer is evidence of missing wiring, not permission to invent a cadence.

For each retirement replacement, trace:

`retired trigger/interface -> replacement package -> enabled closure -> runtime instance -> supported owner readback -> exact evidence gate`.

If any edge is missing, the retirement claim is not truthful enough for a hard cut.

## Pinned owner-interface parity

When repository code validates or adapts a supported native CLI, audit the local contract against the **exact locked dependency**, not a remembered or generic grammar. Compare both directions:

- values accepted locally but rejected by the native parser;
- values accepted natively but rejected locally;
- semantic normalization differences, not merely whether parsing succeeds;
- supported field counts, units, aliases, token classes, output envelopes, and verb-specific flags.

Schedule validators are especially prone to becoming a second authority: a convenience regex may admit unsupported interval units or token forms while rejecting a valid field count. Cite the lockfile revision and the local validator's exact lines. Prefer a native validation command when available; otherwise require parser-parity tests tied to the lock revision. Distinguish this committed-source divergence from the separate deployment gate of whether private runtime policy has been populated.

## Amendment residue inventory

When an amendment removes a feature class, search all changed scope for:

- runtime code and commands;
- Nix options, assertions, imports, services, timers, and secrets;
- host configuration;
- tests and evaluation fixtures;
- docs and runbooks;
- ownership/inventory/retirement contracts;
- evidence fixtures and deployment preconditions.

Separate unrelated pre-existing uses of the same generic word from migration-specific residue. A documentation sentence saying a feature is inapplicable does not cure live code or checks that still require it.

## Audit corrective working-tree changes independently

Do not assume dirty changes are fixes. For each one, classify it as:

- fixes the earlier defect without new divergence;
- incomplete (root cause remains);
- introduces a new protocol/ownership failure;
- exposes an existing evaluation/runtime contradiction.

Useful cross-checks:

- A lease-expiry fence must not consume or acknowledge reclaimable work.
- A dynamic first-poll timestamp must survive failed initialization without skipping events.
- A guard around partial full-replacement API writes is not equivalent to PATCH or full-model merge.
- Adding error propagation does not repair an invalid credential header or unwritable runtime path.

## Evidence-honesty probes

Treat operator-authored snapshots as claims, not observations. A deployment verifier is false-green if it accepts strings such as `active`, generation IDs, package names, proof names, or digests without deriving or binding them to supported runtime readback and the exact deployment closure.

Check that:

- host generation/state comes from observed owner interfaces;
- native cron existence comes from supported Hermes CLI output;
- package proof is tied to the enabled closure, not overlay availability;
- retired-interface absence comes from a real scan;
- event timestamps, not only snapshot capture times, fall inside the evidence window;
- external mutation probes identify the actual authority and query, not arbitrary local files.

## Finding format

For every divergence include:

- stable ID and severity;
- committed-HEAD versus uncommitted status;
- exact normative citation;
- exact `file:line` evidence;
- observed behavior;
- why it diverges;
- runtime consequence;
- minimal correction direction only.

Missing required runtime materialization is Blocking/Important according to impact, never softened merely because a package or template exists. End with `Files modified: none`, read-only evidence used, and explicit unknown live boundaries. Do not run artifact-producing tests when the audit contract forbids repository outputs.
