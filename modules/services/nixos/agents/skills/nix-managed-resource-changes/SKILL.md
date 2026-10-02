---
name: nix-managed-resource-changes
description: Use before changing skills or native jobs. Check Nix ownership and route source changes.
metadata:
  version: "1.1.0"
  hermes:
    tags: [skills, cron, nix, change-control]
---

# Nix-managed resource changes

Before editing, extending, replacing, deleting, or saving a lesson into a skill,
or changing a native job's immutable launcher/contract, check this profile's
inventory. This skill is installed automatically when ordinary managed skills
or selected native jobs exist. It is guidance, not a security/compliance boundary.

## Discover ownership

Nix owns the declared source, definitions, packages and wiring. An agent owns only
runtime preferences or state explicitly delegated by the existing resource contract
and user authority. Discovering a resource does not transfer ownership or authorize
a change. This applies generally; the inventory currently identifies skills and
native jobs. An unlisted resource is not automatically mutable.

1. Read `$HERMES_HOME/managed-resources.json`, or load this skill's linked
   `references/managed-resources.json` with native `skill_view`. The schema-version-2
   inventory covers `skills` and `native_jobs`. Both entry
   points contain the same generated data; never borrow another profile's inventory.
2. Verify the manifest profile matches the active profile. Inspect the exact skill
   name (strip a category only after native discovery confirms it), or the exact
   native job name from the profile's generated native-cron contract.
3. Listed resources are Nix-managed. Never edit their delivered files, mutate a
   managed skill with `skill_manage`, create a local shadow, or rewrite/delete the
   managed scheduler entry to override its immutable contract. Change canonical
   source through a repository PR.
4. An unlisted resource is not owned by this inventory. Check native discovery
   and provenance before treating it as genuinely local and mutable. Missing or
   malformed inventory is not evidence of local ownership: stop and report it.

`source.kind=repository-directory` names a repository-relative skill directory.
`inline` names effective declaration files rather than a source directory.
`external` records a packaged input for diagnosis, NOT an editable local path.
`native-job` names the provider and effective launcher declaration files; request
changes to that canonical provider. Store paths are never editable source locations.

## Preserve profile-owned policy

For example, a native job's package-owned launcher and immutable contract go
through a source PR; runtime preferences/state explicitly delegated by its contract
stay with the existing profile workflow and user authority. Consult that contract
rather than inventing setting-by-setting ownership rules. Local resources remain
locally maintainable only after their provenance and authority are confirmed.
This inventory neither enables jobs nor grants mutation authority.

## Route one self-contained request

Read `route.mode`, `route.board`, `route.assignee`, and `route.repository` from the inventory.
An absent mode means `kanban` for older inventories. With `mode=manual`, prepare
the request with the helper and return its `handoff` body to the current caller
(the human or current Paperclip task). Do not create a Kanban card or claim that
the recipient has been notified. The board is context only in this mode. A caller
already assigned the source change may implement it under repository policy.
Use the Kanban steps below only in `mode=kanban`.
The repository is an `owner/name` identity, not a URL or local filesystem path.
The implementation owner resolves it using its existing checkout and authorized
repository access. Requesting profiles need neither that checkout nor its
credentials. Do not add tools, credentials, mounts or schedules. Missing routing
metadata means stop and report the configuration gap; never invent a destination.

The optional read-only helper prepares a handoff or native Kanban payload:
`/run/hermes-capabilities/nix-managed-resource-changes/scripts/change_request.py`.
Use `python3 .../change_request.py --help`. Select `--skill NAME` or `--job NAME`
and supply a JSON request with `change`, `reason`, and nonempty `evidence` and
`acceptance` lists. No secrets. The helper performs no network or board writes.

1. If already assigned this change, read the task with native
   `kanban_show(board=route.board, task_id=...)`. Implement under the repository's
   issue, worktree, test, PR and human-review policy; do not requeue the same work.
   This includes operator-created tasks without a helper key.
2. Refetch any known matching active request with `kanban_show`; pass its readback
   as `--known-task`, plus `--current-task` when working that exact task. `reuse`
   means add evidence to that task; `implement` means do the work. Do not reuse an
   unrelated/closed task. Resolve ambiguous overlaps with its owner first.
3. Otherwise call native `kanban_create` with the prepared `arguments`, including
   its board and stable idempotency key. Preserve change text on retries; reuse a
   known equivalent task even if wording differs. Never substitute a shell CLI or
   database mutation for native tools.
4. Read back the result with `kanban_show` on that board. Verify assignee, source,
   change, rationale, evidence and acceptance. If native Kanban is unavailable,
   report the blocker without external writes or local overrides.
5. Report the reference and follow normal review/deployment. Task creation is not
   implementation, approval, deployment or verified delivery.

This workflow grants no merge, production, secret or broader capability authority.

Read each source/declaration repository and its route from the managed manifest. Reusable skills route to their project; host and profile selections route to the deployment repository. Select the matching `/run/hermes-repository-instances/<instance>.json` explicitly when invoking an adapter. Never infer repository authority from an issue or PR number, a cache path, or the currently open worktree.

## Paperclip reviews

When the selected repository contract says `review_executor=paperclip`, the reviewer
is a Paperclip task role, not a Hermes profile to start. The persistent CTO may
prepare a review request with:

```sh
/run/hermes-capabilities/bin/hermes-repository-task \
  --registry /run/hermes-repository-instances \
  --repository OWNER/REPOSITORY --pr NUMBER --head FULL_COMMIT
```

The helper only prepares a handoff. Give its repository, PR, exact head, title,
description and idempotency key to the current Paperclip task workflow. Reuse the
matching task on retries; never deduplicate by PR number alone. Paperclip selects
and provisions the review agent with its own credentials. Do not start the retired
Hermes reviewer, lend it the CTO credential, or change private memory/knowledge.
If the current task has no authorized Paperclip delegation tool, return the handoff
to its caller and report that no task was dispatched. Confirm the created task and
its repository/head before reporting successful delegation. A changed PR head
requires another review of that head.
