# Focused recheck of correlated observation harness blockers

Use this recipe when a prior review identified (a) self-asserted artifact identity or (b) incorrect malformed/timeout scheduler semantics.

## Artifact-substitution probe

Do not alter only the top-level artifact declaration. Make one unrelated but syntactically valid artifact identity and substitute it consistently into all three locations:

1. the observation bundle's top-level `artifact`;
2. the `before` native snapshot's `artifact`;
3. the `after` native snapshot's `artifact`.

Change at least the executable/package path, product version, and source revision; preserve valid SHA/path shapes. Run the real verifier without replacing its trusted inventory. The probe passes only if verification rejects the bundle specifically because the substituted identity does not match the independently loaded frozen inventory. Snapshot-equality errors alone do not prove the blocker fixed.

Record the exact verifier output. Also confirm operational entrypoints cannot inject a replacement inventory from the bundle or an untrusted CLI argument.

## Malformed and timeout semantics

Inspect the exact deployed source revision, not current main or documentation alone. Trace these boundaries separately:

1. malformed/non-JSON script stdout through the native gate parser;
2. subprocess timeout through the script runner's return value;
3. both results through prompt construction and agent short-circuit logic;
4. final scheduler-ledger state after a successful agent response.

Many fail-safe schedulers wake the agent for malformed gate output. A script timeout may likewise become an injected script-error prompt and still yield a completed scheduler execution if the agent reports it successfully. Do not model either as scheduler failure/no model merely because the pre-run script failed.

Use direct function execution against the exact source checkout when practical. A valid focused check should demonstrate that only an explicit canonical `wakeAgent=false` suppresses the agent, while malformed output and timeout preserve a non-empty agent prompt.

## Focused verdict

Report only:

- `PASS` or `NOT PASS`;
- exact staged `file:line` evidence for each fix;
- exact deployed-source `file:line` evidence for native semantics;
- the artifact-substitution probe's actual rejection output;
- any regression that directly invalidates either fix;
- files modified (normally none).

Do not broaden a blocker recheck into style review or a new formal campaign.
