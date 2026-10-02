from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from hermes_workflow_state import (
    GenerationStore,
    ProtocolError,
    StateAbsent,
    exclusive_lock,
)

SCHEMA_VERSION = 1
MAX_ACTIVE_ALERTS = 500
MAX_RETAINED_INCIDENTS = 500
MAX_IDENTITY_LABELS = 24
LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
CONTROL_LABELS = {
    "__name__",
    "actionable",
    "alertstate",
    "digest",
    "domain",
    "notify_agent",
    "notify_human",
    "severity",
    "synthetic",
}
METRIC_NAMES = {
    "runs_total",
    "kanban_mutations_total",
    "firing_incidents",
    "resolved_incidents_total",
    "last_success_timestamp_seconds",
}


class ContractError(RuntimeError):
    """Input or durable state violates the bounded reconciliation contract."""


def _exact_dict(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _bounded_text(value: Any, label: str, maximum: int, *, multiline: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(char) < 32 and not (multiline and char == "\n") for char in value)
    ):
        raise ContractError(f"{label} must be non-empty bounded text without controls")
    return value


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractError("value is not canonical JSON data") from exc


def _labels(row: Any) -> dict[str, str]:
    if not isinstance(row, dict) or not isinstance(row.get("metric"), dict):
        raise ContractError("Prometheus result row must contain a metric object")
    labels = row["metric"]
    if not 1 <= len(labels) <= 32:
        raise ContractError("Prometheus alert labels exceed the bounded contract")
    normalized: dict[str, str] = {}
    for name, value in labels.items():
        if not isinstance(name, str) or not LABEL_NAME_RE.fullmatch(name):
            raise ContractError("Prometheus alert contains an unsafe label name")
        normalized[name] = _bounded_text(value, f"label {name}", 300)
    if normalized.get("alertstate") != "firing":
        raise ContractError("reconciler accepts only firing ALERTS rows")
    if normalized.get("actionable") != "true" or normalized.get("notify_agent") != "true":
        raise ContractError("reconciler accepts only actionable agent-routed alerts")
    if normalized.get("source") == "ntfy" or normalized.get("transport") == "ntfy":
        raise ContractError("ntfy transport is outside Prometheus incident reconciliation")
    _bounded_text(normalized.get("alertname"), "alertname", 160)
    return normalized


def alert_identity(row: Any) -> tuple[str, tuple[tuple[str, str], ...]]:
    labels = _labels(row)
    identity = tuple(
        sorted((name, value) for name, value in labels.items() if name not in CONTROL_LABELS)
    )
    if not identity or len(identity) > MAX_IDENTITY_LABELS:
        raise ContractError("incident identity has an invalid number of resource labels")
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return digest, identity


def _new_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_success": 0,
        "counters": {
            "runs_total": 0,
            "kanban_mutations_total": 0,
            "resolved_incidents_total": 0,
        },
        "incidents": {},
    }


