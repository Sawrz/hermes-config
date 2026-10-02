---
name: grocy-api
description: Use when interactively reading or changing pantry and shopping data through the managed Grocy REST helper.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [grocy, pantry, shopping, stock, cooking, api]
    related_skills: [evidence-driven-change-control]
---

# Managed Grocy API

## Overview

Use this skill for an explicit interactive request against the configured Grocy service. It enables supported pantry, stock, product, and shopping-list operations with file-only authentication, live OpenAPI/version checks, bounded array pagination, origin-safe redirects, typed errors, exact previews, duplicate avoidance, read-before-write reconciliation, and mandatory readback. It starts no scheduler, collector, sync, or integration.

This skill describes what Grocy can do safely. It does not decide whether an agent should use Grocy for pantry or shopping tasks; that choice belongs to the agent's contract.

Grocy 4.6.0's official live schema documents API-key authentication using `GROCY-API-KEY`, generic entity operations, `limit`/`offset` pagination, `/user`, stock operations, and shopping-list operations. Although Grocy also documents a same-named query parameter, this helper deliberately supports the header only.

## Runtime and authorization contract

Use exactly:

- `/run/hermes-credentials/services/grocy/endpoint` — canonical HTTPS `/api` root;
- `/run/hermes-credentials/services/grocy/credential` — API key.

Both must be non-empty, non-symlink regular files with mode `0400`. There is no environment, query-parameter, CLI-token, or alternate-path fallback. Never print, compare, hash, or inspect the key.

A key is not proven least-privilege by its secret-path name. Use a dedicated Grocy user/key with only the required stock/shopping permissions when the deployed version supports that assignment. If it cannot, report that the key has the owning user's broader authority and that restrictions are behavioral, not cryptographic. Verify effective identity and allowed/denied operations before use; do not use an admin key as an unstated default.

Run the immutable helper from any sandbox working directory:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/grocy-api/scripts/grocy_api.py preflight
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/grocy-api/scripts/grocy_api.py schema-check GET /stock
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/grocy-api/scripts/grocy_api.py list /objects/products --cap 500 --page-size 100
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/grocy-api/scripts/grocy_api.py get /stock
```

Preflight calls `/user` but suppresses the private body. Return only requested household data.

## Schema, pagination, and transport

Before every write, the helper fetches `/openapi/specification` without authentication and verifies OpenAPI 3.x, exact method/path, JSON request support, the exact declared top-level payload fields and primitive/container JSON types, required fields, and an optional exact application version. It resolves only one direct escaped local object-schema reference; composition, reference siblings, recursive schemas, undeclared fields, and nested schema interpretation are outside this bounded adapter and fail closed. Never infer an entity, field, or stock operation from memory.

For operations whose live schema advertises both `limit` and `offset`, the helper pages until a short page and probes the cap boundary before claiming completeness. Other array endpoints are read once and rejected if the result exceeds the cap. Redirects remain on the configured origin and `/api`; decoded dot segments and backslashes are rejected. Writes never redirect or retry blindly. Reads have finite timeout and bounded retries.

Errors remain typed: runtime contract, connectivity, authentication, authorization, not found, validation, conflict, rate limit, server, response format, schema/version drift, pagination, origin, authority, confirmation, readback, and indeterminate write.

## Pantry and shopping writes

The agent contract supplies `--authority grocy:pantry` or `--authority grocy:shopping`; the helper validates the assertion against the requested endpoint and rejects mismatched domains and write paths outside its bounded operation set. It does not choose the service or establish durable ownership.

For products and generic objects, use exact names and query duplicate candidates before creation. Preview first:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/grocy-api/scripts/grocy_api.py preview POST /objects/products \
  --payload-file /approved/product.json \
  --authority grocy:pantry \
  --reconcile-file /approved/reconcile.json
```

Apply only after explicit approval and exact confirmation:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/grocy-api/scripts/grocy_api.py apply POST /objects/products \
  --payload-file /approved/product.json \
  --authority grocy:pantry \
  --confirm '<exact confirmation returned by preview>' \
  --reconcile-file /approved/reconcile.json
```

The confirmation binds service/domain, live application version, method, exact path, and the SHA-256 of canonical payload plus reconciliation JSON, so changing either document after preview invalidates approval.

A generic-object POST requires exactly `path`, `query`, and `match`; `match` must contain every submitted payload field. Product identity is derived from its approved name rather than caller query spelling. Stock and shopping actions instead require exactly `path`, a non-empty `query`, exact `before` (stock requires it; shopping may use null for approved absence), and exact `after`. The helper accepts only the bounded amount/product/list fields and proves `after.amount = before.amount + requested.amount`. It deep-snapshots the approved JSON, acquires an owner-private file lock keyed by stable resource identity, and holds that lock across reconciliation, dispatch, ambiguous-outcome reconciliation, and mandatory readback. It dispatches only from the approved before state, accepts an already-present after state without replay, and requires exactly one after-state readback. A timeout, write redirect, malformed success response, or server failure after dispatch is reconciled once. Any other state remains indeterminate or fails closed.

Generic object creates read back `created_object_id`; updates read back the exact object path. Stock and shopping action responses often lack a stable object identity, so they require an exact bounded reconciliation contract against the resulting `/stock` or `/objects/shopping_list` state. The helper checks that state before dispatch for idempotency and again after success or an ambiguous transport/server outcome. A HTTP 200 alone is not success.

Delete, merge, clear-list, consume, inventory correction, undo, and bulk operations are outside this helper and require separately reviewed destructive semantics and explicit approval.

## Completion checklist

- The request was explicit and the agent contract selected Grocy for this operation.
- Effective identity/scope was not inferred from the path name.
- Runtime, schema, pagination, timeout, retry, and origin checks passed.
- Product/entity duplicates were checked before creation.
- Every write had preview, exact confirmation, reconciliation when creating, and mandatory readback.
- No ambiguous retry, operation outside Grocy, authority switch, background run, dual write, or sync occurred.

## Authoritative references

- Grocy API documentation: https://github.com/grocy/grocy/blob/master/docs/api.md
- Grocy 4.6.0 live schema: https://demo.grocy.info/api/openapi/specification
- Deployment-local schema used by the helper: `<grocy-origin>/api/openapi/specification`
