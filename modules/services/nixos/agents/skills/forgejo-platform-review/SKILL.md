---
name: forgejo-platform-review
description: Use for Forgejo PR, comment, review, status, and API mechanics during code review; contains no domain-specific review criteria.
version: 1.1.0
metadata:
  hermes:
    tags: [forgejo, pull-requests, code-review, kanban]
    related_skills: [forge-hosted-pr-review-workflow]
    contracts: [pending-review-submit, exact-head-review, inline-comment-readback]
---

# Forgejo Platform Review

## When to Use

Use this when the review target is hosted on Forgejo or a compatible
Gitea-derived forge. This skill owns platform mechanics only.

## Credentials and auth preflight

The profile-private Forgejo interface is exactly:

- `/run/hermes-credentials/services/forgejo/endpoint`
- `/run/hermes-credentials/services/forgejo/credential`

Before any Forgejo API operation, verify both files are readable and
non-empty inside the active terminal sandbox. Read them at command time into
unexported shell variables, never print or persist the credential, then make
an authenticated `GET <endpoint>/api/v1/user` request and verify the returned
reviewer identity. Do not use or restore `FORGEJO_BASE_URL`,
`FORGEJO_API_TOKEN`, or any environment-variable fallback.

Classify preflight failures before blocking: distinguish a missing mount or
file, an empty file, DNS/TLS/connectivity failure, authentication failure,
authorization or token-scope failure, repository not-found, API/schema
mismatch, and server failure. An unauthenticated private-repository `404` is
not evidence that the repository or endpoint is absent.

## Mechanics

1. Verify PR state, base/head refs, head SHA, changed files, existing
   conversation, review summaries, inline comments, and status/check
   evidence through live Forgejo data when local state may be stale.
2. Forgejo calls merge requests pull requests.
3. Prefer PR bodies that reference issues with `Refs #N` unless the
   project explicitly wants auto-close-on-merge semantics.
4. Check combined commit status and Actions task status from the PR head
   SHA. Do not assume GitHub-style log endpoints exist.
5. Keep review discussion in Forgejo. Reply in the same semantic location
   when possible: issue comments to issue comments, PR conversation
   comments to PR conversation comments, and inline review comments to
   same-review / same-path / same-position comments when supported.
6. If the API cannot create a true inline reply, prefer a normal PR
   conversation comment with a direct source link/quote over pretending
   the comment is threaded.
7. Inspect the deployed Forgejo version's review-comment API schema before
   choosing inline coordinates. Where the schema uses `body`, `path`, and
   `new_position` or `old_position`, use those fields. Do not substitute GitHub's
   `position` or `commit_id`; verify the returned comment's actual placement.

## Review submission invariant

A successful review-create response is not proof that the review is formal.
`POST /api/v1/repos/{owner}/{repo}/pulls/{index}/reviews` can return a review
whose state is `PENDING`, even when the create payload requested an approval.
Treat every create response as possibly pending and retain its review ID.

If the returned state is `PENDING`, explicitly submit it with
`POST /api/v1/repos/{owner}/{repo}/pulls/{index}/reviews/{review_id}`. Use the
formal state value accepted by the deployed server, not the create-operation
verb: on the observed instance an approval create used `APPROVE`, while the
submit payload required `APPROVED`. Inspect live `/swagger.v1.json` for the
endpoint and payload shape, but do not expect its `ReviewStateType` definition
to enumerate accepted values. A rejected or ambiguous submission is a hard
failure; do not retry with guessed state strings or report completion.

After create and any required submit, always read back all of the following:

1. `GET .../pulls/{index}/reviews/{review_id}` and require the intended formal
   state rather than `PENDING`;
2. the review `commit_id` against the current PR head SHA, plus `stale` and
   `official`, so an approval for an old head is never presented as current;
3. `GET .../pulls/{index}/reviews/{review_id}/comments`, including the empty
   list case, so inline findings are accounted for before completion.

Report a review as completed only when those read-backs agree with the intended
state and exact head. Include the review ID, formal state, head SHA, staleness,
and inline-comment count in the durable handoff.

## Boundaries

- Do not merge, close, label, edit branch protection, or mutate issues
  unless the review assignment explicitly asks for that platform
  mutation.
- Do not place secrets, tokens, or raw private data in Forgejo comments
  or Kanban handoffs.