def _validate_state(value: Any) -> dict[str, Any]:
    state = _exact_dict(value, {"schema_version", "last_success", "counters", "incidents"}, "state")
    if state["schema_version"] != SCHEMA_VERSION:
        raise ContractError("state schema is incompatible")
    if type(state["last_success"]) is not int or state["last_success"] < 0:
        raise ContractError("state last-success timestamp is invalid")
    counters = _exact_dict(
        state["counters"],
        {"runs_total", "kanban_mutations_total", "resolved_incidents_total"},
        "state counters",
    )
    if not all(type(value) is int and value >= 0 for value in counters.values()):
        raise ContractError("state counters must be non-negative integers")
    incidents = state["incidents"]
    if not isinstance(incidents, dict) or len(incidents) > MAX_RETAINED_INCIDENTS:
        raise ContractError("state incident cardinality exceeds the contract")
    for digest, incident in incidents.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ContractError("state incident key is invalid")
        incident = _exact_dict(
            incident,
            {
                "identity",
                "status",
                "epoch",
                "card_id",
                "first_seen",
                "last_seen",
                "resolved_at",
                "recovery_marker",
            },
            f"incident {digest}",
        )
        identity = incident["identity"]
        if (
            not isinstance(identity, list)
            or not 1 <= len(identity) <= MAX_IDENTITY_LABELS
            or any(not isinstance(pair, list) or len(pair) != 2 for pair in identity)
        ):
            raise ContractError("state incident identity is invalid")
        rebuilt = tuple(
            (
                _bounded_text(pair[0], "identity label", 128),
                _bounded_text(pair[1], "identity value", 300),
            )
            for pair in identity
        )
        if tuple(sorted(rebuilt)) != rebuilt:
            raise ContractError("state incident identity is not canonical")
        if hashlib.sha256(_canonical_json(rebuilt).encode("utf-8")).hexdigest() != digest:
            raise ContractError("state incident identity digest does not match")
        if incident["status"] not in {"firing", "resolving", "resolved"}:
            raise ContractError("state incident status is invalid")
        if type(incident["epoch"]) is not int or incident["epoch"] < 1:
            raise ContractError("state incident epoch is invalid")
        if incident["card_id"] is not None:
            _bounded_text(incident["card_id"], "card id", 80)
        for field in ("first_seen", "last_seen"):
            if type(incident[field]) is not int or incident[field] < 0:
                raise ContractError(f"state incident {field} is invalid")
        if incident["resolved_at"] is not None and (
            type(incident["resolved_at"]) is not int or incident["resolved_at"] < 0
        ):
            raise ContractError("state incident resolution timestamp is invalid")
        if incident["recovery_marker"] is not None:
            _bounded_text(incident["recovery_marker"], "recovery marker", 200)
    return state


