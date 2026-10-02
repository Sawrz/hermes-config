# Hermes native cron reconciler

This helper applies Nix-owned job definitions through the supported profile-local
Hermes cron CLI/API. Hermes remains the scheduler, execution ledger and delivery
owner. No private scheduler storage is edited, and reminders never call an LLM.

## One policy per profile, one choice per job

Nix publishes a schema-1 `native-cron-contract.json` with declared job names,
launchers, skills, modes and delivery constraints. The profile's agent and user
own mutable preferences. Personal and family profiles each have one policy;
chat access does not grant scheduling ownership or choose a destination.

```text
native-cron-policy.json       # agent-managed choices, mode 0600, schema 2
native-cron-policy.json.lock  # stable cooperative flock, mode 0600; never unlink
native-cron-status.json       # reconciler readiness, mode 0600
job-config/                  # agent-managed workflow preferences
state/                       # workflow processing state, never reset by bootstrap
scripts/profile/.nix-managed/ # Nix-published regular script files
```

A fresh profile starts with every declared entry `unconfigured`:

```json
{
  "schema_version": 2,
  "jobs": {
    "example-job": {"status": "unconfigured"}
  }
}
```

Each entry has one status:

- `unconfigured`: inactive; setup reminders name this job.
- `disabled`: inactive; no setup reminders for this job. Existing preferences may
  be retained for later re-enablement.
- `enabled`: requires valid `schedule` and `deliver`. Invalid enabled entries stay
  unchanged in the policy, remain inactive in the scheduler, and report an error.
  Independently valid enabled entries still run.

Use exact names from the contract. Schedules must recur (`every <duration>` or
cron expressions); this policy intentionally requires `every` for intervals,
so bare durations such as `4h` remain outside its accepted grammar. Local-only
jobs require `deliver: "local"`; Telegram jobs require an explicitly chosen
`telegram:<chat-id>[:<topic-id>]`. Never infer a recipient from chat access.
Enabled entries contain `status`, `schedule`, and `deliver`; inactive entries may
retain those preferences. Unknown fields/statuses are configuration errors.

There is no global policy mute. Missing entries reset only those jobs to
`unconfigured`. Deleting the full policy resets all currently declared jobs.
Bootstrap pauses their managed records in place, preserving scheduler identity,
run history, workflow configuration, and processing state. Re-enabling a paused
job reuses its record. Bootstrap never chooses schedules or destinations.

Removing a declaration removes only scheduler records positively owned by the
exact Nix-managed launcher directory. The corresponding policy entry is retained
as dormant, ignored for execution/reminders/validation until declared again.
Unrelated/manual scheduler jobs are untouched, including same-named jobs outside
the managed launcher tree. An enabled name collision fails rather than adopting
an unrelated record. Removing the final declaration still retires managed jobs.

## Bootstrap, updates and concurrency

Gateway startup and the existing policy path watcher run the same reconciler.
Bootstrap atomically creates a missing policy as a complete private file using
no-clobber publication, or adds missing declared entries to a valid schema-2
policy. Repeated no-change runs do not rewrite the policy. Malformed, unsafe,
symlinked or otherwise invalid policies are diagnosed, never replaced as missing.
If a declared entry is invalid, missing-entry insertion waits for its repair;
existing valid jobs can still reconcile and missing jobs remain inactive.

All cooperating writers, including the agent, must use the stable sidecar lock:

1. Open `native-cron-policy.json.lock` with mode 0600, refusing symlinks. Never
   delete or replace it; locking the JSON inode itself is unsafe across rename.
2. Acquire an exclusive `flock` before reading the latest policy.
3. Change only the authorized entries, preserving other and dormant choices.
4. Write a same-directory 0600 temporary file, flush and fsync, then atomically
   replace the policy; fsync the directory for durable publication.
5. Release the lock. Do not run reconciliation while holding it yourself.

