from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from hermes_repository_instance import (
    instance,
    repository_registry,
    instance_argument,
    activate_instance,
    RepositoryStore,
)
from hermes_workflow_state import ProtocolError, StateAbsent, canonical_json_bytes

SCHEMA_VERSION = 1
PROTOCOL_PREFIX = "kanban-vikunja"
DEFAULT_STATE_ROOT = Path("/var/lib/hermes-kanban-vikunja")
MAX_API_BYTES = 8 * 1024 * 1024
MAX_PAGES = 100
MAX_MAPPINGS = 5000
MAX_EVENTS = 20000
MAX_PENDING = 1000
COMMENT_REVISION_WINDOW = 128
BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TASK_RE = re.compile(r"^t_[0-9a-f]{8,64}$")


def key_re():
    return re.compile(
        re.escape(instance().namespace) + r"-(?:issue-[1-9][0-9]*-questions|pr-[1-9][0-9]*-merge)$"
    )


def marker_re():
    return re.compile(
        r"^\*\*Automation reference:\*\* `(" + key_re().pattern.removesuffix("$") + r")`$"
    )


KANBAN_LINK_PREFIX = "Hermes Kanban: "
COMMENT_AUTHOR = "kanban-vikunja-reconciler"
FORBIDDEN_AUTHORITY = (
    "This evidence is not merge, deploy, rebuild, rollback, verification, or closure authorization."
)


class InputError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def strict_json(raw: bytes | str, label: str = "JSON") -> Any:
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


