---
name: typescript-code-review
description: Use when reviewing TypeScript source, types, Node.js or browser applications, packages, or build tooling.
version: 1.0.0
metadata:
  hermes:
    tags: [typescript, code-review]
    related_skills: [javascript-code-review]
---

# TypeScript Code Review

## Criteria

1. Apply the JavaScript runtime checks, then verify that types accurately
   model runtime values instead of masking uncertainty with broad casts.
2. Check strictness assumptions, null/undefined handling, discriminated
   unions, generic constraints, inferred public API types, and unsafe
   `any`/assertion usage.
3. Check generated/client/server types stay in sync with schemas,
   migrations, API responses, and validation at trust boundaries.
4. Check build configuration, module resolution, emitted artifacts, source
   maps, declarations, and package exports.
5. Check tests include both type-level expectations where useful and
   runtime behavior for important branches.
