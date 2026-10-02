---
name: wger-api
description: Use when interactively reading or changing supported data through the configured wger REST API.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [wger, fitness, nutrition, api, health]
    related_skills: [evidence-driven-change-control]
---

# Managed wger API

Use this skill only for an explicit interactive request concerning the configured wger account. It describes supported wger operations and safety, but does not choose a consuming profile, role, schedule, or workflow.

## Authority and isolation

The permanent API token is an application/account credential, not a fabricated scoped token. Wger account permissions are the authorization boundary.

Use exactly these profile-private runtime files:

- `/run/hermes-credentials/services/wger/endpoint`
- `/run/hermes-credentials/services/wger/credential`

The managed helper accepts no credential argument or environment fallback. It requires both paths to be non-empty, non-symlink regular files with mode `0400`. Authenticate the configured binding independently. Never compare, hash, log, print, inspect, or infer equality between credential values, and never use another binding's files.

The endpoint must be the HTTPS `/api/v2` root. The helper permits cleartext HTTP only for loopback test fixtures.

## Helper

Run the helper shipped with this skill:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/wger-api/scripts/wger_api.py preflight
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/wger-api/scripts/wger_api.py schema-check GET /routine/
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/wger-api/scripts/wger_api.py list /routine/ --cap 200
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/wger-api/scripts/wger_api.py get /routine/42/structure/
```

Use the absolute sandbox entrypoint below from any working directory. The immutable helper tree is read-only; keep requests, locks and caches in the profile workspace or designated state directory.

Preflight suppresses the private response body and returns only authentication status. Reads may return private health data; show only what the user requested and do not place it in logs, Forgejo, Kanban metadata, or unrelated chats. For large or sensitive output, write it to a mode-`0600` local file in the approved output directory.

## Live schema gate

Wger publishes its OpenAPI schema at `/api/v2/schema`. Before every write, the helper verifies that:

- the document is OpenAPI 3.x;
- the exact method and path, including templated resource paths, exist;
- writes accept `application/json`;
- an explicitly requested application version matches exactly.

Do not invent a route, filter, or payload field from memory. If the live schema differs, stop with `schema_drift` or `version_drift` and inspect the current schema. A schema check is necessary but not permission to mutate.

## Pagination and transport safety

List responses must contain `count`, `next`, `previous`, and `results`. `list` follows all pages, rejects loops and malformed pages, and stops before its explicit record cap. Increase the cap only when the request genuinely needs more records.

Every URL and redirect must remain on the configured scheme, host, port, and `/api/v2` path. Cross-origin redirects, credentials in URLs, fragments, API-root escapes, and all write redirects fail closed. Reads use a finite timeout and at most two bounded retries by default. Writes are never blindly retried.

Errors are classified as runtime contract, connectivity, authentication (`401`), authorization (`403`), missing resource (`404`), rate limit (`429`), server, response format, schema/version drift, pagination, origin violation, confirmation, readback, or indeterminate write. Do not relabel authentication or permission failures as missing endpoints.

## Writes: preview, confirm, reconcile, read back

Writes are deliberately limited to reviewed weight-entry operations: create with `POST /weightentry/`, and update an exact numeric record with `PUT` or `PATCH /weightentry/<id>/`. Live OpenAPI presence does not authorize another resource or route. Deletion, workout/routine mutation, schema-path substitution, and bulk mutation are outside this helper and require a separately reviewed, explicitly authorized implementation.

1. Put only the requested fields in a mode-`0600` JSON file.
2. Preview against the live schema:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/wger-api/scripts/wger_api.py preview POST /weightentry/ \
  --payload-file /approved/path/request.json \
  --reconcile-file /approved/path/reconcile.json
```

3. Review the exact method, path, payload, and emitted confirmation phrase.
4. Apply only after explicit user approval, passing the exact phrase:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/wger-api/scripts/wger_api.py apply POST /weightentry/ \
  --payload-file /approved/path/request.json \
  --confirm '<exact APPLY ... SHA256 ... phrase emitted by preview>' \
  --reconcile-file /approved/path/reconcile.json
```

The confirmation includes the SHA-256 digest of the canonical payload and
reconciliation contract, so changing either invalidates prior approval. Never
reconstruct or shorten the phrase; copy the exact value emitted by `preview`.

For a `POST`, the reconciliation file must contain exactly:

```json
{
  "path": "/weightentry/",
  "query": {"date": "2026-08-28"},
  "match": {"date": "2026-08-28"}
}
```

Choose query and match fields from the live schema. Query and match must be the same non-empty identity, and every field must equal the submitted payload; make that identity specific enough to identify one intended record. The helper rejects a `POST` before preview/confirmation unless this canonical contract is present. It locks the current profile's private credential binding while performing reconciliation, create, and readback, so concurrent helper processes cannot both pass read-before-write. If transport fails after dispatch, it reconciles once while retaining that lock; zero or multiple matches remain `indeterminate_write` and must not be retried automatically.

Every successful write is read back from a same-origin `Location`, returned `id`, or the exact `PUT`/`PATCH` path. All requested fields must match. Missing, ambiguous, or mismatched readback is failure, not success.

## Completion checklist

- The request was explicit and interactive.
- Current profile runtime files passed the helper's fail-closed checks.
- No value, derivative, or comparison of credentials was exposed.
- Exact live method/path/schema and optional version were verified.
- Pagination was fully consumed within a deliberate cap when completeness mattered.
- The operation stayed on the configured origin and API root.
- A write had preview, explicit exact confirmation, and mandatory readback.
- A create had an exact-target reconciliation contract or remained unperformed.
- Private response data went only to the requested destination.
- No cron, collector, automatic Kanban path, compatibility fallback, or second authority was introduced.
