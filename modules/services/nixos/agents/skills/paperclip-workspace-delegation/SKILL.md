---
name: paperclip-workspace-delegation
description: Select native Paperclip workspaces when creating or delegating code tasks, implementation tasks, or reviews. Use separate isolated workspaces for independent parallel work and deliberate reuse for sequential continuation; verify source handoffs and workspace assignments.
---

# Paperclip workspace delegation

Apply this guidance when creating execution tasks or deciding whether tasks may run concurrently. It applies to every agent with task-creation capability, regardless of reporting relationships. Ordinary API-only coordination does not require a new code workspace.

## Independent work

For independent implementation, experiments, code review, or QA that reads/builds/tests a checkout, request a new native isolated workspace explicitly. Reviews can write caches and build outputs even when they do not edit source.

Keep the task's legitimate parent relationship. On the native issue-creation API, include this fragment alongside the project's ID and normal task fields:

```json
{
  "parentId": "PARENT_ISSUE_ID",
  "executionWorkspaceSettings": {
    "mode": "isolated_workspace"
  }
}
```

For a new independently isolated task, omit `executionWorkspaceId`, `inheritExecutionWorkspaceFromIssueId`, and any `executionWorkspacePreference: "reuse_existing"`. Do not copy the parent's realized branch, checkout path, or workspace binding into the request. If the chosen tool does not expose workspace selection, use an authorized native API or report that concrete limitation; do not assume the project default prevents inheritance.

Paperclip can inherit the parent's existing workspace when child creation omits workspace controls, even when the project defaults to isolation. Both instance and project isolation settings must support the requested mode. Check existing configuration through native reads; report a missing prerequisite rather than silently changing instance-wide settings.

## Source and verification

State the exact source commit or required predecessor handoff. A new isolated workspace may start from the project's base ref; it does not automatically contain a predecessor's unmerged work. Use an authorized base selection or a verified source artifact handoff, then verify the actual source identity before implementation or review. Do not invent a branch change or remote Git permission.

Read back the task's workspace settings. After Paperclip realizes the workspace, check its execution workspace ID and checkout path against the parent and any overlapping tasks. Independent tasks must have distinct destinations. An unrealized workspace is not yet proof of isolation. Paperclip may normalize the preference to `reuse_existing` after realization; that value alone does not imply parent sharing.

Do not treat separate SSH run directories as proof of separate restore destinations. Do not rely on the shared-workspace Serialize setting as verified protection for local/SSH execution or for multiple tasks reusing one isolated workspace.

## Deliberate continuation

Reuse an existing workspace when continuation genuinely needs the same checkout and branch. Make that choice explicit and sequence work through native dependencies. Before dependent source work starts, verify that the predecessor has completed workspace restoration and that the expected source is present. An issue marked done alone does not establish successful finalization.

If an unexpected shared binding or restore failure is discovered, avoid further overlapping runs against that destination. Preserve branches, artifacts and unexported work; inspect the native run/workspace evidence and reconcile source before an authorized rebind or retry. Do not reset the parent, delete locks, patch Paperclip, or replay completed work merely to make historical runs green.

This skill guides native task creation. It does not enforce server policy, grant additional permissions, or repair existing task bindings automatically.

References: [Workspace modes](https://docs.paperclip.ing/guides/projects-workflow/workspaces/), [Issues API](https://docs.paperclip.ing/reference/api/issues/).
