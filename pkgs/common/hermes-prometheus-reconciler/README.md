# Prometheus incident reconciliation

This package reconciles machine incidents from Prometheus into Hermes Kanban. Prometheus/Alertmanager remains the sole normal authority for machine incidents. The reconciler queries the canonical Prometheus HTTP API, maps each exact alert series to one stable logical identity, and uses Hermes' supported native Kanban CLI with an explicit board and idempotency key. It does not read ntfy, start a model, open Kanban SQLite, implement a scheduler, or send human notifications.

## Lifecycle

- Only `ALERTS{alertstate="firing",actionable="true",notify_agent="true"}` rows are accepted.
- Identity is SHA-256 over canonical resource labels. Routing labels (`severity`, `domain`, `notify_*`, and similar controls) are excluded so policy changes do not split an active incident.
- The first firing transition uses `prometheus:<identity>:<epoch>` as the native Kanban idempotency key.
- Repeated firing and process restarts reuse the same task.
- A successful query where the identity is absent appends one read-back-verified recovery comment. It does not auto-complete work or claim that the root cause is fixed.
- A later resolved-to-firing transition increments the epoch and creates a fresh task.
- Failed or malformed Prometheus queries never infer recovery.

Durable state uses `hermes-workflow-state` immutable generations and retains only the current generation plus two predecessors. A separate run lock makes this single-authority integration one writer at a time. The native Kanban idempotency key safely retries a create whose response was lost. Do not deploy a second reconciler state root: Hermes 0.20.0 checks idempotency before insert but does not provide a database uniqueness constraint for competing independent writers. Metrics are fixed, label-free, and suitable for the node-exporter textfile collector.

## Narrow external watchdog

`external-watchdog` is a separate deterministic path deliberately limited to:

- Prometheus `/-/ready` through the authenticated HTTP API;
- the reconciler's fixed, label-free last-success metric while Prometheus is
  queryable; and
- one fixed, label-free node-exporter textfile result plus a failing systemd
  invocation when either check fails.

It runs outside the Prometheus host/failure domain, with a
dedicated least-privilege Prometheus basic-auth identity. It does not use SSH,
Hermes, Kanban, ntfy, or a general target/unit list and cannot become a second
normal incident source. The watchdog and normal reconciler are independent
module options; the module rejects watchdog placement on a host that runs
Prometheus.

## Commands

```console
hermes-prometheus-reconciler reconcile \
  --state-dir /var/lib/hermes-prometheus-reconciler \
  --metrics-file /var/lib/node-exporter/textfile/hermes-prometheus-reconciler.prom \
  --prometheus-url http://prometheus.example.test:9090 \
  --prometheus-username-file /run/credentials/prometheus-username \
  --prometheus-password-file /run/credentials/prometheus-password \
  --hermes /run/current-system/sw/bin/hermes \
  --board BOARD --tenant TENANT --assignee ASSIGNEE

hermes-prometheus-reconciler external-watchdog \
  --metrics-file /var/lib/node-exporter/textfile/hermes-prometheus-control-plane-watchdog.prom \
  --prometheus-url http://prometheus.example.test:9090 \
  --prometheus-username-file /run/credentials/watchdog-username \
  --prometheus-password-file /run/credentials/watchdog-password \
  --reconciler-max-age-seconds 180
```

Use the NixOS module rather than hand-writing production units. Supply the evaluated direct HTTP endpoint; do not add an SSH forward or local TLS proxy. The module default is disabled and deployment configuration owns activation.

## Verification

```console
python3 -m unittest discover -s pkgs/common/hermes-prometheus-reconciler/tests -v
nix build .#nixosConfigurations.<host>.pkgs.hermes-prometheus-reconciler --no-link
```
