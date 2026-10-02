# Hermes correlated verification harness

This directory defines the C04 behavioral evidence contract for Hermes cron and
integration changes. It does not add a scheduler, collector, sender, state
store, or application fork. It checks evidence produced by supported Hermes
CLI interfaces, a disposable provider/delivery boundary, and read-only probes.

## Acceptance boundary

A healthy/no-change run passes only when one correlated, bounded observation
shows all of the following against the exact deployed artifact:

- one successful scheduler execution;
- `wakeAgent=false` and zero changed events;
- zero new agent sessions;
- zero physical model/API attempts and zero successful logical model calls;
- zero input and output token deltas;
- zero delivery attempts;
- unchanged canonical read-only snapshots for Kanban, Forgejo, ntfy, and
  Prometheus;
- unchanged state for at least one unauthorized profile.

Static inspection, a direct gate-script run, `[SILENT]`, an empty response, or a
successful scheduler status alone is not proof. Missing, failed, malformed,
ambiguous, uncorrelated, timed-out, killed, or restart-interrupted evidence is
not a pass.

The positive control must prove that the same path can observe a real changed
fixture: one session, at least one correlated physical model/API request at the
disposable provider fixture, at least one terminal logical success, non-zero
input/output tokens, and one correlated request at a
disposable local delivery sink. This prevents a broken telemetry or delivery
observation path from making every no-change fixture look free.

## Files

- `artifact-inventory.json`: exact Hermes 0.20.0 artifact and native state
  interfaces observed on Alakazam.
- `fixture-catalog.json`: frozen source queries, correlation/window rules, and
  controlled fixture procedures.
- `collect.py`: supported-interface snapshot collector. It invokes `hermes cron
  runs` and `hermes sessions list`, validates the exact disposable provider and
  delivery-sink schemas, and hashes probe/profile trees without following
  symlinks. It never opens Hermes databases directly.
- `contract.json`: machine-readable expected outcomes.
- `verify.py`: fail-closed observation-bundle verifier that derives counters
  from supported-interface before/after snapshots; declared counters cannot
  override those derivations.
- `fixtures/`: synthetic examples that document the bundle shape. They are test
  fixtures, not live evidence.
- `tests/test_verify.py`: positive and adversarial regressions.

## Correlation and stable windows

Use a disposable dedicated profile/HERMES_HOME with no concurrent work. Create
one UUID for the run and carry it in the fixture job name, gate marker, evidence
bundle, and every collector record. Capture all supported Hermes CLI and
external read-only probes immediately before execution. Execute exactly once.
Wait until the cron execution is terminal and the disposable provider/delivery
logs have settled, then capture all sources immediately after and record
`settle_seconds`.

Every newly observed scheduler claim, provider receive/completion, and delivery
receive timestamp must fall inside that one window. Provider and delivery rows
also carry the exact profile, new native session ID, and SHA-256 of the complete
tested artifact identity. New IDs with stale timestamps, unrelated sessions, or
another runtime artifact fail closed.
Normalize the native ledger only in the bundle: `completed` becomes `success`, a
`failed` row with proven timeout evidence becomes `timed_out`, and `unknown`
after owner loss remains fail-closed as `failed` with the native error preserved.

Session identity comes from the pinned artifact's public
`hermes sessions list --source cron` interface. Scheduler identity and terminal
state come from `hermes cron runs <job-id>`. The attempt source is the disposable
OpenAI-compatible provider fixture's
append-only request log. It records the exact correlation, job, profile, session,
and runtime-artifact identifiers
before producing a response, so interrupted and retried physical requests are
still visible. Completed provider events also record outcome and input/output
usage from the controlled response. The positive control additionally requires
a completed native cron execution and a correlated delivery-sink request, so a
provider receipt alone cannot masquerade as end-to-end success. No private
Hermes SQLite schema is part of this contract.

## External mutation probes

Each workflow must define canonical, read-only before/after snapshots for the
write targets it could reach. At minimum the controlled evidence bundle records
Kanban, Forgejo, ntfy, and Prometheus probe digests. Use supported native
Kanban tools/CLI and service APIs; never open the Kanban SQLite database from a
collector. Normalize volatile response fields before hashing and record the
exact query, target, and source-native cursor/ID in the durable evidence.

The repository fixtures use placeholder digests only to test validation. They
are not acceptable as operational evidence.

## Profile isolation

The fixture profile is authorized only for its own config, state, telemetry,
and explicitly configured read-only probes. Snapshot at least one unrelated
profile's relevant state before and after. Any changed digest, missing snapshot,
or broadened credential/mount visibility fails the run.

## Controlled live procedure

1. Confirm the exact package, source revision, config digest, gate-script digest,
   Nix generation, profile, and job ID.
2. Materialize canonical read-only API/CLI snapshots for Kanban, Forgejo, ntfy,
   and Prometheus. Do not let those snapshot commands mutate their sources.
3. Capture `before` with `collect.py`, passing the snapshots as
   `--profile PROFILE`,
   `--external-probe NAME=PATH`, every unauthorized profile tree as
   `--isolation-profile NAME=PATH`, the provider request JSONL as
   `--model-log PATH`, and the disposable delivery-sink JSONL as
   `--delivery-log PATH`.
4. Trigger exactly one fixture execution through the native scheduler.
5. Wait for its terminal public cron-history row, provider/delivery settlement,
   and the configured settle period.
6. Refresh the same read-only API/CLI snapshots, capture `after` with identical
   collector arguments, and preserve the exact gate result.
7. Assemble one observation bundle and run `verify.py`. Any failed, ambiguous,
   missing, timed-out, killed, or non-exclusive observation is NOT PASS.

## Running the checks

```bash
python3 -m unittest discover \
  modules/services/nixos/agents/verification-harness/tests -v

python3 modules/services/nixos/agents/verification-harness/verify.py \
  modules/services/nixos/agents/verification-harness/fixtures/healthy-no-change.json
```

A real run uses the same verifier against a captured observation bundle. Preserve
the bundle and its SHA-256 digest with the exact revision under review. Do not
replace live evidence with the synthetic fixtures.
