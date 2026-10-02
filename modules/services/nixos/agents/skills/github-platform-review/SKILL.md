---
name: github-platform-review
description: Use for GitHub PR, review, comment, check, and API mechanics during code review; contains no domain-specific review criteria.
version: 1.0.0
metadata:
  hermes:
    tags: [github, pull-requests, code-review]
    related_skills: [forge-hosted-pr-review-workflow]
---

# GitHub Platform Review

## When to Use

Use this when the review target is hosted on GitHub. This skill owns
platform mechanics only.

## Mechanics

1. Verify PR state, base/head refs, head SHA, changed files, existing
   conversation, review comments, requested reviewers, labels, and check
   suite/status evidence through live GitHub data when local state may
   be stale.
2. Prefer `gh pr view`, `gh pr diff`, `gh pr checks`, and `gh api` when
   authenticated; otherwise use the REST or GraphQL API directly.
3. Keep review discussion on the PR. Use review comments for code-line
   findings, PR conversation comments for cross-cutting findings, and a
   review summary when submitting an approval/request-changes/comment
   review.
4. Match comments to the current head SHA and diff side/line when placing
   inline review comments.
5. Treat pending, skipped, cancelled, failed, neutral, and successful
   checks according to the repository's merge policy; do not call CI
   green from a stale or unrelated commit.

## Boundaries

- Do not merge, close, label, edit branch protection, request reviewers,
  or mutate issues unless the review assignment explicitly asks for that
  platform mutation.
- Do not place secrets, tokens, or raw private data in GitHub comments or
  Kanban handoffs.
