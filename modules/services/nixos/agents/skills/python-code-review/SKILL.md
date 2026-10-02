---
name: python-code-review
description: Use when reviewing Python source, packaging, tests, CLIs, services, or automation scripts.
version: 1.0.0
metadata:
  hermes:
    tags: [python, code-review]
    related_skills: [code-reviewer-review-process]
---

# Python Code Review

## Criteria

1. Check runtime correctness: imports, entrypoints, exception paths,
   resource cleanup, idempotency, retries, timeouts, and deterministic
   behavior under empty, malformed, or partial input.
2. Check data handling: schema validation, timezone handling, path safety,
   encoding/decoding, serialization, pagination, and large-output bounds.
3. Check dependency and packaging behavior: declared dependencies,
   interpreter assumptions, CLI arguments, environment variables, and
   compatibility with the repository's runner or service wrapper.
4. Check security: shell quoting, subprocess boundaries, untrusted input,
   secret redaction, file permissions, network calls, and logging.
5. Check tests: unit coverage for branching behavior, fixtures for edge
   cases, no live-service dependency unless explicitly isolated, and
   meaningful failure assertions.
