---
name: rust-code-review
description: Use when reviewing Rust source, crates, CLIs, services, or system tooling.
version: 1.0.0
metadata:
  hermes:
    tags: [rust, code-review]
    related_skills: [code-reviewer-review-process]
---

# Rust Code Review

## Criteria

1. Check ownership and lifetimes for correctness without unnecessary
   cloning, shared mutable state, or hidden panics.
2. Check error handling: avoid `unwrap`/`expect` in operational paths,
   preserve context, distinguish user errors from internal failures, and
   return meaningful exit codes for CLIs.
3. Check concurrency and async behavior: cancellation, blocking calls in
   async paths, task lifetimes, channel backpressure, locks, and races.
4. Check serialization, parsing, path handling, and input validation for
   malformed or hostile input.
5. Check dependency features, binary size/runtime assumptions, tests,
   benchmarks where relevant, and compatibility with the repository's
   build target.
