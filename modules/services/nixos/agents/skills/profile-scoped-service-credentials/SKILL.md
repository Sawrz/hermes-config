---
name: profile-scoped-service-credentials
description: Use when configuring or verifying profile-private credential delivery for an existing external service.
version: 2.0.0
metadata:
  hermes:
    tags: [credentials, hermes, isolation, external-services]
    related_skills: [evidence-driven-change-control]
---

# Profile-scoped Service Credentials

## Purpose

Use this skill to configure or verify how an existing external-service credential reaches one Hermes profile. The skill owns credential-delivery safety, not service provisioning, secret generation, service selection, or application policy.

This skill never rotates, revokes, migrates, deletes, generates, or synchronizes credential material. Hand lifecycle changes to a separate, explicitly authorized credential-lifecycle workflow.

## Contract

Keep these layers separate:

1. **Credential source** — an operator-provided secret reference.
2. **Profile delivery** — only the authorized profile can receive the source.
3. **Runtime interface** — stable files consumed at command time.
4. **Service capability** — a separate service skill defines supported API operations.
5. **Agent contract** — decides whether and why the profile uses the service.

Do not put source secret names, users, hosts, service ownership, schedules, or deployment history in a reusable skill.

## Runtime interface

Use the configured service directory:

```text
/run/hermes-credentials/services/<service>/
```

Common files are:

```text
endpoint
credential
```

Multi-field services may use explicit names such as `username`, `password`, or `client-id`. Keep secret and non-secret sources distinct and reject duplicate runtime filenames.

Credential files must be regular, non-symlink files, readable only by the intended runtime, and mounted read-only where supported. Read credentials at command time. Do not copy them into environment variables, command arguments, logs, generated prompts, skills, task metadata, or persistent worker state.

## Procedure

### 1. Inspect the existing declaration

Identify:

- the authorized profile;
- service name and endpoint;
- operator-provided credential source;
- required runtime filenames;
- every execution plane that legitimately consumes the service;
- unauthorized profiles that must not receive it.

Stop if identity, scope, lifecycle, or intended consumer is ambiguous. Do not infer secret-manager names or invent credential values.

### 2. Validate credential semantics

Determine whether the supplied material is directly consumable read-only state. Static API tokens and fixed credentials may use read-only files. Rotating refresh tokens or writable authentication state need an explicit lifecycle owner and must not be presented as static file delivery.

### 3. Configure the narrow delivery

Declare only the required service files for the intended profile. Preserve existing stable runtime filenames unless the interface itself is wrong. Associate restarts or reloads only with consumers that must reopen the files.

Do not add environment fallbacks, aliases, shared convenience directories, Docker `--env-file`, or broad dispatcher access.

### 4. Verify without exposing values

Verify:

- source references differ where distinct identities are required;
- files exist as regular non-symlinks with the intended ownership and mode;
- authorized runtimes can read them through the stable interface;
- unauthorized profiles and unrelated services cannot access them;
- generated environments and process arguments do not contain credentials;
- a harmless authenticated operation proves effective identity and scope;
- authentication, authorization, connectivity, and schema failures remain distinguishable.

Never print values, prefixes, lengths, hashes, or encoded forms.

## Common pitfalls

- Treating separate mounts of one token as separate identities.
- Treating file presence as proof of authentication or authorization.
- Giving credentials to a dispatcher or default profile for convenience.
- Modeling writable OAuth state as a static read-only token.
- Adding an environment fallback because one consumer cannot see the file.
- Deleting or rotating material while merely configuring a consumer.

## Verification checklist

- [ ] The service already exists and the intended profile and scope are explicit.
- [ ] No credential value was generated, printed, copied, or inferred.
- [ ] Delivery is profile-private, file-backed, and read-only where supported.
- [ ] Unauthorized profiles lack the service files.
- [ ] Effective service identity and permitted scope were verified safely.
- [ ] No credential lifecycle mutation occurred.

## References

- `references/external-service-option-contract.md` — typed external-service option shape and collision checks.
- `references/nested-tmpfiles-runtime-ownership.md` — first-use runtime directory ownership diagnostics.
