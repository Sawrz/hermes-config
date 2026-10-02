---
name: forge-hosted-pr-review-workflow
description: Use for platform-independent pull-request review lifecycle and Kanban handoff flow on forge-hosted code; combine with a concrete platform skill.
version: 1.0.0
metadata:
  hermes:
    tags: [pull-requests, code-review, kanban]
    related_skills: [code-reviewer-review-process]
---

# Forge-Hosted PR Review Workflow

## When to Use

Use this for pull-request or review-card assignments hosted on a forge.
It owns the platform-independent lifecycle only. Use a concrete platform
skill for comments, reviews, status checks, API calls, and placement.

## Workflow

1. Start with `kanban_show()` for the assigned review card and read the
   PR URL, issue URL, branch/head SHA, parent handoff, stated checks,
   and workflow metadata.
2. Establish scope: what changed, why, what is explicitly out of scope,
   and what acceptance criteria or human comments triggered the review.
3. Inspect the PR diff locally or from the forge, then read enough nearby
   code and repository instructions to judge the change in context.
4. Use the selected platform skill to fetch authoritative PR state,
   statuses/checks, existing comments, and to place review discussion. Inspect
   every formal review's inline comments, human conversation after the latest
   approval, and exact-head review evidence before deciding readiness.
5. Keep durable review discussion on the forge when the project has a PR.
   Kanban is the queue and handoff trail, not a replacement for replying
   to PR comments.
6. If the instance explicitly selects the Kanban-to-Vikunja integration, its
   deterministic projection owns human-inbox updates after durable forge/Kanban
   evidence. Reviewers do not write to that projection. Without that integration,
   follow the configured handoff contract; this skill does not require Vikunja.
7. If changes are needed, leave concrete forge findings first, then add a durable
   Kanban review-card comment, create the platform-appropriate child handoff
   assigned to the implementation owner, and complete the review card.
8. If the PR is clean, leave exact-head clean review evidence and complete the
   review card only when required CI is also successful.

## Handoff Evidence

Record the PR URL, head SHA, files inspected, checks/statuses consulted,
findings or clean-review rationale, every formal review/inline-comment surface
consulted, and any child handoff target.

## Boundaries

- Do not merge, close, label, edit branch protection, or mutate issues
  unless the review assignment explicitly asks for that action.
- Do not place secrets, tokens, or raw private data in forge comments or
  Kanban handoffs.
- The selected hosting platform (for example Forgejo or GitHub) remains
  authoritative for PR state; native Kanban records execution and handoffs.
  If selected, Vikunja is only the managed human-action projection. A projected
  checkbox, comment, assignment, or completion is not merge or deployment
  authorization.
