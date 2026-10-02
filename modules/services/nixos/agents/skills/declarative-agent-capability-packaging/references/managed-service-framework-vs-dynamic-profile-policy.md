# Managed Service Frameworks vs Dynamic Profile Policy

Use this reference when an agent accesses a credential-backed application and the integration has both stable safety rules and changeable usage policy.

## Three-layer boundary

### 1. Managed service/application framework

Repository-manage the canonical application skill when drift could change credential handling, external mutations, data semantics, or safety. It owns:

- profile-local endpoint and credential-file discovery;
- authentication headers and identity-safe preflight;
- supported API/schema discovery and version handling;
- maintained helper scripts and deterministic client behavior;
- pagination, filtering, validation, error classification, and retries;
- application-specific object semantics and conventions;
- naming, import, update, duplicate-detection, and readback rules;
- confirmation requirements for deletion, bulk operations, undo, and other destructive actions;
- secret redaction and prohibition of environment/query-parameter fallbacks where file/header delivery is canonical.

This is broader than transport mechanics. Stable application behavior that users deliberately tuned—such as recipe naming and deduplication or stock/unit mutation semantics—belongs in the managed framework.

Helper scripts should implement deterministic API mechanics, not decide which application currently owns a domain or when the agent should perform business actions.

### 2. Profile-owned dynamic policy

Keep legitimate changeable choices profile-owned:

- when to use an application;
- which application currently owns a domain;
- current household, coaching, or workflow preferences;
- goals and qualitative decisions;
- whether and when an explicit cross-service transfer is useful.

A current statement such as “application A stores recipes” or “application B owns shopping lists” may change without an infrastructure PR. Store it in durable profile policy/context, not in the immutable service framework.

### 3. Managed meta-guardrails

The framework must constrain dynamic policy without hardcoding its current answer:

- require one explicit current authority per data domain;
- forbid silent dual writes;
- preserve the selected authority until the user changes it;
- require explicit, verified one-way transfers across applications;
- forbid unmanaged background or bidirectional synchronization;
- do not let profile policy widen credential authority, weaken confirmation gates, or reintroduce deprecated secret delivery.

## Identity and authorization

Document the intended ideal identity separately from deployed proof. A profile-specific secret path or item name does not prove that the key belongs to a dedicated restricted account.

For implementation acceptance:

1. authenticate through the profile-private credential file;
2. identify the effective application user without printing sensitive data;
3. inspect effective permissions where the application exposes them;
4. prove intended operations succeed;
5. prove unrelated admin/user/module operations are denied;
6. rotate or replace an overprivileged key through the normal human-owned secret workflow.

If upstream provides only broad user tokens, state that role restrictions are behavioral rather than cryptographic. If upstream supports user permissions, prefer a dedicated restricted service user.

## Inventory before standardization

Before promoting mutable skills into a canonical package, inspect what each owning profile actually built. Use read-only, profile-scoped inventory tasks when another profile's mutable tree is intentionally inaccessible.

Collect:

- exact skill and support-file names/paths;
- endpoint and credential discovery;
- auth and schema/version behavior;
- scripts, languages, dependencies, and whether they mix mechanics with policy;
- operation coverage and application-specific conventions;
- confirmation, idempotency, readback, pagination, and error handling;
- environment-variable fallbacks;
- scheduled jobs;
- dynamic policy currently embedded in the artifact.

Do not expose secret values, hashes, lengths, prefixes, or private application data. Do not mutate the inspected skill or external service during inventory. Reconcile all consumers into one package style rather than promoting one mutable copy by default.

## Verified hard cut

For replacement of mutable duplicate skills or legacy environment delivery:

1. package the canonical managed framework once;
2. expose it through the existing profile/service dependency graph;
3. test a fresh worker using only canonical endpoint/credential files;
4. run harmless identity/capability reads and one bounded permitted mutation with readback where appropriate;
5. prove unrelated profiles lack the mount and unauthorized operations fail;
6. remove mutable duplicates and environment passthroughs;
7. recreate the worker and repeat the file-only runtime probe;
8. remove old credentials after the restricted replacement is verified.

Do not leave compatibility copies or dual credential-delivery paths in the final state.

## Typical examples

- A fitness API framework is managed; a coach or nutritionist decides dynamically when its data is relevant.
- A recipe application's API mechanics, recipe naming, imports, updates, and deduplication are managed; the chef decides whether that application currently owns recipes.
- A pantry application's stock and unit semantics are managed; the chef decides whether it currently owns shopping lists.
