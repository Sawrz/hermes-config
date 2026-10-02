---
name: daily-intake-closeout
description: Use when producing a deterministic daily intake closeout from explicit food records.
version: 1.1.0
metadata:
  hermes:
    tags: [nutrition, macros, privacy, deterministic]
---

# Daily intake closeout

Produce an exact daily calorie and macro summary from records supplied by the caller. This skill does not choose a nutrition service, profile, schedule, delivery target, or source of authority.

## Procedure

1. Require each food record to identify its quantity, unit, nutrition source, and source version. Represent quantities and nutrition values as decimal strings.
2. Normalize only supported mass or volume units. Volume conversion requires an explicit density; missing density or unknown units fail closed.
3. Calculate totals deterministically with the installed nutrition helper. Do not use model arithmetic.
4. Preserve uncertainty and missing-data flags. Never invent serving sizes, ingredients, measurements, or nutrition values.
5. Return calories and requested macros together with the exact source records and calculation warnings used for the result.

Private intake data must remain in the caller-approved private input boundary. Do not copy it into Nix, the store, process arguments, logs, Kanban, skills, memory, fixtures, or unrelated profile messages.

A complete result has reproducible arithmetic, explicit provenance, and no hidden assumptions. Service writes, scheduling, acknowledgement, delivery, and cross-profile handoff belong to the caller's agent or integration contract.

## Executable helper

Use `/run/hermes-capabilities/bin/hermes-nutrition-workflows --help` for the installed CLI contract. The helper and its imports are immutable; preferences, private overlays and state remain under the designated writable `$HERMES_HOME` paths.
