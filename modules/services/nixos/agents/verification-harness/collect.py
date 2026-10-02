#!/usr/bin/env python3
"""Capture supported Hermes CLI and boundary evidence for one isolated verification run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CRON_RUN_RE = re.compile(
    r"^(?P<id>\S+)\s+(?P<status>claimed|running|completed|failed|unknown)\s+"
    r"job=(?P<job_id>\S+)\s+source=(?P<source>\S+)\s+(?P<claimed_at>\S+)$"
)
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
HERMES_VERSION_RE = re.compile(r"^Hermes Agent v?(?P<version>[0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)")
MAX_GATE_EXECUTION_BYTES = 64 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(path: Path) -> str:
    """Hash one file/tree without following symlinks or reading special files."""
    root = path.resolve(strict=True)
    digest = hashlib.sha256()
    paths = [root] if root.is_file() else [root, *sorted(root.rglob("*"))]
    for item in paths:
        relative = "." if item == root else item.relative_to(root).as_posix()
        info = item.lstat()
        mode = stat.S_IFMT(info.st_mode)
        digest.update(f"{relative}\0{mode:o}\0{info.st_mode & 0o7777:o}\0".encode())
        if stat.S_ISLNK(info.st_mode):
            digest.update(os.readlink(item).encode())
        elif stat.S_ISREG(info.st_mode):
            digest.update(sha256_file(item).encode())
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"unsupported object in snapshot: {item}")
        digest.update(b"\0")
    return digest.hexdigest()


def parse_named_paths(values: list[str], option: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for raw in values:
        name, separator, path = raw.partition("=")
        if not separator or not name or not path or name in result:
            raise ValueError(f"{option} requires unique NAME=PATH values")
        result[name] = Path(path)
    return result


def _jsonl(path: Path | None, label: str) -> list[dict[str, Any]]:
    if path is None:
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{label} line {number} must be a JSON object")
            events.append(value)
    return events


def read_delivery_events(path: Path | None, artifact: dict[str, Any]) -> list[dict[str, Any]]:
    events = _jsonl(path, "delivery log")
    required = {
        "event_id",
        "correlation_id",
        "job_id",
        "session_id",
        "profile",
        "artifact_sha256",
        "received_at",
    }
    artifact_sha256 = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for number, value in enumerate(events, 1):
        if set(value) != required or not all(
            isinstance(value[field], str) and value[field] for field in required
        ):
            raise ValueError(f"delivery log line {number} has an invalid schema")
        if value["profile"] != artifact["profile"] or value["artifact_sha256"] != artifact_sha256:
            raise ValueError(
                f"delivery log line {number} is not bound to the tested runtime artifact"
            )
    return events


def read_model_events(path: Path | None, artifact: dict[str, Any]) -> list[dict[str, Any]]:
    events = _jsonl(path, "model log")
    required = {
        "event_id",
        "correlation_id",
        "job_id",
        "session_id",
        "profile",
        "artifact_sha256",
        "received_at",
        "completed_at",
        "outcome",
        "input_tokens",
        "output_tokens",
    }
    artifact_sha256 = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for number, value in enumerate(events, 1):
        if set(value) != required:
            raise ValueError(f"model log line {number} has an invalid field set")
        if not all(
            isinstance(value[field], str) and value[field]
            for field in (
                "event_id",
                "correlation_id",
                "job_id",
                "session_id",
                "profile",
                "artifact_sha256",
                "received_at",
            )
        ):
            raise ValueError(f"model log line {number} has invalid identity fields")
        if value["profile"] != artifact["profile"] or value["artifact_sha256"] != artifact_sha256:
            raise ValueError(f"model log line {number} is not bound to the tested runtime artifact")
        if value["outcome"] not in {"success", "error", "interrupted"}:
            raise ValueError(f"model log line {number} has an invalid outcome")
        if value["outcome"] == "interrupted":
            if value["completed_at"] is not None:
                raise ValueError(f"model log line {number} interrupted attempt must not complete")
        elif not isinstance(value["completed_at"], str) or not value["completed_at"]:
            raise ValueError(f"model log line {number} completed attempt needs completed_at")
        for field in ("input_tokens", "output_tokens"):
            if (
                not isinstance(value[field], int)
                or isinstance(value[field], bool)
                or value[field] < 0
            ):
                raise ValueError(f"model log line {number} has invalid {field}")
    return events


def command_output(argv: list[str], hermes_home: Path) -> str:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    completed = subprocess.run(
        argv,
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    return completed.stdout.strip()


def parse_cron_runs(output: str, job_id: str) -> list[dict[str, Any]]:
    if not output or output == "No cron execution attempts recorded.":
        return []
    records: list[dict[str, Any]] = []
    for line in output.splitlines():
        if line.startswith((" ", "\t")):
            if not records or records[-1].get("error") is not None:
                raise ValueError("unexpected or repeated cron-run error line")
            records[-1]["error"] = line.strip()
            continue
        match = CRON_RUN_RE.fullmatch(line.strip())
        if match is None:
            raise ValueError(f"unsupported `hermes cron runs` output: {line!r}")
        record = match.groupdict()
        if record["job_id"] != job_id:
            raise ValueError("cron runs output contains a different job")
        record["error"] = None
        records.append(record)
    return records


def parse_session_ids(output: str) -> list[dict[str, str]]:
    if not output or output == "No sessions found.":
        return []
    sessions: list[dict[str, str]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("Title", "Preview", "─")):
            continue
        session_id = stripped.rsplit(maxsplit=1)[-1]
        if not SESSION_ID_RE.fullmatch(session_id):
            raise ValueError(f"unsupported `hermes sessions list` output: {line!r}")
        sessions.append({"id": session_id})
    return sessions


def parse_hermes_version(output: str) -> str:
    first_line = output.splitlines()[0] if output else ""
    match = HERMES_VERSION_RE.match(first_line)
    if match is None:
        raise ValueError(f"unsupported `hermes --version` output: {first_line!r}")
    return match.group("version")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_gate_execution(
    path: Path | None,
    artifact: dict[str, Any],
    correlation_id: str,
    scheduler: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if path is None:
        return None
    raw_record = path.read_bytes()
    if not raw_record or len(raw_record) > MAX_GATE_EXECUTION_BYTES:
        raise ValueError("gate execution record must be non-empty and bounded")
    value = json.loads(raw_record, object_pairs_hook=_strict_json_object)
    required = {
        "correlation_id",
        "execution_id",
        "parser_result",
        "raw_output",
        "raw_output_sha256",
        "scheduler_consumed_decision",
        "script_identity",
        "script_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("gate execution record has an invalid schema")
    if value["correlation_id"] != correlation_id:
        raise ValueError("gate execution record has the wrong correlation")
    if value["execution_id"] not in {row["id"] for row in scheduler}:
        raise ValueError("gate execution record has the wrong scheduler execution")
    if (
        value["script_identity"] != artifact["gate_script_identity"]
        or value["script_sha256"] != artifact["gate_script_sha256"]
    ):
        raise ValueError("gate execution record has the wrong script identity")
    return value


def capture(args: argparse.Namespace) -> dict[str, Any]:
    home = args.hermes_home.resolve(strict=True)
    probes = parse_named_paths(args.external_probe, "--external-probe")
    profiles = parse_named_paths(args.isolation_profile, "--isolation-profile")
    executable = Path(args.hermes_executable).resolve(strict=True)
    scheduler = parse_cron_runs(
        command_output([str(executable), "cron", "runs", args.job_id, "--limit", "500"], home),
        args.job_id,
    )
    sessions = parse_session_ids(
        command_output(
            [str(executable), "sessions", "list", "--source", "cron", "--limit", "10000"],
            home,
        )
    )
    package_store_path = executable.parent.parent if executable.parent.name == "bin" else executable
    artifact = {
        "git_revision": args.git_revision,
        "hermes_version": parse_hermes_version(
            command_output([str(executable), "--version"], home)
        ),
        "hermes_source_revision": args.hermes_source_revision,
        "package_store_path": str(package_store_path),
        "nix_generation": str(Path(args.nix_generation).resolve(strict=True)),
        "profile_config_sha256": sha256_file(args.profile_config.resolve(strict=True)),
        "job_id": args.job_id,
        "profile": args.profile,
        "gate_script_identity": str(args.gate_script.resolve(strict=True)),
        "gate_script_sha256": sha256_file(args.gate_script.resolve(strict=True)),
    }
    return {
        "schema_version": "hermes.verification.supported-snapshot.v2",
        "correlation_id": args.correlation_id,
        "captured_at": utc_now(),
        "artifact": artifact,
        "scheduler": scheduler,
        "sessions": sessions,
        "model_events": read_model_events(args.model_log, artifact),
        "delivery_events": read_delivery_events(args.delivery_log, artifact),
        "mutation_probes": {name: canonical_hash(path) for name, path in sorted(probes.items())},
        "profile_isolation": {
            name: canonical_hash(path) for name, path in sorted(profiles.items())
        },
        "gate_execution": read_gate_execution(
            getattr(args, "gate_execution_record", None),
            artifact,
            args.correlation_id,
            scheduler,
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", required=True, type=Path)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument("--git-revision", required=True)
    parser.add_argument("--hermes-source-revision", required=True)
    parser.add_argument("--hermes-executable", required=True, type=Path)
    parser.add_argument("--nix-generation", required=True, type=Path)
    parser.add_argument("--profile-config", required=True, type=Path)
    parser.add_argument("--gate-script", required=True, type=Path)
    parser.add_argument("--gate-execution-record", type=Path)
    parser.add_argument("--model-log", type=Path)
    parser.add_argument("--delivery-log", type=Path)
    parser.add_argument("--external-probe", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--isolation-profile", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    snapshot = capture(args)
    args.output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
