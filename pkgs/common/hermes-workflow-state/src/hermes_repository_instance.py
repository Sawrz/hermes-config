"""Explicit repository-instance contracts for the existing deterministic adapters.

There is one immutable contract per CLI invocation. Library callers use
instance_scope; subprocesses receive the same explicit contract path. No profile
skill or installation-specific default can supply machine authority.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator
from urllib.parse import urlsplit

from hermes_workflow_state import (
    GenerationStore,
    ProtocolError,
    SelectedGeneration,
    canonical_json_bytes,
)

ENV = "HERMES_REPOSITORY_INSTANCE"
_ACTIVE: ContextVar[RepositoryInstance | None] = ContextVar("repository_instance", default=None)
SLUG = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}")


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ProtocolError(f"{label}: required fields are {sorted(keys)}")
    return value


def _name(value, label):
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise ProtocolError(f"invalid {label}")
    return value


def _origin(value, label):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtocolError(f"invalid {label}")
    p = urlsplit(value)
    if (
        p.scheme != "https"
        or not p.hostname
        or p.username
        or p.password
        or p.path
        or p.query
        or p.fragment
    ):
        raise ProtocolError(f"{label} must be an exact HTTPS origin")
    try:
        port = p.port
    except ValueError as exc:
        raise ProtocolError(f"invalid {label} port") from exc
    netloc = p.hostname + (f":{port}" if port is not None else "")
    if value != "https://" + netloc or any(ord(c) < 33 for c in value):
        raise ProtocolError(f"{label} must use canonical host spelling")
    return value


@dataclass(frozen=True)
class RepositoryInstance:
    identity: str
    repository: str
    default_branch: str
    namespace: str
    workflow_key_prefix: str
    board: str
    implementer: str
    reviewer: str
    tenant: str
    automation_authors: tuple[str, ...]
    forgejo_origin: str | None
    vikunja_origin: str | None
    human_project: int | None
    human_owner: str | None
    human_project_title: str | None
    repository_job: str | None
    legacy_container_marker: str | None
    legacy_generations: tuple[tuple[str, str], ...]
    review_executor: str

    @classmethod
    def parse(cls, value: Any) -> RepositoryInstance:
        row = _object(
            value,
            {
                "schema_version",
                "review_executor",
                "id",
                "repository",
                "default_branch",
                "namespace",
                "workflow_key_prefix",
                "board",
                "roles",
                "tenant",
                "automation_authors",
                "forgejo_origin",
                "projection",
                "repository_job",
                "legacy_container_marker",
                "legacy_generations",
            },
            "repository instance",
        )
        if row["schema_version"] != 1 or type(row["schema_version"]) is not int:
            raise ProtocolError("unsupported repository instance schema")
        if row["review_executor"] not in ("hermes", "paperclip"):
            raise ProtocolError("unknown independent review executor")
        repository = row["repository"]
        if not isinstance(repository, str) or len(repository.split("/")) != 2:
            raise ProtocolError("repository must be owner/name")
        for part in repository.split("/"):
            _name(part, "repository component")
        branch = row["default_branch"]
        if (
            not isinstance(branch, str)
            or not branch
            or branch.startswith(("/", "-"))
            or any(x in branch for x in ("..", "@{", "//", "\\"))
            or re.search(r"[\s~^:?*\[\x00-\x1f]", branch)
            or branch.endswith(("/", ".", ".lock"))
            or branch == "@"
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
        ):
            raise ProtocolError("invalid default branch")
        roles = _object(row["roles"], {"implementer", "reviewer"}, "native roles")
        for role in roles.values():
            _name(role, "native profile")
        if roles["implementer"] == roles["reviewer"]:
            raise ProtocolError("implementation and independent review need distinct profiles")
        authors = row["automation_authors"]
        if (
            not isinstance(authors, list)
            or not all(isinstance(a, str) for a in authors)
            or len(authors) != len(set(authors))
        ):
            raise ProtocolError("automation authors must be a unique list")
        for author in authors:
            _name(author, "automation author")
        project = row["projection"]
        if project is not None:
            _object(project, {"origin", "project", "owner", "title"}, "human projection")
            if type(project["project"]) is not int or project["project"] <= 0:
                raise ProtocolError("human project must be a positive integer")
            _name(project["owner"], "human owner")
            if not isinstance(project["title"], str) or not project["title"].strip():
                raise ProtocolError("human project requires its expected title")
            if _origin(project["origin"], "Vikunja origin") is None:
                raise ProtocolError("projection requires a Vikunja origin")
        legacy = row["legacy_generations"]
        if not isinstance(legacy, dict) or any(
            not isinstance(k, str)
            or not SLUG.fullmatch(k)
            or not isinstance(v, str)
            or not re.fullmatch(r"g-[0-9a-f]{64}", v)
            for k, v in legacy.items()
        ):
            raise ProtocolError("legacy adoption requires explicit protocol/generation evidence")
        if row["legacy_container_marker"] is not None:
            _name(row["legacy_container_marker"], "legacy container marker")
        job = row["repository_job"]
        if job is not None:
            _name(job, "repository registry job")
        return cls(
            _name(row["id"], "instance id"),
            repository,
            branch,
            _name(row["namespace"], "identity namespace"),
            _name(row["workflow_key_prefix"], "workflow key prefix"),
            _name(row["board"], "board"),
            roles["implementer"],
            roles["reviewer"],
            _name(row["tenant"], "tenant"),
            tuple(authors),
            _origin(row["forgejo_origin"], "Forgejo origin"),
            project["origin"] if project else None,
            project["project"] if project else None,
            project["owner"] if project else None,
            project["title"] if project else None,
            job,
            row["legacy_container_marker"],
            tuple(sorted(legacy.items())),
            row["review_executor"],
        )

    def binding(self) -> dict[str, Any]:
        # Role/target changes need explicit reconciliation, not reinterpretation
        # of existing receipts under a new profile or human owner.
        # Keep the existing Hermes receipt identity; Paperclip may not open
        # these stores at all (enforced by RepositoryStore).
        return {
            k: v
            for k, v in self.__dict__.items()
            if k not in ("legacy_generations", "review_executor")
        }

    def check_origin(self, origin: str, service: str) -> None:
        expected = self.forgejo_origin if service == "forgejo" else self.vikunja_origin
        if expected is None or origin != expected:
            raise ProtocolError(f"{service} endpoint differs from the instance binding")

    def phase_owners(self):
        return {
            p: self.reviewer if p == "review" else self.implementer
            for p in (
                "implementation",
                "review",
                "handoff",
                "outcome",
                "rebuild",
                "verify",
                "manual-verify",
            )
        }


def load_instance(path: Path) -> RepositoryInstance:
    if not path.is_absolute() or not path.is_file():
        raise ProtocolError("instance config must be an existing absolute file")

    def pairs(rows):
        result = {}
        for k, v in rows:
            if k in result:
                raise ProtocolError("duplicate instance config field")
            result[k] = v
        return result

    try:
        return RepositoryInstance.parse(json.loads(path.read_bytes(), object_pairs_hook=pairs))
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError("malformed instance config") from exc


def instance() -> RepositoryInstance:
    current = _ACTIVE.get()
    if current is not None:
        return current
    configured = os.environ.get(ENV)
    if not configured:
        raise ProtocolError("an explicit repository instance config is required")
    # Library/test callers can change their explicit environment between isolated
    # runs; there is no process-global mutable configuration cache.
    return load_instance(Path(configured))


def repository_registry() -> tuple[RepositoryInstance, ...]:
    """Declared peers may share a board; they never expand this invocation's authority."""
    current = instance()
    directory = os.environ.get("HERMES_REPOSITORY_INSTANCES")
    if directory is None:
        return (current,)
    root = Path(directory)
    if not root.is_absolute() or not root.is_dir():
        raise ProtocolError("repository registry must be an existing absolute directory")
    paths = sorted(root.glob("*.json"))
    if not paths or len(paths) > 128:
        raise ProtocolError("repository registry must contain between 1 and 128 contracts")
    peers = tuple(load_instance(path) for path in paths)
    for field in ("identity", "repository", "namespace"):
        if len({getattr(peer, field) for peer in peers}) != len(peers):
            raise ProtocolError("repository registry contains colliding " + field)
    if current not in peers:
        raise ProtocolError("selected repository differs from its registry contract")
    return peers


