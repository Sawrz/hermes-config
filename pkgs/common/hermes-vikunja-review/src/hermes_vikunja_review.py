from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from hermes_workflow_state import GenerationStore, ProtocolError, canonical_json_bytes

SCHEMA_VERSION = 1
STATE_DOCUMENT_VERSION = 2
STATE_PROTOCOL = "vikunja-reviews"
ALLOWED_ROLES = {"due", "starting", "inbox", "waiting", "someday", "overdue", "recurring"}
TASK_REQUIRED = {
    "id",
    "title",
    "project_id",
    "due_date",
    "start_date",
    "done",
    "priority",
    "repeat_after",
    "repeat_mode",
    "updated",
}
FILTER_KEYS = {"filter", "filter_include_nulls", "s", "sort_by", "order_by"}
MARKDOWN_ESCAPES = re.compile(r"([\\`*_{}\[\]()<>#+.!|~-])")


class ContractError(RuntimeError):
    """Input, API data, or state violates the review contract."""


def _exact_dict(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(char) < 32 for char in value)
    ):
        raise ContractError(f"{label} must be non-empty bounded text without controls")
    return value


def _positive_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError(f"{label} must be between {minimum} and {maximum}")
    return value


def validate_config(value: Any) -> dict[str, Any]:
    config = _exact_dict(
        value,
        {"schema_version", "api_version", "timezone", "inbox_project_id", "limits", "reviews"},
        "review config",
    )
    if config["schema_version"] != SCHEMA_VERSION:
        raise ContractError("review config schema is incompatible")
    if config["api_version"] != "v2":
        raise ContractError("review config requires Vikunja API v2")
    _bounded_text(config["timezone"], "timezone", 100)
    _positive_int(config["inbox_project_id"], "Inbox project id", 1, 2**63 - 1)
    limits = _exact_dict(
        config["limits"],
        {"per_page", "max_pages", "max_tasks", "timeout_seconds"},
        "review limits",
    )
    _positive_int(limits["per_page"], "page size", 1, 100)
    _positive_int(limits["max_pages"], "page cap", 1, 20)
    _positive_int(limits["max_tasks"], "task cap", 1, 500)
    _positive_int(limits["timeout_seconds"], "request timeout", 1, 60)
    reviews = _exact_dict(config["reviews"], {"daily", "weekly"}, "reviews")
    filter_ids: list[int] = []
    inbox_owners: list[tuple[str, str]] = []
    for kind in ("daily", "weekly"):
        review = _exact_dict(reviews[kind], {"sections"}, f"{kind} review")
        sections = review["sections"]
        if not isinstance(sections, list) or not 1 <= len(sections) <= 12:
            raise ContractError(f"{kind} review must contain 1 to 12 sections")
        names: set[str] = set()
        for section in sections:
            section = _exact_dict(section, {"name", "role", "saved_filter_id"}, f"{kind} section")
            name = _bounded_text(section["name"], "section name", 80)
            if name in names:
                raise ContractError(f"{kind} section names must be unique")
            names.add(name)
            role = section["role"]
            if role not in ALLOWED_ROLES:
                raise ContractError(f"unsupported review section role: {role}")
            filter_id = _positive_int(section["saved_filter_id"], "saved filter id", 1, 2**63 - 1)
            filter_ids.append(filter_id)
            if role == "inbox":
                inbox_owners.append((kind, name))
    if len(filter_ids) != len(set(filter_ids)):
        raise ContractError("saved filter ids must be unique across daily and weekly reviews")
    if len(inbox_owners) != 1 or inbox_owners[0][0] != "weekly":
        raise ContractError("weekly review must be the sole Inbox owner")
    return config


