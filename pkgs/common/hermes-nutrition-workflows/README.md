# hermes-nutrition-workflows

Deterministic gates and calculations for three private workflows:

- `weight-check-in-and-trend`
- `daily-intake-closeout`
- `household-portion-planning`

This package is not a scheduler, delivery client, Wger client, private-data store, or cross-profile transport. Deployment configuration owns schedules, private runtime paths, and delivery; the service client owns Wger API mechanics.

## Runtime boundary

The command consumes:

```console
hermes-nutrition-workflows \
  --preferences /run/profile/nutrition/preferences.json \
  --private-overlay /run/profile/nutrition/private.json \
  --state-dir /var/lib/profile/nutrition/state
```

The bounded profile-private preference contains the workflow, IANA timezone, local start date, daily/weekly recurrence, wall-clock time, interval/weekdays, and dependency presence/revision. It contains no health or nutrition values. Native Hermes cron still owns only polling cadence; this package derives due occurrences independently from that cadence.

The private overlay is one workflow-local regular, non-symlink file with mode `0400`:

```json
{
  "schema_version": 1,
  "workflow": "daily-intake-closeout",
  "data": {
    "entries": [],
    "data_quality_questions": []
  }
}
```

Do not generate an overlay in Nix, copy it to the store, pass values in arguments, or log it. Each selected workflow gets its own file; do not combine household/profile data into one broad overlay.

## Gate order and output

The command validates preferences and computes the latest logical local-day occurrence first. It returns successful zero-byte stdout, without reading the private overlay or creating state, when:

- the run is not due;
- its required dependency is absent;
- the logical occurrence is already rendered in managed due state.

For a due run it validates and renders the private input, then advances managed due state by recording `<workflow>:<local-date>` in `rendered_occurrences` before stdout is handed to native Hermes. `rendered` means output generation and durable local persistence succeeded; it does not prove that native Hermes handed the output to the destination or that the destination accepted it. Dependency revision changes do not duplicate a rendered logical occurrence. Concurrent and restarted duplicate runs emit zero bytes. State contains only logical occurrence identities; it contains no food, measurement, provenance, amount, calorie, or macro values.

Timezone semantics are explicit: local calendar days define identity; ambiguous fall-back wall times use the earliest fold; nonexistent spring-forward wall times advance to the first valid local minute. Recurrence search, intervals, weekdays, rendered-occurrence history, and inputs are bounded and malformed state fails closed.

Exact reminders and summaries are rendered in the script. A non-empty `data_quality_questions` list produces one conditional actionable prompt, capped at 8 questions, 200 characters each, and 1200 UTF-8 bytes total. No prompt is produced before all due/dependency/rendered-state gates pass. Deployment must not model-enable exact-summary jobs and must prove no-op runs create zero attempts/tokens before activation.

The accepted delivery policy is best-effort and duplicate-averse. A failure after rendered-state persistence but before destination acceptance can suppress that occurrence from automatic retry. Recovery requires operator reconciliation and a manual resend may duplicate an ambiguously delivered result. Native Hermes remains the sole delivery owner; this package must not add a retry sender or delivery receipt subsystem.

## Calculations and data quality

All quantities and nutrition facts are decimal strings. Binary floats, non-finite values, negative values, unsupported units, volume without density, malformed provenance, and unknown fields fail closed.

Supported units are `mg`, `g`, `kg`, `ml`, and `l`; volume requires `density_g_per_ml`. Energy and macro totals use `Decimal`. Uncertainty is conservative linear propagation: source absolute uncertainty ranges are summed and reported as a percentage of the aggregate.

Weight trends require at least three effective measurements spanning at least seven calendar days. Exact duplicate observations are collapsed in input order. Explicit corrections supersede one existing record; missing targets, correction chains, and conflicting IDs fail. A device, calibration version, placement, or conditions change disqualifies the trend rather than silently joining incomparable measurements.

## Verification

```console
python3 -m unittest discover -s pkgs/common/hermes-nutrition-workflows/tests -v
nix build .#nixosConfigurations.<host>.pkgs.hermes-nutrition-workflows --no-link
```

The suite covers unit/portion arithmetic, uncertainty/provenance, duplicate/correction/calibration context, recurrence/local-day/timezone/DST/dependency/rendered-state advancement, legacy-state migration, restart/concurrency suppression, private-overlay file safety, state redaction, exact outputs, bounded actionable prompts, and CLI zero-byte/error behavior. Live cron configuration, zero-attempt/token telemetry, private file ownership, and delivery require deployment verification.
