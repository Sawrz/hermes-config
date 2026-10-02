# Kanban–Vikunja human-action projection

This package implements two deterministic, independently stateful edges:

- native Hermes Kanban → Vikunja human-action tasks;
- Vikunja comments/completion → native Kanban evidence comments.

It consumes the C03 immutable-generation state library and the F11 native
Kanban CLI contract. It does not read Kanban SQLite, run a model, use Hermes
cron as transport, or create a direct Forgejo→Vikunja authority path.

## Authority and task model

Project `110 — PRs` is permanently selected by explicit include mode and
permanently uses `linkedOnly`. Only native Kanban tasks with the complete
contract below project:

```text
Human action: Questions|Merge
Vikunja project: 110
Vikunja assignee: sandro|none
Vikunja done: true|false
Vikunja automation reference: nix-config-issue-<N>-questions|nix-config-pr-<N>-merge
```

The Vikunja description contains exactly one stable reciprocal reference and
one deterministic marker. The integration ledger remains the machine mapping;
comments are human-visible evidence, not a shadow metadata database. Unlinked
project-110 tasks remain silent, including tasks with only an automation marker
from another integration. An explicit `Hermes Kanban:` reference selects a task
for strict validation; an existing ledger mapping also keeps a task selected if
its reference is removed. Neither a generic marker nor this selection rule can
adopt an unmapped task or bypass reciprocal identity checks.

Vikunja completion is projected only as evidence. The Vikunja→Kanban adapter
has no merge, deploy, rebuild, rollback, verify, close, or Kanban-complete
operation. A human comment may unblock an already blocked mapped card when the
edge explicitly enables that behavior.

## Runtime

The NixOS module creates separate oneshot services/timers and separate state
subdirectories for both directions. Every actionable run validates the exact v2
OpenAPI operations before its first write; healthy no-change runs avoid that
large schema transfer. Every write path then:

1. validates project identity, title, owner, selectors, mapping, and assignee;
2. persists pending intent before an external mutation;
3. reconciles ambiguous writes by stable marker and exact readback;
4. commits acknowledgement only after both target mutation and reciprocal
   evidence read back;
5. stays silent on a healthy no-op.

The existing system-admin Vikunja credential is read from its file at command
time. `code-reviewer` receives no direct Vikunja credential or integration
unit.

## Lifecycle updates and recovery

Mapped identity is checked before a change; the new human-action state is
checked after the write. Review readiness, assignment clearing, rebuild,
verification and completion update the same task. A human checkbox does not
change source authority: a prematurely completed projection is reopened.

Mapped updates can be retried before or after a lost response. Unmapped creates
still require the exact committed create payload for recovery. A missing mapped
task or a marker moved to another task is a conflict, not permission to create a
replacement. A failed pending operation cannot be overwritten by a newer source.

Task descriptions use the rich-text-aware API-v2 `PUT ...?format=markdown`, not
generic PATCH. Read/retain the non-owned writable fields before PUT so a lifecycle
update does not erase dates, priority, repeat settings or other task metadata.
Assignees remain a separate explicit bulk replacement with verified readback.

These rules repair the projection transport. They do not, by themselves, derive
PR readiness from Forgejo or make an immutable opening card body advance across
PR phases. That source-lifecycle integration needs its own acceptance evidence.

## Verification

```sh
PYTHONPATH=pkgs/common/kanban-vikunja-projection/src:pkgs/common/hermes-workflow-state/src \
  python3 -m unittest discover -s pkgs/common/kanban-vikunja-projection/tests -v
```

The suite covers selector failures, project-110 policy, stable references,
create/readback, response loss, marker recovery, comment edits, duplicates,
echo suppression, completion evidence, owner/assignee denial, restart,
concurrency, failed-edge-only retry, pagination, redirect denial, actionable
positive control, and zero-mutation no-op.
