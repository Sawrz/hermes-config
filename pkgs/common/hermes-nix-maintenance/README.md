# Nix maintenance runtime

One package owns deterministic documentation gates and container inventory,
curation and issue-intent state. It uses the declared immutable repository cache,
`hermes-workflow-state` generations, native Kanban interfaces and the existing
Forgejo client. It is not a second scheduler, issue router, PR controller or
credential store.

## Documentation audit

The generated no-agent `nix-config-monthly-docs-review-gate` calls:

```sh
hermes-nix-maintenance --instance-config /absolute/instance.json docs-gate \
  --registry "$HERMES_HOME/job-config/repository-sync/registry.json" \
  --state-dir "$HERMES_HOME/state/nix-config-docs-review/commit-audit" \
  --hermes /absolute/path/to/hermes --board <explicit-native-board>
```

The explicit instance contract selects one enrolled repository-sync registry job.
Its repository and full `refs/heads/<default_branch>` ref must agree with the
contract. Consumers require `immutable-reference-cache` mode and reject wrong
origin/ref, dirty or ignored content, shallow history and writable caches. CLI
consumers require the registry; direct checkout evaluation remains a library
interface for isolated tests, not an alternate deployed source selection.

The sole successful baseline is `successful.json: {commit: <SHA>}`. First run
creates a native full-tree semantic audit; later runs cover Git diff/log from the
last successful commit to a fixed target. Missed runs and cadence changes cannot
advance coverage. No relevant change is deterministic and silent. Pending work
lives in the native task, not a second pending-work database.

The task pins the managed `nix-maintenance-protocol` skill. Completion must
carry `metadata.docs_audit={identity,key,base,target,task_id,result:"success",findings:[{task_id}]}` and
explain coverage in its summary. The gate validates the exact scope, successful
terminal run and each finding's durable card before advancing the marker. Partial,
failed, missing or mismatched proof is not success. A moving default-branch commit becomes
the next range, never part of an already-issued audit's completion proof.

`nix-config-docs-inventory --repo <checkout> --json` is an optional mechanical
preflight, not semantic coverage. Wiki publication remains owned by the existing
reviewed default-branch CI path; see `references/monthly-docs-review.md`.

## Container producer and consumer

All three generated container jobs are `noAgent=true`:

1. `container-refresh --registry <registry.json> --feeds <feeds.json>
   --state-dir <snapshot-root>` evaluates effective enabled host/service/component
   image tuples from one immutable Git revision and discovers actual registry or
   GitHub-release updates through reviewed source/tag rules.
2. `container-curation-apply --snapshot-state-dir <snapshot-root>
   --state-dir <intent-root>` requires a successful snapshot, applies the reviewed
   policies and stages a source-bound intent. Mixed-case component names, host
   overrides, exact pins, flavours, exclusions, risk and notes remain intact.
3. `container-request-delivery --state-dir <intent-root>` grants publication for
   the selected digest in that same ledger. No request means no service-side
   publication, even when a curation intent exists.

Generated paths:

- Cache registry: `$HERMES_HOME/job-config/repository-sync/registry.json`
- Complete reviewed feed registry: `$HERMES_HOME/job-config/container-image-maintenance/feeds.json`
- Atomic inventory/report/health generations: `$HERMES_HOME/state/container-image-maintenance/inventory`
- Intent ledger: `$HERMES_HOME/state/container-image-maintenance/update-intent`

The existing `hermes-forgejo-kanban-workflow` service then calls
`container-deliver --require-request --state-dir <intent-root> --endpoint <origin>
--credential-file <its-existing-host-credential>`. The selecting profile must own
both container maintenance and Forgejo-to-Kanban. No terminal credential mount is
assumed by native cron, no secret is copied into job environment, and no LLM bridges
host/terminal credentials. The explicit terminal interface also supports
`--endpoint-file` with the profile-scoped Forgejo service files.

The refresh defaults its report period to the UTC month; `--period YYYY-MM` is an
explicit recovery/test override. This is separate from native scheduler timezone
and must be checked against the desired calendar boundary at cutover.

