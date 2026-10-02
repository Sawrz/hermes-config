---
name: javascript-code-review
description: Use when reviewing JavaScript source, Node.js scripts, browser code, packages, or build tooling.
version: 1.0.0
metadata:
  hermes:
    tags: [javascript, nodejs, code-review]
    related_skills: [code-reviewer-review-process]
---

# JavaScript Code Review

## Criteria

1. Check runtime behavior: module format, async promises, error handling,
   event-loop blocking, timers, cleanup, and deterministic exits for
   scripts.
2. Check data and DOM handling: validation, escaping, injection risks,
   browser/server boundary mistakes, and safe handling of external data.
3. Check package behavior: dependency changes, lockfile consistency,
   scripts, bundler configuration, environment assumptions, and version
   compatibility.
4. Check observability and operations: useful errors, no secret logging,
   bounded output, retries/timeouts for network calls, and CI-friendly
   commands.
5. Check tests/lint/build evidence relevant to the changed package.