def _validate_runtime_preferences(value: Any) -> dict[str, Any]:
    document = _exact_dict(value, {"schema_version", "values"}, "review preferences")
    if document["schema_version"] != 1:
        raise ContractError("review preference schema is incompatible")
    values = _exact_dict(
        document["values"],
        {
            "enabled",
            "daily_hour",
            "weekly_hour",
            "weekly_weekday",
            "timezone",
            "render_style",
            "daily_sections",
            "weekly_sections",
        },
        "review preference values",
    )
    if type(values["enabled"]) is not bool:
        raise ContractError("review enabled preference must be boolean")
    _positive_int(values["daily_hour"], "daily review hour", 5, 10)
    _positive_int(values["weekly_hour"], "weekly review hour", 5, 10)
    if values["weekly_weekday"] not in {"saturday", "sunday"}:
        raise ContractError("weekly review weekday must be saturday or sunday")
    if values["timezone"] != "Europe/Berlin":
        raise ContractError("review timezone is outside the declarative contract")
    if values["render_style"] not in {"compact", "detailed"}:
        raise ContractError("review render style is unsupported")
    for key in ("daily_sections", "weekly_sections"):
        sections = values[key]
        if (
            not isinstance(sections, list)
            or not 1 <= len(sections) <= 12
            or len(sections) != len(set(sections))
        ):
            raise ContractError(f"{key} must be a bounded unique non-empty list")
        for section in sections:
            _bounded_text(section, f"{key} item", 80)
    return values


def effective_config(config: Mapping[str, Any], preferences: Any, kind: str) -> dict[str, Any]:
    validated = validate_config(config)
    values = _validate_runtime_preferences(preferences)
    if kind not in {"daily", "weekly"}:
        raise ContractError("review kind must be daily or weekly")
    inbox_names = {
        section["name"]
        for section in validated["reviews"]["weekly"]["sections"]
        if section["role"] == "inbox"
    }
    if not inbox_names <= set(values["weekly_sections"]):
        raise ContractError("weekly runtime preferences cannot remove the sole Inbox review")
    allowed = {section["name"] for section in validated["reviews"][kind]["sections"]}
    requested = values[f"{kind}_sections"]
    unknown = set(requested) - allowed
    if unknown:
        raise ContractError(
            f"runtime preferences contain unauthorized {kind} sections: {sorted(unknown)}"
        )
    if kind == "weekly":
        inbox_names = {
            section["name"]
            for section in validated["reviews"]["weekly"]["sections"]
            if section["role"] == "inbox"
        }
        if not inbox_names <= set(requested):
            raise ContractError("weekly runtime preferences cannot remove the sole Inbox review")
    result = json.loads(json.dumps(validated))
    result["reviews"][kind]["sections"] = [
        section
        for section in result["reviews"][kind]["sections"]
        if section["name"] in set(requested)
    ]
    return validate_config(result)


class ReviewClient(Protocol):
    def get(self, path: str, params: Mapping[str, str | list[str]] | None = None) -> Any: ...


class FixtureClient:
    """Deterministic synthetic transport used by boundary fixtures."""

    def __init__(self, responses: Mapping[tuple[str, tuple[tuple[str, str], ...]], Any]):
        self.responses = dict(responses)
        self.calls: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []

    def get(self, path: str, params: Mapping[str, str | list[str]] | None = None) -> Any:
        normalized_items: list[tuple[str, str]] = []
        for key in sorted(params or {}):
            value = (params or {})[key]
            if isinstance(value, list):
                normalized_items.extend((str(key), str(item)) for item in value)
            else:
                normalized_items.append((str(key), str(value)))
        normalized = tuple(normalized_items)
        self.calls.append(("GET", path, normalized))
        key = (path, normalized)
        if key not in self.responses:
            raise ContractError(f"fixture has no response for GET {path}")
        return json.loads(json.dumps(self.responses[key]))