@contextmanager
def instance_scope(config: RepositoryInstance) -> Iterator[RepositoryInstance]:
    token = _ACTIVE.set(config)
    try:
        yield config
    finally:
        _ACTIVE.reset(token)


def instance_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--instance-config",
        type=Path,
        default=os.environ.get(ENV),
        help=f"explicit instance contract (or {ENV})",
    )


def activate_instance(
    path: Path | None, *, review_executor: str | None = None
) -> RepositoryInstance:
    if path is None:
        raise ProtocolError("--instance-config is required")
    path = Path(path)
    value = load_instance(path)
    if review_executor is not None and value.review_executor != review_executor:
        raise ProtocolError(f"this adapter requires {review_executor} review routing")
    os.environ[ENV] = str(path)
    _ACTIVE.set(value)
    return value


class RepositoryStore(GenerationStore):
    """Same generation/lock/commit protocol, with a scope document in that generation.

    Legacy conversion is only allowed for an explicitly pinned complete previous
    generation. The pin is evidence supplied by the operator, never guessed from
    an empty state or a repository name. The domain transform still validates all
    old documents before the binding can be published atomically.
    """

    def __init__(self, *args, legacy_validator=None, **kwargs):
        self.bound_instance = instance()
        if self.bound_instance.review_executor != "hermes":
            raise ProtocolError("Paperclip task routing cannot open Hermes workflow state")
        super().__init__(*args, **kwargs)
        self.legacy_validator = legacy_validator

    def _unwrap(self, selected, *, legacy=False):
        if selected is None:
            return None
        if instance().binding() != self.bound_instance.binding():
            raise ProtocolError("repository context changed while using a state handle")
        documents = dict(selected.documents)
        binding = documents.pop("instance.json", None)
        expected = json.loads(canonical_json_bytes(self.bound_instance.binding()))
        if binding is None:
            allowed = dict(self.bound_instance.legacy_generations).get(self.protocol)
            if not legacy or allowed != selected.generation or self.legacy_validator is None:
                raise ProtocolError(
                    "unbound legacy state: explicit exact-generation adoption required"
                )
            self.legacy_validator(documents)
        elif binding != expected:
            raise ProtocolError("repository state belongs to a different instance or authority")
        return replace(selected, documents=documents)

    def read(self) -> SelectedGeneration:
        selected = super().read()
        if "instance.json" not in selected.documents:
            # Re-read, validate and extend under the existing exclusive commit lock.
            def preserve(current):
                if current is None:
                    raise ProtocolError("legacy state disappeared during adoption")
                return current.documents

            return self.update(preserve)
        result = self._unwrap(selected)
        assert result is not None
        return result

    def update(self, transform) -> SelectedGeneration:
        def bound_transform(selected):
            clean = self._unwrap(selected, legacy=True)
            documents = dict(transform(clean))
            if "instance.json" in documents:
                raise ProtocolError("domain data cannot overwrite the instance binding")
            return {**documents, "instance.json": self.bound_instance.binding()}

        result = self._unwrap(super().update(bound_transform))
        assert result is not None
        return result

    def publish(self, documents):
        return self.update(lambda _selected: documents).generation

    def cleanup(self, keep_previous=1):
        # A mismatched invocation cannot even prune another instance's history.
        self.read()
        return super().cleanup(keep_previous)