The inventory envelope is schema 2. The issue intent is independently schema 2:
its immutable payload includes exact inventory/curation inputs, their digests and
candidate release-source/policy facts. The ledger generation protocol remains
schema 1. Readback/recovery retains schema-1 intent support; candidate-less legacy
intents cannot be published blindly. A requested publication records an attempt
immediately before a possible write, finds existing monthly lineage across open
and closed issues, then verifies an authenticated exact report before ACK.
Historical marker/title aliases are recognized without converting them into ACKs.
Human issue-body text is preserved; subsequent reports are appended once as
comments. Closed, duplicate or foreign-owned monthly lineage requires resolution.
Reports are bounded to 64 KiB; oversize reports fail without publication.

The normal journal and native issue router consume the resulting issue and start
semantic planning. These helpers do not create implementation cards, PRs, branches,
labels, deployments or closure. `container-plan` and individual
begin/failure/acknowledgement commands remain explicit lower-level interfaces; the
native jobs use the complete refresh/curation/request/delivery path instead.

Source failures publish error health and exit unsuccessfully; they are never an
empty successful refresh. Unchanged generations, absent intents and acknowledged
work remain silent. Concurrent staging cannot replace unacknowledged work.
Ambiguous writes and lost local ACK recover by exact authenticated readback only;
missing readback never authorizes a blind retry POST.

## Policy, migration and verification

Nix owns executable contracts. The owning profile selects cadence/delivery from
the native contract, including any supported subset or explicit `{}`. This package
does not choose schedules, recipients or a cutover. Preparing valid cache/feed
inputs is distinct from scheduler-registration readiness.

See `references/automation-cutover.md` for the complete historical job and
knowledge-preservation matrix. Preserve mutable worktrees, private policy, support
files, open lineage and old state; do not import historical success as ACK.

The targeted `hermes-forgejo-native-intake` check executes generated Nix container
launchers through pinned native `scheduler.run_job`, the generated service consumer
against isolated HTTPS, and normal native planning routing. It traps provider
resolution/agent construction before semantic work. Installed commands run without
development `PYTHONPATH`. Other tests cover real Git/Nix, registry/release discovery,
source errors, races, response loss, exact readback and cron lifecycle. These are
isolated runtime proofs, not deployment, live scheduler/token or human delivery
verification. Required package checks are separate from lightweight PR CI.

## Configuration, identities and compatibility

Every invocation takes `--instance-config` or the explicit
`HERMES_REPOSITORY_INSTANCE` path. Nix derives this contract from the owning
integration and its existing service/projection settings. It supplies repository,
default branch, native role profiles, board, automation identities, human project,
and endpoint bindings. Credentials are still passed through profile service
references. Generic Questions projection can omit Forgejo completely.

Historical `nix-config-docs-audit` and `system-admin-intents` generation protocol
names and deployed job/state paths are schema identifiers, not runtime repository
or role policy. The instance's namespace, workflow key prefix and optional legacy
container marker explicitly preserve deployed task identities. Do not rename
those values during an upgrade. Different instances use separate state roots,
boards and unique namespaces; one host still has one owning repository workflow.

Existing generations without `instance.json` are rejected unless the owning
integration explicitly pins their complete generation ID in `legacyGenerations`.
The adapter validates the old domain documents (including available source and
receipt evidence), then adds the binding in the same atomic generation without
changing pending work, ACKs or mappings. The coordinator must establish the
repository/service identity of each pin before configuring it; a generation
hash alone does not discover historical endpoint ownership. Empty or malformed
source evidence must not be guessed. A legacy reverse-projection generation
without a stored board/project-bearing mapping or intent is therefore blocked;
the coordinator must reconcile its historical evidence separately, preserving
the old generation. An explicit pin is not a bypass for absent identity evidence. Production migration is a separate action.
Bound states reject any identity/authority mismatch, including attempts to replace
or prune them. Changes of repository, endpoints, roles or write targets require
separate reconciliation; they cannot silently reuse old authority.

The native job names retained from the cutover are historical schedule identities.
No new schedule or second Wiki writer is installed. `nix-config-docs-inventory`
and `nix-config-wiki-sync` retain existing command names for CI compatibility;
the Wiki command requires an explicit repository, source ref and endpoint.

Docs-audit cards and completion metadata carry the same explicit instance/source
identity. Recovery validates the native idempotency key, assignment and executing
profile before advancing the commit marker. Cards from other identified sources
are ignored even with identical Git history. Unbound legacy cards block automatic
recovery and require separately verified adoption; no migration is performed by
this helper. New task keys include a digest of the source identity, while existing
local successful-commit generations are retained.
