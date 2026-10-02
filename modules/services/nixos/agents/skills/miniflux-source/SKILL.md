---
name: miniflux-source
description: Use when safely reading or managing feed sources through the configured Miniflux API.
version: 2.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [miniflux, rss, atom, feeds, source-management]
    related_skills: [evidence-driven-change-control]
---

# Miniflux source operations

Use this skill for an explicit request to discover, list, add, test, configure, disable, or remove feed sources in a configured Miniflux account. It describes Miniflux operations and safety; it does not decide whether Miniflux is authoritative for a workflow or select a digest, job-discovery, scheduler, or delivery architecture.

## Runtime contract

Use exactly:

- `/run/hermes-credentials/services/miniflux/endpoint`
- `/run/hermes-credentials/services/miniflux/credential`

Both must be non-empty, non-symlink regular files with mode `0400`. The endpoint is an HTTPS Miniflux service root. The credential is sent only as `X-Auth-Token`; the helper accepts no token argument or environment fallback. Never print, hash, compare, or inspect credentials.

The helper calls `/v1/me` and rejects admin accounts. It validates `user_id` on returned categories and feeds. Foreign or malformed objects fail closed.

## Helper

Use the absolute sandbox entrypoint below from any working directory. The immutable helper tree is read-only; keep requests, locks and caches in the profile workspace or designated state directory.

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/miniflux-source/scripts/miniflux_source.py preflight
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/miniflux-source/scripts/miniflux_source.py categories
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/miniflux-source/scripts/miniflux_source.py sources
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/miniflux-source/scripts/miniflux_source.py discover https://example.com
```

List before changing anything. Discovery does not subscribe. API output may contain private feed metadata; return only what was requested.

## Safe source lifecycle

New sources are created disabled:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/miniflux-source/scripts/miniflux_source.py add https://example.com/feed.xml --category-id 42
```

Creation reconciles by exact feed URL after an ambiguous transport outcome. Success requires one exact match; otherwise report `indeterminate_write` and do not retry blindly.

A deliberate refresh can test a disabled source:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/miniflux-source/scripts/miniflux_source.py refresh 123 --confirm 'REFRESH 123'
```

Enabling and crawler or scraper changes require target-bound confirmation:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/miniflux-source/scripts/miniflux_source.py enable 123 \
  --confirm 'CONFIGURE 123 {"disabled":false}'
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/miniflux-source/scripts/miniflux_source.py configure 123 \
  --crawler on --scraper-rules 'article' \
  --confirm 'CONFIGURE 123 {"crawler":true,"scraper_rules":"article"}'
```

Prefer a publisher's native RSS or Atom feed. Use crawler, scraper, rewrite, blocklist, keeplist, proxy, user-agent, cache, or feed-adapter URLs only when source-specific evidence requires them; this skill does not select or provision such services.

Disabling is the reversible default:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/miniflux-source/scripts/miniflux_source.py disable 123
```

Removal is destructive and requires exact ID plus current URL, followed by absence readback:

```console
/run/hermes-capabilities/bin/python3 /run/hermes-capabilities/miniflux-source/scripts/miniflux_source.py remove 123 \
  --confirm 'REMOVE 123 https://example.com/feed.xml'
```

Category creation and deletion likewise use exact title-bound confirmation. A non-empty category cannot be deleted by this helper.

## Transport and verification

- Keep requests and redirects on the configured origin and `/v1` root.
- Reject write redirects.
- Use finite timeouts and bounded retries for reads; never blindly retry writes.
- Read back every update and verify requested fields.
- Verify source deletion by absence.
- Preserve authentication, authorization, connectivity, validation, isolation, origin, confirmation, and indeterminate-write error classes.

Polling, workflow authority, scheduling, rendering, projection, delivery, and shadow state are outside this skill and belong to the caller's agent or integration contract.