def bounded_text(value: Any, label: str, limit: int, *, controls: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise InputError(f"{label} must be non-empty bounded text")
    if not controls and any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise InputError(f"{label} contains forbidden controls")
    return value


def canonical_origin(endpoint: str) -> str:
    if (
        not isinstance(endpoint, str)
        or endpoint != endpoint.strip()
        or any(ord(char) < 0x21 or ord(char) == 0x7F for char in endpoint)
    ):
        raise InputError("Vikunja endpoint contains whitespace/control characters or is empty")
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise InputError("Vikunja endpoint must be an exact HTTPS origin")
    if (
        parsed.path not in {"", "/", "/api/v1", "/api/v1/", "/api/v2", "/api/v2/"}
        or parsed.query
        or parsed.fragment
    ):
        raise InputError("Vikunja endpoint has an unsupported suffix")
    netloc = parsed.hostname + (f":{parsed.port}" if parsed.port is not None else "")
    origin = f"https://{netloc}"
    if endpoint not in {
        origin,
        origin + "/",
        origin + "/api/v1",
        origin + "/api/v1/",
        origin + "/api/v2",
        origin + "/api/v2/",
    }:
        raise InputError("Vikunja endpoint is not canonical")
    return origin


def read_secret(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise InputError("credential file is unavailable") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise InputError("credential file is unsafe")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise InputError("credential file is empty")
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def action_key(reference: str, action_type: str) -> str:
    if not key_re().fullmatch(reference) or action_type not in {"Questions", "Merge"}:
        raise InputError("human-action identity is invalid")
    if action_type == "Questions" and not reference.endswith("-questions"):
        raise InputError("Questions action conflicts with its canonical automation reference")
    if action_type == "Merge" and not reference.endswith("-merge"):
        raise InputError("Merge action conflicts with its canonical automation reference")
    return reference


def reference_is_selected(key: str, peers) -> bool:
    owners = [
        peer
        for peer in peers
        if re.fullmatch(
            re.escape(peer.namespace) + r"-(?:issue-[1-9][0-9]*-questions|pr-[1-9][0-9]*-merge)",
            key,
        )
    ]
    if len(owners) != 1:
        raise InputError("automation reference has unknown or ambiguous repository ownership")
    return owners[0] == instance()


def select_native_task(task, peers, mapped_ids):
    body = str(task.get("body") or "")
    if not any(line.startswith("Human action: ") for line in body.splitlines()):
        return True
    references = [
        line.removeprefix("Vikunja automation reference: ")
        for line in body.splitlines()
        if line.startswith("Vikunja automation reference: ")
    ]
    if len(references) != 1:
        raise InputError("projected Kanban task lacks exactly one repository reference")
    selected = reference_is_selected(references[0], peers)
    if not selected and task.get("id") in mapped_ids:
        raise InputError("mapped Kanban task changed repository ownership")
    return selected


def select_vikunja_task(task, peers, mapped_ids):
    markers = [
        line
        for line in str(task.get("description") or "").splitlines()
        if line.startswith("**Automation reference:**")
    ]
    if len(markers) != 1:
        raise InputError("linked task requires exactly one automation marker")
    match = re.fullmatch(r"\*\*Automation reference:\*\* `([^`]+)`", markers[0])
    if match is None:
        raise InputError("linked task automation marker is malformed")
    selected = reference_is_selected(match[1], peers)
    if not selected and task.get("id") in mapped_ids:
        raise InputError("mapped Vikunja task changed repository ownership")
    return selected


def stable_marker(key: str) -> str:
    if not isinstance(key, str) or not key_re().fullmatch(key):
        raise InputError("automation key is invalid")
    return f"**Automation reference:** `{key}`"


def normalize_markdown(value: str) -> str:
    return "\n".join(
        "* * *" if line.strip() in {"---", "* * *"} else line
        for line in value.rstrip("\n").splitlines()
    )


def parse_link(description: str) -> tuple[str, str, str]:
    bounded_text(description, "Vikunja task description", 50000)
    if "\r" in description:
        raise InputError("Vikunja task description must use canonical LF line endings")
    links = [line for line in description.splitlines() if line.startswith(KANBAN_LINK_PREFIX)]
    markers = [
        match.group(1)
        for line in description.splitlines()
        if (match := marker_re().fullmatch(line))
    ]
    if len(links) != 1 or len(markers) != 1:
        raise InputError("linked task requires exactly one Kanban reference and automation marker")
    identity = links[0][len(KANBAN_LINK_PREFIX) :]
    if identity.count("/") != 1:
        raise InputError("Kanban reference is malformed")
    board, task_id = identity.split("/", 1)
    key = markers[0]
    if (
        not BOARD_RE.fullmatch(board)
        or not TASK_RE.fullmatch(task_id)
        or not key_re().fullmatch(key)
    ):
        raise InputError("linked task identity is unsafe")
    return board, task_id, key


def comment_event_key(vikunja_task_id: int, comment: Mapping[str, Any]) -> str:
    comment_id = comment.get("id")
    if (
        type(vikunja_task_id) is not int
        or vikunja_task_id <= 0
        or type(comment_id) is not int
        or comment_id <= 0
    ):
        raise InputError("Vikunja comment identity is invalid")
    body = bounded_text(comment.get("comment"), "Vikunja comment", 20000)
    updated = bounded_text(
        comment.get("updated") or comment.get("created"), "Vikunja comment revision", 100
    )
    return "vc-" + digest(
        {"task": vikunja_task_id, "comment": comment_id, "updated": updated, "body": body}
    )


def completion_event_key(task: Mapping[str, Any]) -> str:
    task_id = task.get("id")
    if type(task_id) is not int or task_id <= 0 or type(task.get("done")) is not bool:
        raise InputError("Vikunja completion identity is invalid")
    return "vd-" + digest({"task": task_id, "done": task["done"]})


def _selector(edge: dict[str, Any], name: str) -> None:
    selector = exact_dict(
        edge["selector"], {"include", "exclude", "include_future_projects"}, f"{name} selector"
    )
    include = selector["include"]
    exclude = selector["exclude"]
    if (include is None) == (exclude is None):
        raise InputError(f"{name} must configure exactly one of include or exclude")
    selected = include if include is not None else exclude
    if (
        not isinstance(selected, list)
        or len(selected) != len(set(selected))
        or any(type(row) is not int or row <= 0 for row in selected)
    ):
        raise InputError(f"{name} project selector must be a unique positive-integer list")
    if type(selector["include_future_projects"]) is not bool:
        raise InputError(f"{name} include_future_projects must be boolean")
    if exclude is not None and selector["include_future_projects"] is not True:
        raise InputError(f"{name} exclude mode requires include_future_projects=true")
    if include is not None and selector["include_future_projects"]:
        raise InputError(f"{name} include mode cannot include future projects")


def validate_intake_ownership(claims: Sequence[Mapping[str, Any]]) -> None:
    """Reject two enabled owners for the same directional project intake."""
    owned: dict[tuple[str, int], str] = {}
    for claim in claims:
        if set(claim) != {"name", "direction", "enabled", "projects"}:
            raise InputError("intake ownership claim has unknown or missing fields")
        if type(claim["enabled"]) is not bool or claim["direction"] not in {
            "kanban-to-vikunja",
            "vikunja-to-kanban",
        }:
            raise InputError("intake ownership claim is invalid")
        projects = claim["projects"]
        if not isinstance(projects, list) or any(
            type(row) is not int or row <= 0 for row in projects
        ):
            raise InputError("intake ownership projects are invalid")
        if not claim["enabled"]:
            continue
        for project_id in projects:
            identity = (claim["direction"], project_id)
            if identity in owned:
                raise InputError(
                    f"overlapping enabled intake owners {owned[identity]!r} and {claim['name']!r} "
                    f"for {claim['direction']} project {project_id}"
                )
            owned[identity] = str(claim["name"])


def validate_config(value: Any) -> dict[str, Any]:
    if instance().human_project is None:
        raise InputError("human-action projection requires an explicit project binding")
    cfg = exact_dict(
        value, {"schema_version", "board", "project_owner", "edges"}, "projection config"
    )
    if cfg["schema_version"] != SCHEMA_VERSION or not BOARD_RE.fullmatch(str(cfg["board"])):
        raise InputError("projection schema or board is invalid")
    if cfg["project_owner"] != instance().human_owner or cfg["board"] != instance().board:
        raise InputError("projection owner/board differs from its instance binding")
    edges = exact_dict(cfg["edges"], {"kanban_to_vikunja", "vikunja_to_kanban"}, "projection edges")
    normalized = {
        "schema_version": 1,
        "board": cfg["board"],
        "project_owner": cfg["project_owner"],
        "edges": {},
    }
    for name, raw in edges.items():
        expected = {"enabled", "selector", "default_policy", "project_policies"}
        if name == "vikunja_to_kanban":
            expected.add("wake_on_human_comment")
        edge = exact_dict(raw, expected, name)
        if type(edge["enabled"]) is not bool or edge["default_policy"] not in {
            "linkedOnly",
            "taskAllowlist",
            "projectWide",
        }:
            raise InputError(f"{name} edge policy is invalid")
        if name == "vikunja_to_kanban" and type(edge["wake_on_human_comment"]) is not bool:
            raise InputError("wake_on_human_comment must be boolean")
        _selector(edge, name)
        policies = edge["project_policies"]
        if not isinstance(policies, dict):
            raise InputError(f"{name} project policies must be an object")
        clean_policies: dict[str, dict[str, Any]] = {}
        for project, policy in policies.items():
            if not re.fullmatch(r"[1-9][0-9]*", str(project)):
                raise InputError("project policy key is invalid")
            policy = exact_dict(policy, {"mode", "task_allowlist"}, f"project policy {project}")
            if policy["mode"] not in {"linkedOnly", "taskAllowlist", "projectWide"}:
                raise InputError("project intake mode is invalid")
            allowlist = policy["task_allowlist"]
            if (
                not isinstance(allowlist, list)
                or len(allowlist) != len(set(allowlist))
                or any(type(row) is not int or row <= 0 for row in allowlist)
            ):
                raise InputError("task allowlist is invalid")
            if policy["mode"] == "taskAllowlist" and not allowlist:
                raise InputError("taskAllowlist mode requires at least one task")
            if policy["mode"] != "taskAllowlist" and allowlist:
                raise InputError("task allowlist is only valid in taskAllowlist mode")
            clean_policies[str(project)] = {
                "mode": policy["mode"],
                "task_allowlist": list(allowlist),
            }
        selector = edge["selector"]
        include = selector["include"]
        exclude = selector["exclude"]
        human_project_selected = include is not None and instance().human_project in include
        if exclude is not None:
            human_project_selected = instance().human_project not in exclude
        if (
            edge["enabled"]
            and human_project_selected
            and clean_policies.get(str(instance().human_project))
            != {"mode": "linkedOnly", "task_allowlist": []}
        ):
            raise InputError("configured human-action project is permanently linkedOnly")
        normalized_edge = dict(edge)
        normalized_edge["project_policies"] = clean_policies
        # Include mode has an enumerable static claim. Exclude mode is resolved
        # against bounded live project discovery; only configured human-action project can be
        # represented here for collision checks, and only when it is not
        # explicitly excluded. Exclude mode never upgrades its linkedOnly policy.
        normalized_edge["selected_projects"] = (
            sorted(include)
            if include is not None
            else ([instance().human_project] if human_project_selected else [])
        )
        normalized["edges"][name] = normalized_edge
    validate_intake_ownership(
        [
            {
                "name": name,
                "direction": name.replace("_", "-"),
                "enabled": edge["enabled"],
                "projects": edge["selected_projects"],
            }
            for name, edge in normalized["edges"].items()
        ]
    )
    return normalized


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "cursor": 0,
        "mappings": {},
        "pending": {},
        "events": {},
        "retries": {},
        "comment_revisions": {},
        "task_revisions": {},
    }


def _validate_state(value: Any) -> dict[str, Any]:
    state = exact_dict(
        value,
        {
            "schema_version",
            "cursor",
            "mappings",
            "pending",
            "events",
            "retries",
            "comment_revisions",
            "task_revisions",
        },
        "projection state",
    )
    if state["schema_version"] != 1 or type(state["cursor"]) is not int or state["cursor"] < 0:
        raise InputError("projection state schema or cursor is invalid")
    for field, limit in (
        ("mappings", MAX_MAPPINGS),
        ("pending", MAX_PENDING),
        ("events", MAX_EVENTS),
        ("retries", MAX_PENDING),
        ("comment_revisions", MAX_MAPPINGS),
        ("task_revisions", MAX_MAPPINGS),
    ):
        if not isinstance(state[field], dict) or len(state[field]) > limit:
            raise InputError(f"projection state {field} is invalid or unbounded")
    for revision in state["comment_revisions"].values():
        if (
            not isinstance(revision, dict)
            or set(revision) != {"high_id", "recent"}
            or type(revision["high_id"]) is not int
            or revision["high_id"] < 0
            or not isinstance(revision["recent"], dict)
            or len(revision["recent"]) > COMMENT_REVISION_WINDOW
        ):
            raise InputError("projection comment revision window is malformed or unbounded")
    target_ids: set[int] = set()
    automation_keys: set[str] = set()
    mapping_fields = {
        "board",
        "kanban_task_id",
        "vikunja_project_id",
        "vikunja_task_id",
        "vikunja_url",
        "action_type",
        "automation_key",
        "desired_assignee",
        "desired_digest",
    }
    for source, mapping in state["mappings"].items():
        if not isinstance(mapping, dict) or set(mapping) != mapping_fields:
            raise InputError("projection mapping has unknown or missing fields")
        expected_source = f"{mapping['board']}/{mapping['kanban_task_id']}"
        if (
            source != expected_source
            or not BOARD_RE.fullmatch(str(mapping["board"]))
            or not TASK_RE.fullmatch(str(mapping["kanban_task_id"]))
            or type(mapping["vikunja_project_id"]) is not int
            or mapping["vikunja_project_id"] <= 0
            or type(mapping["vikunja_task_id"]) is not int
            or mapping["vikunja_task_id"] <= 0
            or mapping["action_type"] not in {"Questions", "Merge"}
            or action_key(str(mapping["automation_key"]), str(mapping["action_type"]))
            != mapping["automation_key"]
            or mapping["desired_assignee"] not in {None, instance().human_owner}
            or not re.fullmatch(r"[0-9a-f]{64}", str(mapping["desired_digest"]))
        ):
            raise InputError("projection mapping identity is malformed")
        parsed_url = urllib.parse.urlsplit(str(mapping["vikunja_url"]))
        if (
            parsed_url.scheme != "https"
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
            or parsed_url.path != f"/tasks/{mapping['vikunja_task_id']}"
        ):
            raise InputError("projection mapping URL is not canonical")
        if mapping["vikunja_task_id"] in target_ids or mapping["automation_key"] in automation_keys:
            raise InputError("projection mappings contain duplicate target or automation identity")
        target_ids.add(mapping["vikunja_task_id"])
        automation_keys.add(mapping["automation_key"])
    return state


def validate_legacy_projection(documents):
    state = _validate_state(exact_dict(documents, {"edge.json"}, "legacy projection")["edge.json"])
    evidence = False
    for mapping in state["mappings"].values():
        origin = canonical_origin(mapping["vikunja_url"].split("/tasks/", 1)[0])
        if (
            mapping["board"] != instance().board
            or mapping["vikunja_project_id"] != instance().human_project
        ):
            raise InputError("legacy mapping contradicts the configured board/project")
        instance().check_origin(origin, "vikunja")
        evidence = True
    for operation in state["pending"].values():
        if operation.get("kind") == "ensure_task":
            if (
                not str(operation.get("source", "")).startswith(instance().board + "/")
                or operation.get("desired", {}).get("project_id") != instance().human_project
            ):
                raise InputError("legacy pending intent contradicts the configured board/project")
            evidence = True
    if not evidence:
        raise InputError(
            "legacy projection has no board/project identity evidence; adoption blocked"
        )
    return state


class ProjectionState:
    def __init__(self, root: Path, protocol: str) -> None:
        if not root.is_absolute() or protocol not in {"kanban-to-vikunja", "vikunja-to-kanban"}:
            raise InputError("projection state root or direction is invalid")
        self.root = root
        self.protocol = protocol
        self.store = RepositoryStore(
            root / "store",
            protocol=f"{PROTOCOL_PREFIX}-{protocol}",
            schema_version=1,
            legacy_validator=validate_legacy_projection,
        )

    @contextmanager
    def run_lock(self, timeout: float = 30.0) -> Iterator[None]:
        self.root.mkdir(mode=0o750, parents=True, exist_ok=True)
        path = self.root / ".run.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o640)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("projection run lock is busy")
                    time.sleep(0.01)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def read(self) -> dict[str, Any]:
        try:
            return _validate_state(self.store.read().documents["edge.json"])
        except StateAbsent:
            return _empty_state()
        except KeyError as exc:
            raise InputError("projection generation lacks edge.json") from exc

    def change(self, transform: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        def apply(selected: Any) -> dict[str, Any]:
            current = (
                _empty_state()
                if selected is None
                else _validate_state(selected.documents["edge.json"])
            )
            updated = strict_json(canonical_json_bytes(current), "projection state copy")
            transform(updated)
            updated["cursor"] += 1
            return {"edge.json": _validate_state(updated)}

        return _validate_state(self.store.update(apply).documents["edge.json"])

    def pending(self, key: str, operation: dict[str, Any]) -> None:
        bounded_text(key, "pending key", 200)
        self.change(lambda state: state["pending"].__setitem__(key, operation))

    def failed(self, key: str, error: Exception) -> None:
        message = " ".join(str(error).split())[:300] or error.__class__.__name__

        def transform(state: dict[str, Any]) -> None:
            retry = state["retries"].get(key, {"attempts": 0, "error": None})
            retry["attempts"] += 1
            retry["error"] = message
            state["retries"][key] = retry

        self.change(transform)

    def confirm_mapping(self, source: str, mapping: dict[str, Any], pending_key: str) -> None:
        def transform(state: dict[str, Any]) -> None:
            state["mappings"][source] = mapping
            state["pending"].pop(pending_key, None)
            state["retries"].pop(pending_key, None)

        self.change(transform)

    def confirm_event(
        self,
        event_key: str,
        proof: dict[str, Any],
        pending_key: str | None = None,
        *,
        vikunja_task_id: int | None = None,
        comment_id: int | None = None,
        completion: bool = False,
    ) -> None:
        def transform(state: dict[str, Any]) -> None:
            proof["ack_cursor"] = state["cursor"] + 1
            state["events"][event_key] = proof
            if vikunja_task_id is not None and comment_id is not None:
                task_key = str(vikunja_task_id)
                revision = state["comment_revisions"].setdefault(
                    task_key, {"high_id": 0, "recent": {}}
                )
                revision["high_id"] = max(revision["high_id"], comment_id)
                revision["recent"][str(comment_id)] = event_key
                retained = sorted((int(key), key) for key in revision["recent"])[
                    -COMMENT_REVISION_WINDOW:
                ]
                revision["recent"] = {key: revision["recent"][key] for _, key in retained}
            if vikunja_task_id is not None and completion:
                state["task_revisions"][str(vikunja_task_id)] = event_key
            if pending_key:
                state["pending"].pop(pending_key, None)
                state["retries"].pop(pending_key, None)
            if len(state["events"]) > MAX_EVENTS:
                oldest = sorted(
                    state["events"],
                    key=lambda key: (state["events"][key].get("ack_cursor", 0), key),
                )
                for key in oldest[: len(state["events"]) - MAX_EVENTS]:
                    del state["events"][key]

        self.change(transform)


def _selected_projects(cfg: dict[str, Any], edge_name: str, vikunja: Any) -> list[int]:
    edge = cfg["edges"][edge_name]
    selector = edge["selector"]
    if selector["include"] is not None:
        projects = sorted(selector["include"])
    else:
        visible = vikunja.list_visible_projects()
        projects = sorted(row["id"] for row in visible if row["id"] not in selector["exclude"])
    validated = vikunja.validate_projects(projects, cfg["project_owner"])
    if set(validated) != set(projects):
        raise InputError("Vikunja project discovery did not exactly match selected projects")
    if instance().human_project in projects:
        project = validated[instance().human_project]
        if (
            project.get("title") != instance().human_project_title
            or (project.get("owner") or {}).get("username") != cfg["project_owner"]
        ):
            raise InputError("configured human-action project identity or ownership is invalid")
    return projects


def _exact_line(body: str, label: str, allowed: set[str]) -> str | None:
    matches = [line[len(label) :] for line in body.splitlines() if line.startswith(label)]
    if not matches:
        return None
    if len(matches) != 1 or matches[0] not in allowed:
        raise InputError(f"{label.rstrip(': ')} declaration is invalid or duplicated")
    return matches[0]


def parse_human_action(task: Mapping[str, Any]) -> dict[str, Any] | None:
    task_id = str(task.get("id") or "")
    if not TASK_RE.fullmatch(task_id):
        raise InputError("Kanban task id is invalid")
    body = task.get("body") or ""
    bounded_text(body, "Kanban task body", 100000)
    action = _exact_line(body, "Human action: ", {"Questions", "Merge"})
    if action is None:
        return None
    project = _exact_line(body, "Vikunja project: ", {str(instance().human_project)})
    assignee = _exact_line(body, "Vikunja assignee: ", {instance().human_owner, "none"})
    done = _exact_line(body, "Vikunja done: ", {"true", "false"})
    references = [
        line[len("Vikunja automation reference: ") :]
        for line in body.splitlines()
        if line.startswith("Vikunja automation reference: ")
    ]
    if project is None or assignee is None or done is None or len(references) != 1:
        raise InputError("projected Kanban task lacks the complete human-action contract")
    return {
        "action_type": action,
        "project_id": int(project),
        "assignee": None if assignee == "none" else assignee,
        "done": done == "true",
        "automation_key": action_key(references[0], action),
    }


def _description(task: Mapping[str, Any], action: Mapping[str, Any], board: str, key: str) -> str:
    body = str(task.get("body") or "").strip()
    return (
        f"{body}\n\n"
        "---\n\n"
        f"Action type: {action['action_type']}\n\n"
        f"Hermes Kanban: {board}/{task['id']}\n\n"
        f"{stable_marker(key)}"
    )


def desired_task(task: Mapping[str, Any], action: Mapping[str, Any], board: str) -> dict[str, Any]:
    key = str(action["automation_key"])
    title = bounded_text(task.get("title"), "Kanban task title", 500)
    desired = {
        "automation_key": key,
        "board": board,
        "kanban_task_id": str(task["id"]),
        "project_id": action["project_id"],
        "action_type": action["action_type"],
        "title": title,
        "description": _description(task, action, board, key),
        "done": action["done"],
        "assignee": action["assignee"],
    }
    desired["digest"] = digest(desired)
    return desired


def _assignee_names(task: Mapping[str, Any]) -> list[str]:
    assignees = task.get("assignees")
    if not isinstance(assignees, list):
        raise InputError("Vikunja task assignees are malformed")
    names = []
    for row in assignees:
        if not isinstance(row, Mapping) or not isinstance(row.get("username"), str):
            raise InputError("Vikunja task assignee lacks username")
        names.append(row["username"])
    return sorted(names)


def verify_vikunja_identity(row: Mapping[str, Any], desired: Mapping[str, Any]) -> dict[str, Any]:
    task_id = row.get("id")
    if type(task_id) is not int or task_id <= 0 or row.get("project_id") != desired["project_id"]:
        raise InputError("Vikunja task readback identity is invalid")
    board, kanban_id, key = parse_link(str(row.get("description") or ""))
    if (
        key != desired["automation_key"]
        or board != desired["board"]
        or kanban_id != desired["kanban_task_id"]
    ):
        raise InputError("Vikunja reciprocal reference does not match desired identity")
    url = str(row.get("url") or "")
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/tasks/{task_id}"
    ):
        raise InputError("Vikunja task URL is not canonical")
    return {
        "id": task_id,
        "url": url,
        "board": board,
        "kanban_task_id": kanban_id,
        "automation_key": key,
    }


def verify_vikunja_task(row: Mapping[str, Any], desired: Mapping[str, Any]) -> dict[str, Any]:
    proof = verify_vikunja_identity(row, desired)
    expected_assignees = [] if desired["assignee"] is None else [desired["assignee"]]
    if (
        row.get("title") != desired["title"]
        or normalize_markdown(str(row.get("description") or ""))
        != normalize_markdown(desired["description"])
        or row.get("done") is not desired["done"]
        or _assignee_names(row) != expected_assignees
    ):
        raise RuntimeError("Vikunja task readback differs from the requested human-action state")
    return proof


def verify_vikunja_core(row: Mapping[str, Any], desired: Mapping[str, Any]) -> None:
    """Prove a response-lost create before finishing its assignee step.

    Vikunja creates the task before the separate bulk-assignee request. A lost
    create response therefore leaves an exact task with no assignee. Recovery
    may finish that operation only when every create-payload field and the
    reciprocal identity already match exactly.
    """
    task_id = row.get("id")
    if type(task_id) is not int or task_id <= 0 or row.get("project_id") != desired["project_id"]:
        raise InputError("pending Vikunja task core identity is invalid")
    board, kanban_id, key = parse_link(str(row.get("description") or ""))
    if (
        key != desired["automation_key"]
        or board != desired["board"]
        or kanban_id != desired["kanban_task_id"]
        or row.get("title") != desired["title"]
        or normalize_markdown(str(row.get("description") or ""))
        != normalize_markdown(desired["description"])
        or row.get("done") is not desired["done"]
    ):
        raise InputError("pending Vikunja task does not match the exact committed create payload")


def _reciprocal_comment(row: Mapping[str, Any], desired: Mapping[str, Any]) -> tuple[str, str]:
    marker = f"kanban-vikunja-link:{desired['automation_key']}"
    body = (
        f"{marker}\n"
        f"Vikunja task ID: {row['id']}\n"
        f"Vikunja task URL: {row['url']}\n"
        f"Action type: {desired['action_type']}\n"
        f"Automation key: {desired['automation_key']}"
    )
    return marker, body


def reconcile_kanban_to_vikunja(
    cfg_value: Any, kanban: Any, vikunja: Any, state: ProjectionState, *, lifecycle: Any = None
) -> dict[str, int]:
    cfg = validate_config(cfg_value)
    edge = cfg["edges"]["kanban_to_vikunja"]
    if not edge["enabled"]:
        return {"created_or_updated": 0, "noop": 1, "failed": 0}
    projects = _selected_projects(cfg, "kanban_to_vikunja", vikunja)
    result = {"created_or_updated": 0, "noop": 0, "failed": 0}

    def prepare(task: Mapping[str, Any], action: Mapping[str, Any]) -> dict[str, Any]:
        if lifecycle is not None:
            task, action = lifecycle.resolve(task, action)
        return desired_task(task, action, cfg["board"])

    def confirm_source(
        task: Mapping[str, Any], action: Mapping[str, Any], desired: dict[str, Any]
    ) -> None:
        if lifecycle is not None:
            current = kanban.show(task["id"])["task"]
            if current != task:
                raise RuntimeError("PR anchor changed before the projection write")
            fresh = prepare(current, action)
            # Preserved history is target-owned evidence, not a lifecycle input.
            if "\n\n## Preserved legacy record\n\n" in desired["description"]:
                from pr_legacy_adoption import preserve_record

                fresh = preserve_record(fresh, desired)
            if fresh != desired:
                raise RuntimeError("PR lifecycle changed before the projection write")

    with state.run_lock():
        # Recover indeterminate writes before consulting the latest source
        # listing. A source task may have become archived between target commit
        # and response/readback; the persisted exact operation still has enough
        # authority to reconcile by native show plus stable target marker.
        for pending_key, operation in list(state.read()["pending"].items()):
            if operation.get("kind") != "ensure_task":
                raise InputError("Kanban-to-Vikunja state contains an unexpected pending operation")
            desired = operation.get("desired")
            source = operation.get("source")
            if (
                not isinstance(desired, dict)
                or not isinstance(source, str)
                or source.count("/") != 1
            ):
                raise InputError("pending Kanban-to-Vikunja operation is malformed")
            board, task_id = source.split("/", 1)
            if board != cfg["board"] or not TASK_RE.fullmatch(task_id):
                raise InputError("pending Kanban-to-Vikunja source identity is invalid")
            try:
                native = kanban.show(task_id)["task"]
                if native.get("id") != task_id:
                    raise InputError(
                        "pending Kanban task no longer reads back at its exact identity"
                    )
                matches = vikunja.find_by_marker(
                    desired["project_id"], stable_marker(desired["automation_key"])
                )
                if len(matches) > 1:
                    raise RuntimeError("multiple Vikunja tasks contain the pending stable marker")
                mapping = state.read()["mappings"].get(source)
                if mapping:
                    if not matches:
                        raise InputError("mapped Vikunja task is missing; refusing replacement")
                    proof = verify_vikunja_identity(matches[0], desired)
                    if mapping["vikunja_task_id"] != proof["id"]:
                        raise InputError("pending update targets a different mapped task")
                elif operation.get("legacy") is not None:
                    if not matches:
                        raise InputError("pending legacy task disappeared")
                    try:
                        verify_vikunja_identity(matches[0], desired)
                    except InputError:
                        from pr_legacy_adoption import validate_pending_legacy

                        validate_pending_legacy(matches[0], operation["legacy"])
                elif operation.get("target_id") is not None:
                    if not matches or matches[0].get("id") != operation["target_id"]:
                        raise InputError("recovered projection target is missing or changed")
                    verify_vikunja_identity(matches[0], desired)
                elif matches:
                    # Only the exact create payload proves an unmapped,
                    # response-lost creation. Never adopt an arbitrary marker.
                    verify_vikunja_core(matches[0], desired)
                action = parse_human_action(native)
                if action is None:
                    raise InputError("pending projection anchor lost its action contract")
                fresh = prepare(native, action)
                if matches:
                    from pr_legacy_adoption import preserve_record

                    fresh = preserve_record(fresh, desired)
                if fresh["automation_key"] != desired["automation_key"]:
                    raise InputError("pending projection anchor changed its PR identity")
                if lifecycle is not None and fresh != desired:
                    # The old outcome is now known by exact identity (or no
                    # create occurred). Persist a new intent before writing;
                    # never replay a stale human assignment after a head change.
                    operation = {**operation, "desired": fresh}
                    if matches:
                        operation["target_id"] = matches[0]["id"]
                    state.pending(pending_key, operation)
                    desired = fresh
                confirm_source(native, action, desired)
                row = vikunja.ensure_task(
                    {
                        **desired,
                        "legacy": operation.get("legacy"),
                        "target_id": operation.get("target_id"),
                    }
                )
                proof = verify_vikunja_task(row, desired)
                marker, body = _reciprocal_comment(row, desired)
                kanban.comment_once(task_id, body, marker)
                state.confirm_mapping(
                    source,
                    {
                        "board": board,
                        "kanban_task_id": task_id,
                        "vikunja_project_id": desired["project_id"],
                        "vikunja_task_id": proof["id"],
                        "vikunja_url": proof["url"],
                        "action_type": desired["action_type"],
                        "automation_key": desired["automation_key"],
                        "desired_assignee": desired["assignee"],
                        "desired_digest": desired["digest"],
                    },
                    pending_key,
                )
                result["created_or_updated"] += 1
            except (InputError, RuntimeError, OSError, ProtocolError, KeyError) as exc:
                state.failed(pending_key, exc)
                result["failed"] += 1

        peers = repository_registry()
        mapped_ids = {row["kanban_task_id"] for row in state.read()["mappings"].values()}
        tasks = {
            task["id"]: task
            for task in kanban.list_tasks()
            if select_native_task(task, peers, mapped_ids)
        }
        # A permanent PR anchor survives completed/archived execution cards.
        # Native show is authoritative; no mapping is copied to a continuation.
        for mapping in state.read()["mappings"].values():
            task_id = mapping["kanban_task_id"]
            if task_id not in tasks:
                tasks[task_id] = kanban.show(task_id)["task"]
        anchors: dict[str, str] = {}
        for task in tasks.values():
            action = parse_human_action(task)
            if action is not None:
                previous = anchors.setdefault(action["automation_key"], task["id"])
                if previous != task["id"]:
                    raise InputError("multiple native anchors claim the same human action")
        for task in tasks.values():
            action = parse_human_action(task)
            if action is None:
                continue
            if action["project_id"] not in projects:
                raise InputError("Kanban task requests a project outside the selected authority")
            desired = prepare(task, action)
            source = f"{cfg['board']}/{task['id']}"
            pending_key = "ensure:" + desired["automation_key"]
            current = state.read()
            mapping = current["mappings"].get(source)
            pending_operation = current["pending"].get(pending_key)
            if pending_operation is not None:
                # Recovery above failed. Preserve the exact unresolved intent;
                # a newer source must not overwrite it or attempt a second write.
                continue
            matches = vikunja.find_by_marker(
                action["project_id"], stable_marker(desired["automation_key"])
            )
            if len(matches) > 1:
                raise RuntimeError("multiple Vikunja tasks contain the stable automation marker")
            if mapping and not matches:
                raise InputError("mapped Vikunja task is missing; refusing replacement")
            if mapping and matches:
                from pr_legacy_adoption import preserve_record

                desired = preserve_record(desired, matches[0])
                proof = verify_vikunja_identity(matches[0], desired)
                if mapping.get("vikunja_task_id") != proof["id"]:
                    raise InputError("Vikunja marker moved away from its mapped task")
                try:
                    verify_vikunja_task(matches[0], desired)
                except RuntimeError:
                    pass  # Known identity, changed source or projection drift: update below.
                else:
                    if mapping.get("desired_digest") == desired["digest"]:
                        marker, body = _reciprocal_comment(matches[0], desired)
                        kanban.comment_once(task["id"], body, marker)
                        result["noop"] += 1
                        continue
            if (
                not matches
                and not mapping
                and any(
                    line.startswith("Vikunja legacy task: ")
                    for line in str(task.get("body") or "").splitlines()
                )
            ):
                raise InputError("explicit legacy target lacks its marker; refusing replacement")
            legacy = None
            if matches and not mapping and pending_operation is None:
                from pr_legacy_adoption import legacy_identity, preserve_record

                legacy = legacy_identity(task, matches[0], desired)
                desired = preserve_record(desired, matches[0], adopting=True)
            operation = {"kind": "ensure_task", "source": source, "desired": desired}
            if matches:
                operation["target_id"] = matches[0]["id"]
            if legacy is not None:
                operation["legacy"] = legacy
            state.pending(pending_key, operation)
            try:
                if matches and not mapping and legacy is None:
                    # An unmapped write still requires the exact committed
                    # create payload. Existing mapped tasks may change lifecycle.
                    verify_vikunja_core(matches[0], desired)
                confirm_source(task, action, desired)
                row = vikunja.ensure_task(
                    {
                        **desired,
                        "legacy": operation.get("legacy"),
                        "target_id": operation.get("target_id"),
                    }
                )
                proof = verify_vikunja_task(row, desired)
                marker, body = _reciprocal_comment(row, desired)
                kanban.comment_once(task["id"], body, marker)
                state.confirm_mapping(
                    source,
                    {
                        "board": cfg["board"],
                        "kanban_task_id": task["id"],
                        "vikunja_project_id": action["project_id"],
                        "vikunja_task_id": proof["id"],
                        "vikunja_url": proof["url"],
                        "action_type": action["action_type"],
                        "automation_key": desired["automation_key"],
                        "desired_assignee": desired["assignee"],
                        "desired_digest": desired["digest"],
                    },
                    pending_key,
                )
                result["created_or_updated"] += 1
            except (InputError, RuntimeError, OSError, ProtocolError) as exc:
                state.failed(pending_key, exc)
                result["failed"] += 1
        if result["created_or_updated"] == result["failed"] == 0 and result["noop"] == 0:
            result["noop"] = 1
    return result


def _mapped(
    mapping_state: dict[str, Any],
    board: str,
    task_id: str,
    vikunja_task: Mapping[str, Any],
    key: str,
) -> dict[str, Any]:
    source = f"{board}/{task_id}"
    mapping = mapping_state["mappings"].get(source)
    if not mapping:
        raise InputError("linked Vikunja task lacks a directional ledger mapping")
    if (
        mapping.get("vikunja_task_id") != vikunja_task.get("id")
        or mapping.get("vikunja_project_id") != vikunja_task.get("project_id")
        or mapping.get("automation_key") != key
    ):
        raise InputError("Vikunja reciprocal reference conflicts with the directional ledger")
    expected = [] if mapping.get("desired_assignee") is None else [mapping["desired_assignee"]]
    if _assignee_names(vikunja_task) != expected:
        raise InputError("Vikunja task assignee no longer matches the mapped authority contract")
    return mapping


def _comment_body(
    vikunja_task: Mapping[str, Any], comment: Mapping[str, Any], event_key: str
) -> str:
    author = bounded_text(
        (comment.get("author") or {}).get("username"), "Vikunja comment author", 100
    )
    text = bounded_text(comment.get("comment"), "Vikunja comment", 20000)
    return (
        f"kanban-vikunja-event:{event_key}\n"
        f"Vikunja comment evidence from {author} on task {vikunja_task['id']}:\n\n{text}\n\n"
        f"{FORBIDDEN_AUTHORITY}"
    )


def _completion_body(vikunja_task: Mapping[str, Any], event_key: str) -> str:
    return (
        f"kanban-vikunja-event:{event_key}\n"
        f"Vikunja completion evidence: human-action task {vikunja_task['id']} is marked complete.\n\n"
        "Completion records that the human acted. It does not authorize merge, deploy, rebuild, rollback, verification, or closure."
    )


def _human_route(kanban: Any, task_id: str, projected: Mapping[str, Any]) -> dict[str, Any]:
    from pr_projection_lifecycle import field

    anchor = kanban.show(task_id)["task"]
    body = str(anchor.get("body") or "")
    if field(body, "PR workflow") != "native-v1":
        return {"task_id": task_id, "wake": True}
    description = str(projected.get("description") or "")
    target, gate = field(description, "PR human continuation"), field(description, "PR human gate")
    if not target and not gate:
        return {"task_id": task_id, "wake": False}
    if not target or not gate or target.count("/") != 1:
        raise InputError("projected human continuation is incomplete")
    board, continuation = target.split("/", 1)
    if board != kanban.board or not TASK_RE.fullmatch(continuation) or continuation == task_id:
        raise InputError("projected human continuation leaves its native board/anchor")
    return {
        "task_id": continuation,
        "anchor": task_id,
        "gate": gate,
        "head": field(description, "Current PR head"),
        "requested_at": field(description, "PR human requested at"),
        "wake": True,
    }


def _wake_human_route(kanban: Any, route: Mapping[str, Any]) -> None:
    from pr_projection_lifecycle import field, phase_state

    if not route.get("wake"):
        return
    task_id = route["task_id"]
    if route.get("anchor"):
        envelope = kanban.show(task_id)
        task = envelope["task"]
        body, _ = phase_state(envelope)
        if (
            task.get("status") != "blocked"
            or task.get("assignee") != instance().implementer
            or field(body, "PR projection anchor") != f"{kanban.board}/{route['anchor']}"
            or field(body, "Human gate") != route["gate"]
            or field(body, "PR head") != route["head"]
        ):
            return  # Old input remains evidence; it cannot release a replacement gate.
    kanban.unblock_if_blocked(task_id)


def _validate_vikunja_source_contract(vikunja: Any) -> None:
    validator = getattr(vikunja, "validate_schema", None)
    if validator is not None:
        validator()


def reconcile_vikunja_to_kanban(
    cfg_value: Any,
    kanban: Any,
    vikunja: Any,
    state: ProjectionState,
    mapping_state: ProjectionState,
    *,
    actor_username: str | None = None,
) -> dict[str, int]:
    cfg = validate_config(cfg_value)
    actor_username = actor_username or instance().implementer
    edge = cfg["edges"]["vikunja_to_kanban"]
    if not edge["enabled"]:
        return {"evidence": 0, "noop": 1, "failed": 0}
    projects = _selected_projects(cfg, "vikunja_to_kanban", vikunja)
    mappings = mapping_state.read()
    mapped_task_ids = {row["vikunja_task_id"] for row in mappings["mappings"].values()}
    result = {"evidence": 0, "noop": 0, "failed": 0}
    with state.run_lock():
        # Recover response-lost native comments before refetching Vikunja. The
        # source comment/task may disappear after the native write commits; the
        # pending record therefore carries the exact target postcondition.
        for pending_key, operation in list(state.read()["pending"].items()):
            kind = operation.get("kind")
            if kind not in {"comment", "completion"}:
                raise InputError("Vikunja-to-Kanban state contains an unexpected pending operation")
            event_key = operation.get("event_key")
            task_id = operation.get("task_id")
            body = operation.get("body")
            marker = operation.get("marker")
            vikunja_task_id = operation.get("vikunja_task_id")
            comment_id = operation.get("comment_id")
            if (
                not isinstance(event_key, str)
                or not TASK_RE.fullmatch(str(task_id))
                or not isinstance(body, str)
                or marker != f"kanban-vikunja-event:{event_key}"
                or type(vikunja_task_id) is not int
                or (kind == "comment" and type(comment_id) is not int)
                or (kind == "completion" and comment_id is not None)
            ):
                raise InputError("pending Vikunja-to-Kanban operation is malformed")
            try:
                kanban.comment_once(task_id, body, marker)
                if kind == "comment" and edge["wake_on_human_comment"]:
                    _wake_human_route(
                        kanban, operation.get("route", {"task_id": task_id, "wake": False})
                    )
                state.confirm_event(
                    event_key,
                    {"kind": kind, "vikunja_task_id": vikunja_task_id, "kanban_task_id": task_id},
                    pending_key,
                    vikunja_task_id=vikunja_task_id,
                    comment_id=comment_id,
                    completion=kind == "completion",
                )
                result["evidence"] += 1
            except (InputError, RuntimeError, OSError, ProtocolError, KeyError) as exc:
                state.failed(pending_key, exc)
                result["failed"] += 1

        peers = repository_registry()
        for project_id in projects:
            policy = edge["project_policies"].get(
                str(project_id), {"mode": edge["default_policy"], "task_allowlist": []}
            )
            for task in vikunja.list_tasks(project_id):
                description = str(task.get("description") or "")
                # Other integrations also use Automation reference markers.
                # Only an explicit Kanban link or our committed mapping selects
                # a task. Keep mapped tasks selected even if their link is lost,
                # so damaged metadata cannot silently bypass validation.
                has_reference = KANBAN_LINK_PREFIX in description
                if (
                    policy["mode"] == "linkedOnly"
                    and not has_reference
                    and task.get("id") not in mapped_task_ids
                ):
                    continue
                if (
                    policy["mode"] == "taskAllowlist"
                    and task.get("id") not in policy["task_allowlist"]
                ):
                    continue
                if not has_reference:
                    raise InputError(
                        f"Vikunja task {task.get('id')} lacks a supported reciprocal reference"
                    )
                if not select_vikunja_task(task, peers, mapped_task_ids):
                    continue
                try:
                    board, task_id, key = parse_link(description)
                except InputError as exc:
                    raise InputError(f"Vikunja task {task.get('id')}: {exc}") from exc
                if board != cfg["board"]:
                    raise InputError("Vikunja task points at a different Kanban board")
                _mapped(mappings, board, task_id, task, key)
                current_state = state.read()
                seen = current_state["events"]
                revision = current_state["comment_revisions"].get(
                    str(task["id"]), {"high_id": 0, "recent": {}}
                )
                for comment in vikunja.list_comments(task["id"]):
                    event_key = comment_event_key(task["id"], comment)
                    comment_id = comment["id"]
                    retained_key = revision["recent"].get(str(comment_id))
                    if event_key in seen or retained_key == event_key:
                        continue
                    if comment_id <= revision["high_id"] and retained_key is None:
                        # The comment is older than the documented bounded edit
                        # overlap. Its original delivery remains represented by
                        # the durable high-water mark; it cannot replay merely
                        # because the detailed tombstone was compacted.
                        continue
                    author = (comment.get("author") or {}).get("username")
                    if author == actor_username or author == COMMENT_AUTHOR:
                        state.confirm_event(
                            event_key,
                            {"kind": "echo", "vikunja_task_id": task["id"]},
                            vikunja_task_id=task["id"],
                            comment_id=comment_id,
                        )
                        seen[event_key] = True
                        revision["high_id"] = max(revision["high_id"], comment_id)
                        revision["recent"][str(comment_id)] = event_key
                        continue
                    pending_key = "event:" + event_key
                    _validate_vikunja_source_contract(vikunja)
                    body = _comment_body(task, comment, event_key)
                    route = _human_route(kanban, task_id, task)
                    if route.get("anchor"):
                        from forgejo_event_journal import parse_time

                        requested = route.get("requested_at")
                        if (
                            author != cfg["project_owner"]
                            or not isinstance(requested, str)
                            or not requested.isdigit()
                            or int(requested) <= 0
                            or parse_time(
                                comment.get("created"), "human comment creation"
                            ).timestamp()
                            < int(requested)
                        ):
                            route["wake"] = False
                    marker = f"kanban-vikunja-event:{event_key}"
                    state.pending(
                        pending_key,
                        {
                            "kind": "comment",
                            "event_key": event_key,
                            "task_id": route["task_id"],
                            "route": route,
                            "body": body,
                            "marker": marker,
                            "vikunja_task_id": task["id"],
                            "comment_id": comment_id,
                        },
                    )
                    try:
                        kanban.comment_once(route["task_id"], body, marker)
                        if edge["wake_on_human_comment"]:
                            _wake_human_route(kanban, route)
                        state.confirm_event(
                            event_key,
                            {
                                "kind": "comment",
                                "vikunja_task_id": task["id"],
                                "kanban_task_id": task_id,
                            },
                            pending_key,
                            vikunja_task_id=task["id"],
                            comment_id=comment_id,
                        )
                        seen[event_key] = True
                        revision["high_id"] = max(revision["high_id"], comment_id)
                        revision["recent"][str(comment_id)] = event_key
                        result["evidence"] += 1
                    except (InputError, RuntimeError, OSError, ProtocolError) as exc:
                        state.failed(pending_key, exc)
                        result["failed"] += 1
                completion_key = completion_event_key(task)
                task_revision = state.read()["task_revisions"].get(str(task["id"]))
                if task.get("done") is True and completion_key != task_revision:
                    pending_key = "event:" + completion_key
                    _validate_vikunja_source_contract(vikunja)
                    body = _completion_body(task, completion_key)
                    marker = f"kanban-vikunja-event:{completion_key}"
                    state.pending(
                        pending_key,
                        {
                            "kind": "completion",
                            "event_key": completion_key,
                            "task_id": task_id,
                            "body": body,
                            "marker": marker,
                            "vikunja_task_id": task["id"],
                            "comment_id": None,
                        },
                    )
                    try:
                        # Completion is deliberately comment evidence only. This
                        # adapter has no complete/merge/deploy/rebuild verb.
                        kanban.comment_once(task_id, body, marker)
                        state.confirm_event(
                            completion_key,
                            {
                                "kind": "completion",
                                "vikunja_task_id": task["id"],
                                "kanban_task_id": task_id,
                            },
                            pending_key,
                            vikunja_task_id=task["id"],
                            completion=True,
                        )
                        result["evidence"] += 1
                    except (InputError, RuntimeError, OSError, ProtocolError) as exc:
                        state.failed(pending_key, exc)
                        result["failed"] += 1
        if result["evidence"] == result["failed"] == 0:
            result["noop"] = 1
    return result


class NativeKanban:
    def __init__(
        self,
        hermes: Path,
        board: str,
        hermes_home: Path,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        if (
            not hermes.is_absolute()
            or not hermes_home.is_absolute()
            or not BOARD_RE.fullmatch(board)
        ):
            raise InputError("Hermes adapter path, home, or board is invalid")
        self.hermes = hermes
        self.board = board
        self.hermes_home = hermes_home
        self._runner = runner

    def _run(self, args: Sequence[str], *, expect_json: bool = False) -> Any:
        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.hermes_home)
        env["HERMES_KANBAN_BOARD"] = self.board
        command = [str(self.hermes), "kanban", "--board", self.board, *args]
        result = self._runner(
            command, env=env, text=True, capture_output=True, timeout=60, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(
                "native Kanban command failed: " + " ".join(result.stderr.split())[:500]
            )
        return (
            strict_json(result.stdout, "native Kanban response") if expect_json else result.stdout
        )

    def list_tasks(self) -> list[dict[str, Any]]:
        result = self._run(["list", "--json"], expect_json=True)
        if isinstance(result, dict):
            result = result.get("tasks", result.get("items"))
        if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
            raise RuntimeError("native Kanban list returned an unexpected JSON envelope")
        return result

    def list_workflow_tasks(self) -> list[dict[str, Any]]:
        result = self._run(["list", "--archived", "--json"], expect_json=True)
        if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
            raise RuntimeError("native workflow list returned an invalid envelope")
        return result

    def show(self, task_id: str) -> dict[str, Any]:
        if not TASK_RE.fullmatch(task_id):
            raise InputError("Kanban task id is invalid")
        result = self._run(["show", task_id, "--json"], expect_json=True)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("task"), dict)
            or result["task"].get("id") != task_id
            or not isinstance(result.get("comments"), list)
        ):
            raise RuntimeError("native Kanban show returned the wrong response envelope")
        return result

    def comment_once(self, task_id: str, body: str, marker: str) -> None:
        bounded_text(body, "Kanban evidence comment", 30000)
        bounded_text(marker, "Kanban evidence marker", 300)

        def matches() -> list[dict[str, Any]]:
            rows = []
            for row in self.show(task_id)["comments"]:
                if not isinstance(row, dict):
                    raise TypeError("native Kanban comment readback is malformed")
                if marker in str(row.get("body") or ""):
                    rows.append(row)
            return rows

        existing = matches()
        if len(existing) > 1:
            raise RuntimeError("native Kanban contains duplicate evidence markers")
        if existing:
            if existing[0].get("body") != body or existing[0].get("author") != COMMENT_AUTHOR:
                raise RuntimeError("native Kanban marker collision lacks exact trusted evidence")
            return
        try:
            self._run(["comment", task_id, body, "--author", COMMENT_AUTHOR])
        except RuntimeError:
            existing = matches()
            if (
                len(existing) == 1
                and existing[0].get("body") == body
                and existing[0].get("author") == COMMENT_AUTHOR
            ):
                return
            raise
        existing = matches()
        if (
            len(existing) != 1
            or existing[0].get("body") != body
            or existing[0].get("author") != COMMENT_AUTHOR
        ):
            raise RuntimeError("native Kanban comment did not read back exactly")

    def unblock_if_blocked(self, task_id: str) -> None:
        before = self.show(task_id)["task"]
        if before.get("status") != "blocked":
            return
        self._run(["unblock", task_id])
        after = self.show(task_id)["task"]
        if after.get("status") not in {"ready", "todo", "review"}:
            raise RuntimeError("native Kanban unblock did not reach a supported source phase")


