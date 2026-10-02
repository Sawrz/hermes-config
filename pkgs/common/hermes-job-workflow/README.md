# Hermes job discovery workflow

This package owns normalized public vacancy state, deterministic passive
collection, revalidation, retention, the bounded active-research gate, and a
pure projection planner. It uses `hermes-workflow-state` generations for
crash-safe publication.

## Authority boundaries

- Miniflux/feed cadence is passive and script-only. Feed observations are never
  promoted to official closure evidence.
- Greenhouse, Lever, and employer adapters accept bounded page/record sets.
  Official employer/ATS data wins over feed data for the same canonical URL.
- Timeout, malformed response, denial, and bot protection produce `unknown`.
  Only an explicit official record can produce `closed`.
- Active research is a separate managed cron claim. Empty, disabled, in-flight,
  and not-due runs return `wakeAgent=false`; the positive envelope is capped.
- CV text may be loaded from one `0400` file only for an in-memory ranking
  envelope. Collector, durable-state, preference, and projection schemas have
  no CV field.
- Projection modes are `off|manual|suggest|auto` and default to `manual`. `auto` requires a separate
  immutable authorization input that is deliberately absent from the mutable
  preference contract. P70 must leave that gate false unless Sandro explicitly
  authorizes it. This package plans mutations; the managed Vikunja integration
  owns authenticated writes and readback.

`hermes-job-workflow projection-plan` is the supported non-mutating interface
for `manual` and `suggest` modes. It reads the selected durable job generation;
it never refetches sources or writes Vikunja. `auto` remains unavailable because
the required immutable authorization is deliberately absent.
- There is no application, CV upload, recruiter-contact, external-message,
  deletion, or task-completion operation.

## State

One immutable generation contains:

- `jobs.json`: active, closed, or unknown records plus TTL metadata;
- `tombstones.json`: retired stable identities preventing accidental reuse;
- `meta.json`: last public-state update time.

Closed jobs remain visible for the configured 30–730 day retention window.
Projected closed jobs are annotated by the downstream projector; they are not
deleted or completed.

## Commands

```console
hermes-job-workflow miniflux-passive \
  --state-dir /var/lib/hermes/jobs/passive \
  --preferences /run/hermes-managed/jobs/preferences.json \
  --entries /run/hermes-managed/jobs/miniflux-entries.json \
  --now 2026-08-28T08:00:00Z

hermes-job-workflow miniflux-active-gate \
  --state-dir /var/lib/hermes/jobs/active \
  --preferences /run/hermes-managed/jobs/preferences.json \
  --entries /run/hermes-managed/jobs/miniflux-entries.json \
  --now 2026-08-28T08:00:00Z

hermes-job-workflow revalidate \
  --state-dir /var/lib/hermes/jobs/passive \
  --preferences /run/hermes-managed/jobs/preferences.json \
  --results /run/hermes-managed/jobs/revalidation-results.json \
  --now 2026-08-28T09:00:00Z

hermes-job-workflow active-complete \
  --state-dir /var/lib/hermes/jobs/active \
  --claim-id claim-EXACT \
  --proof research-summary-sha256:EXACT
```

`miniflux-passive` maps one bounded, category-scoped Miniflux readback and always
returns `{"wakeAgent":false}` after durable ingestion. It is not a Hermes model
job. `miniflux-active-gate` maps a fresh readback and is the only model wake
boundary. The generic `passive` and `active-gate` commands remain available for
bounded already-fetched ATS/employer adapters; the Alakazam profile uses the
Miniflux commands so no static records file can become a fake collector.
Completion uses the worker host's trusted UTC clock and rejects expired or
superseded claims; callers cannot supply a completion timestamp.

## Verification

```console
PYTHONPATH=pkgs/common/hermes-job-workflow/src:pkgs/common/hermes-workflow-state/src \
  python3 -m unittest discover -s pkgs/common/hermes-job-workflow/tests -v

nix build .#checks.x86_64-linux.hermes-job-workflow --no-link
```
