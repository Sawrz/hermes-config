---
name: household-portion-planning
description: Use when calculating deterministic portions from explicit food and household inputs.
version: 1.1.0
metadata:
  hermes:
    tags: [nutrition, portions, household, privacy]
---

# Household portion planning

Calculate total and per-portion nutrition from explicit recipe, quantity, and household inputs. This skill does not choose a recipe, pantry, shopping, or nutrition service and does not establish authority between them.

## Procedure

1. Require ingredient quantities, units, nutrition source identifiers, source versions, portion count, and any requested allocation rules.
2. Use decimal strings for quantities and nutrition values. Normalize only supported units; volume conversion requires an explicit density.
3. Calculate recipe totals and per-portion values deterministically with the installed nutrition helper. Do not use model arithmetic.
4. Keep incomplete ingredients and uncertainty visible. Do not infer missing density, serving size, nutrition values, or household preferences.
5. Return the exact assumptions, totals, allocation rule, and rounding behavior needed to reproduce the result.

Private household values stay within the caller-approved private input boundary. Do not place them in Nix, the store, command arguments, logs, Kanban, skills, memory, or synthetic fixtures derived from real data.

Writing results to a service, composing a shopping workflow, scheduling, delivery, and cross-profile handoff belong to the caller's agent or integration contract.

## Executable helper

Use `/run/hermes-capabilities/bin/hermes-nutrition-workflows --help` for the installed CLI contract. The helper and its imports are immutable; preferences, private overlays and state remain under the designated writable `$HERMES_HOME` paths.
