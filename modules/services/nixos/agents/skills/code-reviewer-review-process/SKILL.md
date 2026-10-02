---
name: code-reviewer-review-process
description: Use first for every code review; defines the generic evidence standard, boundaries, finding format, and severity labels only.
version: 1.2.0
metadata:
  hermes:
    tags: [code-review, security, process]
    related_skills: [forge-hosted-pr-review-workflow]
    contracts: [kiss-yagni, abstraction-altitude, two-round-stop, evidence-severity, exact-head-gate, final-pr-only]
---

# Code Reviewer Review Process

## When to Use

Use this first for every review assignment before judging a change.
This skill is intentionally generic: it owns review discipline, evidence
standards, boundaries, finding format, and severity labels only.

## Evidence Standard

1. Establish the requested change and acceptance criteria from the PR,
   issue, Kanban card, parent handoff, commit messages, and workflow
   metadata.
2. Inspect the actual diff and enough nearby code to understand existing
   conventions, architecture, data flow, state changes, permissions,
   success paths, failure paths, and edge cases.
3. Treat worker summaries, confidence, familiar patterns, and passing
   smoke checks as clues, not proof.
4. Distinguish newly introduced problems from pre-existing issues.
5. Verify claims against source files, generated output, local checks,
   CI/status evidence, or documented platform state where available.
6. State which additional workflow, platform, repository-context, and
   domain-specific skills were used or why they were not needed.

## Review Judgment

- Prefer the simplest design that satisfies the explicit contract. Apply
  KISS and YAGNI, and prefer existing repository and native platform patterns
  over custom machinery.
- Distinguish a concrete defect from a wrong abstraction altitude. If the
  implementation keeps accumulating local exceptions, review whether the
  contract or shared boundary is wrong instead of requesting another patch.
- After two review/fix rounds for the same class of finding, or as soon as a
  proposed fix expands synonym, regex, or parser vocabulary, stop that loop
  and route the work to architecture/simplification review.
- Do not demand semantic proof over arbitrary prose. Prefer typed or
  enumerated fields, exact canonical sections, and existing parsers or
  schemas; reject unsupported forms explicitly and fail closed.
- Bound adversarial probes to materially different failure mechanisms, not
  synonyms or cosmetic formatting variants.

## Finding Gate

- A Blocking finding must cite an accepted requirement or demonstrated behavior,
  include a minimal reproduction, and explain why a simpler contract cannot
  address it.
- Speculative hardening, style, and theoretical completeness are non-blocking
  unless tied to an explicit requirement or demonstrated risk.
- Exact-head, security, permission, data-loss, runtime-boundary, and fail-closed findings remain blocking when supported by evidence.

## Integrated Campaign Reviews

For coherent multi-card work converging into one PR, package cards own focused
tests and evidence. Read the explicit campaign contract for the integration owner,
required review stages and their dependencies. Do not invent an extra review stage
or carry task names or campaign IDs into standing policy. Review the complete
integrated head at the required stage; a head change invalidates evidence tied to
the previous head. Native dependencies must represent those actual prerequisites.

## Finding Format

Each finding must include:

- severity: one of `Blocking`, `Important`, `Suggestion`, or `Question`
- location: file path plus line/range, symbol, command, or exact behavior
- issue: what is wrong or uncertain
- impact: why it matters in realistic operation
- suggested fix: the smallest durable correction or next check
- confidence: high/medium/low when uncertainty matters

If no findings remain, say what evidence was checked and what residual
risk, if any, remains.

## Severity Labels

- `Blocking`: likely correctness, safety, security, data-loss,
  deployability, or policy break that should stop merge/acceptance.
- `Important`: material maintainability, reliability, or operational risk
  that should normally be fixed before merge unless explicitly accepted.
- `Suggestion`: cleanup or improvement that is useful but not required.
- `Question`: a missing decision or unclear requirement that materially
  changes the review outcome.

## Boundaries

- Do not implement fixes, push branches, merge PRs, deploy, restart
  services, edit CI/CD or branch protection, rotate or inspect secrets,
  or mutate live systems.
- Do not trust or expose secrets. Do not request secret values for review.
- Do not invent findings to look thorough, and do not downgrade material
  risks because fixing them is inconvenient.
