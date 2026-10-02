# hermes-repository-sync

Deterministic, fail-closed synchronization for explicitly enrolled repository jobs:
`cv-repo-pull`, `obsidian-main-vault-pull`, `nix-config-pull`, `omanix-pull`,
`nix-containers-pull`, `family-controls-pull`, and `hermes-config-pull`.

## Boundary decision

This package deliberately supports one materialization mode:
`immutable-reference-cache`. It is a normal Git checkout made read-only after
validation and managed as a replaceable remote-derived materialization. A run
proceeds only when the checkout has the exact declared origin and branch, has no
tracked, untracked, or ignored local data, and its HEAD is an ancestor of the
declared remote ref.

Writable or human-authored checkouts are deliberately unsupported. They have
concurrent-write and preservation semantics incompatible with atomic replacement.
Consumers may read this cache, but local edits, ignored files, and untracked data
do not belong in it.

## State machine

- Absent destination: clone to a temporary sibling, validate the complete
  checkout, then rename it into place.
- Current destination: compare the exact remote ref and return success with
  zero output.
- Remote advance: clone and validate a complete sibling, prove the old HEAD is
  an ancestor, fsync the complete candidate, revalidate the old checkout,
  atomically exchange the two directories with Linux
  `renameat2(RENAME_EXCHANGE)`, and fsync the publication parent before success.
- A write detected during the exchange window triggers a second atomic exchange
  that restores the original cache with the unexpected data intact.
- Wrong origin/ref, detached HEAD, dirty/untracked/ignored content, divergence,
  unsafe path, lock contention, timeout, authentication, or host-key failure:
  fail non-zero without changing the destination checkout.

The helper never calls `git pull`, `git merge`, `git reset`, or `git clean`.
It never writes a branch ref in place. Atomic replacement means interruption
before publication leaves the old checkout, and interruption after publication
leaves the complete new checkout. A fixed per-job staging directory bounds crash
residue to one path; its presence blocks later runs for operator inspection and
is never auto-deleted. The helper removes only staging it created during its
current run; unenrollment has no delete path and never removes the destination.

## Declarative contract

The registry schema fixes:

- one of the allowlisted job identities;
- canonical `ssh://git@host/owner/repository.git` origin;
- matching `owner/repository` identity;
- exact `refs/heads/...` ref;
- absolute destination;
- SSH host and port, pinned `known_hosts` file, and private identity file;
- timeout, retry, and lock bounds.

Unknown fields, unknown jobs, non-SSH production transport, encoded/query
origins, symlink paths, loose identity modes, and unsupported materialization
modes fail closed. The `local-test` transport exists only behind the in-process
test gate and is not accepted by the installed CLI.

`schedule-templates.json` provides the native Hermes script-only safety shapes for
all allowlisted jobs. It intentionally
contains no schedule. The owning profile supplies its cadence when it materializes
the job through the supported Hermes cron interface.

All jobs use `no_agent=true`, no skills, and no toolsets. The profile owns roots,
actual repository/destination values, credential mounts, state paths, and final
native cron registration. Removing a root or job registration does not invoke
this helper and therefore cannot delete a checkout.

## Nix-owned profile inventory

Select jobs through `services.hermes-agent.profiles.<profile>.serviceIntegrations.repository-sync.instances`.
Optional `repository-sync.settings` declares `sshHost`, `sshPort` (default 22),
`knownHosts` (pinned OpenSSH host-key lines), and `repositories` keyed by the exact
selected job names. Each entry requires `origin`, `repository`, and `ref`.
The profile must already have its own SSH identity; no new credential is generated.
A declared inventory must cover exactly the selected repository jobs. Unknown
settings, missing pins, missing identity, or mismatched canonical origins fail evaluation.

Nix renders the registry and pins in the store, and publishes a managed link at
`$HERMES_HOME/job-config/repository-sync/registry.json` during profile activation.
Unknown/manual files at that location are never overwritten. Removing the declared
inventory removes only this profile's owned registry link. It does not delete
checkouts; native scheduler reconciliation separately removes deselected owned jobs.
Profiles without a declarative inventory retain the existing private registry contract.

On Alakazam, `system-admin` selects `nixos/nix-config` at `refs/heads/master` and
`nixos/omanix`, `nixos/nix-containers`, `nixos/family-controls`, and
`nixos/hermes-config` at `refs/heads/main`, all over the declared Forgejo SSH URLs.
Caches appear inside that profile's sandbox as `/workspace/repositories/<repo>`.
They deliberately do not adopt `/workspace/nix-config` or its implementation
worktrees: those are writable, human/agent-owned data, not replaceable caches.
The first scheduled execution clones an absent cache; subsequent executions are
silent no-ops or validated atomic fast-forward replacements. Implementation work
belongs in separate writable clones/worktrees, never in these reference caches.

**Deployment is not scheduling.** This declaration provides the immutable contracts
and launchers only. Cadence remains in the profile-owned `native-cron-policy.json`.
Do not invent a schedule, resume a paused historical job, or manually clone caches
as a substitute for the declarative change. After merge/deployment, select the jobs
with an authorized recurring schedule and `deliver: local`; require native cron
readback and a real execution before reporting automatic synchronization active.

## Observability

Success and no-change write only bounded Prometheus textfile metrics under the
declared state directory and emit zero stdout/stderr. Failures return non-zero,
emit one redacted JSON diagnostic from the CLI, and record a bounded failure
class. Metrics never label repository origins or destination paths.

## Verification

```console
python3 -m unittest discover -s pkgs/common/hermes-repository-sync/tests -v
nix build .#hermes-repository-sync --no-link
```

The fixture suite exercises absent clone, no-change, remote advance, dirty,
untracked, ignored, detached, wrong origin/ref, divergence, interruption,
locking, symlink escape, failed clone, metrics, schedule shape, mode separation,
and unenrollment retention.
