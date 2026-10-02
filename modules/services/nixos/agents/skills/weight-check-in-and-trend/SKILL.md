---
name: weight-check-in-and-trend
description: Use when validating private weight check-ins and calculating qualified trends.
version: 1.1.0
metadata:
  hermes:
    tags: [nutrition, measurement, privacy, deterministic]
---

# Weight check-in and trend

Validate explicit weight observations and calculate a qualified trend. This skill does not choose a tracking service, profile, schedule, delivery target, or source of authority.

## Check-in procedure

1. Require timestamp, decimal measurement, unit, and measurement-context identifier.
2. Normalize only supported units. Reject malformed, non-positive, or ambiguous measurements.
3. A correction must explicitly identify an existing observation; never guess which record to replace.
4. Preserve device, calibration, placement, conditions, and provenance supplied by the caller.

## Trend procedure

A trend is qualified only with at least three effective observations spanning at least seven days and one stable measurement context. Do not combine changed calibration contexts, infer missing observations, suppress uncertainty, or interpret a shorter series as a trend.

Return the observations used, normalized values, date span, calculation method, and every exclusion or warning. Arithmetic must be deterministic rather than model-generated.

Measurements and context are private. Keep them within the caller-approved private input boundary and out of Nix, the store, process arguments, logs, Kanban, skills, memory, fixtures, and unrelated profile messages. Service reads/writes, reminders, scheduling, delivery, and cross-profile handoff belong to the caller's agent or integration contract.

## Executable helper

Use `/run/hermes-capabilities/bin/hermes-nutrition-workflows --help` for the installed CLI contract. The helper and its imports are immutable; preferences, private overlays and state remain under the designated writable `$HERMES_HOME` paths.
