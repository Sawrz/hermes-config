from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENERATION_RE = re.compile(r"^g-[0-9a-f]{64}$")
TEMPORARY_GENERATION_RE = re.compile(r"^\.tmp-[0-9a-f]{32}$")
CLEANUP_TOMBSTONE_RE = re.compile(r"^\.cleanup-[0-9a-f]{32}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
DOCUMENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}\.json$")
MAX_SELECTOR_BYTES = 4096
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_GENERATION_BYTES = 8 * 1024 * 1024
METRIC_NAMES = {
    "publications_total",
    "reconciliations_total",
    "retries_total",
    "corruptions_total",
    "lock_contentions_total",
}


class ProtocolError(RuntimeError):
    """Durable state is malformed, incompatible, or unsafe."""


class StateAbsent(ProtocolError):
    """No protocol state has ever been published."""


class LockUnavailable(ProtocolError):
    """The single-writer lock could not be acquired in time."""


def _exact_dict(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ProtocolError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProtocolError(f"non-finite JSON number is forbidden: {value}")


def _read_regular(
    path: Path,
    maximum_bytes: int = MAX_DOCUMENT_BYTES,
    label: str = "document",
) -> bytes:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ProtocolError(f"required state file is missing: {path.name}") from exc
    if stat.S_ISLNK(mode):
        raise ProtocolError(f"state file is a symlink: {path.name}")
    if not stat.S_ISREG(mode):
        raise ProtocolError(f"state object is not a regular file: {path.name}")
    if path.stat().st_size > maximum_bytes:
        raise ProtocolError(f"{label} exceeds byte limit: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ProtocolError(f"cannot safely open state file: {path.name}") from exc
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(maximum_bytes + 1)
    if len(raw) > maximum_bytes:
        raise ProtocolError(f"{label} exceeds byte limit: {path.name}")
    return raw


def _read_regular_at(
    directory_fd: int,
    name: str,
    maximum_bytes: int,
    label: str,
) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ProtocolError(f"required state file is missing: {name}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise ProtocolError(f"state file is a symlink: {name}")
    if not stat.S_ISREG(before.st_mode):
        raise ProtocolError(f"state object is not a regular file: {name}")
    if before.st_size > maximum_bytes:
        raise ProtocolError(f"{label} exceeds byte limit: {name}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ProtocolError(f"cannot safely open state file: {name}") from exc
    try:
        opened = os.fstat(fd)
        if _stat_identity(opened) != _stat_identity(before):
            raise ProtocolError(f"state file changed during validation: {name}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(maximum_bytes + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > maximum_bytes:
        raise ProtocolError(f"{label} exceeds byte limit: {name}")
    return raw, _stat_identity(opened)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Bind a validated file to metadata that changes across replacement or mutation."""
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate_staged_reachability(
    staged_target: str | None,
    completed_documents: set[str],
) -> None:
    if staged_target is None:
        return
    if staged_target == "manifest.json":
        if not completed_documents:
            raise ProtocolError("staged manifest has no completed documents")
        return
    if any(name >= staged_target for name in completed_documents):
        raise ProtocolError("staged document is out of publication order")


def load_json(
    path: Path,
    maximum_bytes: int = MAX_DOCUMENT_BYTES,
    label: str = "document",
) -> Any:
    raw = _read_regular(path, maximum_bytes, label)
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except ProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"state JSON is malformed: {path.name}") from exc


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("state is not canonical JSON data") from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_directory(path: Path) -> bool:
    created = False
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        path.mkdir(mode=0o750, parents=False)
        created = True
        mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise ProtocolError(f"protocol directory is a symlink: {path.name}")
    if not stat.S_ISDIR(mode):
        raise ProtocolError(f"protocol object is not a directory: {path.name}")
    return created


def _write_new(path: Path, raw: bytes, mode: int = 0o640) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _atomic_replace(path: Path, raw: bytes, mode: int = 0o640) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}"
    try:
        _write_new(temporary, raw, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_root(root: Path, create: bool) -> None:
    if not root.is_absolute():
        raise ProtocolError("state root must be an absolute path")
    try:
        mode = root.lstat().st_mode
    except FileNotFoundError:
        if not create:
            raise StateAbsent("protocol state is absent")
        root.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        try:
            root.mkdir(mode=0o750)
        except FileExistsError:
            # Another publisher won the initial-state creation race.
            pass
        _fsync_dir(root.parent)
        mode = root.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise ProtocolError("state root is a symlink")
    if not stat.S_ISDIR(mode):
        raise ProtocolError("state root is not a directory")


@contextmanager
def _lock(root: Path, timeout: float, *, shared: bool, create: bool) -> Iterator[None]:
    if timeout < 0:
        raise ProtocolError("lock timeout must be non-negative")
    _validate_root(root, create=create)
    lock_path = root / ".lock"
    flags = (os.O_RDONLY if shared else os.O_RDWR | os.O_CREAT) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o640)
    except FileNotFoundError as exc:
        raise ProtocolError("state lock is missing") from exc
    deadline = time.monotonic() + timeout
    operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    try:
        while True:
            try:
                fcntl.flock(fd, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockUnavailable("state lock is busy")
                time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def exclusive_lock(root: Path, timeout: float = 10.0) -> Iterator[None]:
    with _lock(root, timeout, shared=False, create=True):
        yield


@contextmanager
def shared_lock(root: Path, timeout: float = 10.0) -> Iterator[None]:
    with _lock(root, timeout, shared=True, create=False):
        yield


@dataclass(frozen=True)
class SelectedGeneration:
    generation: str
    documents: dict[str, Any]


class GenerationStore:
    """Publish complete immutable document sets behind one durable selector."""

    def __init__(
        self,
        root: Path,
        *,
        protocol: str,
        schema_version: int,
        fault: Callable[[str], None] | None = None,
        lock_timeout: float = 10.0,
    ) -> None:
        if not root.is_absolute():
            raise ProtocolError("state root must be absolute")
        if not isinstance(protocol, str) or not NAME_RE.fullmatch(protocol):
            raise ProtocolError("protocol name is unsafe")
        if type(schema_version) is not int or schema_version < 1:
            raise ProtocolError("schema version must be a positive integer")
        self.root = root
        self.protocol = protocol
        self.schema_version = schema_version
        self.fault = fault or (lambda _label: None)
        self.lock_timeout = lock_timeout

    def _layout(self, create: bool) -> Path:
        _validate_root(self.root, create=create)
        generations = self.root / "generations"
        if create:
            created = _ensure_directory(generations)
            if created:
                _fsync_dir(self.root)
        else:
            try:
                mode = generations.lstat().st_mode
            except FileNotFoundError as exc:
                raise ProtocolError("generation directory is missing") from exc
            if stat.S_ISLNK(mode):
                raise ProtocolError("generation directory is a symlink")
            if not stat.S_ISDIR(mode):
                raise ProtocolError("generation object is not a directory")
        return generations

    def _manifest(self, documents: Mapping[str, Any]) -> tuple[bytes, dict[str, bytes]]:
        if not isinstance(documents, Mapping) or not documents:
            raise ProtocolError("a generation must contain at least one document")
        encoded: dict[str, bytes] = {}
        rows: dict[str, dict[str, Any]] = {}
        for name in sorted(documents):
            if (
                not isinstance(name, str)
                or not DOCUMENT_RE.fullmatch(name)
                or name == "manifest.json"
            ):
                raise ProtocolError(f"unsafe generation document name: {name!r}")
            raw = canonical_json_bytes(documents[name])
            if len(raw) > MAX_DOCUMENT_BYTES:
                raise ProtocolError(f"generation document exceeds byte limit: {name}")
            encoded[name] = raw
            rows[name] = {"sha256": _sha256(raw), "size": len(raw)}
        manifest = {
            "protocol": self.protocol,
            "schema_version": self.schema_version,
            "documents": rows,
        }
        manifest_raw = canonical_json_bytes(manifest)
        if len(manifest_raw) > MAX_MANIFEST_BYTES:
            raise ProtocolError("generation manifest exceeds byte limit")
        if len(manifest_raw) + sum(map(len, encoded.values())) > MAX_GENERATION_BYTES:
            raise ProtocolError("complete generation exceeds byte limit")
        return manifest_raw, encoded

    def _publish_locked(self, documents: Mapping[str, Any]) -> SelectedGeneration:
        manifest_raw, encoded = self._manifest(documents)
        generation = "g-" + _sha256(manifest_raw)
        selector = {
            "protocol": self.protocol,
            "schema_version": self.schema_version,
            "generation": generation,
            "manifest_sha256": _sha256(manifest_raw),
        }
        generations = self._layout(create=True)
        final = generations / generation
        try:
            final_mode = final.lstat().st_mode
        except FileNotFoundError:
            final_mode = None
        if final_mode is not None:
            if stat.S_ISLNK(final_mode) or not stat.S_ISDIR(final_mode):
                raise ProtocolError("existing generation is not a safe directory")
            self._read_generation(selector)
        else:
            temporary = generations / f".tmp-{uuid.uuid4().hex}"
            temporary.mkdir(mode=0o750)
            _fsync_dir(generations)
            try:
                self.fault("after_temporary_generation")
                for name in sorted(encoded):
                    staged = temporary / f".{name}.tmp"
                    _write_new(staged, encoded[name])
                    self.fault(f"after_document_stage:{name}")
                    os.replace(staged, temporary / name)
                    _fsync_dir(temporary)
                    self.fault(f"after_document:{name}")
                staged_manifest = temporary / ".manifest.json.tmp"
                _write_new(staged_manifest, manifest_raw)
                self.fault("after_manifest_stage")
                os.replace(staged_manifest, temporary / "manifest.json")
                _fsync_dir(temporary)
                self.fault("after_manifest")
                self.fault("after_generation_fsync")
                try:
                    os.rename(temporary, final)
                except FileExistsError:
                    self._read_generation(selector)
                self.fault("after_generation_publish")
            finally:
                if temporary.exists():
                    _remove_tree_confined(temporary)
        # Required even when recovering an already-renamed generation whose
        # prior publisher crashed before syncing the parent directory.
        _fsync_dir(generations)
        _atomic_replace(self.root / "current.json", canonical_json_bytes(selector))
        self.fault("after_selector_replace")
        _fsync_dir(self.root)
        self.fault("after_selector_fsync")
        return self._read_generation(selector)

    def publish(self, documents: Mapping[str, Any]) -> str:
        with exclusive_lock(self.root, timeout=self.lock_timeout):
            return self._publish_locked(documents).generation

    def update(
        self,
        transform: Callable[[SelectedGeneration | None], Mapping[str, Any]],
    ) -> SelectedGeneration:
        """Atomically read, transform, publish, and read back one generation."""
        with exclusive_lock(self.root, timeout=self.lock_timeout):
            try:
                current = self._read_generation(self._read_selector())
            except StateAbsent:
                current = None
            except ProtocolError:
                if (self.root / "current.json").exists():
                    raise
                current = self._recover_unselected_first_generation()
            documents = transform(current)
            return self._publish_locked(documents)

    def _recover_unselected_first_generation(self) -> SelectedGeneration | None:
        """Recover a clean retry or one complete first generation after a crash.

        This is writer-only recovery. Normal readers continue to require the
        durable selector and fail closed when it is absent.
        """
        root_entries = {entry.name for entry in self.root.iterdir() if entry.name != ".lock"}
        if root_entries != {"generations"}:
            raise ProtocolError("protocol selector is missing from non-empty state")
        generations = self._layout(create=False)
        generations_stat = generations.lstat()
        generations_identity = (generations_stat.st_dev, generations_stat.st_ino)
        entries = list(generations.iterdir())
        if not entries:
            # A first publisher creates generations/ before its temporary
            # generation. Every pre-rename failure removes that confined
            # temporary tree, leaving exactly this safe empty layout. The
            # locked writer may retry from absence; readers still fail closed.
            return None
        if len(entries) != 1:
            raise ProtocolError("protocol selector is missing from ambiguous state")
        generation = entries[0]
        generation_stat = generation.lstat()
        mode = generation_stat.st_mode
        generation_identity = (generation_stat.st_dev, generation_stat.st_ino)
        if TEMPORARY_GENERATION_RE.fullmatch(generation.name):
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ProtocolError("temporary generation is not a safe directory")
            self._validate_temporary_generation(generation)
            self.fault("before_cleanup_tombstone_rename")
            try:
                current_generation = generation.lstat()
                current_parent = generations.lstat()
            except FileNotFoundError as exc:
                raise ProtocolError("temporary generation changed before cleanup rename") from exc
            if (
                (current_generation.st_dev, current_generation.st_ino) != generation_identity
                or stat.S_ISLNK(current_generation.st_mode)
                or not stat.S_ISDIR(current_generation.st_mode)
                or (current_parent.st_dev, current_parent.st_ino) != generations_identity
                or stat.S_ISLNK(current_parent.st_mode)
                or not stat.S_ISDIR(current_parent.st_mode)
            ):
                raise ProtocolError("temporary generation changed before cleanup rename")
            tombstone = generations / generation.name.replace(".tmp-", ".cleanup-", 1)
            generation.rename(tombstone)
            self.fault("after_cleanup_tombstone_rename")
            _fsync_dir(generations)
            self.fault("after_cleanup_tombstone_fsync")
            generation = tombstone
        elif CLEANUP_TOMBSTONE_RE.fullmatch(generation.name):
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ProtocolError("cleanup tombstone is not a safe directory")
        if CLEANUP_TOMBSTONE_RE.fullmatch(generation.name):
            self.fault("before_cleanup_tombstone_open")
            self._cleanup_tombstone(
                generations,
                generation,
                generations_identity,
                generation_identity,
            )
            return None
        if (
            not GENERATION_RE.fullmatch(generation.name)
            or stat.S_ISLNK(mode)
            or not stat.S_ISDIR(mode)
        ):
            raise ProtocolError("protocol selector is missing from unsafe state")
        selector = {
            "protocol": self.protocol,
            "schema_version": self.schema_version,
            "generation": generation.name,
            "manifest_sha256": generation.name[2:],
        }
        return self._read_generation(selector)

    def _validate_temporary_generation(self, temporary: Path) -> None:
        """Accept only residue that this publisher could have written pre-rename."""
        entries = list(temporary.iterdir())
        manifest_raw: bytes | None = None
        document_raw: dict[str, bytes] = {}
        parsed: dict[str, Any] = {}
        staged_target: str | None = None
        for entry in entries:
            if entry.name.startswith(".") and entry.name.endswith(".tmp"):
                target = entry.name[1:-4]
                if target != "manifest.json" and not DOCUMENT_RE.fullmatch(target):
                    raise ProtocolError("temporary generation contains an unsafe staging object")
                if staged_target is not None or (temporary / target).exists():
                    raise ProtocolError("temporary generation has ambiguous staging state")
                limit = MAX_MANIFEST_BYTES if target == "manifest.json" else MAX_DOCUMENT_BYTES
                _read_regular(entry, limit, "temporary staged document")
                staged_target = target
                continue
            if entry.name == "manifest.json":
                if manifest_raw is not None:
                    raise ProtocolError("temporary generation has duplicate manifest")
                raw = _read_regular(entry, MAX_MANIFEST_BYTES, "temporary manifest")
                manifest_raw = raw
            elif DOCUMENT_RE.fullmatch(entry.name):
                raw = _read_regular(entry, MAX_DOCUMENT_BYTES, "temporary document")
                document_raw[entry.name] = raw
            else:
                raise ProtocolError("temporary generation contains an unsafe object")
            try:
                value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
            except ProtocolError:
                raise
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProtocolError("temporary generation contains malformed JSON") from exc
            if canonical_json_bytes(value) != raw:
                raise ProtocolError("temporary generation contains non-canonical JSON")
            if entry.name != "manifest.json":
                parsed[entry.name] = value
        _validate_staged_reachability(staged_target, set(parsed))
        if not parsed:
            if manifest_raw is not None:
                raise ProtocolError("temporary generation has a manifest without documents")
            return
        expected_manifest, expected_documents = self._manifest(parsed)
        if document_raw != expected_documents:
            raise ProtocolError("temporary generation document encoding is invalid")
        if manifest_raw is not None and manifest_raw != expected_manifest:
            raise ProtocolError("temporary generation manifest is incomplete or invalid")

    def _validate_cleanup_tombstone(
        self, tombstone_fd: int, names: list[str]
    ) -> dict[str, tuple[int, int, int, int, int, int]]:
        """Accept only confined residue from an interrupted validated cleanup."""
        staged_target: str | None = None
        document_raw: dict[str, bytes] = {}
        document_values: dict[str, Any] = {}
        manifest_raw: bytes | None = None
        observed_bytes = 0
        identities: dict[str, tuple[int, int, int, int, int, int]] = {}
        for name in names:
            if name.startswith(".") and name.endswith(".tmp"):
                target = name[1:-4]
                if target != "manifest.json" and not DOCUMENT_RE.fullmatch(target):
                    raise ProtocolError("cleanup tombstone contains an unsafe staging object")
                if staged_target is not None or target in names:
                    raise ProtocolError("cleanup tombstone has ambiguous staging state")
                limit = MAX_MANIFEST_BYTES if target == "manifest.json" else MAX_DOCUMENT_BYTES
                staged_raw, identities[name] = _read_regular_at(
                    tombstone_fd, name, limit, "cleanup staged document"
                )
                observed_bytes += len(staged_raw)
                if observed_bytes > MAX_GENERATION_BYTES:
                    raise ProtocolError("cleanup tombstone generation exceeds byte limit")
                staged_target = target
                continue
            if name == "manifest.json":
                limit = MAX_MANIFEST_BYTES
            elif DOCUMENT_RE.fullmatch(name):
                limit = MAX_DOCUMENT_BYTES
            else:
                raise ProtocolError("cleanup tombstone contains an unsafe object")
            raw, identities[name] = _read_regular_at(tombstone_fd, name, limit, "cleanup document")
            observed_bytes += len(raw)
            if observed_bytes > MAX_GENERATION_BYTES:
                raise ProtocolError("cleanup tombstone generation exceeds byte limit")
            try:
                value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
            except ProtocolError:
                raise
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProtocolError("cleanup tombstone contains malformed JSON") from exc
            if canonical_json_bytes(value) != raw:
                raise ProtocolError("cleanup tombstone contains non-canonical JSON")
            if name == "manifest.json":
                manifest_raw = raw
            else:
                document_raw[name] = raw
                document_values[name] = value
        _validate_staged_reachability(staged_target, set(document_values))
        if manifest_raw is None:
            if document_values:
                expected_manifest, _expected_documents = self._manifest(document_values)
                if observed_bytes + len(expected_manifest) > MAX_GENERATION_BYTES:
                    raise ProtocolError("cleanup tombstone generation exceeds byte limit")
            return identities
        if staged_target is not None:
            raise ProtocolError("cleanup tombstone has impossible manifest staging state")
        manifest = _exact_dict(
            json.loads(manifest_raw, object_pairs_hook=_pairs, parse_constant=_reject_constant),
            {"protocol", "schema_version", "documents"},
            "cleanup manifest",
        )
        if (
            manifest["protocol"] != self.protocol
            or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != self.schema_version
        ):
            raise ProtocolError("cleanup tombstone manifest is incompatible")
        rows = manifest["documents"]
        if not isinstance(rows, dict) or not rows:
            raise ProtocolError("cleanup tombstone manifest has no documents")
        announced_bytes = len(manifest_raw)
        for name, metadata in rows.items():
            if (
                not isinstance(name, str)
                or not DOCUMENT_RE.fullmatch(name)
                or name == "manifest.json"
            ):
                raise ProtocolError("cleanup tombstone manifest has an unsafe document name")
            metadata = _exact_dict(metadata, {"sha256", "size"}, f"cleanup metadata for {name}")
            digest = metadata["sha256"]
            size = metadata["size"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ProtocolError("cleanup tombstone document digest is malformed")
            if type(size) is not int or size < 0 or size > MAX_DOCUMENT_BYTES:
                raise ProtocolError("cleanup tombstone document size is malformed")
            announced_bytes += size
            if announced_bytes > MAX_GENERATION_BYTES:
                raise ProtocolError("cleanup tombstone generation exceeds byte limit")
            if name in document_raw:
                raw = document_raw[name]
                if len(raw) != size or _sha256(raw) != digest:
                    raise ProtocolError("cleanup tombstone surviving document is invalid")
        if not set(document_raw) <= set(rows):
            raise ProtocolError("cleanup tombstone has an unannounced surviving document")
        original_names = sorted(["manifest.json", *rows])
        surviving_names = sorted(names)
        if surviving_names != original_names[-len(surviving_names) :]:
            raise ProtocolError("cleanup tombstone has an impossible deletion gap")
        return identities

    def _cleanup_tombstone(
        self,
        generations: Path,
        tombstone: Path,
        expected_parent_identity: tuple[int, int],
        expected_tombstone_identity: tuple[int, int],
    ) -> None:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(generations, directory_flags)
        tombstone_fd = -1
        try:
            parent_stat = os.fstat(parent_fd)
            if (parent_stat.st_dev, parent_stat.st_ino) != expected_parent_identity:
                raise ProtocolError("cleanup tombstone parent changed before removal")
            tombstone_fd = os.open(tombstone.name, directory_flags, dir_fd=parent_fd)
            opened = os.fstat(tombstone_fd)
            if (opened.st_dev, opened.st_ino) != expected_tombstone_identity:
                raise ProtocolError("cleanup tombstone changed before removal")
            names = os.listdir(tombstone_fd)
            identities = self._validate_cleanup_tombstone(tombstone_fd, names)
            self.fault("after_cleanup_tombstone_validation")
            for name in sorted(names):
                child_stat = os.stat(name, dir_fd=tombstone_fd, follow_symlinks=False)
                mode = child_stat.st_mode
                if stat.S_ISLNK(mode):
                    raise ProtocolError("cleanup refuses symlink children")
                if not stat.S_ISREG(mode):
                    raise ProtocolError("cleanup refuses unexpected object types")
                if _stat_identity(child_stat) != identities[name]:
                    raise ProtocolError("cleanup tombstone child changed before removal")
                os.unlink(name, dir_fd=tombstone_fd)
                self.fault(f"after_cleanup_unlink:{name}")
            try:
                current = os.stat(tombstone.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise ProtocolError("cleanup tombstone changed during removal") from exc
            if (
                stat.S_ISLNK(current.st_mode)
                or not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise ProtocolError("cleanup tombstone changed during removal")
            try:
                os.rmdir(tombstone.name, dir_fd=parent_fd)
            except OSError as exc:
                raise ProtocolError("cleanup tombstone changed during removal") from exc
            self.fault("after_cleanup_tombstone_remove")
            try:
                parent_path_stat = generations.lstat()
            except FileNotFoundError as exc:
                raise ProtocolError("cleanup tombstone parent changed during removal") from exc
            if (
                stat.S_ISLNK(parent_path_stat.st_mode)
                or not stat.S_ISDIR(parent_path_stat.st_mode)
                or (parent_path_stat.st_dev, parent_path_stat.st_ino) != expected_parent_identity
            ):
                raise ProtocolError("cleanup tombstone parent changed during removal")
            os.fsync(parent_fd)
            self.fault("after_cleanup_fsync")
        finally:
            if tombstone_fd >= 0:
                os.close(tombstone_fd)
            os.close(parent_fd)

    def _read_selector(self) -> dict[str, Any]:
        selector_path = self.root / "current.json"
        if not selector_path.exists():
            entries = [entry.name for entry in self.root.iterdir() if entry.name != ".lock"]
            if entries:
                raise ProtocolError("protocol selector is missing from non-empty state")
            raise StateAbsent("protocol state is absent")
        selector = _exact_dict(
            load_json(selector_path, MAX_SELECTOR_BYTES, "selector"),
            {"protocol", "schema_version", "generation", "manifest_sha256"},
            "selector",
        )
        if selector["protocol"] != self.protocol:
            raise ProtocolError("selector protocol is incompatible")
        if (
            type(selector["schema_version"]) is not int
            or selector["schema_version"] != self.schema_version
        ):
            raise ProtocolError("selector schema is incompatible")
        generation = selector["generation"]
        if not isinstance(generation, str) or not GENERATION_RE.fullmatch(generation):
            raise ProtocolError("selector generation is unsafe")
        if selector["manifest_sha256"] != generation[2:]:
            raise ProtocolError("selector manifest digest does not match generation")
        return selector

    def _read_generation(self, selector: Mapping[str, Any]) -> SelectedGeneration:
        generation = selector["generation"]
        directory = self.root / "generations" / generation
        try:
            mode = directory.lstat().st_mode
        except FileNotFoundError as exc:
            raise ProtocolError("selected generation is missing") from exc
        if stat.S_ISLNK(mode):
            raise ProtocolError("selected generation is a symlink")
        if not stat.S_ISDIR(mode):
            raise ProtocolError("selected generation is not a directory")
        manifest_raw = _read_regular(directory / "manifest.json", MAX_MANIFEST_BYTES, "manifest")
        if _sha256(manifest_raw) != selector["manifest_sha256"]:
            raise ProtocolError("selected manifest digest is invalid")
        try:
            manifest = json.loads(
                manifest_raw, object_pairs_hook=_pairs, parse_constant=_reject_constant
            )
        except ProtocolError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("selected manifest JSON is malformed") from exc
        manifest = _exact_dict(manifest, {"protocol", "schema_version", "documents"}, "manifest")
        if (
            manifest["protocol"] != self.protocol
            or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != self.schema_version
        ):
            raise ProtocolError("selected manifest is incompatible")
        rows = manifest["documents"]
        if not isinstance(rows, dict) or not rows:
            raise ProtocolError("selected manifest has no documents")
        expected = {"manifest.json"}
        documents: dict[str, Any] = {}
        announced_generation_bytes = len(manifest_raw)
        for name, metadata in rows.items():
            if (
                not isinstance(name, str)
                or not DOCUMENT_RE.fullmatch(name)
                or name == "manifest.json"
            ):
                raise ProtocolError("selected manifest contains an unsafe document name")
            metadata = _exact_dict(metadata, {"sha256", "size"}, f"metadata for {name}")
            if not isinstance(metadata["sha256"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", metadata["sha256"]
            ):
                raise ProtocolError("document digest is malformed")
            if type(metadata["size"]) is not int or metadata["size"] < 0:
                raise ProtocolError("document size is malformed")
            if metadata["size"] > MAX_DOCUMENT_BYTES:
                raise ProtocolError(f"document exceeds byte limit: {name}")
            announced_generation_bytes += metadata["size"]
            if announced_generation_bytes > MAX_GENERATION_BYTES:
                raise ProtocolError("complete generation exceeds byte limit")
            raw = _read_regular(directory / name, MAX_DOCUMENT_BYTES, "document")
            if len(raw) != metadata["size"] or _sha256(raw) != metadata["sha256"]:
                raise ProtocolError(f"document digest or size is invalid: {name}")
            try:
                documents[name] = json.loads(
                    raw, object_pairs_hook=_pairs, parse_constant=_reject_constant
                )
            except ProtocolError:
                raise
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProtocolError(f"document JSON is malformed: {name}") from exc
            expected.add(name)
        actual = {entry.name for entry in directory.iterdir()}
        if actual != expected:
            raise ProtocolError("selected generation contains unexpected or missing objects")
        return SelectedGeneration(generation=generation, documents=documents)

    def read(self) -> SelectedGeneration:
        self._layout(create=False)
        self._read_selector()
        with shared_lock(self.root, timeout=self.lock_timeout):
            return self._read_generation(self._read_selector())

    def cleanup(self, keep_previous: int = 1) -> list[str]:
        if type(keep_previous) is not int or keep_previous < 0 or keep_previous > 10:
            raise ProtocolError("cleanup retention must be between 0 and 10")
        with exclusive_lock(self.root, timeout=self.lock_timeout):
            generations = self._layout(create=False)
            selected = self._read_generation(self._read_selector()).generation
            candidates: list[Path] = []
            for entry in generations.iterdir():
                mode = entry.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise ProtocolError("generation cleanup encountered a symlink")
                if entry.name.startswith(".tmp-"):
                    if not stat.S_ISDIR(mode):
                        raise ProtocolError("temporary generation is not a directory")
                    candidates.append(entry)
                elif GENERATION_RE.fullmatch(entry.name):
                    if not stat.S_ISDIR(mode):
                        raise ProtocolError("generation cleanup encountered a non-directory")
                    if entry.name != selected:
                        candidates.append(entry)
                else:
                    raise ProtocolError("generation cleanup encountered an unexpected object")
            complete = [p for p in candidates if GENERATION_RE.fullmatch(p.name)]
            complete.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
            retained = {p.name for p in complete[:keep_previous]}
            removed: list[str] = []
            for path in candidates:
                if path.name in retained:
                    continue
                _remove_tree_confined(path)
                removed.append(path.name)
            _fsync_dir(generations)
            return sorted(removed)


def _remove_tree_confined(path: Path) -> None:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise ProtocolError("cleanup refuses symlink objects")
    if not stat.S_ISDIR(mode):
        raise ProtocolError("cleanup target is not a directory")
    for entry in path.iterdir():
        child_mode = entry.lstat().st_mode
        if stat.S_ISLNK(child_mode):
            raise ProtocolError("cleanup refuses symlink children")
        if stat.S_ISDIR(child_mode):
            _remove_tree_confined(entry)
        elif stat.S_ISREG(child_mode):
            entry.unlink()
        else:
            raise ProtocolError("cleanup refuses unexpected object types")
    path.rmdir()


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    initial_delay_seconds: int = 1
    maximum_delay_seconds: int = 300

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 20:
            raise ProtocolError("retry attempts must be between 1 and 20")
        if (
            type(self.initial_delay_seconds) is not int
            or not 1 <= self.initial_delay_seconds <= 3600
        ):
            raise ProtocolError("initial retry delay must be between 1 and 3600 seconds")
        if (
            type(self.maximum_delay_seconds) is not int
            or not self.initial_delay_seconds <= self.maximum_delay_seconds <= 86400
        ):
            raise ProtocolError(
                "maximum retry delay must be bounded and not below the initial delay"
            )

    def delay_seconds(self, attempt: int) -> int:
        if type(attempt) is not int or attempt < 1:
            raise ProtocolError("retry attempt number must be positive")
        return min(self.maximum_delay_seconds, self.initial_delay_seconds * (2 ** (attempt - 1)))


@dataclass
class DirectionalEdge:
    state: str = "pending"
    attempts: int = 0
    attempt_id: str | None = None
    transient: bool | None = None
    error: str | None = None
    proof: str | None = None


@dataclass
class DirectionalWork:
    work_id: str
    edges: dict[str, DirectionalEdge]

    @classmethod
    def create(cls, work_id: str, edge_names: list[str]) -> DirectionalWork:
        _bounded_text(work_id, "work id", 200)
        if not edge_names or len(edge_names) != len(set(edge_names)):
            raise ProtocolError("directional work requires unique edges")
        edges: dict[str, DirectionalEdge] = {}
        for name in sorted(edge_names):
            if not NAME_RE.fullmatch(name):
                raise ProtocolError("directional edge name is unsafe")
            edges[name] = DirectionalEdge()
        return cls(work_id=work_id, edges=edges)

    def _edge(self, name: str) -> DirectionalEdge:
        try:
            return self.edges[name]
        except KeyError as exc:
            raise ProtocolError(f"unknown directional edge: {name}") from exc

    def start(self, name: str, attempt_id: str) -> None:
        edge = self._edge(name)
        _bounded_text(attempt_id, "attempt id", 200)
        if edge.state not in {"pending", "transient_failed"}:
            raise ProtocolError("only pending or transient-failed edges may start")
        edge.state = "in_flight"
        edge.attempts += 1
        edge.attempt_id = attempt_id
        edge.transient = None
        edge.error = None
        edge.proof = None

    def confirm(self, name: str, proof: str) -> None:
        edge = self._edge(name)
        _bounded_text(proof, "confirmation proof", 500)
        if edge.state not in {"in_flight", "ambiguous"}:
            raise ProtocolError("only attempted edges may be confirmed")
        edge.state = "confirmed"
        edge.proof = proof
        edge.transient = None
        edge.error = None

    def lost_response(self, name: str, error: str) -> None:
        edge = self._edge(name)
        _bounded_text(error, "lost-response error", 300)
        if edge.state != "in_flight":
            raise ProtocolError("lost response requires an in-flight edge")
        edge.state = "ambiguous"
        edge.error = error

    def reconcile(self, name: str, *, observed: bool, proof: str) -> None:
        edge = self._edge(name)
        _bounded_text(proof, "readback proof", 500)
        if type(observed) is not bool or edge.state not in {"in_flight", "ambiguous"}:
            raise ProtocolError("reconciliation requires an attempted edge and boolean readback")
        edge.proof = proof
        if observed:
            edge.state = "confirmed"
            edge.transient = None
            edge.error = None
        else:
            edge.state = "transient_failed"
            edge.transient = True
            edge.error = "negative readback"

    def fail(self, name: str, *, transient: bool, error: str) -> None:
        edge = self._edge(name)
        _bounded_text(error, "edge error", 300)
        if type(transient) is not bool or edge.state != "in_flight":
            raise ProtocolError("failure requires an in-flight edge and a boolean class")
        edge.state = "transient_failed" if transient else "permanent_failed"
        edge.transient = transient
        edge.error = error

    def retryable_edges(self, policy: RetryPolicy) -> list[str]:
        return [
            name
            for name, edge in sorted(self.edges.items())
            if edge.state == "transient_failed" and edge.attempts < policy.max_attempts
        ]

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "work_id": self.work_id,
            "edges": {
                name: {
                    "state": edge.state,
                    "attempts": edge.attempts,
                    "attempt_id": edge.attempt_id,
                    "transient": edge.transient,
                    "error": edge.error,
                    "proof": edge.proof,
                }
                for name, edge in sorted(self.edges.items())
            },
        }

    @classmethod
    def from_document(cls, value: Any) -> DirectionalWork:
        doc = _exact_dict(value, {"schema_version", "work_id", "edges"}, "directional work")
        if doc["schema_version"] != 1 or not isinstance(doc["edges"], dict) or not doc["edges"]:
            raise ProtocolError("directional work schema is incompatible")
        work = cls.create(doc["work_id"], list(doc["edges"]))
        allowed_states = {
            "pending",
            "in_flight",
            "ambiguous",
            "confirmed",
            "transient_failed",
            "permanent_failed",
        }
        for name, raw in doc["edges"].items():
            raw = _exact_dict(
                raw,
                {"state", "attempts", "attempt_id", "transient", "error", "proof"},
                f"edge {name}",
            )
            if (
                raw["state"] not in allowed_states
                or type(raw["attempts"]) is not int
                or raw["attempts"] < 0
            ):
                raise ProtocolError("directional edge state is malformed")
            for field, maximum in (("attempt_id", 200), ("error", 300), ("proof", 500)):
                if raw[field] is not None:
                    _bounded_text(raw[field], field, maximum)
            if raw["transient"] not in {None, True, False}:
                raise ProtocolError("directional edge transient marker is malformed")
            edge = DirectionalEdge(**raw)
            attempted = edge.attempts >= 1 and edge.attempt_id is not None
            invariant = {
                "pending": edge.attempts == 0
                and all(
                    value is None
                    for value in (edge.attempt_id, edge.transient, edge.error, edge.proof)
                ),
                "in_flight": attempted
                and all(value is None for value in (edge.transient, edge.error, edge.proof)),
                "ambiguous": attempted
                and edge.error is not None
                and edge.transient is None
                and edge.proof is None,
                "confirmed": attempted
                and edge.proof is not None
                and edge.transient is None
                and edge.error is None,
                "transient_failed": attempted and edge.transient is True and edge.error is not None,
                "permanent_failed": attempted
                and edge.transient is False
                and edge.error is not None
                and edge.proof is None,
            }[edge.state]
            if not invariant:
                raise ProtocolError(f"directional edge invariant is invalid: {name}")
            work.edges[name] = edge
        return work


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(ch) < 32 for ch in value)
    ):
        raise ProtocolError(f"{label} must be non-empty bounded text without controls")
    return value


def validate_preference_contract(value: Any) -> dict[str, Any]:
    contract = _exact_dict(
        value, {"schema_version", "name", "preference_version", "properties"}, "preference contract"
    )
    if contract["schema_version"] != 1:
        raise ProtocolError("preference contract schema is incompatible")
    if not isinstance(contract["name"], str) or not NAME_RE.fullmatch(contract["name"]):
        raise ProtocolError("preference contract name is unsafe")
    if type(contract["preference_version"]) is not int or contract["preference_version"] < 1:
        raise ProtocolError("preference version must be positive")
    properties = contract["properties"]
    if not isinstance(properties, dict) or not properties or len(properties) > 64:
        raise ProtocolError("preference contract must define 1 to 64 properties")
    for name, spec in properties.items():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ProtocolError("preference property name is unsafe")
        if not isinstance(spec, dict) or spec.get("type") not in {
            "boolean",
            "integer",
            "string",
            "string-list",
        }:
            raise ProtocolError(f"preference property type is unsupported: {name}")
        kind = spec["type"]
        expected = {
            "boolean": {"type", "default"},
            "integer": {"type", "minimum", "maximum", "default"},
            "string": {"type", "enum", "default"},
            "string-list": {"type", "max_items", "max_length", "default"},
        }[kind]
        _exact_dict(spec, expected, f"preference property {name}")
        _validate_preference_value(name, spec, spec["default"])
    enabled = properties.get("enabled")
    if enabled != {"type": "boolean", "default": False}:
        raise ProtocolError(
            "preference contract requires an enabled boolean with safe default false"
        )
    return contract


def _validate_preference_value(name: str, spec: Mapping[str, Any], value: Any) -> Any:
    kind = spec["type"]
    if kind == "boolean":
        if type(value) is not bool:
            raise ProtocolError(f"preference {name} must be boolean")
    elif kind == "integer":
        if (
            type(spec["minimum"]) is not int
            or type(spec["maximum"]) is not int
            or spec["minimum"] > spec["maximum"]
        ):
            raise ProtocolError(f"preference {name} integer bounds are invalid")
        if type(value) is not int or not spec["minimum"] <= value <= spec["maximum"]:
            raise ProtocolError(f"preference {name} is outside its integer bounds")
    elif kind == "string":
        enum = spec["enum"]
        if (
            not isinstance(enum, list)
            or not enum
            or len(enum) > 64
            or not all(
                isinstance(row, str)
                and row
                and len(row) <= 200
                and not any(ord(ch) < 32 for ch in row)
                for row in enum
            )
        ):
            raise ProtocolError(f"preference {name} enum is invalid")
        if len(enum) != len(set(enum)) or value not in enum:
            raise ProtocolError(f"preference {name} is not an allowed value")
    elif kind == "string-list":
        if type(spec["max_items"]) is not int or not 0 <= spec["max_items"] <= 128:
            raise ProtocolError(f"preference {name} item bound is invalid")
        if type(spec["max_length"]) is not int or not 1 <= spec["max_length"] <= 500:
            raise ProtocolError(f"preference {name} string bound is invalid")
        if (
            not isinstance(value, list)
            or len(value) > spec["max_items"]
            or len(value) != len(set(value))
        ):
            raise ProtocolError(f"preference {name} must be a bounded unique string list")
        for row in value:
            _bounded_text(row, f"preference {name} item", spec["max_length"])
    return value


def validate_preferences(contract: Any, values: Any) -> dict[str, Any]:
    contract = validate_preference_contract(contract)
    if not isinstance(values, dict):
        raise ProtocolError("preference input must be an object")
    unknown = set(values) - set(contract["properties"])
    if unknown:
        raise ProtocolError(f"preference input contains unauthorized properties: {sorted(unknown)}")
    normalized: dict[str, Any] = {}
    for name, spec in sorted(contract["properties"].items()):
        normalized[name] = _validate_preference_value(name, spec, values.get(name, spec["default"]))
    return {"schema_version": contract["preference_version"], "values": normalized}


def render_metrics(counters: Mapping[str, int]) -> str:
    if not isinstance(counters, Mapping) or set(counters) - METRIC_NAMES:
        raise ProtocolError("metrics contain an unbounded or unknown name")
    lines: list[str] = []
    for name in sorted(METRIC_NAMES):
        value = counters.get(name, 0)
        if type(value) is not int or value < 0:
            raise ProtocolError("metric values must be non-negative integers")
        lines.append(f"hermes_workflow_state_{name} {value}")
    return "\n".join(lines) + "\n"


def write_metrics(path: Path, counters: Mapping[str, int]) -> None:
    if not path.is_absolute():
        raise ProtocolError("metrics path must be absolute")
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None and (stat.S_ISLNK(mode) or not stat.S_ISREG(mode)):
        raise ProtocolError("metrics target is not a regular file")
    _atomic_replace(path, render_metrics(counters).encode("ascii"))
    _fsync_dir(path.parent)
