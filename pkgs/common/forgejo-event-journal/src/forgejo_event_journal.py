from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from hermes_repository_instance import (
    instance,
    instance_argument,
    activate_instance,
    RepositoryStore,
)
from hermes_workflow_state import ProtocolError, StateAbsent, canonical_json_bytes

SCHEMA_VERSION = 2
PROTOCOL = "forgejo-event-journal"
DEFAULT_STATE_ROOT = Path("/var/lib/hermes-forgejo-event-journal")
MAX_API_BODY_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 10000
MAX_PENDING = 5000
MAX_PAGES = 100
MAX_SUMMARY = 500
POLL_OVERLAP = dt.timedelta(minutes=5)
EVENT_KINDS = {"issue", "comment", "pull_request", "review", "status"}
DISPOSITIONS = {"actionable", "evidence"}
EVENT_ID_RE = re.compile(r"^fj-[0-9a-f]{64}$")
SOURCE_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}:[0-9]+(?::[A-Za-z0-9._/-]{1,160})?$")
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")


class InputError(ValueError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise InputError(f"{label} must be a non-empty timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise InputError(f"{label} lacks a timezone")
    return parsed.astimezone(dt.timezone.utc)


def format_time(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def strict_json(raw: bytes, label: str = "JSON") -> Any:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            if key in result:
                raise InputError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise InputError(f"{label} contains non-finite number {value}")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except InputError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{label} is malformed") from exc


def exact_dict(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise InputError(f"{label} must contain exactly {sorted(keys)}")
    return value


def bounded_text(value: Any, limit: int = MAX_SUMMARY) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())[:limit]


def canonical_origin(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint:
        raise InputError("Forgejo endpoint is empty")
    if endpoint != endpoint.strip() or any(
        ord(char) < 0x21 or ord(char) == 0x7F for char in endpoint
    ):
        raise InputError("Forgejo endpoint contains whitespace or control characters")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not HOST_RE.fullmatch(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise InputError("Forgejo endpoint must be an exact HTTPS origin")
    if parsed.path not in {"", "/", "/api/v1"} or parsed.query or parsed.fragment:
        raise InputError("Forgejo endpoint must not contain an extra path, query, or fragment")
    netloc = parsed.hostname
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    origin = f"https://{netloc}"
    if endpoint not in {origin, origin + "/", origin + "/api/v1"}:
        raise InputError("Forgejo endpoint is not canonical")
    return origin


def repository_from_payload(payload: Mapping[str, Any]) -> str:
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        raise InputError("Forgejo payload lacks repository")
    full_name = repository.get("full_name") or repository.get("fullName")
    if full_name != instance().repository:
        raise InputError("repository is not allowlisted")
    return instance().repository


def canonical_target(origin: str, kind: str, number: int) -> str:
    if kind not in {"issue", "pull_request"} or type(number) is not int or number <= 0:
        raise InputError("target identity is invalid")
    segment = "issues" if kind == "issue" else "pulls"
    return f"{origin}/{instance().repository}/{segment}/{number}"


def stable_event_id(event: Mapping[str, Any]) -> str:
    identity = {
        "repository": event["repository"],
        "kind": event["kind"],
        "source_key": event["source_key"],
        "revision": event["revision"],
        "payload_digest": event["payload_digest"],
    }
    return "fj-" + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def validate_event(value: Any) -> dict[str, Any]:
    event = exact_dict(
        value,
        {
            "schema_version",
            "event_id",
            "repository",
            "kind",
            "source_key",
            "revision",
            "observed_at",
            "target",
            "disposition",
            "summary",
            "payload_digest",
            "sources",
        },
        "event",
    )
    if event["schema_version"] != SCHEMA_VERSION or event["repository"] != instance().repository:
        raise InputError("event schema or repository is invalid")
    if event["kind"] not in EVENT_KINDS or event["disposition"] not in DISPOSITIONS:
        raise InputError("event kind or disposition is invalid")
    if not isinstance(event["source_key"], str) or not SOURCE_KEY_RE.fullmatch(event["source_key"]):
        raise InputError("event source key is invalid")
    parse_time(event["revision"], "event revision")
    parse_time(event["observed_at"], "event observation")
    target = exact_dict(event["target"], {"kind", "number", "url"}, "event target")
    if (
        target["kind"] not in {"issue", "pull_request"}
        or type(target["number"]) is not int
        or target["number"] <= 0
    ):
        raise InputError("event target is invalid")
    parsed = urllib.parse.urlsplit(target["url"])
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise InputError("event target URL is not canonical")
    expected_suffix = f"/{instance().repository}/{'issues' if target['kind'] == 'issue' else 'pulls'}/{target['number']}"
    if (
        parsed.path != expected_suffix
        or f"{parsed.scheme}://{parsed.netloc}" != instance().forgejo_origin
    ):
        raise InputError("event target URL does not match its target")
    if not isinstance(event["summary"], str) or len(event["summary"]) > MAX_SUMMARY:
        raise InputError("event summary is invalid")
    if not isinstance(event["payload_digest"], str) or not HEX_SHA256_RE.fullmatch(
        event["payload_digest"]
    ):
        raise InputError("event payload digest is invalid")
    if (
        not isinstance(event["sources"], list)
        or not event["sources"]
        or event["sources"] != sorted(set(event["sources"]))
    ):
        raise InputError("event sources must be a sorted unique non-empty list")
    if not all(source == "poll" for source in event["sources"]):
        raise InputError("event source is unsupported")
    if event["event_id"] != stable_event_id(event):
        raise InputError("event id is not bound to its canonical identity")
    return event


def empty_documents() -> dict[str, Any]:
    return {
        "journal.json": {"schema_version": SCHEMA_VERSION, "events": []},
        "pending.json": {"schema_version": SCHEMA_VERSION, "event_ids": []},
        "receipts.json": {"schema_version": SCHEMA_VERSION, "receipts": []},
        "cursor.json": {
            "schema_version": SCHEMA_VERSION,
            "initial_poll_since": None,
            "last_completed_poll": None,
        },
        "health.json": {
            "schema_version": SCHEMA_VERSION,
            "last_poll_at": None,
            "last_error_at": None,
            "last_error": None,
            "poll_degraded": False,
        },
        "metrics.json": {
            "schema_version": SCHEMA_VERSION,
            "poll_runs_total": 0,
            "poll_failures_total": 0,
            "deduplicated_total": 0,
            "events_persisted_total": 0,
            "acknowledged_total": 0,
        },
    }


def validate_documents(documents: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "journal.json",
        "pending.json",
        "receipts.json",
        "cursor.json",
        "health.json",
        "metrics.json",
    }
    if not isinstance(documents, Mapping) or set(documents) != expected:
        raise InputError("journal generation has the wrong document set")
    journal = exact_dict(documents["journal.json"], {"schema_version", "events"}, "journal")
    pending = exact_dict(documents["pending.json"], {"schema_version", "event_ids"}, "pending")
    receipts = exact_dict(documents["receipts.json"], {"schema_version", "receipts"}, "receipts")
    cursor = exact_dict(
        documents["cursor.json"],
        {"schema_version", "initial_poll_since", "last_completed_poll"},
        "cursor",
    )
    health = exact_dict(
        documents["health.json"],
        {
            "schema_version",
            "last_poll_at",
            "last_error_at",
            "last_error",
            "poll_degraded",
        },
        "health",
    )
    metrics = exact_dict(
        documents["metrics.json"],
        {
            "schema_version",
            "poll_runs_total",
            "poll_failures_total",
            "deduplicated_total",
            "events_persisted_total",
            "acknowledged_total",
        },
        "metrics",
    )
    for document in (journal, pending, receipts, cursor, health, metrics):
        if document["schema_version"] != SCHEMA_VERSION:
            raise InputError("journal document schema is incompatible")
    if not isinstance(journal["events"], list) or len(journal["events"]) > MAX_EVENTS:
        raise InputError("journal events are invalid or unbounded")
    events = [validate_event(row) for row in journal["events"]]
    event_ids = [row["event_id"] for row in events]
    if event_ids != sorted(event_ids) or len(event_ids) != len(set(event_ids)):
        raise InputError("journal events must be sorted and unique")
    if not isinstance(pending["event_ids"], list) or len(pending["event_ids"]) > MAX_PENDING:
        raise InputError("pending event list is invalid or unbounded")
    if pending["event_ids"] != sorted(set(pending["event_ids"])) or not set(
        pending["event_ids"]
    ).issubset(event_ids):
        raise InputError("pending event ids are invalid")
    if not isinstance(receipts["receipts"], list):
        raise InputError("receipt list is invalid")
    receipt_ids: list[str] = []
    for row in receipts["receipts"]:
        row = exact_dict(row, {"event_id", "acknowledged_at", "proof_id"}, "receipt")
        if (
            row["event_id"] not in event_ids
            or not isinstance(row["proof_id"], str)
            or not row["proof_id"]
            or len(row["proof_id"]) > 256
        ):
            raise InputError("receipt is invalid")
        parse_time(row["acknowledged_at"], "receipt acknowledgement")
        receipt_ids.append(row["event_id"])
    if receipt_ids != sorted(receipt_ids) or len(receipt_ids) != len(set(receipt_ids)):
        raise InputError("receipts must be sorted and unique")
    if set(pending["event_ids"]) & set(receipt_ids):
        raise InputError("an event cannot be both pending and acknowledged")
    for key in ("initial_poll_since", "last_completed_poll"):
        if cursor[key] is not None:
            parse_time(cursor[key], key)
    for key in ("last_poll_at", "last_error_at"):
        if health[key] is not None:
            parse_time(health[key], key)
    if health["last_error"] is not None and (
        not isinstance(health["last_error"], str) or len(health["last_error"]) > 500
    ):
        raise InputError("health error is invalid")
    if type(health["poll_degraded"]) is not bool:
        raise InputError("poll degraded state must be boolean")
    for key, value in metrics.items():
        if key != "schema_version" and (type(value) is not int or value < 0):
            raise InputError("metric counters must be non-negative integers")
    return {name: documents[name] for name in sorted(documents)}


def latest_issue_events(events: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in events:
        if row["kind"] == "issue":
            previous = latest.get(row["source_key"])
            if previous is None or parse_time(row["revision"], "revision") > parse_time(
                previous["revision"], "revision"
            ):
                latest[row["source_key"]] = row
    return latest


def compact_acknowledged(documents: dict[str, Any], completed_at: str | None) -> None:
    if completed_at is None:
        return
    cutoff = parse_time(completed_at, "last completed poll") - POLL_OVERLAP
    pending = set(documents["pending.json"]["event_ids"])
    acknowledged = {row["event_id"] for row in documents["receipts.json"]["receipts"]}
    # An issue's updated_at also advances for comments and automation replies.
    # Keep its latest semantic observation in the existing bounded journal,
    # rather than losing deduplication whenever the polling overlap expires.
    latest = {
        row["event_id"] for row in latest_issue_events(documents["journal.json"]["events"]).values()
    }
    removable = {
        row["event_id"]
        for row in documents["journal.json"]["events"]
        if row["event_id"] in acknowledged
        and row["event_id"] not in pending
        and row["event_id"] not in latest
        and parse_time(row["revision"], "event revision") < cutoff
    }
    if not removable:
        return
    documents["journal.json"]["events"] = [
        row for row in documents["journal.json"]["events"] if row["event_id"] not in removable
    ]
    documents["receipts.json"]["receipts"] = [
        row for row in documents["receipts.json"]["receipts"] if row["event_id"] not in removable
    ]


def validate_legacy_documents(documents):
    validated = validate_documents(documents)
    if not validated["journal.json"]["events"]:
        raise InputError("legacy journal lacks source identity evidence")
    return validated


class Journal:
    def __init__(self, root: Path, *, fault: Callable[[str], None] | None = None) -> None:
        self.store = RepositoryStore(
            root,
            protocol=PROTOCOL,
            schema_version=SCHEMA_VERSION,
            fault=fault,
            legacy_validator=validate_legacy_documents,
        )

    def read(self) -> dict[str, Any]:
        try:
            return validate_documents(self.store.read().documents)
        except StateAbsent:
            return empty_documents()

    def initialize(self) -> None:
        """Publish the empty selector before accepting any external event."""
        root = self.store.root
        selector = root / "current.json"
        if selector.exists() or selector.is_symlink():
            self.store.read()
            self.store.cleanup(keep_previous=1)
            return
        if root.exists() or root.is_symlink():
            if root.is_symlink() or not root.is_dir():
                raise ProtocolError("state root is unsafe")
            unexpected = {entry.name for entry in root.iterdir()} - {".lock"}
            if unexpected:
                raise ProtocolError("protocol selector is missing from non-empty state")
        self.store.publish(validate_documents(empty_documents()))

    def update(self, transform: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        self.initialize()
        try:
            result = self.store.update(
                lambda selected: validate_documents(
                    transform(
                        validate_documents(selected.documents) if selected else empty_documents()
                    )
                )
            )
        finally:
            self.store.cleanup(keep_previous=1)
        return validate_documents(result.documents)

    def ingest(
        self, events: Iterable[dict[str, Any]], *, source: str, observed_at: str | None = None
    ) -> dict[str, int]:
        if source != "poll":
            raise InputError("ingest source is invalid")
        incoming = [validate_event(row) for row in events]
        if len(incoming) > MAX_PENDING:
            raise InputError("ingest batch is too large")
        now_value = observed_at or utc_now()
        parse_time(now_value, "ingest time")
        result: dict[str, int] = {}

        def apply(documents: dict[str, Any]) -> dict[str, Any]:
            compact_acknowledged(documents, documents["cursor.json"]["last_completed_poll"])
            journal = {row["event_id"]: row for row in documents["journal.json"]["events"]}
            pending = set(documents["pending.json"]["event_ids"])
            receipts = {row["event_id"] for row in documents["receipts.json"]["receipts"]}
            persisted = deduplicated = 0
            latest = latest_issue_events(journal.values())
            for row in sorted(incoming, key=lambda row: parse_time(row["revision"], "revision")):
                current = journal.get(row["event_id"])
                if current is not None:
                    merged_sources = sorted(set(current["sources"]) | set(row["sources"]))
                    if (
                        current["payload_digest"] != row["payload_digest"]
                        or current["target"] != row["target"]
                    ):
                        raise InputError("canonical event identity collided with different content")
                    if merged_sources != current["sources"]:
                        current = dict(current)
                        current["sources"] = merged_sources
                        journal[row["event_id"]] = validate_event(current)
                    deduplicated += 1
                    continue
                previous = latest.get(row["source_key"]) if row["kind"] == "issue" else None
                if previous is not None and previous["payload_digest"] == row["payload_digest"]:
                    deduplicated += 1
                    continue
                if len(journal) >= MAX_EVENTS or len(pending) >= MAX_PENDING:
                    raise InputError("journal retention or pending bound reached")
                journal[row["event_id"]] = row
                if row["kind"] == "issue":
                    latest[row["source_key"]] = row
                if row["event_id"] not in receipts:
                    pending.add(row["event_id"])
                persisted += 1
            documents["journal.json"]["events"] = [journal[key] for key in sorted(journal)]
            documents["pending.json"]["event_ids"] = sorted(pending)
            metrics = documents["metrics.json"]
            metrics["events_persisted_total"] += persisted
            metrics["deduplicated_total"] += deduplicated
            result.update(persisted=persisted, deduplicated=deduplicated, pending=len(pending))
            return documents

        self.update(apply)
        return result

    def complete_poll(
        self, events: Iterable[dict[str, Any]], *, completed_at: str | None = None
    ) -> dict[str, int]:
        incoming = [validate_event(row) for row in events]
        when = completed_at or utc_now()
        result = self.ingest(incoming, source="poll", observed_at=when)

        def apply(documents: dict[str, Any]) -> dict[str, Any]:
            documents["cursor.json"]["last_completed_poll"] = when
            documents["health.json"]["last_poll_at"] = when
            documents["health.json"]["poll_degraded"] = False
            documents["metrics.json"]["poll_runs_total"] += 1
            compact_acknowledged(documents, when)
            return documents

        self.update(apply)
        return result

    def bind_initial_poll_since(self, requested: str) -> str:
        if requested == "service-start":
            candidate = utc_now()
        else:
            candidate = format_time(parse_time(requested, "initial since"))
        selected = candidate

        def apply(documents: dict[str, Any]) -> dict[str, Any]:
            nonlocal selected
            current = documents["cursor.json"]["initial_poll_since"]
            if current is None:
                documents["cursor.json"]["initial_poll_since"] = candidate
            else:
                selected = current
            return documents

        self.update(apply)
        return selected

    def mark_failure(self, message: str, *, source: str, when: str | None = None) -> None:
        when_value = when or utc_now()
        text = bounded_text(message)

        def apply(documents: dict[str, Any]) -> dict[str, Any]:
            documents["health.json"]["last_error_at"] = when_value
            documents["health.json"]["last_error"] = text
            if source != "poll":
                raise InputError("failure source is invalid")
            documents["metrics.json"]["poll_failures_total"] += 1
            documents["health.json"]["poll_degraded"] = True
            return documents

        self.update(apply)

    def acknowledge(
        self, event_id: str, proof_id: str, *, acknowledged_at: str | None = None
    ) -> bool:
        if not EVENT_ID_RE.fullmatch(event_id or ""):
            raise InputError("acknowledgement event id is invalid")
        if not isinstance(proof_id, str) or not proof_id.strip() or len(proof_id) > 256:
            raise InputError("acknowledgement proof id is invalid")
        when = acknowledged_at or utc_now()
        changed = False

        def apply(documents: dict[str, Any]) -> dict[str, Any]:
            nonlocal changed
            event_ids = {row["event_id"] for row in documents["journal.json"]["events"]}
            if event_id not in event_ids:
                raise InputError("acknowledgement references an unknown event")
            receipts = {row["event_id"]: row for row in documents["receipts.json"]["receipts"]}
            expected = {"event_id": event_id, "acknowledged_at": when, "proof_id": proof_id.strip()}
            if event_id in receipts:
                if receipts[event_id]["proof_id"] != proof_id.strip():
                    raise InputError("event is already acknowledged with different proof")
                return documents
            receipts[event_id] = expected
            documents["receipts.json"]["receipts"] = [receipts[key] for key in sorted(receipts)]
            documents["pending.json"]["event_ids"] = [
                row for row in documents["pending.json"]["event_ids"] if row != event_id
            ]
            documents["metrics.json"]["acknowledged_total"] += 1
            changed = True
            return documents

        self.update(apply)
        return changed

    def pending(self, disposition: str | None = None) -> list[dict[str, Any]]:
        if disposition is not None and disposition not in DISPOSITIONS:
            raise InputError("pending disposition is invalid")
        documents = self.read()
        pending = set(documents["pending.json"]["event_ids"])
        return [
            row
            for row in documents["journal.json"]["events"]
            if row["event_id"] in pending
            and (disposition is None or row["disposition"] == disposition)
        ]


def make_event(
    *,
    origin: str,
    kind: str,
    source_key: str,
    revision: str,
    target_kind: str,
    target_number: int,
    disposition: str,
    summary: str,
    semantic: Mapping[str, Any],
    source: str,
    observed_at: str | None = None,
) -> dict[str, Any]:
    parse_time(revision, "source revision")
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": "",
        "repository": instance().repository,
        "kind": kind,
        "source_key": source_key,
        "revision": revision,
        "observed_at": observed_at or utc_now(),
        "target": {
            "kind": target_kind,
            "number": target_number,
            "url": canonical_target(origin, target_kind, target_number),
        },
        "disposition": disposition,
        "summary": bounded_text(summary),
        # Hash normalized source identity, not the transport-specific envelope:
        # repeated polling representations of the same Forgejo transition
        # intentionally converge to one event.
        "payload_digest": hashlib.sha256(canonical_json_bytes(semantic)).hexdigest(),
        "sources": [source],
    }
    event["event_id"] = stable_event_id(event)
    return validate_event(event)


def issue_semantic(issue: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "number": int(issue["number"]),
        "state": str(issue.get("state") or "unknown"),
        "title": bounded_text(issue.get("title")),
        "body": str(issue.get("body") or ""),
        "labels": sorted(label["name"] for label in issue.get("labels", [])),
    }


def comment_semantic(comment: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": int(comment["id"]), "body": str(comment.get("body") or "")}


def pull_semantic(pull: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "number": int(pull["number"]),
        "state": str(pull.get("state") or "unknown"),
        "head": str((pull.get("head") or {}).get("sha") or ""),
        "merged": bool(pull.get("merged")),
    }


def review_semantic(review: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(review["id"]),
        "state": str(review.get("state") or "unknown").upper(),
        "commit_id": str(review.get("commit_id") or ""),
        "body": str(review.get("body") or ""),
    }


def status_semantic(status: Mapping[str, Any], head: str) -> dict[str, Any]:
    return {
        "id": int(status["id"]),
        "head": head,
        "context": str(status.get("context") or "unknown"),
        "status": str(status.get("status") or status.get("state") or "unknown"),
    }


def comment_disposition(body: str, author: str) -> str:
    if not body.strip():
        return "evidence"
    if author in instance().automation_authors:
        # Source-author identity comes from authenticated Forgejo readback, not
        # the marker itself. Keep the legacy receipt format during cutover so
        # already-published acknowledgements cannot recursively create work.
        if f"{instance().namespace}-forgejo-event:" in body or re.search(
            rf"<!-- hermes:handled-comment repo={re.escape(instance().repository)} "
            r"source=(?:comment|review-comment|review):[1-9][0-9]* "
            r"card=t_[0-9a-f]+ -->",
            body,
        ):
            return "evidence"
    return "actionable"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class ForgejoClient:
    def __init__(self, endpoint: str, credential_file: Path) -> None:
        self.origin = canonical_origin(endpoint)
        instance().check_origin(self.origin, "forgejo")
        mode = credential_file.lstat().st_mode if credential_file.exists() else None
        if mode is None or stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise InputError("Forgejo credential file is unavailable or unsafe")
        self.token = credential_file.read_text(encoding="utf-8").strip()
        if not self.token:
            raise InputError("Forgejo credential file is empty")
        # Authenticated API requests are exact-origin. A redirect is a protocol
        # failure and must never receive the repository credential.
        self._opener = urllib.request.build_opener(NoRedirect()).open

    def get(self, path: str, query: Mapping[str, Any] | None = None) -> Any:
        if (
            not path.startswith(f"/repos/{instance().repository}/")
            and path != f"/repos/{instance().repository}"
        ):
            raise InputError("Forgejo API path escapes the repository allowlist")
        url = self.origin + "/api/v1" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url, headers={"Authorization": "token " + self.token, "Accept": "application/json"}
        )
        try:
            with self._opener(request, timeout=30) as response:
                raw = response.read(MAX_API_BODY_BYTES + 1)
                if len(raw) > MAX_API_BODY_BYTES:
                    raise RuntimeError("Forgejo response exceeds the bounded body limit")
                return strict_json(raw, "Forgejo response")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Forgejo GET failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Forgejo GET failed: {exc.reason}") from exc

    def pages(
        self, path: str, query: Mapping[str, Any] | None = None, limit: int = 50
    ) -> Iterable[dict[str, Any]]:
        for page in range(1, MAX_PAGES + 1):
            parameters = dict(query or {})
            parameters.update(page=page, limit=limit)
            rows = self.get(path, parameters)
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise RuntimeError("paginated Forgejo response is not a list of objects")
            yield from rows
            if len(rows) < limit:
                return
        raise RuntimeError(f"Forgejo pagination exceeded its {MAX_PAGES}-page hard bound")


def poll_events(
    client: ForgejoClient, since: dt.datetime, *, observed_at: str | None = None
) -> list[dict[str, Any]]:
    origin = client.origin
    events: dict[str, dict[str, Any]] = {}
    root = f"/repos/{instance().repository}"

    def recent(value: Any) -> bool:
        return parse_time(value, "source revision") >= since

    since_text = format_time(since)
    for issue in client.pages(
        root + "/issues", {"state": "all", "type": "issues", "since": since_text}
    ):
        revision = issue.get("updated_at") or issue.get("created_at")
        if not recent(revision):
            continue
        number = int(issue["number"])
        event = make_event(
            origin=origin,
            kind="issue",
            source_key=f"issue:{number}",
            revision=str(revision),
            target_kind="issue",
            target_number=number,
            disposition=(
                "actionable"
                if issue.get("state") == "open"
                and not issue.get("pull_request")
                and "human-only" not in issue_semantic(issue)["labels"]
                else "evidence"
            ),
            summary=f"Issue #{number} {issue.get('state')}",
            semantic=issue_semantic(issue),
            source="poll",
            observed_at=observed_at,
        )
        events[event["event_id"]] = event

    for comment in client.pages(root + "/issues/comments", {"since": since_text}):
        revision = comment.get("updated_at") or comment.get("created_at")
        if not recent(revision):
            continue
        target_url = str(comment.get("issue_url") or comment.get("pull_request_url") or "")
        try:
            number = int(target_url.rstrip("/").rsplit("/", 1)[-1])
            comment_id = int(comment["id"])
        except (KeyError, ValueError) as exc:
            raise InputError("polled comment lacks canonical identity") from exc
        body = str(comment.get("body") or "")
        author = str((comment.get("user") or {}).get("login") or "")
        disposition = comment_disposition(body, author)
        html_url = str(comment.get("html_url") or "")
        target_kind = "pull_request" if "/pulls/" in html_url else "issue"
        event = make_event(
            origin=origin,
            kind="comment",
            source_key=f"comment:{comment_id}",
            revision=str(revision),
            target_kind=target_kind,
            target_number=number,
            disposition=disposition,
            summary=f"Comment {comment_id} on #{number}",
            semantic=comment_semantic(comment),
            source="poll",
            observed_at=observed_at,
        )
        events[event["event_id"]] = event

    for pull in client.pages(root + "/pulls", {"state": "all", "sort": "recentupdate"}):
        revision = pull.get("updated_at") or pull.get("created_at")
        is_open = pull.get("state") == "open"
        if not recent(revision) and not is_open:
            continue
        number = int(pull["number"])
        head = str((pull.get("head") or {}).get("sha") or "")
        if recent(revision):
            event = make_event(
                origin=origin,
                kind="pull_request",
                source_key=f"pull:{number}",
                revision=str(revision),
                target_kind="pull_request",
                target_number=number,
                disposition="evidence",
                summary=f"PR #{number} {pull.get('state')} head {head[:12]}",
                semantic=pull_semantic(pull),
                source="poll",
                observed_at=observed_at,
            )
            events[event["event_id"]] = event
        for review in client.pages(root + f"/pulls/{number}/reviews"):
            review_id = int(review["id"])
            for comment in client.pages(root + f"/pulls/{number}/reviews/{review_id}/comments"):
                comment_revision = comment.get("updated_at") or comment.get("created_at")
                if not recent(comment_revision):
                    continue
                body = str(comment.get("body") or "")
                author = str((comment.get("user") or {}).get("login") or "")
                inline = make_event(
                    origin=origin,
                    kind="comment",
                    source_key=f"review-comment:{int(comment['id'])}",
                    revision=str(comment_revision),
                    target_kind="pull_request",
                    target_number=number,
                    disposition=comment_disposition(body, author),
                    summary=f"Inline review comment {comment['id']} on PR #{number}",
                    semantic=comment_semantic(comment),
                    source="poll",
                    observed_at=observed_at,
                )
                events[inline["event_id"]] = inline
            review_revision = review.get("submitted_at") or review.get("created_at")
            if not recent(review_revision):
                continue
            review_id = int(review["id"])
            state = bounded_text(review.get("state"), 64).upper()
            disposition = (
                "actionable"
                if state in {"REQUEST_CHANGES", "REQUESTED_CHANGES"}
                or (
                    state == "COMMENT"
                    and comment_disposition(
                        str(review.get("body") or ""),
                        str((review.get("user") or {}).get("login") or ""),
                    )
                    == "actionable"
                )
                else "evidence"
            )
            event = make_event(
                origin=origin,
                kind="review",
                source_key=f"review:{review_id}",
                revision=str(review_revision),
                target_kind="pull_request",
                target_number=number,
                disposition=disposition,
                summary=f"Review {review_id} on PR #{number}: {state}",
                semantic=review_semantic(review),
                source="poll",
                observed_at=observed_at,
            )
            events[event["event_id"]] = event
        if COMMIT_RE.fullmatch(head):
            for status in client.pages(root + f"/commits/{head}/statuses"):
                status_revision = status.get("updated_at") or status.get("created_at")
                if not recent(status_revision):
                    continue
                status_id = int(status.get("id") or 0)
                if status_id <= 0:
                    raise InputError("polled status lacks stable identity")
                event = make_event(
                    origin=origin,
                    kind="status",
                    source_key=f"status:{status_id}:{head}",
                    revision=str(status_revision),
                    target_kind="pull_request",
                    target_number=number,
                    disposition="evidence",
                    summary=f"PR #{number} status {status.get('context')}: {status.get('status') or status.get('state')}",
                    semantic=status_semantic(status, head),
                    source="poll",
                    observed_at=observed_at,
                )
                events[event["event_id"]] = event
    return [events[key] for key in sorted(events)]


def prometheus_metrics(documents: Mapping[str, Any]) -> str:
    metrics = documents["metrics.json"]
    pending_ids = set(documents["pending.json"]["event_ids"])
    events = documents["journal.json"]["events"]
    actionable = sum(
        row["event_id"] in pending_ids and row["disposition"] == "actionable" for row in events
    )
    evidence = sum(
        row["event_id"] in pending_ids and row["disposition"] == "evidence" for row in events
    )
    poll_degraded = 1 if documents["health.json"]["poll_degraded"] else 0
    lines = [
        "# TYPE hermes_forgejo_event_journal_pending gauge",
        f'hermes_forgejo_event_journal_pending{{disposition="actionable"}} {actionable}',
        f'hermes_forgejo_event_journal_pending{{disposition="evidence"}} {evidence}',
        "# TYPE hermes_forgejo_event_journal_poll_degraded gauge",
        f"hermes_forgejo_event_journal_poll_degraded {poll_degraded}",
    ]
    for name in sorted(key for key in metrics if key != "schema_version"):
        lines.extend(
            [
                f"# TYPE hermes_forgejo_event_journal_{name} counter",
                f"hermes_forgejo_event_journal_{name} {metrics[name]}",
            ]
        )
    return "\n".join(lines) + "\n"


def command_poll(args: argparse.Namespace) -> int:
    instance().check_origin(canonical_origin(args.endpoint), "forgejo")
    journal = Journal(args.state_root)
    journal.initialize()
    try:
        documents = journal.read()
        cursor = documents["cursor.json"]["last_completed_poll"]
        since = (
            parse_time(cursor, "poll cursor") - POLL_OVERLAP
            if cursor
            else parse_time(journal.bind_initial_poll_since(args.initial_since), "initial since")
        )
        completed_at = utc_now()
        client = ForgejoClient(args.endpoint, args.credential_file)
        events = poll_events(client, since, observed_at=completed_at)
        result = journal.complete_poll(events, completed_at=completed_at)
        print(json.dumps({"events": len(events), **result}, sort_keys=True))
        return 0
    except Exception as exc:
        try:
            journal.mark_failure(str(exc), source="poll")
        except (InputError, OSError, ProtocolError, ValueError) as health_exc:
            sys.stderr.write(f"poll: unable to persist degraded state: {health_exc}\n")
        raise


def command_pending(args: argparse.Namespace) -> int:
    rows = Journal(args.state_root).pending(args.disposition)
    print(json.dumps({"schema_version": SCHEMA_VERSION, "events": rows}, sort_keys=True))
    return 0


def command_ack(args: argparse.Namespace) -> int:
    changed = Journal(args.state_root).acknowledge(args.event_id, args.proof_id)
    print(json.dumps({"event_id": args.event_id, "changed": changed}, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="forgejo-event-journal")
    instance_argument(root)
    sub = root.add_subparsers(dest="command", required=True)
    poll = sub.add_parser("poll")
    poll.add_argument("--endpoint", required=True)
    poll.add_argument("--credential-file", type=Path, required=True)
    poll.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    poll.add_argument("--initial-since", required=True)
    poll.set_defaults(func=command_poll)
    pending = sub.add_parser("pending")
    pending.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    pending.add_argument("--disposition", choices=sorted(DISPOSITIONS))
    pending.set_defaults(func=command_pending)
    ack = sub.add_parser("ack")
    ack.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    ack.add_argument("--event-id", required=True)
    ack.add_argument("--proof-id", required=True)
    ack.set_defaults(func=command_ack)
    return root


def main() -> int:
    args = parser().parse_args()
    activate_instance(args.instance_config)
    if hasattr(args, "state_root") and not args.state_root.is_absolute():
        raise SystemExit("state root must be absolute")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
