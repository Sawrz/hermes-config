"""Fail-closed verifier for Hermes cron native observation bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, TypeGuard

ROOT = Path(__file__).resolve().parent
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_MUTATION_PROBES = {"kanban", "forgejo", "ntfy", "prometheus"}
MAX_GATE_OUTPUT_BYTES = 64 * 1024


class DuplicateKeyError(ValueError):
    pass


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=_object_no_duplicates)
    if not isinstance(value, dict):
        raise TypeError(f"{path}: top-level JSON value must be an object")
    return value


def _parse_utc(value: Any, field: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        errors.append(f"{field} must be an ISO-8601 UTC timestamp ending in Z")
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        errors.append(f"{field} is not a valid ISO-8601 timestamp")
        return None


def _is_nonnegative_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _indexed(rows: Any, key: str, label: str, errors: list[str]) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        errors.append(f"{label} must be a list")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(key), str) or not row[key]:
            errors.append(f"{label} contains a row without string {key}")
            continue
        if row[key] in result:
            errors.append(f"{label} contains duplicate {key}: {row[key]}")
            continue
        result[row[key]] = row
    return result


def derive_observations(
    bundle: dict[str, Any], errors: list[str], start: datetime | None, end: datetime | None
) -> tuple[dict[str, int], dict[str, Any], dict[str, str], dict[str, str]]:
    evidence = bundle.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"before", "after"}:
        errors.append(
            "evidence must contain exactly collector-captured before and after snapshots; "
            "an operator-authored gate_result is not executed gate evidence"
        )
        return {}, {}, {}, {}
    before = evidence.get("before")
    after = evidence.get("after")

    for name, snapshot in (("before", before), ("after", after)):
        if not isinstance(snapshot, dict):
            errors.append(f"evidence.{name} must be an object")
            continue
        if snapshot.get("schema_version") != "hermes.verification.supported-snapshot.v2":
            errors.append(f"evidence.{name} has an unsupported schema_version")
        if snapshot.get("correlation_id") != bundle.get("correlation_id"):
            errors.append(f"evidence.{name}.correlation_id does not match")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return {}, {}, {}, {}
    gate_execution = after.get("gate_execution")
    if before.get("gate_execution") is not None or not isinstance(gate_execution, dict):
        errors.append("executed gate evidence must be captured only in the after snapshot")
        return {}, {}, {}, {}

    before_exec = _indexed(before.get("scheduler"), "id", "evidence.before.scheduler", errors)
    after_exec = _indexed(after.get("scheduler"), "id", "evidence.after.scheduler", errors)
    new_exec = [row for key, row in after_exec.items() if key not in before_exec]
    job_id = bundle.get("artifact", {}).get("job_id")
    if any(row.get("job_id") != job_id for row in new_exec):
        errors.append("new scheduler execution does not match artifact.job_id")
    for row in new_exec:
        claimed = _parse_utc(row.get("claimed_at"), "scheduler.claimed_at", errors)
        if (
            start is not None
            and end is not None
            and claimed is not None
            and not (start <= claimed <= end)
        ):
            errors.append("new scheduler execution is outside the correlated window")

    before_sessions = _indexed(before.get("sessions"), "id", "evidence.before.sessions", errors)
    after_sessions = _indexed(after.get("sessions"), "id", "evidence.after.sessions", errors)
    new_session_ids = set(after_sessions) - set(before_sessions)
    artifact = bundle.get("artifact", {})
    artifact_sha256 = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    gate_keys = {
        "correlation_id",
        "execution_id",
        "parser_result",
        "raw_output",
        "raw_output_sha256",
        "scheduler_consumed_decision",
        "script_identity",
        "script_sha256",
    }
    if set(gate_execution) != gate_keys:
        errors.append("executed gate evidence has an invalid field set")
    raw_output = gate_execution.get("raw_output")
    raw_bytes = raw_output.encode("utf-8") if isinstance(raw_output, str) else b""
    if not raw_bytes or len(raw_bytes) > MAX_GATE_OUTPUT_BYTES:
        errors.append("executed gate raw_output must be non-empty and bounded")
    raw_digest = hashlib.sha256(raw_bytes).hexdigest()
    if gate_execution.get("raw_output_sha256") != raw_digest:
        errors.append("executed gate raw_output_sha256 does not match raw_output")
    try:
        if raw_output is None:
            raise TypeError("raw_output is missing or null")
        parsed_output = json.loads(raw_output, object_pairs_hook=_object_no_duplicates)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed_output = None
        errors.append("executed gate raw_output is not strict JSON")
    gate = gate_execution.get("parser_result")
    scheduler_decision = gate_execution.get("scheduler_consumed_decision")
    if parsed_output != gate:
        errors.append("executed gate parser_result does not match raw_output")
    if scheduler_decision != gate:
        errors.append("scheduler-consumed decision does not match parser_result")
    if gate_execution.get("correlation_id") != bundle.get("correlation_id"):
        errors.append("executed gate correlation_id does not match the run")
    new_execution_ids = {row.get("id") for row in new_exec}
    if len(new_execution_ids) != 1 or gate_execution.get("execution_id") not in new_execution_ids:
        errors.append("executed gate execution_id does not match the scheduler execution")
    if gate_execution.get("script_identity") != artifact.get("gate_script_identity"):
        errors.append("executed gate script_identity does not match the tested artifact")
    if gate_execution.get("script_sha256") != artifact.get("gate_script_sha256"):
        errors.append("executed gate script_sha256 does not match the tested artifact")

    before_model = _indexed(
        before.get("model_events"), "event_id", "evidence.before.model_events", errors
    )
    after_model = _indexed(
        after.get("model_events"), "event_id", "evidence.after.model_events", errors
    )
    new_model = [row for key, row in after_model.items() if key not in before_model]
    for row in new_model:
        if row.get("correlation_id") != bundle.get("correlation_id") or row.get("job_id") != job_id:
            errors.append("new model attempt is not correlated to the run")
        if (
            row.get("profile") != artifact.get("profile")
            or row.get("artifact_sha256") != artifact_sha256
        ):
            errors.append("new model attempt is not bound to the tested runtime artifact")
        if row.get("session_id") not in new_session_ids:
            errors.append("new model attempt is not bound to a new Hermes session")
        received = _parse_utc(row.get("received_at"), "model.received_at", errors)
        completed = None
        if row.get("completed_at") is not None:
            completed = _parse_utc(row.get("completed_at"), "model.completed_at", errors)
        for timestamp in (received, completed):
            if (
                start is not None
                and end is not None
                and timestamp is not None
                and not (start <= timestamp <= end)
            ):
                errors.append("new model attempt is outside the correlated window")
    attempts = len(new_model)
    successes = 0
    input_delta = 0
    output_delta = 0
    for row in new_model:
        outcome = row.get("outcome")
        if outcome not in {"success", "error", "interrupted"}:
            errors.append("model event has an invalid outcome")
        if outcome == "success":
            successes += 1
        for field in ("input_tokens", "output_tokens"):
            value = row.get(field)
            if not _is_nonnegative_int(value):
                errors.append(f"model event has invalid {field}")
                value = 0
            if field == "input_tokens":
                input_delta += value
            else:
                output_delta += value

    before_delivery = _indexed(
        before.get("delivery_events"), "event_id", "evidence.before.delivery_events", errors
    )
    after_delivery = _indexed(
        after.get("delivery_events"), "event_id", "evidence.after.delivery_events", errors
    )
    new_delivery = [row for key, row in after_delivery.items() if key not in before_delivery]
    for row in new_delivery:
        if row.get("correlation_id") != bundle.get("correlation_id") or row.get("job_id") != job_id:
            errors.append("new delivery event is not correlated to the run")
        if (
            row.get("profile") != artifact.get("profile")
            or row.get("artifact_sha256") != artifact_sha256
        ):
            errors.append("new delivery event is not bound to the tested runtime artifact")
        if row.get("session_id") not in new_session_ids:
            errors.append("new delivery event is not bound to a new Hermes session")
        received = _parse_utc(row.get("received_at"), "delivery.received_at", errors)
        if (
            start is not None
            and end is not None
            and received is not None
            and not (start <= received <= end)
        ):
            errors.append("new delivery event is outside the correlated window")

    referenced_sessions = {row.get("session_id") for row in new_model}
    if new_session_ids != referenced_sessions:
        errors.append("new Hermes sessions are not exactly correlated to model attempts")

    gate = gate if isinstance(gate, dict) else {}
    changed_events = gate.get("changed_events")
    if not _is_nonnegative_int(changed_events):
        errors.append("executed gate parser_result.changed_events must be a non-negative integer")
        changed_events = -1
    if set(gate) != {"wakeAgent", "changed_events"} or gate.get("wakeAgent") not in {
        True,
        False,
        None,
    }:
        errors.append("executed gate parser_result must contain valid wakeAgent and changed_events")

    observations = {
        "scheduler_executions": len(new_exec),
        "changed_events": changed_events,
        "agent_sessions": len(new_session_ids),
        "model_attempts": attempts,
        "model_successes": successes,
        "input_tokens": max(0, input_delta),
        "output_tokens": max(0, output_delta),
        "deliveries": len(new_delivery),
    }
    mutations_before = before.get("mutation_probes")
    mutations_after = after.get("mutation_probes")
    isolation_before = before.get("profile_isolation")
    isolation_after = after.get("profile_isolation")
    for label, value in (
        ("evidence.before.mutation_probes", mutations_before),
        ("evidence.after.mutation_probes", mutations_after),
        ("evidence.before.profile_isolation", isolation_before),
        ("evidence.after.profile_isolation", isolation_after),
    ):
        if not isinstance(value, dict) or not all(
            isinstance(name, str)
            and name
            and isinstance(digest, str)
            and SHA256_RE.fullmatch(digest)
            for name, digest in value.items()
        ):
            errors.append(f"{label} must be a name-to-SHA-256 object")
    return observations, gate, mutations_before or {}, isolation_before or {}


def verify(
    bundle: dict[str, Any],
    contract: dict[str, Any],
    inventory: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    if inventory is None:
        inventory = load_json(ROOT / "artifact-inventory.json")
    if bundle.get("schema_version") != "hermes.verification.observation.v1":
        errors.append("unsupported observation schema_version")
    if contract.get("schema_version") != "hermes.verification.contract.v1":
        return ["unsupported contract schema_version"]
    correlation_id = bundle.get("correlation_id")
    try:
        uuid.UUID(str(correlation_id))
    except (ValueError, TypeError, AttributeError):
        errors.append("correlation_id must be a UUID")

    fixture = bundle.get("fixture")
    fixture_contract = contract.get("fixtures", {}).get(fixture)
    if not isinstance(fixture_contract, dict):
        errors.append(f"unknown fixture: {fixture!r}")
        return errors

    raw_window = bundle.get("window")
    window: dict[str, Any] = raw_window if isinstance(raw_window, dict) else {}
    if not window:
        errors.append("window must be an object")
    start = _parse_utc(window.get("start"), "window.start", errors)
    end = _parse_utc(window.get("end"), "window.end", errors)
    if start is not None and end is not None and end <= start:
        errors.append("window.end must be later than window.start")
    if not _is_nonnegative_int(window.get("settle_seconds")):
        errors.append("window.settle_seconds must be a non-negative integer")

    raw_artifact = bundle.get("artifact")
    artifact: dict[str, Any] = raw_artifact if isinstance(raw_artifact, dict) else {}
    if not artifact:
        errors.append("artifact must be an object")
    for field in contract.get("artifact_fields", []):
        value = artifact.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"artifact.{field} is required")
    for field in ("profile_config_sha256", "gate_script_sha256"):
        value = artifact.get(field)
        if isinstance(value, str) and not SHA256_RE.fullmatch(value):
            errors.append(f"artifact.{field} must be a lowercase SHA-256 digest")
    if not re.fullmatch(r"[0-9a-f]{40}", str(artifact.get("git_revision", ""))):
        errors.append("artifact.git_revision must be a 40-character Git SHA")
    if isinstance(artifact.get("hermes_source_revision"), str) and not re.fullmatch(
        r"[0-9a-f]{40}", artifact["hermes_source_revision"]
    ):
        errors.append("artifact.hermes_source_revision must be a 40-character Git SHA")
    for field in ("package_store_path", "nix_generation"):
        if isinstance(artifact.get(field), str) and not artifact[field].startswith("/nix/store/"):
            errors.append(f"artifact.{field} must be an absolute Nix store path")
    frozen = {
        "hermes_version": inventory.get("hermes_version"),
        "hermes_source_revision": inventory.get("hermes_source_revision"),
        "package_store_path": inventory.get("wrapper_store_path"),
    }
    for field, expected in frozen.items():
        if not isinstance(expected, str) or not expected:
            errors.append(f"artifact inventory does not define {field}")
        elif artifact.get(field) != expected:
            errors.append(f"artifact.{field} does not match the frozen inventory")

    derived, gate, mutation_before, isolation_before = derive_observations(
        bundle, errors, start, end
    )
    evidence = bundle.get("evidence", {})
    before = evidence.get("before", {}) if isinstance(evidence, dict) else {}
    after = evidence.get("after", {}) if isinstance(evidence, dict) else {}
    for name, snapshot in (("before", before), ("after", after)):
        if isinstance(snapshot, dict):
            captured = _parse_utc(
                snapshot.get("captured_at"), f"evidence.{name}.captured_at", errors
            )
            if (
                start is not None
                and end is not None
                and captured is not None
                and not (start <= captured <= end)
            ):
                errors.append(f"evidence.{name}.captured_at is outside the window")
            if snapshot.get("artifact") != artifact:
                errors.append(f"evidence.{name}.artifact does not match bundle artifact")
    if isinstance(before, dict) and isinstance(after, dict):
        mutations_after = after.get("mutation_probes", {})
        isolation_after = after.get("profile_isolation", {})
        if (
            set(mutation_before) != REQUIRED_MUTATION_PROBES
            or set(mutations_after) != REQUIRED_MUTATION_PROBES
        ):
            errors.append(
                "mutation probes must contain exactly: "
                + ", ".join(sorted(REQUIRED_MUTATION_PROBES))
            )
        if not isolation_before or set(isolation_before) != set(isolation_after):
            errors.append("profile isolation must contain matching unauthorized profiles")
        if (
            fixture_contract.get("require_unchanged_mutation_probes")
            and mutation_before != mutations_after
        ):
            errors.append("external mutation probes changed")
        if (
            fixture_contract.get("require_profile_isolation")
            and isolation_before != isolation_after
        ):
            errors.append("profile isolation changed")

    declared = bundle.get("observations")
    if declared != derived:
        errors.append("declared observations do not match native evidence derivation")
    expected_fields = set(contract.get("observation_fields", []))
    if set(derived) != expected_fields:
        errors.append("derived observations do not match the contract field set")
    for field, expected in fixture_contract.get("exact", {}).items():
        if derived.get(field) != expected:
            errors.append(
                f"observations.{field} mismatch: expected {expected}, got {derived.get(field)!r}"
            )
    for field, minimum in fixture_contract.get("minimum", {}).items():
        value = derived.get(field)
        if not _is_nonnegative_int(value) or value < minimum:
            errors.append(f"observations.{field} must be at least {minimum}, got {value!r}")
    if derived.get("model_successes", 0) > derived.get("model_attempts", 0):
        errors.append("model_successes cannot exceed model_attempts")

    if bundle.get("wake_agent") is not fixture_contract.get("wake_agent") or bundle.get(
        "wake_agent"
    ) is not gate.get("wakeAgent"):
        errors.append("wake_agent does not match fixture and gate evidence")
    if bundle.get("execution_status") != fixture_contract.get("execution_status"):
        errors.append("execution_status mismatch")
    new_scheduler = []
    if isinstance(before, dict) and isinstance(after, dict):
        before_ids = {row.get("id") for row in before.get("scheduler", []) if isinstance(row, dict)}
        new_scheduler = [
            row
            for row in after.get("scheduler", [])
            if isinstance(row, dict) and row.get("id") not in before_ids
        ]
    native_statuses = {row.get("status") for row in new_scheduler}
    execution_status = bundle.get("execution_status")
    if execution_status == "success" and native_statuses != {"completed"}:
        errors.append("successful bundle requires one native completed execution")
    if execution_status == "failed" and not native_statuses <= {"failed", "unknown"}:
        errors.append("failed bundle requires native failed/unknown execution")
    if execution_status == "timed_out" and not any(
        row.get("status") == "failed" and "timeout" in str(row.get("error", "")).lower()
        for row in new_scheduler
    ):
        errors.append("timed_out bundle requires native failed execution with timeout evidence")
    if fixture_contract.get("require_error") and not isinstance(bundle.get("error"), str):
        errors.append("fixture requires a non-empty error")
    if (
        fixture_contract.get("require_error")
        and isinstance(bundle.get("error"), str)
        and not bundle["error"].strip()
    ):
        errors.append("fixture requires a non-empty error")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--contract", type=Path, default=ROOT / "contract.json")
    args = parser.parse_args(argv)
    try:
        contract = load_json(args.contract)
        bundle = load_json(args.bundle)
        errors = verify(bundle, contract)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"NOT PASS: {exc}", file=sys.stderr)
        return 2
    if errors:
        print("NOT PASS")
        for error in errors:
            print(f"- {error}")
        return 1
    digest = hashlib.sha256(args.bundle.read_bytes()).hexdigest()
    print(
        f"PASS fixture={bundle['fixture']} correlation_id={bundle['correlation_id']} sha256={digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