class VikunjaClient:
    def __init__(
        self,
        endpoint: str,
        credential_file: Path,
        *,
        opener: Callable[..., Any] | None = None,
        token: str | None = None,
    ) -> None:
        self.origin = canonical_origin(endpoint)
        instance().check_origin(self.origin, "vikunja")
        self.token = token if token is not None else read_secret(credential_file)
        self._opener = opener or urllib.request.build_opener(NoRedirect()).open
        self.owner_ids: dict[int, int] = {}
        self._schema_validated = False

    def _allowed(self, method: str, path: str) -> None:
        clean = path.split("?", 1)[0]
        patterns = {
            ("GET", "/openapi.json"),
        }
        if (method, clean) in patterns:
            return
        allowed = [
            ("GET", r"/projects(?:/[1-9][0-9]*)?"),
            ("GET", r"/projects/[1-9][0-9]*/tasks"),
            ("POST", r"/projects/[1-9][0-9]*/tasks"),
            ("GET", r"/tasks/[1-9][0-9]*"),
            ("PUT", r"/tasks/[1-9][0-9]*"),
            ("GET", r"/tasks/[1-9][0-9]*/comments"),
            ("PUT", r"/tasks/[1-9][0-9]*/assignees/bulk"),
        ]
        if not any(
            method == wanted and re.fullmatch(pattern, clean) for wanted, pattern in allowed
        ):
            raise InputError("Vikunja API operation escapes the typed allowlist")

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        self._allowed(method, path)
        url = self.origin + "/api/v2" + path
        body = canonical_json_bytes(payload) if payload is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener(request, timeout=30) as response:
                raw = response.read(MAX_API_BYTES + 1)
                if len(raw) > MAX_API_BYTES:
                    raise RuntimeError("Vikunja response exceeds the bounded body limit")
                return strict_json(raw, "Vikunja response") if raw else None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Vikunja {method} failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Vikunja {method} failed: {exc.reason}") from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def put(self, path: str, payload: Mapping[str, Any]) -> Any:
        return self._request("PUT", path, payload)

    def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        return self._request("POST", path, payload)

    def pages(self, path: str, *, per_page: int = 100) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        expected_pages: int | None = None
        for page in range(1, MAX_PAGES + 1):
            separator = "&" if "?" in path else "?"
            response = self.get(f"{path}{separator}page={page}&per_page={per_page}")
            if (
                not isinstance(response, dict)
                or not isinstance(response.get("items"), list)
                or type(response.get("total_pages")) is not int
            ):
                raise RuntimeError("Vikunja pagination response is malformed")
            total_pages = cast(int, response["total_pages"])
            if expected_pages is None:
                expected_pages = total_pages
                if expected_pages < 0 or expected_pages > MAX_PAGES:
                    raise RuntimeError("Vikunja pagination exceeds its request bound")
            elif total_pages != expected_pages:
                raise RuntimeError("Vikunja pagination metadata changed during traversal")
            page_rows = response["items"]
            if not all(isinstance(row, dict) for row in page_rows):
                raise RuntimeError("Vikunja pagination items are malformed")
            rows.extend(page_rows)
            if page >= expected_pages:
                return rows
        raise RuntimeError("Vikunja pagination did not terminate")

    def validate_schema(self) -> None:
        if self._schema_validated:
            return
        schema = self.get("/openapi.json")
        paths = schema.get("paths") if isinstance(schema, dict) else None
        required = {
            "/projects/{project}/tasks": {"get", "post"},
            "/tasks/{projecttask}": {"get", "put"},
            "/tasks/{task}/comments": {"get"},
            "/tasks/{projecttask}/assignees/bulk": {"put"},
        }
        if not isinstance(paths, dict):
            raise InputError("Vikunja OpenAPI paths are unavailable")
        for path, methods in required.items():
            operations = paths.get(path)
            if not isinstance(operations, dict) or not methods.issubset(operations):
                raise InputError(f"Vikunja OpenAPI lacks required typed operation {path}")
        self._schema_validated = True

    def list_visible_projects(self) -> list[dict[str, Any]]:
        return self.pages("/projects")

    def validate_projects(self, project_ids: list[int], owner: str) -> dict[int, dict[str, Any]]:
        result = {}
        for project_id in project_ids:
            row = self.get(f"/projects/{project_id}")
            if not isinstance(row, dict) or row.get("id") != project_id:
                raise InputError("Vikunja project readback identity is invalid")
            project_owner = row.get("owner") or {}
            if project_owner.get("username") != owner or type(project_owner.get("id")) is not int:
                raise InputError("Vikunja project ownership mismatch")
            self.owner_ids[project_id] = project_owner["id"]
            result[project_id] = row
        return result

    def list_tasks(self, project_id: int) -> list[dict[str, Any]]:
        return [
            self._task_url(row)
            for row in self.pages(f"/projects/{project_id}/tasks?format=markdown")
        ]

    def get_task(self, task_id: int) -> dict[str, Any]:
        row = self.get(f"/tasks/{task_id}?format=markdown")
        if not isinstance(row, dict) or row.get("id") != task_id:
            raise InputError("Vikunja task readback identity is invalid")
        return self._task_url(row)

    def _task_url(self, row: dict[str, Any]) -> dict[str, Any]:
        task_id = row.get("id")
        if type(task_id) is not int or task_id <= 0:
            raise InputError("Vikunja task lacks a stable id")
        row = dict(row)
        row["url"] = self.origin + f"/tasks/{task_id}"
        return row

    def find_by_marker(self, project_id: int, marker: str) -> list[dict[str, Any]]:
        return [
            row
            for row in self.list_tasks(project_id)
            if marker in str(row.get("description") or "")
        ]

    def ensure_task(self, desired: Mapping[str, Any]) -> dict[str, Any]:
        self.validate_schema()
        project_id = desired["project_id"]
        marker = stable_marker(desired["automation_key"])
        matches = self.find_by_marker(project_id, marker)
        if len(matches) > 1:
            raise RuntimeError("Vikunja stable marker is duplicated")
        payload = {field: desired[field] for field in ("title", "description", "done")}
        if desired.get("target_id") is not None:
            if not matches or matches[0]["id"] != desired["target_id"]:
                raise InputError("Vikunja target disappeared or changed before mutation")
        if matches:
            task_id = matches[0]["id"]
            current = self.get_task(task_id)
            if current.get("project_id") != project_id:
                raise InputError("Vikunja update target moved to a different project")
            try:
                board, native_id, key = parse_link(desired["description"])
                verify_vikunja_identity(
                    current,
                    {**desired, "board": board, "kanban_task_id": native_id, "automation_key": key},
                )
            except InputError:
                if desired.get("legacy") is None:
                    raise
                from pr_legacy_adoption import validate_pending_legacy

                validate_pending_legacy(current, desired["legacy"])
            # PUT is the rich-text-aware v2 operation; generic PATCH does not
            # accept format=markdown. Preserve non-owned writable task fields
            # because PUT replaces them rather than performing a partial update.
            retained = {
                field: current[field]
                for field in (
                    "bucket_id",
                    "cover_image_attachment_id",
                    "due_date",
                    "end_date",
                    "hex_color",
                    "is_favorite",
                    "percent_done",
                    "priority",
                    "reminders",
                    "repeat_after",
                    "repeat_mode",
                    "start_date",
                )
                if field in current
            }
            self.put(
                f"/tasks/{task_id}?format=markdown",
                {**retained, **payload, "project_id": project_id},
            )
        else:
            created = self.post(f"/projects/{project_id}/tasks?format=markdown", payload)
            if not isinstance(created, dict) or type(created.get("id")) is not int:
                raise RuntimeError("Vikunja task creation returned no stable identity")
            task_id = created["id"]
        assignee = desired["assignee"]
        if assignee is None:
            assignees: list[dict[str, int]] = []
        elif assignee == instance().human_owner and project_id in self.owner_ids:
            assignees = [{"id": self.owner_ids[project_id]}]
        else:
            raise InputError("Vikunja assignee is not the validated project owner")
        self.put(f"/tasks/{task_id}/assignees/bulk", {"assignees": assignees})
        return self.get_task(task_id)

    def list_comments(self, task_id: int) -> list[dict[str, Any]]:
        return self.pages(f"/tasks/{task_id}/comments?format=markdown")


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        raise InputError("config path must be absolute")
    cfg = strict_json(path.read_bytes(), "projection config")
    # Reconciliation validates and normalizes the source schema itself. Do not
    # pass derived fields such as selected_projects back into that boundary.
    validate_config(cfg)
    return cfg


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic native Kanban and Vikunja human-action projection"
    )
    instance_argument(parser)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--hermes", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--actor-username", required=True)
    parser.add_argument("--pr-lifecycle", action="store_true")
    parser.add_argument("--forgejo-endpoint")
    parser.add_argument("--forgejo-credential-file", type=Path)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("direction", choices=("kanban-to-vikunja", "vikunja-to-kanban"))
    args = parser.parse_args(argv)
    activate_instance(args.instance_config, review_executor="hermes")
    instance().check_origin(canonical_origin(args.endpoint), "vikunja")
    if args.forgejo_endpoint:
        instance().check_origin(
            args.forgejo_endpoint.removesuffix("/api/v1").rstrip("/"), "forgejo"
        )
    cfg = load_config(args.config)
    kanban = NativeKanban(args.hermes, cfg["board"], args.hermes_home)
    vikunja = VikunjaClient(args.endpoint, args.credential_file)
    if args.direction == "kanban-to-vikunja":
        state = ProjectionState(args.state_root / "kanban-to-vikunja", "kanban-to-vikunja")
        lifecycle = None
        if args.pr_lifecycle:
            if not args.forgejo_endpoint or not args.forgejo_credential_file:
                raise InputError(
                    "PR lifecycle requires its owning profile's Forgejo read interface"
                )
            from forgejo_kanban_workflow import ForgejoClient
            from pr_projection_lifecycle import PrLifecycle

            lifecycle = PrLifecycle(
                ForgejoClient(args.forgejo_endpoint, args.forgejo_credential_file),
                kanban,
                cfg["board"],
            )
        result = reconcile_kanban_to_vikunja(cfg, kanban, vikunja, state, lifecycle=lifecycle)
    else:
        state = ProjectionState(args.state_root / "vikunja-to-kanban", "vikunja-to-kanban")
        mapping = ProjectionState(args.state_root / "kanban-to-vikunja", "kanban-to-vikunja")
        actor = bounded_text(args.actor_username, "Vikunja bot username", 100)
        result = reconcile_vikunja_to_kanban(
            cfg, kanban, vikunja, state, mapping, actor_username=actor
        )
    if result.get("failed"):
        print(json.dumps(result, sort_keys=True))
        return 1
    # Healthy no-op is deliberately silent. Actionable deterministic work emits
    # one bounded machine-readable line for journald/metrics collection only.
    if not result.get("noop"):
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InputError, ProtocolError, RuntimeError, OSError) as exc:
        print(f"kanban-vikunja-projection: {exc}", file=sys.stderr)
        raise SystemExit(1)
