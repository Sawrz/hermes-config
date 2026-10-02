---
name: mealie-api
description: Use when interactively reading or changing supported Mealie data through the managed REST helper.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [mealie, recipes, meal-plans, cooking, api]
    related_skills: [evidence-driven-change-control]
---

# Managed Mealie API

## Overview

Use this skill for an explicit interactive request against the configured Mealie service. It enables supported recipe, meal-plan, and shopping-list operations with file-only authentication, live OpenAPI checks, bounded pagination and retries, origin-safe redirects, typed errors, exact previews, serialized read-before-write reconciliation, and mandatory readback. It starts no scheduler, collector, sync, integration, or Kanban workflow.

This skill describes what Mealie can do safely. It does not decide whether an agent should use Mealie for recipes, meal planning, or shopping lists; that choice belongs to the agent's contract.

Mealie's official API documentation says long-lived API tokens are created for a user and that each local deployment publishes interactive docs at `/docs`. The deployed user's group and household permissions remain the authorization boundary; a profile-local token path does not prove a restricted identity.

## Runtime contract

Use exactly:

- `/run/hermes-credentials/services/mealie/endpoint` — HTTPS Mealie origin with no path;
- `/run/hermes-credentials/services/mealie/credential` — long-lived user API token.

Both must be non-empty, non-symlink regular files with mode `0400`. The helper accepts no token argument, environment fallback, URL query credential, or alternate path. It sends the token only as a Bearer credential in the `Authorization` header. Never print, compare, hash, or inspect the credential.

POST reconciliation uses owner-private advisory locks under `${HERMES_HOME:-~/.hermes}/locks/mealie-api`. The lock covers reconciliation, dispatch, and mandatory readback for one exact resource identity so concurrent profile processes cannot both create the same object.

Run the immutable helper from any sandbox working directory:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/mealie-api/scripts/mealie_api.py preflight
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/mealie-api/scripts/mealie_api.py schema-check GET /api/recipes
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/mealie-api/scripts/mealie_api.py list /api/recipes --cap 200
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/mealie-api/scripts/mealie_api.py get /api/recipes/family-soup
```

Preflight suppresses the private response body. Other reads may contain private household data; return only what the user requested.

## Schema, pagination, and transport

Before every write, the helper fetches `/openapi.json` without authentication and verifies OpenAPI 3.x, exact method/path, JSON request support, the exact declared top-level payload fields and primitive/container JSON types, required fields, and an optional exact application version. It resolves only one direct local object-schema reference; composition, reference siblings, recursive schemas, undeclared fields, and nested schema interpretation are outside this bounded adapter and fail closed. Treat schema or version drift as a blocker, not permission to guess.

Mealie collection responses must contain `page`, `per_page`, `total`, `total_pages`, `items`, `next`, and `previous`. The helper follows relative `next` paths under `/api`, rejects malformed pages and loops, and stops before an explicit record cap. It never uses the official `perPage=-1` shortcut because bounded pagination is safer.

Every URL and redirect stays on the configured origin and under `/api` (except the schema endpoint); decoded dot segments and backslashes are rejected before dispatch. Writes never follow redirects or retry blindly. Reads retry only bounded rate-limit/server/transport failures. Authentication, authorization, validation, conflict, missing resource, rate limit, server, response format, schema/version drift, pagination, origin, confirmation, readback, and indeterminate-write failures remain distinct.

## Recipes: name, preview, apply, read back

Use exact user-facing recipe names. Before creating, search by exact name and reconcile by stable slug/name. URL import is unsupported because Mealie does not expose a proven payload-bound source identity for safe replay reconciliation; use a separately reviewed manual procedure instead. Do not create a second recipe merely because punctuation, case, or whitespace differs; show the candidate conflict.

1. Put only requested fields in a local mode-`0600` JSON file.
2. Preview with explicit current authority:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/mealie-api/scripts/mealie_api.py preview POST /api/recipes \
  --payload-file /approved/request.json \
  --authority mealie:recipes \
  --reconcile-file /approved/reconcile.json
```

3. Show the exact target, payload, and confirmation phrase. Apply only after explicit approval:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/mealie-api/scripts/mealie_api.py apply POST /api/recipes \
  --payload-file /approved/request.json \
  --authority mealie:recipes \
  --confirm '<exact confirmation returned by preview>' \
  --reconcile-file /approved/reconcile.json
```

The agent contract supplies one domain assertion: `mealie:recipes`, `mealie:meal_plans`, or `mealie:shopping`. The helper validates that assertion against the requested endpoint; it does not choose the service or establish durable ownership. Supported shopping writes are bounded to list/item creation and updates; deletion and bulk mutation remain unsupported. The helper deep-snapshots canonical payload and reconciliation JSON before comparing confirmation, then uses only those approved snapshots through locking, dispatch, and readback. The confirmation binds service/domain, live application version, method, exact path, and the SHA-256 of those snapshots, so changing either document before apply invalidates approval and changing caller-owned mappings while apply waits cannot alter the dispatched write.

A POST requires a reconciliation document containing exactly `path`, `query`, and a non-empty exact `match`; requests without one fail before dispatch. Recipe and shopping-list creates use the non-empty exact name as their stable identity, and that name must equal the direct write payload name; meal-plan and shopping-item matches must equal their payload. The approved query remains confirmation-bound input, but the helper derives the effective lookup and owner-private lock only from that stable identity, so a stale or contradictory filter or a more detailed match cannot hide or race a duplicate. Reconciliation is bound to the matching recipe or meal-plan collection and cannot point to another API resource. A write with a timeout, write redirect, malformed success response, or server failure after dispatch is reconciled once. Zero or multiple stable-identity matches remain `indeterminate_write`; never retry automatically. Successful writes are read back from a same-origin location, returned recipe slug, exact update path, or the exact bounded reconciliation result. Every requested field must be present and equal.

Delete and bulk mutation are unsupported. They require a separately reviewed procedure and explicit destructive approval.

## Completion checklist

- The request was explicit and the agent contract selected Mealie for this operation.
- Runtime files, live schema, exact method/path, pagination cap, and origin checks passed.
- Naming and duplicate candidates were handled before creation/import.
- Every write had an exact preview, confirmation, reconciliation contract when creating, and readback.
- Ambiguous writes were not retried.
- No operation outside Mealie, implicit authority change, background work, dual write, or sync occurred.

## Authoritative references

- Mealie API usage: https://docs.mealie.io/documentation/getting-started/api-usage/
- Deployment-local OpenAPI UI: `<mealie-origin>/docs`
- Deployment-local schema used by the helper: `<mealie-origin>/openapi.json`
