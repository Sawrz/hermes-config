"""Fail-closed, deterministic synchronization for allowlisted Git checkouts."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import unquote, urlsplit

ALLOWED_JOBS = frozenset(
    {
        "cv-repo-pull",
        "nix-config-pull",
        "omanix-pull",
        "nix-containers-pull",
        "family-controls-pull",
        "hermes-config-pull",
        "obsidian-main-vault-pull",
    }
)
SAFE_REF = re.compile(r"^refs/heads/[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?$")
SAFE_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
RENAME_EXCHANGE = 2
AT_FDCWD = -100


class Result(Enum):
    CHANGED = "changed"
    NO_CHANGE = "no_change"


class ConfigError(ValueError):
    pass


class SyncError(RuntimeError):
    def __init__(self, message: str, failure_class: str = "repository_state") -> None:
        super().__init__(message)
        self.failure_class = failure_class


@dataclass(frozen=True)
class Job:
    name: str
    origin: str
    repository: str
    ref: str
    destination: Path
    transport: str
    mode: str
    timeout_seconds: int
    retries: int
    lock_timeout_seconds: int
    ssh_host: str | None = None
    ssh_port: int | None = None
    known_hosts_file: Path | None = None
    identity_file: Path | None = None

    @property
    def branch(self) -> str:
        return self.ref.removeprefix("refs/heads/")


def _exact_keys(data: dict, required: set[str], optional: set[str], where: str) -> None:
    missing = required - data.keys()
    extra = data.keys() - required - optional
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(sorted(missing))}")
        if extra:
            parts.append(f"unsupported {', '.join(sorted(extra))}")
        raise ConfigError(f"{where}: {'; '.join(parts)}")


def _bounded_int(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{name} must be an integer from {low} through {high}")
    return value


def _absolute_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConfigError(f"{name} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"{name} must be an absolute normalized path")
    return path


def _regular_private_file(path: Path, name: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ConfigError(f"{name} does not exist") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"{name} must be a regular non-symlink file")


def _validate_ssh(job_data: dict, repository: str) -> tuple:
    origin = job_data["origin"]
    if not origin.isascii() or any(
        character.isspace() or ord(character) < 0x21 for character in origin
    ):
        raise ConfigError("ssh origin may not contain whitespace or control characters")
    parsed = urlsplit(origin)
    if parsed.scheme != "ssh" or parsed.username != "git" or parsed.password is not None:
        raise ConfigError("ssh origin must use ssh://git@host/owner/repository.git")
    if parsed.query or parsed.fragment or unquote(parsed.path) != parsed.path:
        raise ConfigError("ssh origin may not contain encoding, query, or fragment")
    host = job_data.get("ssh_host")
    port = _bounded_int(job_data.get("ssh_port"), "ssh_port", 1, 65535)
    try:
        parsed_port = parsed.port
    except ValueError as error:
        raise ConfigError("ssh origin contains an invalid port") from error
    if not isinstance(host, str) or parsed.hostname != host or parsed_port not in (None, port):
        raise ConfigError("ssh origin host/port does not match the declared host-key contract")
    expected_path = f"/{repository}.git"
    if parsed.path != expected_path:
        raise ConfigError("ssh origin does not match the declared repository")
    canonical_port = f":{port}" if port != 22 else ""
    canonical_origin = f"ssh://git@{host}{canonical_port}{expected_path}"
    if origin != canonical_origin:
        raise ConfigError("origin must use the exact canonical SSH URL")
    known = _absolute_path(job_data.get("known_hosts_file"), "known_hosts_file")
    identity = _absolute_path(job_data.get("identity_file"), "identity_file")
    _regular_private_file(known, "known_hosts_file")
    _regular_private_file(identity, "identity_file")
    if identity.stat().st_mode & 0o077:
        raise ConfigError("identity_file must not grant group or other permissions")
    marker = host if port == 22 else f"[{host}]:{port}"
    lines = known.read_text(encoding="utf-8").splitlines()
    host_is_pinned = any(
        line and not line.startswith("#") and marker in line.split()[0].split(",") for line in lines
    )
    if not host_is_pinned:
        raise ConfigError("known_hosts_file lacks the declared host and port")
    return host, port, known, identity


def _valid_ref(value: object) -> bool:
    if not isinstance(value, str) or not SAFE_REF.fullmatch(value):
        return False
    branch = value.removeprefix("refs/heads/")
    components = branch.split("/")
    return (
        all(components)
        and ".." not in branch
        and "@{" not in branch
        and all(
            not component.endswith(".lock") and not component.startswith(".")
            for component in components
        )
    )


def load_job(registry_path: Path, job_name: str, allow_local_test_transport: bool) -> Job:
    if job_name not in ALLOWED_JOBS:
        raise ConfigError(f"job {job_name!r} is not allowlisted")
    try:
        raw = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError("registry is unreadable or invalid JSON") from error
    if not isinstance(raw, dict):
        raise ConfigError("registry must be an object")
    _exact_keys(raw, {"schema_version", "jobs"}, set(), "registry")
    if raw["schema_version"] != 1 or not isinstance(raw["jobs"], dict):
        raise ConfigError("registry schema_version must be 1 and jobs must be an object")
    unknown = set(raw["jobs"]) - ALLOWED_JOBS
    if unknown:
        raise ConfigError(f"registry contains non-allowlisted jobs: {', '.join(sorted(unknown))}")
    if job_name not in raw["jobs"]:
        raise ConfigError(f"allowlisted job {job_name!r} is not enrolled")
    data = raw["jobs"][job_name]
    if not isinstance(data, dict):
        raise ConfigError("job contract must be an object")
    required = {
        "origin",
        "repository",
        "ref",
        "destination",
        "transport",
        "mode",
        "timeout_seconds",
        "retries",
        "lock_timeout_seconds",
    }
    optional = {"ssh_host", "ssh_port", "known_hosts_file", "identity_file"}
    _exact_keys(data, required, optional, f"job {job_name}")
    if not isinstance(data["origin"], str) or not data["origin"]:
        raise ConfigError("origin must be a non-empty string")
    if not isinstance(data["repository"], str) or not SAFE_REPOSITORY.fullmatch(data["repository"]):
        raise ConfigError("repository must be an explicit owner/name identity")
    if not _valid_ref(data["ref"]):
        raise ConfigError("ref must be an explicit safe refs/heads/... name")
    if data["mode"] != "immutable-reference-cache":
        raise ConfigError(
            "mode must be immutable-reference-cache; writable checkouts are unsupported"
        )
    destination = _absolute_path(data["destination"], "destination")
    transport = data["transport"]
    ssh_host = ssh_port = known = identity = None
    if transport == "ssh":
        ssh_host, ssh_port, known, identity = _validate_ssh(data, data["repository"])
    elif transport == "local-test" and allow_local_test_transport:
        parsed = urlsplit(data["origin"])
        if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
            raise ConfigError("local-test origin must be a canonical file URI")
    else:
        raise ConfigError("transport must be ssh; local-test is available only to the test harness")
    return Job(
        name=job_name,
        origin=data["origin"],
        repository=data["repository"],
        ref=data["ref"],
        destination=destination,
        transport=transport,
        mode=data["mode"],
        timeout_seconds=_bounded_int(data["timeout_seconds"], "timeout_seconds", 1, 900),
        retries=_bounded_int(data["retries"], "retries", 0, 3),
        lock_timeout_seconds=_bounded_int(
            data["lock_timeout_seconds"], "lock_timeout_seconds", 0, 60
        ),
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        known_hosts_file=known,
        identity_file=identity,
    )


def _reject_symlink_components(path: Path, *, include_leaf: bool = True) -> None:
    candidate = Path(path.root)
    parts = path.parts[1:] if path.is_absolute() else path.parts
    if not include_leaf:
        parts = parts[:-1]
    for part in parts:
        candidate /= part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise SyncError("unsafe symlink in destination path", "unsafe_destination")
        if candidate != path and not stat.S_ISDIR(info.st_mode):
            raise SyncError("unsafe non-directory destination parent", "unsafe_destination")


def _safe_directory(path: Path, mode: int = 0o700) -> None:
    _reject_symlink_components(path)
    path.mkdir(parents=True, mode=mode, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise SyncError("unsafe managed state directory", "unsafe_destination")


def lock_path(state_dir: Path, job_name: str) -> Path:
    return Path(state_dir) / "locks" / f"{job_name}.lock"


@contextlib.contextmanager
def acquire_lock(state_dir: Path, job: Job) -> Iterator[None]:
    path = lock_path(state_dir, job.name)
    _safe_directory(path.parent)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise SyncError("unsafe lock path", "unsafe_destination")
    handle = path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + job.lock_timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise SyncError("repository-scoped lock is busy", "lock_contention") from error
                time.sleep(0.05)
        yield
    finally:
        handle.close()


def _git_env(job: Job) -> dict[str, str]:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if job.transport == "ssh":
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={job.known_hosts_file}",
            "-i",
            str(job.identity_file),
            "-p",
            str(job.ssh_port),
        ]
        env["GIT_SSH_COMMAND"] = shlex.join(command)
    return env


def _classify_git_failure(stderr: str, timed_out: bool = False) -> str:
    if timed_out:
        return "timeout"
    text = stderr.lower()
    auth_errors = ("host key verification failed", "permission denied", "publickey")
    if any(token in text for token in auth_errors):
        return "auth_or_host_key"
    network_errors = (
        "could not resolve",
        "connection refused",
        "connection timed out",
        "network is unreachable",
    )
    if any(token in text for token in network_errors):
        return "network"
    if "remote branch" in text or "couldn't find remote ref" in text:
        return "remote_ref"
    return "git_failure"


def git(job: Job, args: list[str], cwd: Path | None = None, *, retry: bool = False) -> str:
    attempts = job.retries + 1 if retry else 1
    for attempt in range(attempts):
        try:
            proc = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],
                cwd=cwd,
                env=_git_env(job),
                text=True,
                capture_output=True,
                timeout=job.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            if attempt + 1 < attempts:
                continue
            raise SyncError("git operation timed out", "timeout") from error
        if proc.returncode == 0:
            return proc.stdout.strip()
        failure = _classify_git_failure(proc.stderr)
        if attempt + 1 < attempts and failure in {"timeout", "network", "git_failure"}:
            continue
        raise SyncError("git operation failed", failure)
    raise AssertionError("unreachable")


def _validate_checkout_tree(destination: Path) -> None:
    git_dir = destination / ".git"
    try:
        git_info = git_dir.lstat()
    except FileNotFoundError as error:
        raise SyncError("checkout has no .git directory", "wrong_repository") from error
    if stat.S_ISLNK(git_info.st_mode) or not stat.S_ISDIR(git_info.st_mode):
        raise SyncError("checkout .git path is not a real directory", "unsafe_destination")
    for root, directories, files in os.walk(destination, topdown=True, followlinks=False):
        root_path = Path(root)
        if root_path == destination:
            directories[:] = [name for name in directories if name != ".git"]
        for name in directories + files:
            candidate = root_path / name
            if candidate.is_symlink():
                raise SyncError("checkout contains a symlink path", "unsafe_destination")


def _validate_read_only_tree(destination: Path) -> None:
    for root, directories, files in os.walk(destination, topdown=True, followlinks=False):
        candidates = [Path(root), *(Path(root) / name for name in directories + files)]
        for candidate in candidates:
            if candidate.lstat().st_mode & 0o222:
                raise SyncError("immutable cache contains a writable path", "unsafe_destination")


def _make_read_only(destination: Path) -> None:
    for root, directories, files in os.walk(destination, topdown=False, followlinks=False):
        for name in files:
            candidate = Path(root) / name
            if not candidate.is_symlink():
                candidate.chmod(candidate.lstat().st_mode & 0o555)
        for name in directories:
            candidate = Path(root) / name
            if not candidate.is_symlink():
                candidate.chmod(candidate.lstat().st_mode & 0o555)
        root_path = Path(root)
        root_path.chmod(root_path.lstat().st_mode & 0o555)


def _fsync_tree(path: Path) -> None:
    directories = []
    for root, _, files in os.walk(path, topdown=True, followlinks=False):
        root_path = Path(root)
        directories.append(root_path)
        for name in files:
            candidate = root_path / name
            if candidate.is_symlink():
                continue
            descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise SyncError(
            "publication durability could not be confirmed",
            "durability",
        ) from error


def _remove_tree(destination: Path) -> None:
    if not destination.exists() or destination.is_symlink():
        return
    for root, directories, files in os.walk(destination, topdown=False, followlinks=False):
        for name in files:
            candidate = Path(root) / name
            if not candidate.is_symlink():
                candidate.chmod(0o600)
        for name in directories:
            candidate = Path(root) / name
            if not candidate.is_symlink():
                candidate.chmod(0o700)
        Path(root).chmod(0o700)
    shutil.rmtree(destination)


def _checkout_state(job: Job, destination: Path) -> tuple[str, os.stat_result]:
    _reject_symlink_components(destination)
    try:
        info = destination.lstat()
    except FileNotFoundError as error:
        raise SyncError("destination disappeared", "unsafe_destination") from error
    if not stat.S_ISDIR(info.st_mode):
        raise SyncError("destination is not a directory", "unsafe_destination")
    _validate_checkout_tree(destination)
    top = git(job, ["rev-parse", "--show-toplevel"], destination)
    if Path(top) != destination:
        raise SyncError("destination is not the repository root", "wrong_repository")
    try:
        actual_ref = git(job, ["symbolic-ref", "HEAD"], destination)
    except SyncError as error:
        raise SyncError("checkout is detached", "detached") from error
    if actual_ref != job.ref:
        raise SyncError("checkout ref does not match the declared ref", "wrong_ref")
    actual_origin = git(job, ["remote", "get-url", "origin"], destination)
    if actual_origin != job.origin:
        raise SyncError("checkout origin does not match the declared origin", "wrong_origin")
    status_output = git(
        job,
        ["status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"],
        destination,
    )
    if status_output:
        raise SyncError("checkout is dirty or contains ignored local data", "dirty")
    _validate_read_only_tree(destination)
    head = git(job, ["rev-parse", "HEAD"], destination)
    return head, info


def _remote_head(job: Job) -> str:
    try:
        output = git(job, ["ls-remote", "--exit-code", job.origin, job.ref], retry=True)
    except SyncError as error:
        if error.failure_class == "git_failure":
            raise SyncError("declared remote ref is unavailable", "remote_ref") from error
        raise
    rows = [line.split("\t", 1) for line in output.splitlines() if "\t" in line]
    exact = [sha for sha, ref in rows if ref == job.ref and re.fullmatch(r"[0-9a-f]{40,64}", sha)]
    if len(exact) != 1:
        raise SyncError("declared remote ref did not resolve exactly once", "remote_ref")
    return exact[0]


def _clone(job: Job, destination: Path) -> None:
    last_error: SyncError | None = None
    for attempt in range(job.retries + 1):
        if destination.exists() and not destination.is_symlink():
            _remove_tree(destination)
        try:
            git(
                job,
                [
                    "clone",
                    "--no-tags",
                    "--single-branch",
                    "--branch",
                    job.branch,
                    "--origin",
                    "origin",
                    "--",
                    job.origin,
                    str(destination),
                ],
            )
            _validate_checkout_tree(destination)
            git(job, ["fsck", "--strict", "--no-dangling"], destination)
            _fsync_tree(destination)
            _make_read_only(destination)
            _checkout_state(job, destination)
            return
        except SyncError as error:
            last_error = error
            retryable = {"timeout", "network", "git_failure"}
            if attempt >= job.retries or error.failure_class not in retryable:
                raise
    raise last_error or AssertionError("unreachable")


def atomic_exchange(left: Path, right: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise SyncError("atomic directory exchange is unavailable", "atomic_promotion")
    result = renameat2(
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(left)),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(right)),
        ctypes.c_uint(RENAME_EXCHANGE),
    )
    if result != 0:
        errno = ctypes.get_errno()
        raise SyncError(f"atomic directory exchange failed with errno {errno}", "atomic_promotion")


def _materialize(
    job: Job,
    destination_existed: bool,
    old_head: str | None,
    old_info: os.stat_result | None,
) -> Result:
    parent = job.destination.parent
    _reject_symlink_components(parent)
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _reject_symlink_components(parent)
    staging = parent / f".{job.destination.name}.hermes-sync.{job.name}.staging"
    if staging.exists() or staging.is_symlink():
        raise SyncError(
            "stale staging from an uncertain prior run requires operator inspection",
            "stale_staging",
        )
    # Clone directly into the sibling staging directory. Promotion then renames
    # or exchanges two siblings, so the immutable checkout root may already be
    # read-only without requiring a transient permission relaxation.
    temporary = staging
    try:
        _clone(job, temporary)
        _fsync_directory(temporary)
        _fsync_directory(parent)
        new_head, _ = _checkout_state(job, temporary)
        if destination_existed:
            if old_head is None or old_info is None:
                raise AssertionError("missing existing-checkout state")
            try:
                is_ancestor = subprocess.run(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "-c",
                        "core.fsmonitor=false",
                        "merge-base",
                        "--is-ancestor",
                        old_head,
                        new_head,
                    ],
                    cwd=temporary,
                    env=_git_env(job),
                    timeout=job.timeout_seconds,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise SyncError("ancestry verification timed out", "timeout") from error
            if is_ancestor.returncode != 0:
                raise SyncError(
                    "local checkout has diverged from the declared remote ref", "diverged"
                )
            current_head, current_info = _checkout_state(job, job.destination)
            current_identity = (current_info.st_dev, current_info.st_ino)
            old_identity = (old_info.st_dev, old_info.st_ino)
            if current_head != old_head or current_identity != old_identity:
                raise SyncError("checkout changed during synchronization", "concurrent_change")
            atomic_exchange(temporary, job.destination)
            try:
                _checkout_state(job, temporary)
                _checkout_state(job, job.destination)
            except SyncError as error:
                atomic_exchange(temporary, job.destination)
                _fsync_directory(staging)
                _fsync_directory(job.destination.parent)
                raise SyncError(
                    "cache changed during atomic promotion; original cache restored",
                    "concurrent_change",
                ) from error
            _fsync_directory(staging)
            _fsync_directory(job.destination.parent)
        else:
            if job.destination.exists() or job.destination.is_symlink():
                raise SyncError("destination appeared during synchronization", "concurrent_change")
            os.rename(temporary, job.destination)
            _fsync_directory(job.destination.parent)
            _checkout_state(job, job.destination)
        return Result.CHANGED
    finally:
        if staging.exists() and not staging.is_symlink():
            _remove_tree(staging)


def _metric_target(state_dir: Path, job_name: str) -> Path:
    metrics = Path(state_dir) / "metrics"
    _safe_directory(metrics)
    target = metrics / f"{job_name}.prom"
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise SyncError("unsafe metrics target", "unsafe_destination")
    return target


def _previous_success(target: Path, job_name: str) -> int | None:
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SyncError("unsafe metrics target", "unsafe_destination") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            raise SyncError("unsafe metrics target", "unsafe_destination")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            content = handle.read()
    finally:
        os.close(descriptor)
    pattern = re.compile(
        rf'^hermes_repository_sync_last_success_seconds\{{job="{re.escape(job_name)}"\}} ([0-9]+)$'
    )
    for line in content.splitlines():
        match = pattern.fullmatch(line)
        if match:
            return int(match.group(1))
    return None


def _write_metric(state_dir: Path, job_name: str, result: str, *, success: bool) -> None:
    target = _metric_target(state_dir, job_name)
    metrics = target.parent
    now = int(time.time())
    last_success = None
    if success:
        last_success = now
    else:
        last_success = _previous_success(target, job_name)
    lines = [
        "# HELP hermes_repository_sync_last_run_seconds Last completed sync run.",
        "# TYPE hermes_repository_sync_last_run_seconds gauge",
        f'hermes_repository_sync_last_run_seconds{{job="{job_name}",result="{result}"}} {now}',
    ]
    if last_success is not None:
        lines.extend(
            [
                "# HELP hermes_repository_sync_last_success_seconds Last successful sync run.",
                "# TYPE hermes_repository_sync_last_success_seconds gauge",
                f'hermes_repository_sync_last_success_seconds{{job="{job_name}"}} {last_success}',
            ]
        )
    fd, temporary_name = tempfile.mkstemp(prefix=f".{job_name}.", dir=metrics)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
        directory_fd = os.open(metrics, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def run(
    registry_path: Path,
    job_name: str,
    state_dir: Path,
    *,
    allow_local_test_transport: bool = False,
) -> Result:
    state_dir = _absolute_path(str(state_dir), "state_dir")
    job = load_job(Path(registry_path), job_name, allow_local_test_transport)
    if state_dir.is_relative_to(job.destination) or job.destination.is_relative_to(state_dir):
        raise ConfigError("state_dir and destination may not overlap")
    _reject_symlink_components(job.destination)
    _metric_target(state_dir, job_name)
    with acquire_lock(state_dir, job):
        try:
            existed = job.destination.exists() or job.destination.is_symlink()
            if existed:
                old_head, old_info = _checkout_state(job, job.destination)
                remote_head = _remote_head(job)
                if old_head == remote_head:
                    result = Result.NO_CHANGE
                else:
                    result = _materialize(job, True, old_head, old_info)
            else:
                result = _materialize(job, False, None, None)
        except SyncError:
            _write_metric(state_dir, job_name, "failure", success=False)
            raise
        _write_metric(state_dir, job_name, result.value, success=True)
        return result


def _diagnostic(error: Exception, job_name: str) -> str:
    failure_class = error.failure_class if isinstance(error, SyncError) else "configuration"
    return json.dumps(
        {
            "component": "hermes-repository-sync",
            "job": job_name if job_name in ALLOWED_JOBS else "invalid",
            "result": "failure",
            "failure_class": failure_class,
            "message": str(error),
        },
        sort_keys=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run(args.registry, args.job, args.state_dir)
    except (ConfigError, SyncError) as error:
        print(_diagnostic(error, args.job), file=sys.stderr)
        return 1
    except (InterruptedError, KeyboardInterrupt):
        print(
            json.dumps(
                {
                    "component": "hermes-repository-sync",
                    "job": args.job,
                    "result": "failure",
                    "failure_class": "interrupted",
                    "message": "synchronization interrupted before completion",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
