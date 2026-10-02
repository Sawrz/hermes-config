---
name: declarative-agent-capability-packaging
description: Use when packaging agent skills and integrations as a validated declarative dependency graph.
version: 2.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [agents, nix, skills, integrations, dependency-graph]
    related_skills: [profile-scoped-service-credentials, evidence-driven-change-control]
---

# Declarative agent capability packaging

Package reusable agent capabilities once and publish their dependency closure to explicitly selected consumers.

> Consumers select roots; package metadata supplies dependencies.

This skill covers package declaration, closure, publication, conflict detection, and retirement cleanup. It does not choose profile policy, service authority, schedules, targets, credentials, or workflow behavior.

## Capability kinds

Keep these package types distinct:

- **agent skill** — reusable instructions plus optional scripts, references, templates, and runtime packages;
- **external-service capability** — supported operations for one already configured service binding;
- **service integration** — an explicit directional edge with managed executable/state dependencies;
- **native capability requirement** — a harness facility such as a toolset or lifecycle API;
- **managed artifact** — a package, unit, timer, file, or generated contract owned by one capability.

Runtime state, mutable preferences, target inventory, and credentials are inputs to deployed instances, not package metadata.

## 1. Declare one focused package

Give every capability a stable typed ID and one narrow purpose. Declare only facts needed to install and validate it:

- source directory;
- package kind and version;
- directly required capability IDs;
- required native capabilities or toolsets;
- required external-service capability names;
- runtime packages;
- managed artifacts;
- safety metadata needed by the implementation.

A portable skill must not contain profile names, users, hosts, addresses, schedules, deployment history, service-selection policy, or mutable target data. A service skill documents its supported operations and safety rules; the agent contract decides whether and why to use it.

Do not hide required dependencies in prose, shell paths, or copied helper code. Do not make a provider depend upward on one consumer.

## 2. Build deterministic closure

Starting from explicitly selected roots:

1. resolve every direct dependency;
2. reject unknown IDs;
3. detect cycles and report the full cycle;
4. deduplicate by typed capability ID;
5. reject incompatible providers or duplicate artifact ownership;
6. produce deterministic topological order;
7. expose provenance from each selected root to every transitive dependency.

Connectivity declarations must not start behavior. A job or integration is active only when its own root is selected by the profile contract.

## 3. Validate conflicts and requirements

Fail evaluation when:

- two capabilities publish the same skill name or managed artifact;
- a service capability lacks its required service binding or files;
- a package requires an unavailable native capability;
- a selected integration has no unique lifecycle/state owner;
- generated runtime filenames collide;
- dependency closure crosses an explicit profile isolation boundary;
- a package claims behavior the underlying service or harness does not support.

Do not resolve ambiguity with first-wins merging or implicit defaults.

## 4. Publish through native interfaces

Render the resolved closure into the harness's supported configuration and skill directories. Preserve canonical source content; introduce target-specific rendering only for a proven native interface mismatch.

Generated output must be deterministic, read-only where practical, and attributable to package IDs and source revisions. Profiles receive only their selected closure. A dispatcher or default profile receives nothing by convenience.

Shared helper implementations are acceptable. Agent-facing skills remain focused and independently selectable.

## 5. Retire obsolete package edges

When an authorized change replaces a capability:

1. identify every selected root and transitive consumer;
2. update declarations and tests together;
3. remove obsolete package references, generated artifacts, and compatibility aliases;
4. reject dangling IDs and undeclared runtime files;
5. verify a fresh build from empty generated state;
6. verify unrelated profile closures are unchanged.

Migration, credential rotation, external mutation, deployment, and rollback require their own explicit authorization and workflow. Packaging cleanup alone does not authorize them.

## Verification

At minimum test:

- unknown dependency and cycle rejection;
- deterministic closure and ordering;
- duplicate skill/artifact conflict rejection;
- service/native requirement validation;
- profile isolation and absence from unrelated closures;
- exact source publication including linked files and executable modes;
- fresh-state generation and stale-artifact removal;
- idempotent repeated evaluation;
- no behavior from an unselected capability;
- no credentials, target inventory, or mutable policy in generated package metadata.

For profile-policy separation and closure review, see:

- `references/managed-service-framework-vs-dynamic-profile-policy.md`
- `references/native-kanban-capability-closure-audit.md`
- `references/managed-vs-ad-hoc-boundary.md`
