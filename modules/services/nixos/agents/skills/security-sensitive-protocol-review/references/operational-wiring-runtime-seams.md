# Operational wiring: runtime-seam audit

Use this reference when a change declares scheduled jobs, reconcilers, managed scripts, or profile-private service capabilities and the question is whether they can actually execute after activation.

## End-to-end trace

For every job or service, record one row with:

1. **Owner and activation:** who materializes/enables it, through which supported interface, and what runtime readback proves it is active.
2. **Executable:** exact installed path or pinned CLI command and subcommand.
3. **Execution namespace:** host systemd process, gateway subprocess, terminal container, or another sandbox.
4. **Runtime inputs:** config, preferences, request envelopes, private overlays, registries, records, and state paths.
5. **Input producer:** the component or authorized operator procedure that creates and refreshes each input.
6. **Credentials:** source path, runtime projection path, owner/mode, and whether that path exists in the execution namespace.
7. **Transition authority:** exact identifiers, CLI arguments, state directory, and readback required to finish or fail a claim.
8. **Delivery:** exact native route, no-agent/wake semantics, and sole delivery owner.

A declaration, package closure, template, manifest entry, installed wrapper, or startup existence check proves only its own layer. It does not prove the next layer.

## High-yield contradiction checks

- Compare every required job identity against the runtime helper's literal allowlist. A registry example or test that lists an identity cannot override a smaller production constant.
- Inspect the pinned CLI parser for every verb, including uncommon state transitions. Plausible verbs in adapters or fake tests are not contracts. Add a build/check that reads the exact locked parser source, compares the reconciler's literal verbs, and rejects plausible near-synonyms such as `enable` when the real lifecycle verb is `resume`; the fake must mirror this same surface.
- Validate schedule semantics against the pinned parser. Required recurring jobs must reject bare relative delays such as `4h` when those create one-shot work; permit only the exact recurring grammar (for example `every 4h` or a supported cron expression) and keep a regression that distinguishes the two.
- Resolve credential paths from the script's actual process namespace. A path mounted at `/run/...` in terminal Docker containers is unavailable to a host gateway cron subprocess unless separately projected or passed as a host path.
- Inventory every file opened by the executable. For each, identify provisioning, ownership/mode, schema validation, refresh semantics, and a pre-activation gate. Generic directory creation is not file provisioning.
- Distinguish a collector from an ingester. A script that reads a prewritten `records.json` has no live producer merely because its name says “collection.” Trace Miniflux/feed/ATS/API reads to the file or pipe actually consumed.
- For agent-waking gates, compare emitted context with the managed skill's commands. The context/instructions must supply every required state path, claim ID, preferences/result path, private input path, and mandatory CLI flag. Child-process exports do not modify the parent gateway or later agent environment.
- Cross-check docs and templates against production constants. Internal contradictions such as “two allowlisted jobs” beside a three-job template often reveal the real runtime mismatch.

## Finding format

Report only effective-tree defects:

```text
SEVERITY — concise operational failure
Evidence: exact file:line producer/consumer contradiction
Effect: first real execution failure or unsafe behavior
Norm: accepted concept/amendment/plan citation
Smallest durable fix: one owning-layer correction plus the narrow proof
```

Before publishing a dirty-worktree audit, re-read every cited line and classify any concurrent change as fixed, introduced, or still present. Do not repeat findings the parent already corrected unless the effective tree remains broken.