def render_metrics(values: Mapping[str, int]) -> str:
    if not isinstance(values, Mapping) or set(values) - METRIC_NAMES:
        raise ContractError("metrics contain an unknown or unbounded name")
    lines = []
    for name in sorted(METRIC_NAMES):
        value = values.get(name, 0)
        if type(value) is not int or value < 0:
            raise ContractError("metric values must be non-negative integers")
        lines.append(f"hermes_prometheus_reconciler_{name} {value}")
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    if not path.is_absolute():
        raise ContractError("output path must be absolute")
    path.parent.mkdir(parents=True, mode=0o750, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ContractError("output target must be a regular file")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class _RejectRedirects(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ContractError("Prometheus endpoint redirects are not allowed")


def _read_runtime_file(path: Path, label: str, allowed_modes: set[int]) -> str:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ContractError(f"{label} runtime file is unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) not in allowed_modes:
        raise ContractError(f"{label} runtime file has unsafe type or mode")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContractError(f"{label} runtime file cannot be opened safely") from exc
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or stat.S_IMODE(after.st_mode) not in allowed_modes
            or after.st_size > 4096
        ):
            raise ContractError(f"{label} runtime file changed or exceeds its contract")
        raw = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ContractError(f"{label} runtime file is not UTF-8") from exc
    if not value:
        raise ContractError(f"{label} runtime file is empty")
    return value


class PrometheusClient:
    def __init__(self, endpoint: str, username_file: Path, password_file: Path, timeout: int = 20):
        parsed = urlparse.urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ContractError("Prometheus endpoint must use HTTP or HTTPS")
        self.endpoint = endpoint.rstrip("/")
        self.username_file = username_file
        self.password_file = password_file
        self.timeout = timeout

    def _auth_header(self) -> str:
        username = _read_runtime_file(
            self.username_file, "Prometheus username", {0o400, 0o440, 0o444}
        )
        password = _read_runtime_file(self.password_file, "Prometheus password", {0o400, 0o440})
        _bounded_text(username, "Prometheus username", 300)
        _bounded_text(password, "Prometheus password", 1000)
        raw = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        return f"Basic {raw}"

    def _get_json(self, path: str, query: Mapping[str, str] | None = None) -> Any:
        url = self.endpoint + path
        if query:
            url += "?" + urlparse.urlencode(query)
        request = urlrequest.Request(url, headers={"Authorization": self._auth_header()})
        opener = urlrequest.build_opener(_RejectRedirects())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
        except (OSError, urlerror.URLError) as exc:
            raise RuntimeError("Prometheus request failed") from exc
        if len(raw) > 2_000_000:
            raise ContractError("Prometheus response exceeds the bounded size")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("Prometheus response is not valid JSON") from exc

    def firing_alerts(self) -> list[dict[str, Any]]:
        payload = self._get_json(
            "/api/v1/query",
            {"query": 'ALERTS{alertstate="firing",actionable="true",notify_agent="true"}'},
        )
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise RuntimeError("Prometheus query API did not return success")
        data = payload.get("data")
        if (
            not isinstance(data, dict)
            or data.get("resultType") != "vector"
            or not isinstance(data.get("result"), list)
        ):
            raise ContractError("Prometheus query result is not an instant vector")
        return data["result"]

    def reconciler_recent(self, max_age_seconds: int) -> bool:
        if type(max_age_seconds) is not int or not 60 <= max_age_seconds <= 3600:
            raise ContractError("reconciler freshness threshold must be 60..3600 seconds")
        payload = self._get_json(
            "/api/v1/query",
            {"query": ("time() - hermes_prometheus_reconciler_last_success_timestamp_seconds")},
        )
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise RuntimeError("Prometheus freshness query did not return success")
        data = payload.get("data")
        if (
            not isinstance(data, dict)
            or data.get("resultType") != "vector"
            or not isinstance(data.get("result"), list)
        ):
            raise ContractError("Prometheus freshness query is not an instant vector")
        rows = data["result"]
        if not rows:
            return False
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise ContractError("reconciler freshness metric cardinality must be exactly one")
        value = rows[0].get("value")
        if not isinstance(value, list) or len(value) != 2:
            raise ContractError("reconciler freshness sample is malformed")
        try:
            age = float(value[1])
        except (TypeError, ValueError) as exc:
            raise ContractError("reconciler freshness value is not numeric") from exc
        return 0 <= age <= max_age_seconds

    def ready(self) -> bool:
        try:
            request = urlrequest.Request(
                self.endpoint + "/-/ready", headers={"Authorization": self._auth_header()}
            )
            opener = urlrequest.build_opener(_RejectRedirects())
            with opener.open(request, timeout=self.timeout) as response:
                return 200 <= response.status < 300
        except (OSError, urlerror.URLError, ContractError):
            return False


class KanbanCLI:
    def __init__(self, executable: str, board: str, assignee: str, tenant: str):
        for value, label in ((board, "board"), (assignee, "assignee"), (tenant, "tenant")):
            _bounded_text(value, label, 100)
        self.executable = executable
        self.board = board
        self.assignee = assignee
        self.tenant = tenant

    def _run(self, args: list[str], *, json_output: bool = True) -> Any:
        command = [self.executable, "kanban", "--board", self.board, *args]
        if json_output:
            command.append("--json")
        process = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(f"native Kanban CLI failed with exit {process.returncode}")
        if not json_output:
            return None
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise ContractError("native Kanban CLI did not return JSON") from exc

    @staticmethod
    def _task(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict) and isinstance(payload.get("task"), dict):
            return payload["task"]
        if isinstance(payload, dict):
            return payload
        raise ContractError("native Kanban response does not contain a task")

    def _show(self, task_id: str) -> dict[str, Any]:
        payload = self._run(["show", task_id])
        task = self._task(payload)
        if task.get("id") != task_id:
            raise ContractError("native Kanban readback returned the wrong task")
        if not isinstance(payload, dict):
            raise ContractError("native Kanban show response is malformed")
        return payload

    def ensure_task(self, *, idempotency_key: str, title: str, body: str) -> str:
        _bounded_text(idempotency_key, "idempotency key", 200)
        _bounded_text(title, "task title", 200)
        _bounded_text(body, "task body", 8000, multiline=True)
        task = self._task(
            self._run(
                [
                    "create",
                    title,
                    "--assignee",
                    self.assignee,
                    "--tenant",
                    self.tenant,
                    "--idempotency-key",
                    idempotency_key,
                    "--body",
                    body,
                ]
            )
        )
        task_id = _bounded_text(task.get("id"), "created task id", 80)
        readback = self._task(self._show(task_id))
        if readback.get("idempotency_key") not in {None, idempotency_key}:
            raise ContractError("native Kanban idempotency readback does not match")
        return task_id

    def ensure_comment(self, task_id: str, marker: str, body: str) -> None:
        _bounded_text(marker, "comment marker", 200)
        _bounded_text(body, "comment body", 4000, multiline=True)
        task = self._show(task_id)
        comments = task.get("comments", [])
        if not isinstance(comments, list) or any(not isinstance(row, dict) for row in comments):
            raise ContractError("native Kanban comment readback is malformed")
        if any(marker in str(row.get("body", "")) for row in comments):
            return
        self._run(["comment", task_id, f"{marker}\n{body}"], json_output=False)
        comments = self._show(task_id).get("comments", [])
        if not isinstance(comments, list) or any(not isinstance(row, dict) for row in comments):
            raise ContractError("native Kanban post-comment readback is malformed")
        if not any(marker in str(row.get("body", "")) for row in comments):
            raise RuntimeError("native Kanban comment lacks durable readback proof")


class _StateOwner:
    protocol = "prometheus-incidents"

    def __init__(self, state_dir: Path, metrics_path: Path, kanban: Any, clock: Callable[[], int]):
        if not state_dir.is_absolute() or not metrics_path.is_absolute():
            raise ContractError("state and metrics paths must be absolute")
        self.state_dir = state_dir
        self.metrics_path = metrics_path
        self.kanban = kanban
        self.clock = clock
        self.store = GenerationStore(
            state_dir, protocol=self.protocol, schema_version=SCHEMA_VERSION
        )
        self.run_lock = state_dir.parent / f".{state_dir.name}-run-lock"
        self.state: dict[str, Any] = _new_state()

    def _load(self) -> dict[str, Any]:
        try:
            state = self.store.read().documents["state.json"]
        except StateAbsent:
            return _new_state()
        except (KeyError, ProtocolError) as exc:
            raise ContractError("durable reconciler state is unavailable or corrupt") from exc
        return _validate_state(state)

    def _save(self, state: dict[str, Any]) -> None:
        self.store.publish({"state.json": _validate_state(state)})
        self.store.cleanup(keep_previous=2)

    def _make_room(self) -> None:
        incidents = self.state["incidents"]
        if len(incidents) < MAX_RETAINED_INCIDENTS:
            return
        resolved = sorted(
            (
                (incident["resolved_at"] or 0, digest)
                for digest, incident in incidents.items()
                if incident["status"] == "resolved"
            )
        )
        while len(incidents) >= MAX_RETAINED_INCIDENTS and resolved:
            _, digest = resolved.pop(0)
            del incidents[digest]
        if len(incidents) >= MAX_RETAINED_INCIDENTS:
            raise ContractError("active incident state leaves no bounded capacity")

    def _metrics(self, state: dict[str, Any]) -> None:
        firing = sum(
            1 for incident in state["incidents"].values() if incident["status"] != "resolved"
        )
        counters = state["counters"]
        _write_atomic(
            self.metrics_path,
            render_metrics(
                {
                    "runs_total": counters["runs_total"],
                    "kanban_mutations_total": counters["kanban_mutations_total"],
                    "resolved_incidents_total": counters["resolved_incidents_total"],
                    "firing_incidents": firing,
                    "last_success_timestamp_seconds": state["last_success"],
                }
            ),
        )

    def _resolve(self, digest: str, incident: dict[str, Any], now: int) -> None:
        if incident["card_id"] is None:
            raise ContractError("cannot record recovery without a confirmed Kanban task")
        marker = incident["recovery_marker"] or f"Prometheus recovery: {digest}:{incident['epoch']}"
        incident["status"] = "resolving"
        incident["recovery_marker"] = marker
        self._save(self.state)
        self.kanban.ensure_comment(
            incident["card_id"],
            marker,
            "Prometheus no longer reports this exact actionable alert identity as firing. "
            "This is recovery evidence only; the assigned worker still owns diagnosis and completion.",
        )
        incident["status"] = "resolved"
        incident["resolved_at"] = now
        self.state["counters"]["kanban_mutations_total"] += 1
        self.state["counters"]["resolved_incidents_total"] += 1
        self._save(self.state)


class Reconciler(_StateOwner):
    protocol = "prometheus-incidents"

    def __init__(
        self, *, state_dir: Path, metrics_path: Path, prometheus: Any, kanban: Any, clock=time.time
    ):
        super().__init__(state_dir, metrics_path, kanban, lambda: int(clock()))
        self.prometheus = prometheus

    @staticmethod
    def _card(
        digest: str, identity: tuple[tuple[str, str], ...], epoch: int
    ) -> tuple[str, str, str]:
        labels = dict(identity)
        alertname = labels["alertname"]
        resource = (
            labels.get("host")
            or labels.get("service")
            or labels.get("instance")
            or labels.get("job")
            or "unknown"
        )
        title = f"Prometheus: {alertname} on {resource}"[:200]
        routing_key = f"prometheus:{digest}:{epoch}"
        body = "\n".join(
            [
                "Prometheus reports an actionable firing alert.",
                "",
                f"Logical incident key: {routing_key}",
                f"Stable identity SHA-256: {digest}",
                f"Identity labels: `{_canonical_json(identity)}`",
                "",
                (
                    "Use the canonical Prometheus API first. Diagnosis is read-only: systemd/journal/HTTP/SSH probes only. "
                    "Do not restart, rebuild, deploy, edit configuration, consume ntfy intake, or create another incident card. "
                    "A later recovery comment is evidence, not automatic proof that the root cause is fixed."
                ),
            ]
        )
        return routing_key, title, body

    def _ensure_card(
        self,
        digest: str,
        incident: dict[str, Any],
        identity: tuple[tuple[str, str], ...] | None = None,
    ) -> bool:
        if incident["card_id"] is not None:
            return False
        stable_identity = identity or tuple(tuple(pair) for pair in incident["identity"])
        routing_key, title, body = self._card(digest, stable_identity, incident["epoch"])
        incident["card_id"] = self.kanban.ensure_task(
            idempotency_key=routing_key,
            title=title,
            body=body,
        )
        self.state["counters"]["kanban_mutations_total"] += 1
        self._save(self.state)
        return True

    def run(self) -> dict[str, int]:
        with exclusive_lock(self.run_lock, timeout=1.0):
            rows = self.prometheus.firing_alerts()
            if not isinstance(rows, list) or len(rows) > MAX_ACTIVE_ALERTS:
                raise ContractError("active alert cardinality exceeds the reconciliation bound")
            current: dict[str, tuple[tuple[str, str], ...]] = {}
            for row in rows:
                digest, identity = alert_identity(row)
                if digest in current:
                    raise ContractError("Prometheus returned duplicate exact incident identities")
                current[digest] = identity

            self.state = self._load()
            now = self.clock()
            result = {"created": 0, "resolved": 0, "firing": len(current)}

            # Finish any recovery whose comment response was lost before using the
            # current snapshot. Idempotent readback prevents duplicate comments.
            for digest, incident in sorted(self.state["incidents"].items()):
                if incident["status"] == "resolving":
                    self._resolve(digest, incident, now)
                    result["resolved"] += 1

            for digest, identity in sorted(current.items()):
                incident = self.state["incidents"].get(digest)
                if incident is None:
                    self._make_room()
                    incident = {
                        "identity": [list(pair) for pair in identity],
                        "status": "firing",
                        "epoch": 1,
                        "card_id": None,
                        "first_seen": now,
                        "last_seen": now,
                        "resolved_at": None,
                        "recovery_marker": None,
                    }
                    self.state["incidents"][digest] = incident
                elif incident["status"] == "resolved":
                    incident.update(
                        status="firing",
                        epoch=incident["epoch"] + 1,
                        card_id=None,
                        first_seen=now,
                        last_seen=now,
                        resolved_at=None,
                        recovery_marker=None,
                    )
                else:
                    incident["last_seen"] = now

            # Persist the complete Prometheus snapshot once before any new
            # Kanban mutation. Per-mutation saves remain in _ensure_card/_resolve.
            self._save(self.state)
            for digest, identity in sorted(current.items()):
                incident = self.state["incidents"][digest]
                if self._ensure_card(digest, incident, identity):
                    result["created"] += 1

            for digest, incident in sorted(self.state["incidents"].items()):
                if digest not in current and incident["status"] == "firing":
                    if self._ensure_card(digest, incident):
                        result["created"] += 1
                    self._resolve(digest, incident, now)
                    result["resolved"] += 1

            self._prune()
            self.state["last_success"] = now
            self.state["counters"]["runs_total"] += 1
            self._save(self.state)
            self._metrics(self.state)
            return result

    def _prune(self) -> None:
        incidents = self.state["incidents"]
        if len(incidents) <= MAX_RETAINED_INCIDENTS:
            return
        resolved = sorted(
            (
                (incident["resolved_at"] or 0, digest)
                for digest, incident in incidents.items()
                if incident["status"] == "resolved"
            )
        )
        while len(incidents) > MAX_RETAINED_INCIDENTS and resolved:
            _, digest = resolved.pop(0)
            del incidents[digest]
        if len(incidents) > MAX_RETAINED_INCIDENTS:
            raise ContractError("active incident state exceeds the durable bound")


def run_external_watchdog(
    prometheus: PrometheusClient,
    metrics_path: Path,
    reconciler_max_age_seconds: int,
    *,
    clock=time.time,
) -> dict[str, Any]:
    if type(reconciler_max_age_seconds) is not int or not 60 <= reconciler_max_age_seconds <= 3600:
        raise ContractError("reconciler freshness threshold must be 60..3600 seconds")
    prometheus_ready = prometheus.ready()
    reconciler_recent = False
    if prometheus_ready:
        try:
            reconciler_recent = prometheus.reconciler_recent(reconciler_max_age_seconds)
        except (OSError, RuntimeError, ContractError):
            reconciler_recent = False
    healthy = prometheus_ready and reconciler_recent
    observed_at = int(clock())
    _write_atomic(
        metrics_path,
        "\n".join(
            [
                "# HELP hermes_prometheus_external_watchdog_healthy Whether the external watchdog can verify Prometheus and reconciler freshness.",
                "# TYPE hermes_prometheus_external_watchdog_healthy gauge",
                f"hermes_prometheus_external_watchdog_healthy {1 if healthy else 0}",
                "# HELP hermes_prometheus_external_watchdog_last_run_timestamp_seconds Last completed external watchdog run.",
                "# TYPE hermes_prometheus_external_watchdog_last_run_timestamp_seconds gauge",
                f"hermes_prometheus_external_watchdog_last_run_timestamp_seconds {observed_at}",
                "",
            ]
        ),
    )
    return {
        "healthy": healthy,
        "prometheus_ready": prometheus_ready,
        "reconciler_recent": reconciler_recent,
    }


def _common_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--metrics-file", required=True, type=Path)
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument("--prometheus-username-file", required=True, type=Path)
    parser.add_argument("--prometheus-password-file", required=True, type=Path)
    parser.add_argument("--hermes", default="hermes")
    parser.add_argument("--board", required=True)
    parser.add_argument("--assignee", default="system-admin")
    parser.add_argument("--tenant", required=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic Prometheus to native Hermes Kanban reconciliation"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    reconcile_parser = subparsers.add_parser("reconcile")
    _common_parser(reconcile_parser)
    external_watchdog_parser = subparsers.add_parser("external-watchdog")
    external_watchdog_parser.add_argument("--metrics-file", required=True, type=Path)
    external_watchdog_parser.add_argument("--prometheus-url", required=True)
    external_watchdog_parser.add_argument("--prometheus-username-file", required=True, type=Path)
    external_watchdog_parser.add_argument("--prometheus-password-file", required=True, type=Path)
    external_watchdog_parser.add_argument("--reconciler-max-age-seconds", required=True, type=int)
    args = parser.parse_args(argv)

    prometheus = PrometheusClient(
        args.prometheus_url, args.prometheus_username_file, args.prometheus_password_file
    )
    try:
        if args.command == "external-watchdog":
            result = run_external_watchdog(
                prometheus,
                args.metrics_file,
                args.reconciler_max_age_seconds,
            )
        elif args.command == "reconcile":
            kanban = KanbanCLI(args.hermes, args.board, args.assignee, args.tenant)
            result = Reconciler(
                state_dir=args.state_dir,
                metrics_path=args.metrics_file,
                prometheus=prometheus,
                kanban=kanban,
            ).run()
    except Exception as exc:  # noqa: BLE001 - fail closed at the CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(_canonical_json(result))
    return 1 if args.command == "external-watchdog" and not result["healthy"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
