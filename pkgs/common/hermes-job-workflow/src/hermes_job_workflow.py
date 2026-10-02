"""Deterministic vacancy collection, lifecycle, and active-research gate.

Collectors and passive ingestion never use a model. Active research receives only
one bounded, deterministic claim. CV material is deliberately confined to the
in-memory ranking envelope and is never accepted by collector or durable-state
schemas.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from hermes_workflow_state import GenerationStore, ProtocolError, StateAbsent, canonical_json_bytes


class JobError(ProtocolError):
    """Job data or a lifecycle transition violates the managed contract."""


RAW_KEYS = {
    "source_kind",
    "source_name",
    "source_url",
    "source_namespace",
    "source_service",
    "requisition_id",
    "aliases",
    "canonical_url",
    "employer",
    "title",
    "location",
    "description",
    "published_at",
    "observed_at",
    "source_state",
    "official",
    "provenance",
}
PROVENANCE_KEYS = {"url", "observed_at", "kind"}
JOB_STATE_KEYS = {
    "job_id",
    "status",
    "record",
    "first_seen_at",
    "last_seen_at",
    "next_revalidate_at",
    "closed_at",
    "last_error",
}
TOMBSTONE_KEYS = {"job_id", "retired_at"}
PREFERENCE_KEYS = {
    "enabled",
    "active_research_enabled",
    "active_cadence_minutes",
    "max_active_candidates",
    "revalidate_after_minutes",
    "closed_retention_days",
    "projection",
}
SOURCE_KINDS = {"feed", "ats", "employer", "active"}
SOURCE_STATES = {"active", "closed", "unknown"}
ERROR_OUTCOMES = {"timeout", "error", "bot_protection"}
MAX_RECORDS = 500
MAX_PAGES = 20
MAX_CV_BYTES = 2 * 1024 * 1024


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise JobError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _text(value: Any, label: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty) or len(value) > maximum:
        raise JobError(f"{label} must be bounded text")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise JobError(f"{label} contains forbidden controls")
    return value


def parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise JobError(f"{label} must be canonical UTC time")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise JobError(f"{label} is malformed") from exc
    if parsed.tzinfo != dt.timezone.utc or parsed.microsecond:
        raise JobError(f"{label} must use UTC whole seconds")
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise JobError(f"{label} is not canonical")
    return parsed


def format_time(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_url(value: Any, label: str = "URL") -> str:
    _text(value, label, 2048)
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise JobError(f"{label} contains whitespace or controls")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise JobError(f"{label} authority is malformed") from exc
    if not parts.hostname or parts.username or parts.password or parts.fragment:
        raise JobError(f"{label} credentials, fragments, and missing hosts are forbidden")
    host = parts.hostname
    if host != host.lower():
        raise JobError(f"{label} host must be lowercase")
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise JobError(f"{label} host must be canonical ASCII") from exc
    loopback = host in {"127.0.0.1", "::1", "localhost"}
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise JobError(f"{label} must use HTTPS")
    if (parts.scheme, port) in {("https", 443), ("http", 80)}:
        raise JobError(f"{label} must omit default ports")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    if parts.netloc != authority:
        raise JobError(f"{label} authority is not canonical")
    path = parts.path or "/"
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise JobError(f"{label} dot segments are forbidden")
    for match in re.finditer(r"%([0-9A-Fa-f]{2})", path + "?" + parts.query):
        encoded = match.group(1)
        decoded = chr(int(encoded, 16))
        if (
            encoded != encoded.upper()
            or decoded in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
        ):
            raise JobError(f"{label} percent encoding is not canonical")
    result = urlunsplit((parts.scheme, authority, path, parts.query, ""))
    if result != value:
        raise JobError(f"{label} must be exact canonical text")
    return result


def _requisition_identity(row: Mapping[str, Any]) -> str:
    return "/".join(
        (row["source_namespace"], row["source_service"], row["employer"], row["requisition_id"])
    )


def _job_identity(identity: str) -> str:
    return "job-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _provenance(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise JobError("provenance must be a bounded non-empty list")
    result: dict[tuple[str, str, str], dict[str, str]] = {}
    for raw in value:
        row = _exact(raw, PROVENANCE_KEYS, "provenance row")
        canonical_url(row["url"], "provenance URL")
        parse_time(row["observed_at"], "provenance observed_at")
        if row["kind"] not in SOURCE_KINDS:
            raise JobError("provenance kind is unsupported")
        result[(row["url"], row["observed_at"], row["kind"])] = dict(row)
    return [result[key] for key in sorted(result)]


def normalize_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and set(value) == RAW_KEYS | {"job_id"}:
        claimed = value["job_id"]
        value = {key: value[key] for key in RAW_KEYS}
    else:
        claimed = None
    row = _exact(value, RAW_KEYS, "job record")
    if row["source_kind"] not in SOURCE_KINDS:
        raise JobError("source_kind is unsupported")
    _text(row["source_name"], "source_name", 200)
    canonical_url(row["source_url"], "source_url")
    namespace = _text(row["source_namespace"], "source_namespace", 100)
    if re.fullmatch(r"[a-z][a-z0-9.-]{0,31}:[a-z0-9][a-z0-9.-]{0,63}", namespace) is None:
        raise JobError("source_namespace must be namespaced source/service text")
    _text(row["source_service"], "source_service", 200)
    _text(row["requisition_id"], "requisition_id", 200)
    url = canonical_url(row["canonical_url"], "canonical_url")
    _text(row["employer"], "employer", 300)
    _text(row["title"], "title", 500)
    _text(row["location"], "location", 500, empty=True)
    _text(row["description"], "description", 20000, empty=True)
    if row["published_at"] is not None:
        parse_time(row["published_at"], "published_at")
    parse_time(row["observed_at"], "observed_at")
    if row["source_state"] not in SOURCE_STATES:
        raise JobError("source_state is unsupported")
    if type(row["official"]) is not bool:
        raise JobError("official must be boolean and is never inferred")
    if row["source_state"] == "closed" and not row["official"]:
        raise JobError("only an official source may assert closed")
    normalized = dict(row)
    normalized["canonical_url"] = url
    normalized["provenance"] = _provenance(row["provenance"])
    aliases = row["aliases"]
    if not isinstance(aliases, list) or len(aliases) > 16 or len(aliases) != len(set(aliases)):
        raise JobError("aliases must be a bounded unique explicit list")
    normalized["aliases"] = sorted(_text(alias, "requisition alias", 700) for alias in aliases)
    primary_identity = _requisition_identity(normalized)
    if primary_identity in normalized["aliases"]:
        raise JobError("requisition aliases must not repeat the primary identity")
    identity = normalized["aliases"][0] if normalized["aliases"] else primary_identity
    normalized["job_id"] = _job_identity(identity)
    if claimed is not None and claimed != normalized["job_id"]:
        raise JobError("job_id does not match requisition identity")
    return normalized


def _precedence(row: Mapping[str, Any]) -> tuple[int, int]:
    kind_rank = {"feed": 0, "active": 1, "ats": 2, "employer": 3}[row["source_kind"]]
    return (1 if row["official"] else 0, kind_rank)


def _revision(row: Mapping[str, Any]) -> bytes:
    ignored = {"provenance", "observed_at", "source_kind", "source_name", "source_url", "official"}
    return canonical_json_bytes({key: value for key, value in row.items() if key not in ignored})


def normalize_records(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > MAX_RECORDS:
        raise JobError(f"job input must contain at most {MAX_RECORDS} records")
    selected: dict[str, dict[str, Any]] = {}
    identity_owner: dict[str, str] = {}
    for row in (normalize_record(raw) for raw in values):
        identities = {_requisition_identity(row), *row["aliases"]}
        owners = {identity_owner[item] for item in identities if item in identity_owner}
        if len(owners) > 1:
            raise JobError("explicit aliases join conflicting requisition identities")
        selected_id = next(iter(owners), row["job_id"])
        prior = selected.get(selected_id)
        if prior is None:
            selected[selected_id] = row
            for item in identities:
                identity_owner[item] = selected_id
            continue
        if _precedence(row) == _precedence(prior) and _revision(row) != _revision(prior):
            raise JobError("conflicting equal-precedence revisions for one job")
        winner, loser = (row, prior) if _precedence(row) > _precedence(prior) else (prior, row)
        merged = dict(winner)
        merged["provenance"] = _provenance([*winner["provenance"], *loser["provenance"]])
        merged["job_id"] = selected_id
        merged["aliases"] = sorted(set(winner["aliases"]) | set(loser["aliases"]))
        selected[selected_id] = merged
        for item in identities | {_requisition_identity(prior)} | set(prior["aliases"]):
            identity_owner[item] = selected_id
    return [selected[key] for key in sorted(selected)]


def _iso_from_milliseconds(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) not in {int, float} or value < 0:
        raise JobError("epoch milliseconds are malformed")
    return format_time(dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc))


def collect_feed_entries(
    values: Any,
    *,
    source_name: str,
    source_url: str,
    observed_at: str,
) -> list[dict[str, Any]]:
    """Map bounded job-feed entries without promoting them to official evidence."""
    if not isinstance(values, list) or len(values) > MAX_RECORDS:
        raise JobError("feed collector record bound exceeded")
    _text(source_name, "source_name", 200)
    canonical_url(source_url, "source_url")
    parse_time(observed_at, "observed_at")
    expected = {"id", "url", "employer", "title", "location", "description", "published_at"}
    rows = []
    for value in values:
        item = _exact(value, expected, "feed job")
        rows.append(
            {
                "source_kind": "feed",
                "source_name": source_name,
                "source_url": source_url,
                "source_namespace": "feed:generic",
                "source_service": urlsplit(source_url).hostname,
                "requisition_id": str(item["id"]),
                "aliases": [],
                "canonical_url": item["url"],
                "employer": item["employer"],
                "title": item["title"],
                "location": item["location"],
                "description": item["description"],
                "published_at": item["published_at"],
                "observed_at": observed_at,
                "source_state": "active",
                "official": False,
                "provenance": [{"url": item["url"], "observed_at": observed_at, "kind": "feed"}],
            }
        )
    return normalize_records(rows)


def collect_miniflux_entries(values: Any, *, observed_at: str) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > 100:
        raise JobError("Miniflux job entry bound exceeded")
    expected = {
        "content",
        "content_truncated",
        "feed_id",
        "feed_title",
        "feed_url",
        "id",
        "published_at",
        "site_url",
        "title",
        "url",
    }
    rows: list[dict[str, Any]] = []
    for value in values:
        item = _exact(value, expected, "Miniflux job entry")
        if type(item["content_truncated"]) is not bool:
            raise JobError("Miniflux content_truncated must be boolean")
        rows.extend(
            collect_feed_entries(
                [
                    {
                        "description": item["content"],
                        "employer": item["feed_title"],
                        "id": item["id"],
                        "location": "",
                        "published_at": item["published_at"],
                        "title": item["title"],
                        "url": item["url"],
                    }
                ],
                source_name=item["feed_title"],
                source_url=item["site_url"],
                observed_at=observed_at,
            )
        )
    return normalize_records(rows)


def collect_greenhouse_pages(
    pages: Any, *, board: str, employer: str, observed_at: str
) -> list[dict[str, Any]]:
    _text(board, "Greenhouse board", 100)
    _text(employer, "employer", 300)
    parse_time(observed_at, "observed_at")
    if not isinstance(pages, list) or len(pages) > MAX_PAGES:
        raise JobError("collector page bound exceeded")
    rows: list[dict[str, Any]] = []
    for page in pages:
        jobs = page.get("jobs") if isinstance(page, dict) else None
        if not isinstance(jobs, list) or len(jobs) > 100:
            raise JobError("Greenhouse page is malformed or unbounded")
        for item in jobs:
            if not isinstance(item, dict):
                raise JobError("Greenhouse job is malformed")
            location = item.get("location")
            location_name = location.get("name", "") if isinstance(location, dict) else ""
            rows.append(
                {
                    "source_kind": "ats",
                    "source_name": f"Greenhouse:{board}",
                    "source_url": f"https://boards.greenhouse.io/{board}/",
                    "source_namespace": "ats:greenhouse",
                    "source_service": board,
                    "requisition_id": str(item.get("id")),
                    "aliases": [],
                    "canonical_url": item.get("absolute_url"),
                    "employer": employer,
                    "title": item.get("title"),
                    "location": location_name,
                    "description": item.get("content") or "",
                    "published_at": None,
                    "observed_at": observed_at,
                    "source_state": "active",
                    "official": True,
                    "provenance": [
                        {"url": item.get("absolute_url"), "observed_at": observed_at, "kind": "ats"}
                    ],
                }
            )
        if not jobs:
            break
    return normalize_records(rows)


def collect_lever_pages(
    pages: Any, *, site: str, employer: str, observed_at: str
) -> list[dict[str, Any]]:
    _text(site, "Lever site", 100)
    _text(employer, "employer", 300)
    parse_time(observed_at, "observed_at")
    if not isinstance(pages, list) or len(pages) > MAX_PAGES:
        raise JobError("collector page bound exceeded")
    rows: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, list) or len(page) > 100:
            raise JobError("Lever page is malformed or unbounded")
        for item in page:
            if not isinstance(item, dict):
                raise JobError("Lever job is malformed")
            categories = item.get("categories")
            location = categories.get("location", "") if isinstance(categories, dict) else ""
            url = item.get("hostedUrl")
            rows.append(
                {
                    "source_kind": "ats",
                    "source_name": f"Lever:{site}",
                    "source_url": f"https://jobs.lever.co/{site}/",
                    "source_namespace": "ats:lever",
                    "source_service": site,
                    "requisition_id": str(item.get("id")),
                    "aliases": [],
                    "canonical_url": url,
                    "employer": employer,
                    "title": item.get("text"),
                    "location": location,
                    "description": item.get("descriptionPlain") or "",
                    "published_at": _iso_from_milliseconds(item.get("createdAt")),
                    "observed_at": observed_at,
                    "source_state": "active",
                    "official": True,
                    "provenance": [{"url": url, "observed_at": observed_at, "kind": "ats"}],
                }
            )
        if not page:
            break
    return normalize_records(rows)


def collect_employer_json(
    values: Any,
    *,
    source_name: str,
    source_url: str,
    employer: str,
    observed_at: str,
) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > MAX_RECORDS:
        raise JobError("employer collector record bound exceeded")
    _text(source_name, "source_name", 200)
    canonical_url(source_url, "source_url")
    _text(employer, "employer", 300)
    parse_time(observed_at, "observed_at")
    rows = []
    expected = {"id", "title", "url", "location", "description", "published_at", "state"}
    for value in values:
        item = _exact(value, expected, "employer job")
        rows.append(
            {
                "source_kind": "employer",
                "source_name": source_name,
                "source_url": source_url,
                "source_namespace": "employer:careers",
                "source_service": urlsplit(source_url).hostname,
                "requisition_id": str(item["id"]),
                "aliases": [],
                "canonical_url": item["url"],
                "employer": employer,
                "title": item["title"],
                "location": item["location"],
                "description": item["description"],
                "published_at": item["published_at"],
                "observed_at": observed_at,
                "source_state": item["state"],
                "official": True,
                "provenance": [
                    {"url": item["url"], "observed_at": observed_at, "kind": "employer"}
                ],
            }
        )
    return normalize_records(rows)


def validate_preferences(value: Any, *, auto_authorized: bool = False) -> dict[str, Any]:
    row = _exact(value, PREFERENCE_KEYS, "job preferences")
    if type(auto_authorized) is not bool:
        raise JobError("auto authorization must be immutable boolean policy")
    for name in ("enabled", "active_research_enabled"):
        if type(row[name]) is not bool:
            raise JobError(f"{name} must be boolean")
    bounds = {
        "active_cadence_minutes": (60, 10080),
        "max_active_candidates": (1, 25),
        "revalidate_after_minutes": (60, 10080),
        "closed_retention_days": (30, 730),
    }
    for name, (minimum, maximum) in bounds.items():
        if type(row[name]) is not int or not minimum <= row[name] <= maximum:
            raise JobError(f"{name} must be between {minimum} and {maximum}")
    if row["projection"] not in {"off", "manual", "suggest", "auto"}:
        raise JobError("projection mode is unsupported")
    if row["projection"] == "auto" and not auto_authorized:
        raise JobError("auto projection requires separate explicit authorization")
    return dict(row)


def _empty_state() -> dict[str, Any]:
    return {"schema_version": 1, "jobs": [], "tombstones": [], "updated_at": None}


def _state_documents(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "jobs.json": {"schema_version": 1, "jobs": state["jobs"]},
        "tombstones.json": {"schema_version": 1, "tombstones": state["tombstones"]},
        "meta.json": {"schema_version": 1, "updated_at": state["updated_at"]},
    }


def _validate_job_state(value: Any) -> dict[str, Any]:
    row = _exact(value, JOB_STATE_KEYS, "job state row")
    if (
        not isinstance(row["job_id"], str)
        or re.fullmatch(r"job-[0-9a-f]{64}", row["job_id"]) is None
    ):
        raise JobError("job state identity is malformed")
    if row["status"] not in SOURCE_STATES:
        raise JobError("job state status is unsupported")
    record = normalize_record(row["record"])
    if record["job_id"] != row["job_id"]:
        raise JobError("job state record identity does not match")
    for name in ("first_seen_at", "last_seen_at", "next_revalidate_at"):
        parse_time(row[name], name)
    if parse_time(row["first_seen_at"], "first_seen_at") > parse_time(
        row["last_seen_at"], "last_seen_at"
    ):
        raise JobError("job state first_seen_at is after last_seen_at")
    if row["status"] == "closed":
        if row["closed_at"] is None or record["source_state"] != "closed" or not record["official"]:
            raise JobError("closed state lacks matching official closure evidence")
        parse_time(row["closed_at"], "closed_at")
    elif row["closed_at"] is not None:
        raise JobError("non-closed state must not retain closed_at")
    if row["status"] == "active" and record["source_state"] != "active":
        raise JobError("active state lacks matching active evidence")
    if row["last_error"] is not None:
        _text(row["last_error"], "last_error", 400)
        if row["status"] != "unknown":
            raise JobError("only unknown state may retain an error")
    normalized = dict(row)
    normalized["record"] = record
    return normalized


def _validate_tombstone(value: Any) -> dict[str, Any]:
    row = _exact(value, TOMBSTONE_KEYS, "job tombstone")
    if (
        not isinstance(row["job_id"], str)
        or re.fullmatch(r"job-[0-9a-f]{64}", row["job_id"]) is None
    ):
        raise JobError("job tombstone identity is malformed")
    parse_time(row["retired_at"], "retired_at")
    return dict(row)


def _load_documents(documents: Mapping[str, Any]) -> dict[str, Any]:
    if set(documents) != {"jobs.json", "tombstones.json", "meta.json"}:
        raise JobError("job generation has an incompatible document set")
    jobs = _exact(documents["jobs.json"], {"schema_version", "jobs"}, "jobs document")
    tombstones = _exact(
        documents["tombstones.json"], {"schema_version", "tombstones"}, "tombstones document"
    )
    meta = _exact(documents["meta.json"], {"schema_version", "updated_at"}, "meta document")
    if {jobs["schema_version"], tombstones["schema_version"], meta["schema_version"]} != {1}:
        raise JobError("job document schema is incompatible")
    if not isinstance(jobs["jobs"], list) or not isinstance(tombstones["tombstones"], list):
        raise JobError("job state collections are malformed")
    normalized_jobs = [_validate_job_state(row) for row in jobs["jobs"]]
    normalized_tombstones = [_validate_tombstone(row) for row in tombstones["tombstones"]]
    job_ids = [row["job_id"] for row in normalized_jobs]
    tombstone_ids = [row["job_id"] for row in normalized_tombstones]
    if len(job_ids) != len(set(job_ids)) or len(tombstone_ids) != len(set(tombstone_ids)):
        raise JobError("job state contains duplicate identities")
    if set(job_ids) & set(tombstone_ids):
        raise JobError("job identity cannot be both live and tombstoned")
    if meta["updated_at"] is not None:
        parse_time(meta["updated_at"], "updated_at")
    return {
        "schema_version": 1,
        "jobs": normalized_jobs,
        "tombstones": normalized_tombstones,
        "updated_at": meta["updated_at"],
    }


class JobProtocol:
    def __init__(self, root: Path):
        self.store = GenerationStore(root, protocol="jobs", schema_version=1)

    def _read(self) -> dict[str, Any]:
        try:
            return _load_documents(self.store.read().documents)
        except StateAbsent:
            return _empty_state()

    def inspect(self) -> dict[str, Any]:
        return self._read()

    def ingest(self, now: str, values: Any, prefs: Any) -> dict[str, Any]:
        instant = parse_time(now, "now")
        policy = validate_preferences(prefs)
        records = normalize_records(values)

        def transform(current):
            state = _load_documents(current.documents) if current else _empty_state()
            tombstones = {row["job_id"]: row for row in state["tombstones"]}
            jobs = {row["job_id"]: row for row in state["jobs"]}
            for record in records:
                job_id = record["job_id"]
                if job_id in tombstones:
                    raise JobError("a retained tombstoned job identity cannot be reused")
                prior = jobs.get(job_id)
                if prior:
                    prior_observed = parse_time(prior["record"]["observed_at"], "prior observed_at")
                    incoming_observed = parse_time(record["observed_at"], "incoming observed_at")
                    if incoming_observed < prior_observed:
                        continue
                    if incoming_observed == prior_observed and _revision(record) != _revision(
                        prior["record"]
                    ):
                        raise JobError("conflicting equal-time job revision")
                    record = dict(record)
                    record["provenance"] = _provenance(
                        [*prior["record"]["provenance"], *record["provenance"]]
                    )
                first_seen = prior["first_seen_at"] if prior else now
                status = record["source_state"]
                jobs[job_id] = {
                    "job_id": job_id,
                    "status": status,
                    "record": record,
                    "first_seen_at": first_seen,
                    "last_seen_at": now,
                    "next_revalidate_at": format_time(
                        instant + dt.timedelta(minutes=policy["revalidate_after_minutes"])
                    ),
                    "closed_at": now if status == "closed" else None,
                    "last_error": None,
                }
            state = {
                "schema_version": 1,
                "jobs": [jobs[key] for key in sorted(jobs)],
                "tombstones": [tombstones[key] for key in sorted(tombstones)],
                "updated_at": now,
            }
            return _state_documents(state)

        selected = self.store.update(transform)
        return _load_documents(selected.documents)

    def revalidate(self, now: str, results: Any, prefs: Any) -> dict[str, Any]:
        instant = parse_time(now, "now")
        policy = validate_preferences(prefs)
        if not isinstance(results, list) or len(results) > MAX_RECORDS:
            raise JobError("revalidation results are malformed or unbounded")
        normalized_results = []
        seen_job_ids: set[str] = set()
        for value in results:
            row = _exact(value, {"job_id", "outcome", "record", "reason"}, "revalidation result")
            if not isinstance(row["job_id"], str) or not re.fullmatch(
                r"job-[0-9a-f]{64}", row["job_id"]
            ):
                raise JobError("revalidation job_id is malformed")
            if row["job_id"] in seen_job_ids:
                raise JobError("duplicate revalidation identity")
            seen_job_ids.add(row["job_id"])
            if row["outcome"] not in {"active", "closed", *ERROR_OUTCOMES}:
                raise JobError("revalidation outcome is unsupported")
            if row["reason"] is not None:
                _text(row["reason"], "revalidation reason", 300)
            record = None
            if row["outcome"] in {"active", "closed"}:
                if row["record"] is None:
                    raise JobError("successful revalidation requires a record")
                record = normalize_record(row["record"])
                if record["job_id"] != row["job_id"] or record["source_state"] != row["outcome"]:
                    raise JobError("revalidation record identity or state does not match outcome")
                if row["outcome"] == "closed" and not record["official"]:
                    raise JobError("closed revalidation requires official evidence")
            elif row["record"] is not None:
                raise JobError("failed revalidation must not smuggle a record")
            normalized_results.append((dict(row), record))

        def transform(current):
            if current is None:
                raise JobError("cannot revalidate absent job state")
            state = _load_documents(current.documents)
            jobs = {row["job_id"]: row for row in state["jobs"]}
            for result, record in normalized_results:
                job = jobs.get(result["job_id"])
                if job is None:
                    raise JobError("revalidation references an unknown job")
                outcome = result["outcome"]
                if outcome in ERROR_OUTCOMES:
                    job["status"] = "unknown"
                    job["last_error"] = f"{outcome}:{result['reason'] or 'unspecified'}"
                else:
                    job["status"] = outcome
                    job["record"] = record
                    job["last_seen_at"] = now
                    job["closed_at"] = now if outcome == "closed" else None
                    job["last_error"] = None
                job["next_revalidate_at"] = format_time(
                    instant + dt.timedelta(minutes=policy["revalidate_after_minutes"])
                )
            state["jobs"] = [jobs[key] for key in sorted(jobs)]
            state["updated_at"] = now
            return _state_documents(state)

        return _load_documents(self.store.update(transform).documents)

    def prune(self, now: str, prefs: Any) -> dict[str, Any]:
        instant = parse_time(now, "now")
        policy = validate_preferences(prefs)

        def transform(current):
            if current is None:
                return _state_documents(_empty_state())
            state = _load_documents(current.documents)
            kept = []
            tombstones = {row["job_id"]: row for row in state["tombstones"]}
            for job in state["jobs"]:
                closed_at = job.get("closed_at")
                if job.get("status") == "closed" and closed_at is not None:
                    age = instant - parse_time(closed_at, "closed_at")
                    if age > dt.timedelta(days=policy["closed_retention_days"]):
                        tombstones[job["job_id"]] = {"job_id": job["job_id"], "retired_at": now}
                        continue
                kept.append(job)
            state["jobs"] = sorted(kept, key=lambda row: row["job_id"])
            state["tombstones"] = [tombstones[key] for key in sorted(tombstones)]
            state["updated_at"] = now
            return _state_documents(state)

        return _load_documents(self.store.update(transform).documents)


class ActiveResearchGate:
    def __init__(self, root: Path, *, clock=None):
        self.store = GenerationStore(root, protocol="jobs-active", schema_version=1)
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def _trusted_now(self) -> dt.datetime:
        value = self._clock()
        if not isinstance(value, dt.datetime) or value.tzinfo is None:
            raise JobError("trusted clock must return timezone-aware datetime")
        return value.astimezone(dt.timezone.utc).replace(microsecond=0)

    def _read(self) -> dict[str, Any] | None:
        try:
            documents = self.store.read().documents
        except StateAbsent:
            return None
        if set(documents) != {"active.json"}:
            raise JobError("active research state has incompatible documents")
        return documents["active.json"]

    def claim(
        self,
        now: str,
        prefs: Any,
        values: Any,
        *,
        claim_ttl_seconds: int = 3600,
        passive_generation: str | None = None,
    ) -> dict[str, Any]:
        instant = parse_time(now, "now")
        policy = validate_preferences(prefs)
        records = normalize_records(values)
        if not policy["enabled"] or not policy["active_research_enabled"]:
            return {"wakeAgent": False, "reason": "disabled"}
        eligible = [row for row in records if row["source_state"] != "closed"]
        if not eligible:
            return {"wakeAgent": False, "reason": "empty"}
        response: dict[str, Any] = {}

        def transform(current):
            nonlocal response
            lease_now = self._trusted_now()
            previous = current.documents["active.json"] if current else None
            if previous is not None:
                state = previous.get("state")
                if state == "claimed":
                    claim_expires_at = parse_time(
                        previous.get("claim_expires_at"), "claim_expires_at"
                    )
                    if lease_now < claim_expires_at:
                        response = {"wakeAgent": False, "reason": "in_flight"}
                        return {"active.json": previous}
                if state == "completed":
                    completed_at = parse_time(previous["completed_at"], "completed_at")
                    if instant < completed_at + dt.timedelta(
                        minutes=policy["active_cadence_minutes"]
                    ):
                        response = {"wakeAgent": False, "reason": "not_due"}
                        return {"active.json": previous}
            candidates = sorted(
                eligible, key=lambda row: (row["observed_at"], row["job_id"]), reverse=True
            )[: policy["max_active_candidates"]]
            claim_id = "claim-" + uuid.uuid4().hex
            state = {
                "schema_version": 1,
                "state": "claimed",
                "claim_id": claim_id,
                "claimed_at": format_time(lease_now),
                "claim_expires_at": format_time(
                    lease_now + dt.timedelta(seconds=claim_ttl_seconds)
                ),
                "completed_at": None,
                "proof": None,
                "candidate_ids": [row["job_id"] for row in candidates],
                "passive_generation": passive_generation,
            }
            response = {
                "wakeAgent": True,
                "claim_id": claim_id,
                "claimed_at": state["claimed_at"],
                "claim_expires_at": state["claim_expires_at"],
                "candidates": candidates,
                "candidate_ids": state["candidate_ids"],
                "passive_generation": passive_generation,
                "research_policy": {
                    "official_sources_first": True,
                    "bounded": True,
                    "applications_forbidden": True,
                    "external_messages_forbidden": True,
                },
            }
            return {"active.json": state}

        self.store.update(transform)
        return response

    def claim_from_passive(
        self,
        now: str,
        prefs: Any,
        passive: JobProtocol,
        *,
        claim_ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        policy = validate_preferences(prefs)
        if not policy["enabled"] or not policy["active_research_enabled"]:
            return {"wakeAgent": False, "reason": "disabled"}
        if not isinstance(passive, JobProtocol):
            raise JobError("active research requires a durable passive JobProtocol")
        try:
            selected = passive.store.read()
        except StateAbsent:
            return {"wakeAgent": False, "reason": "empty"}
        state = _load_documents(selected.documents)
        records = [row["record"] for row in state["jobs"] if row["status"] != "closed"]
        if not records:
            return {"wakeAgent": False, "reason": "empty"}
        return self.claim(
            now,
            policy,
            records,
            claim_ttl_seconds=claim_ttl_seconds,
            passive_generation=selected.generation,
        )

    def complete(self, claim_id: str, proof: str) -> None:
        _text(claim_id, "claim_id", 80)
        _text(proof, "proof", 200)

        def transform(current):
            if current is None:
                raise JobError("active claim state is absent")
            state = current.documents.get("active.json")
            if (
                not isinstance(state, dict)
                or state.get("state") != "claimed"
                or state.get("claim_id") != claim_id
            ):
                raise JobError("only the current active claim may complete")
            instant = self._trusted_now()
            claimed_at = parse_time(state.get("claimed_at"), "claimed_at")
            expires = parse_time(state.get("claim_expires_at"), "claim_expires_at")
            if instant < claimed_at:
                raise JobError("trusted clock predates the active claim")
            if instant >= expires:
                raise JobError("active claim expired before completion")
            next_state = dict(
                state,
                state="completed",
                completed_at=format_time(instant),
                proof=proof,
            )
            return {"active.json": next_state}

        self.store.update(transform)


class ProjectionPlanner:
    def __init__(
        self,
        mode: str,
        auto_authorized: bool,
        *,
        now: str | None = None,
        max_age_minutes: int = 1440,
    ):
        if mode not in {"off", "manual", "suggest", "auto"}:
            raise JobError("projection mode is unsupported")
        if type(auto_authorized) is not bool:
            raise JobError("auto authorization must be boolean")
        if mode == "auto" and not auto_authorized:
            raise JobError("auto projection is unavailable until explicitly authorized")
        self.mode = mode
        self.auto_authorized = auto_authorized
        self.now = parse_time(now, "projection now") if now is not None else None
        if type(max_age_minutes) is not int or not 60 <= max_age_minutes <= 10080:
            raise JobError("projection freshness bound is invalid")
        self.max_age = dt.timedelta(minutes=max_age_minutes)

    def plan(self, jobs: Any) -> list[dict[str, Any]]:
        if not isinstance(jobs, list) or len(jobs) > MAX_RECORDS:
            raise JobError("projection input is malformed or unbounded")
        if self.mode == "off":
            return []
        actions = []
        for job in sorted(jobs, key=lambda row: row.get("job_id", "")):
            if not isinstance(job, dict) or job.get("status") not in SOURCE_STATES:
                raise JobError("projection job is malformed")
            if self.mode == "manual":
                action = "manual_review"
            elif self.mode == "auto" and job["status"] == "closed":
                action = "annotate_closed"
            elif job["status"] in {"closed", "unknown"}:
                continue
            else:
                record = job.get("record")
                if not isinstance(record, dict) or record.get("official") is not True:
                    continue
                if self.now is None:
                    raise JobError("suggest and auto projection require a trusted current time")
                last_seen = parse_time(job.get("last_seen_at"), "projection last_seen_at")
                if last_seen > self.now or self.now - last_seen > self.max_age:
                    continue
                action = "suggest" if self.mode == "suggest" else "upsert"
            idempotency_key = f"job-projection:{job['job_id']}"
            fields = (
                ["description"]
                if action == "annotate_closed"
                else ["title", "description", "labels"]
            )
            actions.append(
                {
                    "job_id": job["job_id"],
                    "action": action,
                    "idempotency_key": idempotency_key,
                    "annotation": "Vacancy is closed; retained for provenance."
                    if job["status"] == "closed"
                    else None,
                    "mutation": None
                    if action in {"manual_review", "suggest"}
                    else {
                        "method": "PUT",
                        "idempotency_key": idempotency_key,
                        "fields": fields,
                        "readback": "exact-managed-fields",
                    },
                }
            )
        return actions


def load_private_cv(path: Path) -> str:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise JobError("private CV file is unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o400:
        raise JobError("private CV must be a 0400 regular file")
    if before.st_size > MAX_CV_BYTES:
        raise JobError("private CV exceeds the byte bound")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            raise JobError("private CV changed during validation")
        raw = os.read(fd, MAX_CV_BYTES + 1)
    finally:
        os.close(fd)
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JobError("private CV must be UTF-8 text") from exc
    if not value:
        raise JobError("private CV is empty")
    return value


def ranking_envelope(records: Any, private_cv: str) -> dict[str, Any]:
    rows = normalize_records(records)
    _text(private_cv, "private CV", MAX_CV_BYTES)
    return {
        "untrusted_jobs": rows,
        "private_cv": private_cv,
        "policy": {
            "persist_cv": False,
            "collector_access": False,
            "miniflux_access": False,
            "applications_forbidden": True,
            "external_messages_forbidden": True,
        },
    }


def _load_json_file(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise JobError(f"input file is unavailable: {path.name}") from exc
    if len(raw) > 10 * 1024 * 1024:
        raise JobError("input file exceeds byte bound")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JobError(f"input JSON is malformed: {path.name}") from exc


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    passive_parser = subparsers.add_parser("passive")
    passive_parser.add_argument("--state-dir", type=Path, required=True)
    passive_parser.add_argument("--preferences", type=Path, required=True)
    passive_parser.add_argument("--records", type=Path, required=True)
    passive_parser.add_argument("--now", required=True)
    miniflux_passive_parser = subparsers.add_parser("miniflux-passive")
    miniflux_passive_parser.add_argument("--state-dir", type=Path, required=True)
    miniflux_passive_parser.add_argument("--preferences", type=Path, required=True)
    miniflux_passive_parser.add_argument("--entries", type=Path, required=True)
    miniflux_passive_parser.add_argument("--now", required=True)
    for name in ("active-gate", "miniflux-active-gate"):
        command = subparsers.add_parser(name)
        command.add_argument("--state-dir", type=Path, required=True)
        command.add_argument("--preferences", type=Path, required=True)
        command.add_argument("--passive-state-dir", type=Path, required=True)
        command.add_argument("--now", required=True)
    revalidate_parser = subparsers.add_parser("revalidate")
    revalidate_parser.add_argument("--state-dir", type=Path, required=True)
    revalidate_parser.add_argument("--preferences", type=Path, required=True)
    revalidate_parser.add_argument("--results", type=Path, required=True)
    revalidate_parser.add_argument("--now", required=True)
    complete_parser = subparsers.add_parser("active-complete")
    complete_parser.add_argument("--state-dir", type=Path, required=True)
    complete_parser.add_argument("--claim-id", required=True)
    complete_parser.add_argument("--proof", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--state-dir", type=Path, required=True)
    projection_parser = subparsers.add_parser("projection-plan")
    projection_parser.add_argument("--state-dir", type=Path, required=True)
    projection_parser.add_argument("--preferences", type=Path, required=True)
    projection_parser.add_argument("--now", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "passive":
            JobProtocol(args.state_dir).ingest(
                args.now,
                _load_json_file(args.records),
                _load_json_file(args.preferences),
            )
            result = {"wakeAgent": False}
        elif args.command == "active-gate":
            result = ActiveResearchGate(args.state_dir).claim_from_passive(
                args.now,
                _load_json_file(args.preferences),
                JobProtocol(args.passive_state_dir),
            )
        elif args.command == "miniflux-passive":
            preferences = validate_preferences(_load_json_file(args.preferences))
            records = collect_miniflux_entries(_load_json_file(args.entries), observed_at=args.now)
            JobProtocol(args.state_dir).ingest(args.now, records, preferences)
            result = {"wakeAgent": False}
        elif args.command == "miniflux-active-gate":
            preferences = validate_preferences(_load_json_file(args.preferences))
            result = ActiveResearchGate(args.state_dir).claim_from_passive(
                args.now,
                preferences,
                JobProtocol(args.passive_state_dir),
            )
        elif args.command == "revalidate":
            result = JobProtocol(args.state_dir).revalidate(
                args.now,
                _load_json_file(args.results),
                _load_json_file(args.preferences),
            )
        elif args.command == "active-complete":
            ActiveResearchGate(args.state_dir).complete(
                args.claim_id,
                args.proof,
            )
            result = {"status": "completed", "claim_id": args.claim_id}
        elif args.command == "projection-plan":
            preferences = validate_preferences(_load_json_file(args.preferences))
            jobs = JobProtocol(args.state_dir).inspect()["jobs"]
            result = {
                "mode": preferences["projection"],
                "actions": ProjectionPlanner(
                    preferences["projection"],
                    False,
                    now=args.now,
                    max_age_minutes=preferences["revalidate_after_minutes"],
                ).plan(jobs),
            }
        else:
            result = JobProtocol(args.state_dir).inspect()
        sys.stdout.buffer.write(canonical_json_bytes(result))
        return 0
    except (JobError, ProtocolError) as exc:
        print(f"job workflow error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
