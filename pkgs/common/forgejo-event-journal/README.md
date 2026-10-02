# Forgejo event journal

This package is the F10 transport boundary for `nixos/nix-config`.
It is deliberately narrower than the workflow reconciler:

- a systemd timer polls the repository with overlap and feeds one crash-safe journal;
- canonical event identity deduplicates repeated polling observations;
- pending and acknowledged events survive restart and can be replayed without duplicating work;
- issue, PR, review, and CI state is evidence; only substantive comments and requested changes are marked actionable;
- no command invokes Hermes, models, Kanban, Vikunja, delivery, or hook mutation.

The implementation reuses `hermes-workflow-state`. One immutable generation contains
`journal.json`, `pending.json`, `receipts.json`, `cursor.json`, `health.json`, and
`metrics.json`; one durable selector publishes them together. The state root fails closed
on missing/corrupt selectors, unsafe names, symlinks, unexpected object types, duplicate
JSON keys, non-finite numbers, or incompatible schemas.

## Commands

```text
forgejo-event-journal poll ...
forgejo-event-journal pending [--disposition actionable|evidence]
forgejo-event-journal ack --event-id fj-... --proof-id ...
```

`pending` and `ack` form the source-bound delivery handoff. An acknowledgement is
accepted only for a known pending event and is idempotent for the same proof
identity. The event journal never interprets a proof as merge, deploy, rebuild,
closure, or other authority.

## Runtime ownership

Selecting `serviceIntegrations.forgejo-event-ingest` on exactly one enabled Hermes profile derives one polling systemd unit and timer from that profile's Forgejo endpoint and credential reference.
Polling reads the profile-scoped Forgejo credential at command time and stores state in
one private `StateDirectory` as the Hermes service user. The module opens no firewall port,
configures no reverse proxy, and contains no hook receiver or hook-management path.

The deployment assembler must provide an explicit UTC `initialSince` cutoff cursor.
There is no legacy state reader and no default that replays old repository history.
