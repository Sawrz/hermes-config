#!/usr/bin/env python3
"""Deterministic pre-agent gate and domain-state protocol for Hermes digests."""

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
from collections.abc import Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hermes_workflow_state import GenerationStore, ProtocolError, StateAbsent, canonical_json_bytes


class DigestError(ProtocolError):
    """Digest input, state, or transition violates the protocol."""


ROUTE_RE = re.compile(r"^telegram:(-?[0-9]{1,20}):([0-9]{1,20})$")

SOURCE_TIME_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$"
)
CLAIM_RE = re.compile(r"^claim-[0-9a-f]{32}$")
BATCH_RE = re.compile(r"^batch-[0-9a-f]{64}$")
STATES = {"claimed", "rendered", "failed"}
NOOP = {
    "disabled",
    "empty",
    "not_due",
    "in_flight",
    "already_processed",
    "quiet_hours",
}
CANDIDATE_KEYS = {
    "entry_id",
    "source_id",
    "source_title",
    "title",
    "url",
    "published_at",
    "updated_at",
    "media_type",
    "summary",
}
PREFERENCE_KEYS = {
    "enabled",
    "cadence_minutes",
    "edition",
    "language",
    "topics",
    "max_candidates",
    "max_prompt_chars",
    "transcripts",
    "category_ids",
    "media_types",
    "excluded_source_ids",
    "timezone",
    "quiet_hours_start",
    "quiet_hours_end",
    "consolidation",
    "style",
    "max_extracted_chars",
    "max_transcript_chars",
}
DEFAULT_ENDPOINT_FILE = Path("/run/hermes-credentials/services/miniflux/endpoint")
DEFAULT_CREDENTIAL_FILE = Path("/run/hermes-credentials/services/miniflux/credential")
MAX_HTTP_BYTES = 10 * 1024 * 1024


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DigestError("Miniflux redirects are forbidden")


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise DigestError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _bounded_text(value: Any, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > maximum:
        raise DigestError(f"{label} must be bounded text")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise DigestError(f"{label} contains forbidden controls")
    return value


def parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DigestError(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DigestError(f"{label} is malformed") from exc
    if parsed.tzinfo != dt.timezone.utc or parsed.microsecond:
        raise DigestError(f"{label} must be UTC with whole-second precision")
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise DigestError(f"{label} is not canonical")
    return parsed


def format_time(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def source_time(value: Any, label: str) -> str:
    if not isinstance(value, str) or SOURCE_TIME_RE.fullmatch(value) is None:
        raise DigestError(f"{label} must be a canonical UTC source timestamp")
    whole = value[:19] + "Z"
    try:
        dt.datetime.strptime(whole, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise DigestError(f"{label} source timestamp is malformed") from exc
    return value


def canonical_url(value: Any) -> str:
    _bounded_text(value, "candidate URL", 2048)
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise DigestError("candidate URL contains whitespace or controls")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise DigestError("candidate URL authority is malformed") from exc
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise DigestError("candidate URL must be absolute HTTP(S)")
    if parts.username or parts.password or parts.fragment:
        raise DigestError("candidate URL credentials and fragments are forbidden")
    host = parts.hostname
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise DigestError("candidate URL host must use canonical ASCII text") from exc
    if host != host.lower():
        raise DigestError("candidate URL host must be lowercase")
    if parts.scheme == "http" and host not in {"127.0.0.1", "::1", "localhost"}:
        raise DigestError("non-loopback candidate URL must use HTTPS")
    if (parts.scheme, port) in {("https", 443), ("http", 80)}:
        raise DigestError("candidate URL must omit the default port")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    if parts.netloc != authority:
        raise DigestError("candidate URL authority is not canonical")
    path = parts.path or "/"
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise DigestError("candidate URL dot segments are forbidden")
    for match in re.finditer(r"%([0-9A-Fa-f]{2})", path + "?" + parts.query):
        encoded = match.group(1)
        if (
            encoded != encoded.upper()
            or chr(int(encoded, 16))
            in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
        ):
            raise DigestError("candidate URL percent encoding is not canonical")
    canonical = urlunsplit((parts.scheme, authority, path, parts.query, ""))
    if canonical != value:
        raise DigestError("candidate URL must be exact canonical text")
    return value


def canonical_route(value: Any) -> str:
    _bounded_text(value, "delivery route", 80)
    if ROUTE_RE.fullmatch(value) is None:
        raise DigestError("delivery route must be explicit telegram:<chat_id>:<thread_id>")
    return value


def secure_read(path: Path, label: str) -> str:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise DigestError(f"{label} runtime file is unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o400:
        raise DigestError(f"{label} runtime file must be a 0400 regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DigestError(f"{label} runtime file cannot be opened safely") from exc
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            raise DigestError(f"{label} runtime file changed during validation")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
            value = handle.read().strip()
    finally:
        os.close(fd)
    if not value:
        raise DigestError(f"{label} runtime file is empty")
    return value


def _summary(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception as exc:
        raise DigestError("Miniflux entry content is malformed HTML") from exc
    return " ".join(" ".join(parser.parts).split())[:4000]


def _media_type(entry: Mapping[str, Any]) -> str:
    enclosures = entry.get("enclosures")
    if isinstance(enclosures, list):
        for enclosure in enclosures[:16]:
            if not isinstance(enclosure, dict):
                continue
            mime = enclosure.get("mime_type") or enclosure.get("type") or ""
            if isinstance(mime, str) and mime.lower().startswith("audio/"):
                return "podcast"
            if isinstance(mime, str) and mime.lower().startswith("video/"):
                return "video"
    url = entry.get("url")
    if isinstance(url, str) and urlsplit(url).hostname in {
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
    }:
        return "video"
    return "article"


def miniflux_entry_candidate(entry: Any, expected_user_id: int) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise DigestError("Miniflux entry is malformed")
    feed = entry.get("feed")
    if not isinstance(feed, dict):
        raise DigestError("Miniflux entry feed is malformed")
    if type(feed.get("user_id")) is not int or feed["user_id"] != expected_user_id:
        raise DigestError("Miniflux returned an entry without exact current-user ownership")
    published = entry.get("published_at")
    changed = entry.get("changed_at") or published
    return normalize_candidate(
        {
            "entry_id": entry.get("id"),
            "source_id": feed.get("id"),
            "source_title": feed.get("title"),
            "title": entry.get("title"),
            "url": entry.get("url"),
            "published_at": published,
            "updated_at": changed,
            "media_type": _media_type(entry),
            "summary": _summary(entry.get("content")),
        }
    )


class MinifluxDigestClient:
    """Bounded read-only Miniflux collector for the current profile account."""

    def __init__(
        self,
        endpoint_file: Path = DEFAULT_ENDPOINT_FILE,
        credential_file: Path = DEFAULT_CREDENTIAL_FILE,
        timeout: float = 15.0,
    ):
        endpoint = secure_read(endpoint_file, "endpoint").rstrip("/")
        self.token = secure_read(credential_file, "credential")
        parts = urlsplit(endpoint)
        if (
            parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or not parts.hostname
            or parts.path not in {"", "/"}
        ):
            raise DigestError("Miniflux endpoint must be a canonical service root")
        if parts.scheme != "https" and not (
            parts.scheme == "http" and parts.hostname in {"127.0.0.1", "::1", "localhost"}
        ):
            raise DigestError("Miniflux endpoint must use HTTPS")
        self.endpoint = endpoint
        self.timeout = timeout
        self.opener = build_opener(_NoRedirect())

    def request(self, path: str) -> Any:
        if not path.startswith("/v1/") or path.startswith("//"):
            raise DigestError("Miniflux API path escaped /v1")
        request = Request(
            self.endpoint + path,
            headers={
                "Accept": "application/json",
                "X-Auth-Token": self.token,
                "User-Agent": "hermes-digest-gate/1",
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_HTTP_BYTES + 1)
        except DigestError:
            raise
        except HTTPError as exc:
            raise DigestError(f"Miniflux returned HTTP {exc.code}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise DigestError("Miniflux read failed") from exc
        if len(raw) > MAX_HTTP_BYTES:
            raise DigestError("Miniflux response exceeds the byte bound")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DigestError("Miniflux returned malformed JSON") from exc

    def collect(self, category_ids: Any) -> list[dict[str, Any]]:
        if (
            not isinstance(category_ids, list)
            or not 1 <= len(category_ids) <= 16
            or len(category_ids) != len(set(category_ids))
            or not all(type(item) is int and item > 0 for item in category_ids)
        ):
            raise DigestError("Miniflux collection requires a bounded selected category scope")
        category_ids = sorted(category_ids)
        me = self.request("/v1/me")
        if (
            not isinstance(me, dict)
            or type(me.get("id")) is not int
            or me.get("is_admin") is not False
        ):
            raise DigestError("digest collection requires one non-admin Miniflux account")
        rows: list[dict[str, Any]] = []
        page_size = 100
        for category_id in category_ids:
            for offset in range(0, 600, page_size):
                result = self.request(
                    f"/v1/entries?status=unread&category_id={category_id}&order=published_at&direction=desc&limit={page_size}&offset={offset}"
                )
                entries = result.get("entries") if isinstance(result, dict) else None
                if not isinstance(entries, list) or len(entries) > page_size:
                    raise DigestError("Miniflux entries response is malformed or unbounded")
                if len(rows) + len(entries) > 500:
                    raise DigestError("Miniflux unread candidate set exceeds 500 entries")
                rows.extend(miniflux_entry_candidate(entry, me["id"]) for entry in entries)
                if len(entries) < page_size:
                    break
            else:
                raise DigestError("Miniflux pagination did not terminate")
        return rows


def validate_preferences(value: Any) -> dict[str, Any]:
    doc = _exact(value, PREFERENCE_KEYS, "digest preferences")
    if type(doc["enabled"]) is not bool:
        raise DigestError("enabled must be boolean")
    if type(doc["cadence_minutes"]) is not int or not 60 <= doc["cadence_minutes"] <= 10080:
        raise DigestError("cadence_minutes must be between 60 and 10080")
    if doc["edition"] not in {"morning", "evening", "weekly"}:
        raise DigestError("edition is unsupported")
    if doc["language"] not in {"de", "en"}:
        raise DigestError("language is unsupported")
    if (
        not isinstance(doc["topics"], list)
        or len(doc["topics"]) > 16
        or len(set(doc["topics"])) != len(doc["topics"])
    ):
        raise DigestError("topics must be a bounded unique list")
    for topic in doc["topics"]:
        _bounded_text(topic, "topic", 80)
    if type(doc["max_candidates"]) is not int or not 1 <= doc["max_candidates"] <= 50:
        raise DigestError("max_candidates must be between 1 and 50")
    if type(doc["max_prompt_chars"]) is not int or not 2000 <= doc["max_prompt_chars"] <= 100000:
        raise DigestError("max_prompt_chars must be between 2000 and 100000")
    if doc["transcripts"] not in {"never", "selective"}:
        raise DigestError("transcripts must be never or selective")
    for name in ("category_ids", "excluded_source_ids"):
        values = doc[name]
        if (
            not isinstance(values, list)
            or (name == "category_ids" and doc["enabled"] and not values)
            or len(values) > 16
            or values != sorted(set(values))
            or not all(
                isinstance(item, str) and re.fullmatch(r"[1-9][0-9]{0,9}", item) for item in values
            )
        ):
            raise DigestError(
                f"{name} must be a bounded sorted unique positive-integer string list"
            )
    if (
        not isinstance(doc["media_types"], list)
        or not doc["media_types"]
        or len(doc["media_types"]) > 3
        or doc["media_types"] != sorted(set(doc["media_types"]))
        or not set(doc["media_types"]) <= {"article", "podcast", "video"}
    ):
        raise DigestError("media_types must be a bounded sorted unique supported list")
    try:
        ZoneInfo(_bounded_text(doc["timezone"], "timezone", 100))
    except ZoneInfoNotFoundError as exc:
        raise DigestError("timezone is unknown") from exc
    for name in ("quiet_hours_start", "quiet_hours_end"):
        if (
            not isinstance(doc[name], str)
            or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", doc[name]) is None
        ):
            raise DigestError(f"{name} must be canonical HH:MM")
    if doc["consolidation"] not in {"none", "topic", "source"}:
        raise DigestError("consolidation is unsupported")
    if doc["style"] not in {"concise", "balanced", "detailed"}:
        raise DigestError("style is unsupported")
    for name in ("max_extracted_chars", "max_transcript_chars"):
        if type(doc[name]) is not int or not 1000 <= doc[name] <= 50000:
            raise DigestError(f"{name} must be between 1000 and 50000")
    normalized = dict(doc)
    normalized["category_ids"] = [int(item) for item in doc["category_ids"]]
    normalized["excluded_source_ids"] = [int(item) for item in doc["excluded_source_ids"]]
    return normalized


def normalize_candidate(value: Any) -> dict[str, Any]:
    row = _exact(value, CANDIDATE_KEYS, "candidate")
    for name in ("entry_id", "source_id"):
        if type(row[name]) is not int or row[name] <= 0:
            raise DigestError(f"candidate {name} must be a positive integer")
    _bounded_text(row["source_title"], "source title", 200)
    _bounded_text(row["title"], "candidate title", 500)
    _bounded_text(row["summary"], "candidate summary", 4000, allow_empty=True)
    canonical_url(row["url"])
    source_time(row["published_at"], "published_at")
    source_time(row["updated_at"], "updated_at")
    if row["media_type"] not in {"article", "podcast", "video"}:
        raise DigestError("candidate media_type is unsupported")
    return dict(row)


def normalize_candidate_pool(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > 500:
        raise DigestError("candidate input must contain at most 500 rows")
    by_identity: dict[tuple[int, str], dict[str, Any]] = {}
    revisions: dict[int, bytes] = {}
    for raw in values:
        row = normalize_candidate(raw)
        encoded = canonical_json_bytes(row)
        prior = revisions.get(row["entry_id"])
        if prior is not None and prior != encoded:
            raise DigestError("candidate input has conflicting revisions for one entry")
        revisions[row["entry_id"]] = encoded
        by_identity[(row["entry_id"], row["updated_at"])] = row
    return sorted(
        by_identity.values(),
        key=lambda row: (row["published_at"], row["entry_id"]),
        reverse=True,
    )


def select_candidates(
    pool: list[dict[str, Any]], max_candidates: int, max_prompt_chars: int
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for original in pool:
        if len(selected) >= max_candidates:
            break
        row = dict(original)
        proposal = sorted([*selected, row], key=lambda item: item["entry_id"])
        if len(canonical_json_bytes(proposal)) <= max_prompt_chars:
            selected.append(row)
            continue

        # Miniflux summaries are optional context. Preserve all provenance fields
        # and deterministically truncate only the summary to fit the contract.
        low, high = 0, len(row["summary"])
        fitted: dict[str, Any] | None = None
        while low <= high:
            middle = (low + high) // 2
            candidate = dict(
                row, summary=row["summary"][:middle] + ("…" if middle < len(row["summary"]) else "")
            )
            packed = sorted([*selected, candidate], key=lambda item: item["entry_id"])
            if len(canonical_json_bytes(packed)) <= max_prompt_chars:
                fitted = candidate
                low = middle + 1
            else:
                high = middle - 1
        if fitted is not None:
            selected.append(fitted)

    if not selected and pool:
        raise DigestError("candidate metadata cannot fit the prompt character bound")
    return sorted(selected, key=lambda row: row["entry_id"])


def normalize_candidates(
    values: Any, max_candidates: int, max_prompt_chars: int
) -> list[dict[str, Any]]:
    return select_candidates(normalize_candidate_pool(values), max_candidates, max_prompt_chars)


def validate_enrichment(value: Any, preferences: Any | None = None) -> dict[str, Any]:
    doc = _exact(
        value, {"candidate", "state", "extracted_text", "transcript", "errors"}, "enrichment"
    )
    candidate = normalize_candidate(doc["candidate"])
    if doc["state"] not in {"complete", "partial", "metadata_only", "failed"}:
        raise DigestError("enrichment state is unsupported")
    limits = {"extracted_text": 50000, "transcript": 50000}
    if preferences is not None:
        policy = validate_preferences(preferences)
        limits = {
            "extracted_text": policy["max_extracted_chars"],
            "transcript": policy["max_transcript_chars"],
        }
    for name, maximum in limits.items():
        if doc[name] is not None:
            try:
                _bounded_text(doc[name], name, maximum)
            except DigestError as exc:
                policy_name = (
                    "max_extracted_chars" if name == "extracted_text" else "max_transcript_chars"
                )
                raise DigestError(f"{name} exceeds {policy_name}") from exc
    if not isinstance(doc["errors"], list) or len(doc["errors"]) > 8:
        raise DigestError("enrichment errors must be bounded")
    for error in doc["errors"]:
        _bounded_text(error, "enrichment error", 200)
    if doc["state"] == "complete" and doc["extracted_text"] is None and doc["transcript"] is None:
        raise DigestError("complete enrichment needs extracted content")
    if doc["state"] == "partial" and (
        doc["extracted_text"] is None and doc["transcript"] is None or not doc["errors"]
    ):
        raise DigestError("partial enrichment needs content and an explicit error")
    if doc["state"] == "metadata_only" and (
        doc["extracted_text"] is not None or doc["transcript"] is not None or not doc["errors"]
    ):
        raise DigestError("metadata_only enrichment must explain unavailable content")
    if doc["state"] == "failed" and (
        doc["extracted_text"] is not None or doc["transcript"] is not None or not doc["errors"]
    ):
        raise DigestError("failed enrichment must carry only explicit errors")
    return {**doc, "candidate": candidate, "untrusted": True}


def _batch_digest(
    preferences: Mapping[str, Any], candidates: list[dict[str, Any]], route: str
) -> str:
    identity = {
        "edition": preferences["edition"],
        "language": preferences["language"],
        "topics": preferences["topics"],
        "route": route,
        "candidates": candidates,
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def _empty_state() -> dict[str, Any]:
    return {"schema_version": 2, "processed_entry_ranges": [], "batches": []}


def _validate_ranges(value: Any) -> list[list[int]]:
    if not isinstance(value, list) or len(value) > 2048:
        raise DigestError("delivered entry ranges are malformed or unbounded")
    ranges: list[list[int]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or type(item[0]) is not int
            or type(item[1]) is not int
            or item[0] <= 0
            or item[1] < item[0]
            or (ranges and item[0] <= ranges[-1][1] + 1)
        ):
            raise DigestError("delivered entry ranges are not canonical")
        ranges.append([item[0], item[1]])
    return ranges


def _ranges_with_ids(ranges: list[list[int]], ids: list[int]) -> list[list[int]]:
    merged: list[list[int]] = []
    for start, end in sorted([*ranges, *([item, item] for item in ids)]):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    if len(merged) > 2048:
        raise DigestError("processed entry ranges exceed the safe bound")
    return merged


def _entry_was_processed(ranges: list[list[int]], entry_id: int) -> bool:
    return any(start <= entry_id <= end for start, end in ranges)


def _validate_batch(value: Any) -> dict[str, Any]:
    keys = {
        "batch_id",
        "batch_digest",
        "route",
        "state",
        "claim_id",
        "claimed_at",
        "claim_expires_at",
        "candidate_ids",
        "rendered_sha256",
        "rendered_content",
        "error",
    }
    row = _exact(value, keys, "batch")
    if not isinstance(row["batch_id"], str) or BATCH_RE.fullmatch(row["batch_id"]) is None:
        raise DigestError("batch id is malformed")
    if (
        row["batch_id"] != "batch-" + row["batch_digest"]
        or re.fullmatch(r"[0-9a-f]{64}", row["batch_digest"]) is None
    ):
        raise DigestError("batch digest is malformed")
    canonical_route(row["route"])
    if row["state"] not in STATES:
        raise DigestError("batch state is malformed")
    if not isinstance(row["claim_id"], str) or CLAIM_RE.fullmatch(row["claim_id"]) is None:
        raise DigestError("claim id is malformed")
    parse_time(row["claimed_at"], "claimed_at")
    parse_time(row["claim_expires_at"], "claim_expires_at")

    if (
        not isinstance(row["candidate_ids"], list)
        or not row["candidate_ids"]
        or len(row["candidate_ids"]) > 50
        or row["candidate_ids"] != sorted(set(row["candidate_ids"]))
    ):
        raise DigestError("candidate ids are malformed")
    if not all(type(item) is int and item > 0 for item in row["candidate_ids"]):
        raise DigestError("candidate ids are malformed")
    if (
        row["rendered_sha256"] is not None
        and re.fullmatch(r"[0-9a-f]{64}", row["rendered_sha256"]) is None
    ):
        raise DigestError("rendered digest is malformed")
    if row["rendered_content"] is not None:
        _bounded_text(row["rendered_content"], "rendered content", 100000)
    if row["error"] is not None:
        _bounded_text(row["error"], "batch error", 300)
    invariants = {
        "claimed": row["rendered_sha256"] is None
        and row["rendered_content"] is None
        and row["error"] is None,
        "rendered": row["rendered_sha256"] is not None
        and row["rendered_content"] is not None
        and row["error"] is None,
        "failed": row["rendered_sha256"] is None
        and row["rendered_content"] is None
        and row["error"] is not None,
    }[row["state"]]
    if not invariants:
        raise DigestError("batch state invariant is invalid")
    return row


def _validate_state(value: Any) -> dict[str, Any]:
    doc = _exact(
        value,
        {"schema_version", "processed_entry_ranges", "batches"},
        "digest state",
    )
    if (
        doc["schema_version"] != 2
        or not isinstance(doc["batches"], list)
        or len(doc["batches"]) > 128
    ):
        raise DigestError("digest state is incompatible or unbounded")
    processed_ranges = _validate_ranges(doc["processed_entry_ranges"])
    batches = [_validate_batch(row) for row in doc["batches"]]
    ids = [row["batch_id"] for row in batches]
    if len(ids) != len(set(ids)):
        raise DigestError("digest state has duplicate batches")
    return {
        "schema_version": 2,
        "processed_entry_ranges": processed_ranges,
        "batches": batches,
    }


class DigestProtocol:
    def __init__(self, state_dir: Path | str, *, clock=None):
        self.store = GenerationStore(
            Path(state_dir).absolute(), protocol="hermes-digest", schema_version=2
        )
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def _documents(self, current: Any) -> dict[str, Any]:
        if current is None:
            return _empty_state()
        return _validate_state(current.documents.get("digest.json"))

    def _update(self, transform) -> None:
        self.store.update(transform)
        self.store.cleanup(keep_previous=2)

    def claim(
        self, now: str, preferences: Any, candidates: Any, route: str, claim_ttl: int = 1800
    ) -> dict[str, Any]:
        when = parse_time(now, "now")
        prefs = validate_preferences(preferences)
        route = canonical_route(route)
        if type(claim_ttl) is not int or not 60 <= claim_ttl <= 3600:
            raise DigestError("claim TTL must be between 60 and 3600 seconds")
        if not prefs["enabled"]:
            return {"status": "disabled"}
        local = when.astimezone(ZoneInfo(prefs["timezone"]))
        local_minutes = local.hour * 60 + local.minute
        start_hour, start_minute = map(int, prefs["quiet_hours_start"].split(":"))
        end_hour, end_minute = map(int, prefs["quiet_hours_end"].split(":"))
        quiet_start = start_hour * 60 + start_minute
        quiet_end = end_hour * 60 + end_minute
        in_quiet_hours = (
            quiet_start <= local_minutes < quiet_end
            if quiet_start < quiet_end
            else local_minutes >= quiet_start or local_minutes < quiet_end
        )
        if quiet_start != quiet_end and in_quiet_hours:
            return {"status": "quiet_hours"}
        pool = [
            row
            for row in normalize_candidate_pool(candidates)
            if row["media_type"] in prefs["media_types"]
            and row["source_id"] not in prefs["excluded_source_ids"]
        ]
        if not pool:
            return {"status": "empty"}
        result: dict[str, Any] = {}

        def transform(current):
            nonlocal result
            state = self._documents(current)
            batches = state["batches"]
            for prior in batches:
                if prior["state"] == "claimed" and when >= parse_time(
                    prior["claim_expires_at"], "claim_expires_at"
                ):
                    prior["state"] = "failed"
                    prior["error"] = "claim_expired"
            if any(
                row["state"] == "claimed"
                and when < parse_time(row["claim_expires_at"], "claim_expires_at")
                for row in batches
            ):
                result = {"status": "in_flight"}
                return {"digest.json": state}

            eligible_pool = [
                row
                for row in pool
                if not _entry_was_processed(state["processed_entry_ranges"], row["entry_id"])
            ]
            if not eligible_pool:
                result = {"status": "already_processed"}
                return {"digest.json": state}
            eligible = select_candidates(
                eligible_pool, prefs["max_candidates"], prefs["max_prompt_chars"]
            )
            digest = _batch_digest(prefs, eligible, route)
            batch_id = "batch-" + digest
            existing = next((row for row in batches if row["batch_id"] == batch_id), None)
            if existing is not None:
                if existing["state"] == "claimed":
                    result = {"status": "in_flight"}
                elif existing["state"] == "failed" and existing["error"] == "claim_expired":
                    claim_id = "claim-" + uuid.uuid4().hex
                    existing.update(
                        state="claimed",
                        claim_id=claim_id,
                        claimed_at=now,
                        claim_expires_at=format_time(when + dt.timedelta(seconds=claim_ttl)),
                        error=None,
                    )
                    result = {
                        "status": "claimed",
                        "batch_id": batch_id,
                        "claim_id": claim_id,
                        "route": route,
                        "preferences": prefs,
                        "candidates": eligible,
                    }
                else:
                    result = {"status": "already_processed"}
                return {"digest.json": state}

            rendered = [row for row in batches if row["state"] == "rendered"]
            if rendered:
                last = max(parse_time(row["claimed_at"], "claimed_at") for row in rendered)
                if when < last + dt.timedelta(minutes=prefs["cadence_minutes"]):
                    result = {"status": "not_due"}
                    return {"digest.json": state}
            claim_id = "claim-" + uuid.uuid4().hex
            batches.append(
                {
                    "batch_id": batch_id,
                    "batch_digest": digest,
                    "route": route,
                    "state": "claimed",
                    "claim_id": claim_id,
                    "claimed_at": now,
                    "claim_expires_at": format_time(when + dt.timedelta(seconds=claim_ttl)),
                    "candidate_ids": sorted(row["entry_id"] for row in eligible),
                    "rendered_sha256": None,
                    "rendered_content": None,
                    "error": None,
                }
            )
            state["batches"] = batches[-128:]
            result = {
                "status": "claimed",
                "batch_id": batch_id,
                "claim_id": claim_id,
                "route": route,
                "preferences": prefs,
                "candidates": eligible,
            }
            return {"digest.json": state}

        self._update(transform)
        return result

    def _transition(self, batch_id: str, claim_id: str, mutate) -> None:
        if (
            not isinstance(batch_id, str)
            or BATCH_RE.fullmatch(batch_id) is None
            or not isinstance(claim_id, str)
            or CLAIM_RE.fullmatch(claim_id) is None
        ):
            raise DigestError("batch or claim identity is malformed")
        when = self._clock()
        if not isinstance(when, dt.datetime) or when.tzinfo is None:
            raise DigestError("trusted transition clock returned an invalid time")
        when = when.astimezone(dt.timezone.utc)
        transition_error: str | None = None

        def transform(current):
            nonlocal transition_error
            state = self._documents(current)
            row = next((item for item in state["batches"] if item["batch_id"] == batch_id), None)
            if row is None or row["claim_id"] != claim_id:
                raise DigestError("batch claim does not match selected state")
            if row["state"] == "claimed" and when >= parse_time(
                row["claim_expires_at"], "claim_expires_at"
            ):
                row["state"] = "failed"
                row["error"] = "claim_expired"
                transition_error = "batch claim has expired"
                _validate_batch(row)
                return {"digest.json": state}
            mutate(row, state)
            _validate_batch(row)
            return {"digest.json": state}

        self._update(transform)
        if transition_error is not None:
            raise DigestError(transition_error)

    def record_rendered(self, batch_id: str, rendered: str, claim_id: str) -> None:
        _bounded_text(rendered, "rendered digest", 100000)
        digest = hashlib.sha256(rendered.encode()).hexdigest()

        def mutate(row, state):
            if row["state"] != "claimed":
                raise DigestError("only a claimed batch may record rendering")
            row["state"] = "rendered"
            row["rendered_sha256"] = digest
            row["rendered_content"] = rendered
            state["processed_entry_ranges"] = _ranges_with_ids(
                state["processed_entry_ranges"], row["candidate_ids"]
            )

        self._transition(batch_id, claim_id, mutate)

    def fail(self, batch_id: str, claim_id: str, *, error: str) -> None:
        _bounded_text(error, "failure", 300)

        def mutate(row, state):
            if row["state"] != "claimed":
                raise DigestError("only a claimed batch may fail")
            row["state"] = "failed"
            row["error"] = error
            state["processed_entry_ranges"] = _ranges_with_ids(
                state["processed_entry_ranges"], row["candidate_ids"]
            )

        self._transition(batch_id, claim_id, mutate)

    def inspect(self) -> dict[str, Any]:
        try:
            selected = self.store.read()
        except StateAbsent:
            return _empty_state()
        return _validate_state(selected.documents.get("digest.json"))


def load_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DigestError(f"cannot read valid JSON from {path}") from exc


def write_json(value: Any) -> None:
    sys.stdout.write(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    gate = sub.add_parser("gate")
    gate.add_argument("--state-dir", required=True)
    gate.add_argument("--preferences", required=True)
    gate.add_argument("--candidates", required=True)
    gate.add_argument("--route", required=True)
    gate.add_argument("--now", required=True)
    gate.add_argument("--claim-ttl", type=int, default=1800)
    live_gate = sub.add_parser("live-gate")
    live_gate.add_argument("--state-dir", required=True)
    live_gate.add_argument("--preferences", required=True)
    live_gate.add_argument("--route", required=True)
    live_gate.add_argument("--now")
    live_gate.add_argument("--claim-ttl", type=int, default=1800)
    live_gate.add_argument("--endpoint-file", default=str(DEFAULT_ENDPOINT_FILE))
    live_gate.add_argument("--credential-file", default=str(DEFAULT_CREDENTIAL_FILE))
    for name in ("rendered", "fail"):
        command = sub.add_parser(name)
        command.add_argument("--state-dir", required=True)
        command.add_argument("--batch-id", required=True)
        command.add_argument("--claim-id", required=True)
        if name == "rendered":
            command.add_argument("--rendered-file", required=True)
        elif name == "fail":
            command.add_argument("--error", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--state-dir", required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        protocol = DigestProtocol(args.state_dir)
        if args.command in {"gate", "live-gate"}:
            preferences = load_json(args.preferences)
            if args.command == "live-gate" and not validate_preferences(preferences)["enabled"]:
                write_json({"wakeAgent": False})
                return 0
            if args.command == "gate":
                candidates = load_json(args.candidates)
                now = args.now
            else:
                candidates = MinifluxDigestClient(
                    Path(args.endpoint_file), Path(args.credential_file)
                ).collect(validate_preferences(preferences)["category_ids"])
                now = args.now or format_time(dt.datetime.now(dt.timezone.utc))
            result = protocol.claim(
                now,
                preferences,
                candidates,
                args.route,
                args.claim_ttl,
            )
            if result["status"] == "claimed":
                context = dict(result)
                context["state_dir"] = str(Path(args.state_dir).absolute())
                write_json({"wakeAgent": True, "context": context})
            elif result["status"] in NOOP:
                write_json({"wakeAgent": False})
            else:
                raise DigestError("gate returned an unknown state")
        elif args.command == "rendered":
            protocol.record_rendered(
                args.batch_id,
                Path(args.rendered_file).read_text(encoding="utf-8"),
                args.claim_id,
            )
            write_json({"status": "rendered"})
        elif args.command == "fail":
            protocol.fail(args.batch_id, args.claim_id, error=args.error)
            write_json({"status": "failed"})
        else:
            write_json(protocol.inspect())
    except (DigestError, OSError) as exc:
        sys.stderr.write(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}, sort_keys=True) + "\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