The reconciler holds the same lock across bootstrap/migration, native scheduler
changes, readback and status publication. Read-only checks/reminders use a shared
lock when present and do not create files. An uncooperative writer that ignores
the lock is outside this supported concurrency contract; atomic rename alone
cannot serialize a read-modify-write. The generated agent prompt explains this
protocol. No second scheduler, daemon, cadence or delivery owner is introduced.

`hermes-native-cron-<profile>.path` watches creation, deletion and atomic replacement
of the policy and applies changes without restarting the agent. Selected jobs are
created/edited/resumed; inactive declared jobs are paused rather than removed.
Readback checks schedules, destinations, launchers, skills, modes and absence of
active inactive-policy records. Full native API correction also clears obsolete
prompt/toolset/workdir/context/model fields while preserving runtime history.

## Migration from schema 1

The mutation path converts a valid old policy once to schema 2:

- Configured entries become `enabled`, with exact schedule/delivery preferences.
- Omitted jobs in the current contract become `disabled`, preserving old opt-outs.
- Legacy `{}` and schema-1 empty `jobs` disable only the current declared set.
- Future declarations receive `unconfigured`; there is no lasting global opt-out.
- Dormant legacy choices are preserved. Invalid old files remain unchanged.

Only bootstrap/reconciliation converts the format; validation, reminders and
`--check` require schema 2. This is a conversion boundary, not a second runtime
policy engine. Agents must write schema 2, not manufacture new legacy policies.

## Readiness and failure behavior

Status is `needs-configuration`, `ready`, `disabled`, or `error`. A pending job
can coexist with active valid enabled jobs. `needs-configuration` names pending
jobs and does not mean every job is inactive. A successful status is fingerprinted
against the complete contract and policy. Check both status and native cron
readback before reporting activation; scheduling readiness does not prove that a
workflow's own private inputs or downstream integrations are ready.

`--check --print-deliver <job>` verifies native records without mutation and prints
the selected job's validated delivery route (used by the digest gate). It rejects
invalid policy, absent policy and scheduler drift; unconfigured siblings do not
prevent checking valid enabled jobs.

On invalid policy, affected records are paused and read back before reporting a
policy error. `--bootstrap` records `error` but permits the interactive gateway to
start for repair. A scheduler/list/pause/readback failure remains a hard error;
it is never downgraded to a configuration-needed state. Ordinary reconciliation
returns nonzero on policy errors. Operational state and history are not reset.

## Daily setup reminders

The existing `jobSetupReminders` timer/sender remains responsible for delivery.
Each profile's explicit `jobSetupReminders.targets` receives the same bounded list
of currently declared `unconfigured` jobs and a suggested setup prompt. Enabled
(including invalid enabled), disabled and dormant entries are not setup-reminder
subjects. Readiness failure is not a reason to relabel a job unconfigured.
Malformed/missing pre-bootstrap policy reports a local error without publishing
private diagnostics or inventing a job list. Empty declared sets are silent.

Targets are deduplicated; delivery failure at one target does not skip others.
Checks are deterministic, do not wake a model, and never mutate policy, status or
jobs. Existing daily cadence and bounded random delay are unchanged.

## Previous launcher layout

The existing bounded retirement also removes records in the previous exact
Nix-owned `scripts/job-users/<id>/.nix-managed` and `scripts/.nix-managed` layouts
through the native CLI. It preserves old preferences, state and script files;
per-user settings are never automatically merged into profile preferences.

## Verification

```console
nix build .#checks.x86_64-linux.hermes-native-cron-reconciler --no-link
nix build .#checks.x86_64-linux.hermes-native-cron-bootstrap --no-link
nix build .#checks.x86_64-linux.hermes-native-cron-reminders --no-link
```

The bootstrap check exercises the pinned native Hermes CLI/API in disposable
profile homes as well as policy/reset/migration/concurrency/reminder tests and
generated prompts for sibling profiles. It never runs a production scheduler,
model, messaging delivery or live policy change.