class _RejectRedirects(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ContractError("Vikunja redirect is forbidden")


class VikunjaClient:
    """Bounded API-v2 client. Scheduled collection exposes GET only."""

    def __init__(self, endpoint_file: Path, credential_file: Path, *, timeout: int = 20):
        self.endpoint = self._runtime_file(endpoint_file, "endpoint", secret=False).rstrip("/")
        credential = self._runtime_file(credential_file, "credential", secret=True)
        split = urlparse.urlsplit(self.endpoint)
        if (
            split.scheme != "https"
            or split.query
            or split.fragment
            or split.username
            or split.password
        ):
            raise ContractError("Vikunja endpoint must be a credential-free HTTPS URL")
        if split.path.rstrip("/") != "/api/v2":
            raise ContractError("Vikunja endpoint must end exactly in /api/v2")
        self.origin = urlparse.urlunsplit((split.scheme, split.netloc, "", "", ""))
        self.credential = credential
        self.timeout = _positive_int(timeout, "request timeout", 1, 60)
        self.opener = urlrequest.build_opener(_RejectRedirects())

    @staticmethod
    def _runtime_file(path: Path, label: str, *, secret: bool) -> str:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError as exc:
            raise ContractError(f"Vikunja {label} runtime file is missing") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ContractError(f"Vikunja {label} runtime file must be a regular non-symlink file")
        if stat.S_IMODE(mode) != 0o400:
            raise ContractError(f"Vikunja {label} runtime file must have mode 0400")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ContractError(f"Vikunja {label} runtime file cannot be safely opened") from exc
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            value = handle.read(4097)
        value = value.strip()
        if not value or len(value) > 4096 or any(ord(char) < 32 for char in value):
            raise ContractError(f"Vikunja {label} runtime file is malformed")
        if secret and not value.startswith("tk_"):
            raise ContractError("Vikunja credential is not an API token")
        return value

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, str | list[str]] | None = None,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        if not path.startswith("/") or ".." in urlparse.unquote(path) or "\\" in path:
            raise ContractError("Vikunja API path is unsafe")
        url = self.endpoint + path
        if params:
            url += "?" + urlparse.urlencode(sorted(params.items()), doseq=True)
        payload = None if body is None else canonical_json_bytes(body)
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.credential}",
        }
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = urlrequest.Request(url, data=payload, headers=request_headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
                response_headers = dict(response.headers.items())
        except urlerror.HTTPError as exc:
            detail = "HTTP error"
            try:
                problem = json.loads(exc.read(64_001))
                if isinstance(problem, dict) and isinstance(problem.get("detail"), str):
                    detail = problem["detail"][:300]
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise ContractError(f"Vikunja request failed with HTTP {exc.code}: {detail}") from exc
        except (OSError, urlerror.URLError) as exc:
            raise RuntimeError("Vikunja request failed") from exc
        if len(raw) > 2_000_000:
            raise ContractError("Vikunja response exceeds the size cap")
        try:
            return json.loads(raw), response_headers
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("Vikunja response is not valid JSON") from exc

    def get(self, path: str, params: Mapping[str, str | list[str]] | None = None) -> Any:
        return self._request("GET", path, params)[0]

    def patch(self, path: str, patch: Mapping[str, Any]) -> Any:
        return self._request("PATCH", path, body=patch)[0]


def _validate_filter(value: Any, expected_id: int) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("id") != expected_id:
        raise ContractError("saved filter identity readback did not match")
    filters = value.get("filters")
    if not isinstance(filters, dict) or set(filters) != FILTER_KEYS:
        raise ContractError("saved filter query contract is malformed")
    expression = _bounded_text(filters["filter"], "saved filter expression", 2000)
    search = filters["s"]
    if not isinstance(search, str) or len(search) > 500 or any(ord(char) < 32 for char in search):
        raise ContractError("saved filter search text is malformed")
    include_nulls = filters["filter_include_nulls"]
    if type(include_nulls) is not bool:
        raise ContractError("saved filter include-nulls value must be boolean")
    sort_by = filters["sort_by"]
    order_by = filters["order_by"]
    if (
        not isinstance(sort_by, list)
        or not isinstance(order_by, list)
        or not 1 <= len(sort_by) <= 5
        or len(sort_by) != len(order_by)
        or any(
            field not in {"id", "title", "due_date", "start_date", "priority", "updated"}
            for field in sort_by
        )
        or any(order not in {"asc", "desc"} for order in order_by)
    ):
        raise ContractError("saved filter ordering is unsupported or unbounded")
    query: dict[str, str | list[str]] = {
        "filter": expression,
        "filter_include_nulls": "true" if include_nulls else "false",
        "sort_by": list(sort_by),
        "order_by": list(order_by),
    }
    if search:
        query["q"] = search
    return query


def _validate_task(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not TASK_REQUIRED <= set(value):
        raise ContractError("task response is missing required review fields")
    task_id = _positive_int(value["id"], "task id", 1, 2**63 - 1)
    project_id = _positive_int(value["project_id"], "task project id", 1, 2**63 - 1)
    title = _bounded_text(value["title"], "task title", 500)
    if type(value["done"]) is not bool:
        raise ContractError("task done state must be boolean")
    if type(value["priority"]) is not int or not 0 <= value["priority"] <= 5:
        raise ContractError("task priority is outside the supported range")
    if type(value["repeat_after"]) is not int or not 0 <= value["repeat_after"] <= 10 * 365 * 86400:
        raise ContractError("task repeat_after is outside the supported range")
    if value["repeat_mode"] not in {0, 1, 2}:
        raise ContractError("task repeat_mode is unsupported")
    for field in ("due_date", "start_date", "updated"):
        if not isinstance(value[field], str) or len(value[field]) > 40:
            raise ContractError(f"task {field} is malformed")
    return {
        "id": task_id,
        "title": title,
        "project_id": project_id,
        "due_date": value["due_date"],
        "start_date": value["start_date"],
        "done": value["done"],
        "priority": value["priority"],
        "repeat_after": value["repeat_after"],
        "repeat_mode": value["repeat_mode"],
        "updated": value["updated"],
    }


def _task_sort_key(task: Mapping[str, Any]) -> tuple[str, int, int]:
    due = task["due_date"]
    if due.startswith("0001-") or not due:
        due = "9999-12-31T23:59:59Z"
    return due, -task["priority"], task["id"]


def _pages(
    client: ReviewClient, query: Mapping[str, str | list[str]], limits: Mapping[str, int]
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    expected_total: int | None = None
    for page in range(1, limits["max_pages"] + 1):
        params = dict(query, page=str(page), per_page=str(limits["per_page"]))
        envelope = client.get("/tasks", params)
        if not isinstance(envelope, dict) or set(envelope) != {
            "$schema",
            "items",
            "total",
            "page",
            "per_page",
            "total_pages",
        }:
            raise ContractError("task pagination envelope is malformed")
        schema = envelope["$schema"]
        if not isinstance(schema, str) or len(schema) > 1000 or not schema.startswith("https://"):
            raise ContractError("task pagination schema reference is malformed")
        if (
            type(envelope["page"]) is not int
            or type(envelope["per_page"]) is not int
            or type(envelope["total"]) is not int
            or type(envelope["total_pages"]) is not int
            or envelope["page"] != page
            or envelope["per_page"] != limits["per_page"]
            or envelope["total"] < 0
            or envelope["total_pages"] < 0
            or not isinstance(envelope["items"], list)
        ):
            raise ContractError("task pagination metadata is inconsistent")
        if envelope["total_pages"] == 0:
            if page != 1 or envelope["total"] != 0 or envelope["items"]:
                raise ContractError("zero-page task pagination is inconsistent")
            return []
        if envelope["total_pages"] > limits["max_pages"]:
            raise ContractError("task page cap exhausted before complete collection")
        if expected_total is None:
            expected_total = envelope["total"]
        elif expected_total != envelope["total"]:
            raise ContractError("task pagination total changed during collection")
        tasks.extend(_validate_task(item) for item in envelope["items"])
        if len(tasks) > limits["max_tasks"] or envelope["total"] > limits["max_tasks"]:
            raise ContractError("task cap exhausted before complete collection")
        if page == envelope["total_pages"]:
            break
    if expected_total is None or len(tasks) != expected_total:
        raise ContractError("task pagination did not produce the declared total")
    return tasks


def collect_review(
    client: ReviewClient, config: Mapping[str, Any], kind: str, now: dt.datetime
) -> dict[str, Any]:
    config = validate_config(config)
    if kind not in {"daily", "weekly"}:
        raise ContractError("review kind must be daily or weekly")
    if not isinstance(now, dt.datetime) or now.tzinfo is None:
        raise ContractError("review timestamp must be timezone-aware")
    seen: set[int] = set()
    sections: list[dict[str, Any]] = []
    inbox_project_id = config["inbox_project_id"]
    for section in config["reviews"][kind]["sections"]:
        filter_id = section["saved_filter_id"]
        query = _validate_filter(client.get(f"/filters/{filter_id}"), filter_id)
        rows = _pages(client, query, config["limits"])
        selected: list[dict[str, Any]] = []
        for row in rows:
            if kind == "daily" and row["project_id"] == inbox_project_id:
                raise ContractError("daily review saved filter leaked an Inbox task")
            if section["role"] == "inbox" and row["project_id"] != inbox_project_id:
                raise ContractError("weekly Inbox section leaked a non-Inbox task")
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            selected.append(row)
        sections.append(
            {
                "name": section["name"],
                "role": section["role"],
                "tasks": sorted(selected, key=_task_sort_key),
            }
        )
    return {
        "kind": kind,
        "generated_at": now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "sections": sections,
    }


def recurrence_semantics(task: Mapping[str, Any]) -> str:
    repeat_mode = task.get("repeat_mode")
    repeat_after = task.get("repeat_after")
    if repeat_mode == 1:
        return "monthly-calendar"
    if repeat_mode == 2:
        return "from-current-date"
    if type(repeat_after) is not int or repeat_after < 0:
        raise ContractError("task recurrence is malformed")
    if repeat_after == 0:
        return "none"
    if repeat_after == 90 * 86400:
        return "duration-90-days-not-quarterly"
    if repeat_after % 86400 == 0:
        return f"duration-{repeat_after // 86400}-days"
    if repeat_after % 3600 == 0:
        return f"duration-{repeat_after // 3600}-hours"
    return f"duration-{repeat_after}-seconds"


def _markdown(value: str) -> str:
    single_line = " ".join(value.splitlines())
    return MARKDOWN_ESCAPES.sub(r"\\\1", single_line)


def render_review(review: Mapping[str, Any]) -> str:
    sections = review.get("sections")
    if not isinstance(sections, list):
        raise ContractError("review sections are malformed")
    if not any(section.get("tasks") for section in sections):
        return ""
    kind = review.get("kind")
    if kind not in {"daily", "weekly"}:
        raise ContractError("review kind is malformed")
    lines = [f"# {_markdown(kind.title())} Vikunja review", ""]
    for section in sections:
        tasks = section.get("tasks")
        if not tasks:
            continue
        lines.extend(
            [f"## {_markdown(_bounded_text(section.get('name'), 'section name', 80))}", ""]
        )
        for task in tasks:
            semantics = recurrence_semantics(task)
            recurrence = "" if semantics == "none" else f"; recurrence: {semantics}"
            due = task["due_date"]
            due_text = "no due date" if not due or due.startswith("0001-") else due
            lines.append(
                f"- [ ] {_markdown(task['title'])} (#{task['id']}; due: {_markdown(due_text)}{recurrence})"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _initial_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_DOCUMENT_VERSION,
        "daily": {"content_sha256": None, "last_rendered": None},
        "weekly": {"content_sha256": None, "last_rendered": None},
    }


def _validate_state(value: Any) -> dict[str, Any]:
    state = _exact_dict(value, {"schema_version", "daily", "weekly"}, "review state")
    version = state["schema_version"]
    if version == 1:
        timestamp_field = "last_success"
    elif version == STATE_DOCUMENT_VERSION:
        timestamp_field = "last_rendered"
    else:
        raise ContractError("review state schema is incompatible")
    normalized: dict[str, Any] = {"schema_version": STATE_DOCUMENT_VERSION}
    for kind in ("daily", "weekly"):
        row = _exact_dict(
            state[kind],
            {"content_sha256", timestamp_field},
            f"{kind} review state",
        )
        digest = row["content_sha256"]
        if digest is not None and (
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ContractError("review state content digest is malformed")
        timestamp = row[timestamp_field]
        if timestamp is not None and (
            not isinstance(timestamp, str) or len(timestamp) > 40 or not timestamp.endswith("Z")
        ):
            raise ContractError("review state timestamp is malformed")
        normalized[kind] = {
            "content_sha256": digest,
            "last_rendered": timestamp,
        }
    return normalized


def run_review(
    client: ReviewClient, config: Mapping[str, Any], kind: str, state_dir: Path, now: dt.datetime
) -> str:
    review = collect_review(client, config, kind, now)
    content = render_review(review)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    store = GenerationStore(state_dir, protocol=STATE_PROTOCOL, schema_version=SCHEMA_VERSION)
    changed = False

    def transform(selected: Any | None) -> dict[str, Any]:
        nonlocal changed
        if selected is None:
            state = _initial_state()
        else:
            if set(selected.documents) != {"review.json"}:
                raise ContractError("review state has an unexpected document set")
            state = _validate_state(selected.documents["review.json"])
        changed = state[kind]["content_sha256"] != digest
        updated = json.loads(json.dumps(state))
        updated[kind] = {
            "content_sha256": digest,
            "last_rendered": review["generated_at"],
        }
        return {"review.json": updated}

    try:
        store.update(transform)
    except ProtocolError as exc:
        raise ContractError(str(exc)) from exc
    return content if content and changed else ""


@dataclass(frozen=True)
class MutationPlan:
    task_id: int
    source_updated: str
    patch: dict[str, Any]
    confirmation: str
    expected_readback: dict[str, Any]


def plan_mutation(current: Mapping[str, Any], request: Any) -> MutationPlan:
    current_task = _validate_task(current)
    if not isinstance(request, dict) or not request:
        raise ContractError("mutation request must be a non-empty object")
    allowed = {"done", "due_date", "start_date", "priority", "title", "recurrence"}
    unknown = set(request) - allowed
    if unknown:
        raise ContractError(f"mutation request contains unsupported fields: {sorted(unknown)}")
    patch: dict[str, Any] = {}
    for field, value in request.items():
        if field == "done":
            if type(value) is not bool:
                raise ContractError("done mutation must be boolean")
            patch[field] = value
        elif field == "priority":
            if type(value) is not int or not 0 <= value <= 5:
                raise ContractError("priority mutation is outside the supported range")
            patch[field] = value
        elif field == "title":
            patch[field] = _bounded_text(value, "task title mutation", 500)
        elif field in {"due_date", "start_date"}:
            if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
                raise ContractError(f"{field} mutation must be a canonical UTC timestamp")
            try:
                dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ContractError(f"{field} mutation timestamp is invalid") from exc
            patch[field] = value
        elif field == "recurrence":
            if value == "quarterly":
                raise ContractError(
                    "exact quarterly recurrence is not representable by the deployed Vikunja task schema"
                )
            recurrence = {
                "none": {"repeat_after": 0, "repeat_mode": 0},
                "monthly": {"repeat_after": 0, "repeat_mode": 1},
                "weekly": {"repeat_after": 7 * 86400, "repeat_mode": 0},
            }
            if value not in recurrence:
                raise ContractError("requested recurrence is unsupported")
            patch.update(recurrence[value])
    canonical = canonical_json_bytes(
        {
            "task_id": current_task["id"],
            "source_updated": current_task["updated"],
            "patch": patch,
        }
    )
    digest = hashlib.sha256(canonical).hexdigest()
    return MutationPlan(
        task_id=current_task["id"],
        source_updated=current_task["updated"],
        patch=patch,
        confirmation=f"CONFIRM VIKUNJA TASK {current_task['id']} {digest}",
        expected_readback=patch,
    )


def apply_mutation(client: VikunjaClient, plan: MutationPlan, confirmation: str) -> dict[str, Any]:
    if confirmation != plan.confirmation:
        raise ContractError("mutation confirmation does not match the exact preview")
    current_task = _validate_task(client.get(f"/tasks/{plan.task_id}"))
    if current_task["id"] != plan.task_id:
        raise ContractError("mutation target readback identity does not match")
    if current_task["updated"] != plan.source_updated:
        raise ContractError("Vikunja task changed since preview; create a new preview")
    try:
        client.patch(f"/tasks/{plan.task_id}", plan.patch)
    except RuntimeError as exc:
        raise ContractError(
            "Vikunja mutation outcome is indeterminate; perform readback before any retry"
        ) from exc
    readback = _validate_task(client.get(f"/tasks/{plan.task_id}"))
    for field, expected in plan.expected_readback.items():
        if readback.get(field) != expected:
            raise ContractError(f"mutation readback did not confirm field: {field}")
    return readback


def _load_json(path: Path, label: str) -> Any:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ContractError(f"{label} file is missing") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ContractError(f"{label} file must be a regular non-symlink file")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} file is not valid JSON") from exc


def verify_no_agent_evidence(value: Any) -> None:
    evidence = _exact_dict(value, {"schema_version", "job", "run"}, "no-agent evidence")
    if evidence["schema_version"] != "vikunja.no-agent.evidence.v1":
        raise ContractError("no-agent evidence schema is incompatible")
    job = _exact_dict(evidence["job"], {"no_agent", "script", "prompt", "skills"}, "no-agent job")
    if job["no_agent"] is not True:
        raise ContractError("Vikunja review job must be no_agent")
    script = _bounded_text(job["script"], "Vikunja review script", 1000)
    if "hermes-vikunja-review run --kind " not in script:
        raise ContractError("Vikunja review job script is not the managed collector")
    if job["prompt"] is not None or job["skills"] != []:
        raise ContractError("script-only Vikunja review job cannot carry a prompt or skills")
    run = _exact_dict(
        evidence["run"],
        {
            "outcome",
            "execution_status",
            "scheduler_executions",
            "agent_sessions",
            "model_attempts",
            "model_successes",
            "input_tokens",
            "output_tokens",
            "deliveries",
        },
        "no-agent run",
    )
    if (
        run["outcome"] not in {"changed", "empty", "unchanged"}
        or run["execution_status"] != "success"
    ):
        raise ContractError("no-agent run outcome or execution status is unsupported")
    expected_zero = {
        "agent_sessions",
        "model_attempts",
        "model_successes",
        "input_tokens",
        "output_tokens",
    }
    for field in expected_zero:
        if run[field] != 0 or type(run[field]) is not int:
            raise ContractError(f"script-only Vikunja review must record {field}=0")
    if run["scheduler_executions"] != 1 or type(run["scheduler_executions"]) is not int:
        raise ContractError("no-agent evidence must cover exactly one scheduler execution")
    expected_deliveries = 1 if run["outcome"] == "changed" else 0
    if run["deliveries"] != expected_deliveries or type(run["deliveries"]) is not int:
        raise ContractError("no-agent delivery count does not match changed/silent outcome")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Deterministic Vikunja daily/weekly review collector"
    )
    commands = result.add_subparsers(dest="command", required=True)

    def add_api_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--endpoint-file",
            type=Path,
            default=Path("/run/hermes-credentials/services/vikunja/endpoint"),
        )
        command.add_argument(
            "--credential-file",
            type=Path,
            default=Path("/run/hermes-credentials/services/vikunja/credential"),
        )

    run = commands.add_parser("run", help="read-only script output for a native no_agent cron job")
    add_api_arguments(run)
    run.add_argument("--kind", choices=("daily", "weekly"), required=True)
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--preferences", type=Path, required=True)
    run.add_argument("--state-dir", type=Path, required=True)
    preview = commands.add_parser(
        "preview-mutation", help="interactive mutation preview; performs no write"
    )
    add_api_arguments(preview)
    preview.add_argument("--task-id", type=int, required=True)
    preview.add_argument("--request", type=Path, required=True)
    apply = commands.add_parser(
        "apply-mutation",
        help="interactive confirmed mutation with fresh revision check and readback",
    )
    add_api_arguments(apply)
    apply.add_argument("--task-id", type=int, required=True)
    apply.add_argument("--request", type=Path, required=True)
    apply.add_argument("--confirm", required=True)
    verify = commands.add_parser(
        "verify-no-agent", help="verify captured Hermes script-only run evidence"
    )
    verify.add_argument("--evidence", type=Path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "verify-no-agent":
            verify_no_agent_evidence(_load_json(args.evidence, "no-agent evidence"))
            sys.stdout.write("verified\n")
            return 0
        if args.command == "run":
            declared = validate_config(_load_json(args.config, "config"))
            preferences = _load_json(args.preferences, "preferences")
            config = effective_config(declared, preferences, args.kind)
            if not _validate_runtime_preferences(preferences)["enabled"]:
                return 0
            client = VikunjaClient(
                args.endpoint_file,
                args.credential_file,
                timeout=config["limits"]["timeout_seconds"],
            )
            output = run_review(
                client, config, args.kind, args.state_dir, dt.datetime.now(dt.timezone.utc)
            )
            sys.stdout.write(output)
            return 0
        if args.command in {"preview-mutation", "apply-mutation"}:
            task_id = _positive_int(args.task_id, "task id", 1, 2**63 - 1)
            client = VikunjaClient(args.endpoint_file, args.credential_file)
            current = client.get(f"/tasks/{task_id}")
            plan = plan_mutation(current, _load_json(args.request, "request"))
            if plan.task_id != task_id:
                raise ContractError("mutation target identity does not match the requested task")
            if args.command == "preview-mutation":
                sys.stdout.buffer.write(
                    canonical_json_bytes(
                        {
                            "task_id": plan.task_id,
                            "source_updated": plan.source_updated,
                            "patch": plan.patch,
                            "confirmation": plan.confirmation,
                            "expected_readback": plan.expected_readback,
                        }
                    )
                )
            else:
                readback = apply_mutation(client, plan, args.confirm)
                sys.stdout.buffer.write(
                    canonical_json_bytes(
                        {
                            "task_id": readback["id"],
                            "confirmed": plan.expected_readback,
                        }
                    )
                )
            return 0
        raise ContractError("unsupported command")
    except (ContractError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
