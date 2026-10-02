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
import tempfile
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from croniter import CroniterError, croniter


class ReconcileError(RuntimeError):
    pass


class PolicyError(ReconcileError):
    """Policy is invalid, but affected scheduler records are verified inactive."""


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_JOB_HEADER = re.compile(r"^\s{2}(\S+)\s+\[(active|paused|disabled|completed)\]\s*$")
_FIELD = re.compile(r"^\s{4}(Name|Schedule|Deliver|Skills|Script|Mode):\s*(.*)$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DURATION = re.compile(
    r"^every [0-9]+\s*(?:m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$",
    re.IGNORECASE,
)
_CRON_FIELD = re.compile(r"^[0-9*\-,/]+$")
_TELEGRAM_ROUTE = re.compile(r"^telegram:-?[0-9]{1,20}(?::[0-9]{1,20})?$")


def _load_json(path: Path, *, private: bool) -> Any:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ReconcileError(f"cannot stat {path}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ReconcileError(f"{path} must be a regular non-symlink file")
    if private and stat.S_IMODE(before.st_mode) & 0o077:
        raise ReconcileError(f"{path} must not be accessible by group or other")
    if before.st_size <= 0 or before.st_size > 1024 * 1024:
        raise ReconcileError(f"{path} has an unsafe size")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReconcileError(f"cannot open {path} safely: {exc}") from exc
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            raise ReconcileError(f"{path} changed during validation")
        if private and stat.S_IMODE(after.st_mode) & 0o077:
            raise ReconcileError(f"{path} must not be accessible by group or other")
        if after.st_size <= 0 or after.st_size > 1024 * 1024:
            raise ReconcileError(f"{path} has an unsafe size")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            content = handle.read()
    except (OSError, UnicodeError) as exc:
        raise ReconcileError(f"cannot read {path} safely: {exc}") from exc
    finally:
        os.close(descriptor)
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ReconcileError(f"cannot read valid JSON from {path}: {exc}") from exc


def parse_cron_list(output: str, scripts_root: Path | None = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    field_names = {
        "Name": "name",
        "Schedule": "schedule",
        "Deliver": "deliver",
        "Skills": "skills",
        "Script": "script",
        "Mode": "mode",
    }
    for line in _ANSI.sub("", output).splitlines():
        header = _JOB_HEADER.match(line)
        if header:
            if current:
                rows.append(current)
            current = {"id": header.group(1), "state": header.group(2)}
            continue
        field = _FIELD.match(line)
        if field and current is not None:
            current[field_names[field.group(1)]] = field.group(2).strip()
    if current:
        rows.append(current)
    seen_ids: set[str] = set()
    for row in rows:
        if not row.get("name") or row["id"] in seen_ids:
            raise ReconcileError("Hermes cron output is incomplete or has duplicate IDs")
        seen_ids.add(row["id"])
        if scripts_root is not None and row.get("script"):
            row["script"] = str(scripts_root / row["script"])
    return rows


def _validate_contract(raw: Any) -> tuple[list[dict[str, Any]], Path]:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "managed_script_dir", "jobs"}
        or raw.get("schema_version") != 1
    ):
        raise ReconcileError("required-job contract schema_version or fields are invalid")
    managed_script_dir_value = raw["managed_script_dir"]
    if not isinstance(managed_script_dir_value, str):
        raise ReconcileError("managed script directory must be an absolute .nix-managed directory")
    managed_script_dir = Path(managed_script_dir_value)
    if (
        not managed_script_dir.is_absolute()
        or managed_script_dir == Path("/")
        or ".." in managed_script_dir.parts
        or managed_script_dir.name != ".nix-managed"
    ):
        raise ReconcileError("managed script directory must be an absolute .nix-managed directory")
    jobs = raw.get("jobs")
    if not isinstance(jobs, list):
        raise ReconcileError("required-job contract jobs must be a list")
    names: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for row in jobs:
        if not isinstance(row, dict) or set(row) != {
            "name",
            "script",
            "no_agent",
            "deliver_prefix",
            "skills",
        }:
            raise ReconcileError("required-job contract row has unexpected fields")
        name = row["name"]
        if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name) or name in names:
            raise ReconcileError("required-job names must be safe and unique")
        script = row["script"]
        if (
            not isinstance(script, str)
            or not Path(script).is_absolute()
            or ".." in Path(script).parts
            or Path(script).parent != managed_script_dir
        ):
            raise ReconcileError(f"required job {name} is outside the managed script directory")
        no_agent = row["no_agent"]
        skills = row["skills"]
        if not isinstance(no_agent, bool) or not isinstance(skills, list):
            raise ReconcileError(f"required job {name} has invalid mode or skills")
        if any(not isinstance(skill, str) or not _SAFE_NAME.fullmatch(skill) for skill in skills):
            raise ReconcileError(f"required job {name} has unsafe skills")
        if no_agent and skills:
            raise ReconcileError(f"script-only required job {name} may not attach skills")
        deliver_prefix = row["deliver_prefix"]
        if deliver_prefix not in {"local", "telegram:"}:
            raise ReconcileError(f"required job {name} has unsupported delivery ownership")
        names.add(name)
        normalized.append(row)
    return normalized, managed_script_dir


def _valid_schedule(value: str) -> bool:
    if _DURATION.fullmatch(value):
        return True
    fields = value.split()
    if len(fields) < 5 or not all(_CRON_FIELD.fullmatch(field) for field in fields[:5]):
        return False
    try:
        croniter(value)
    except CroniterError:
        return False
    return True


def _publish_policy(path: Path, value: Any, *, create_only: bool = False) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".native-cron-policy-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if create_only:
            try:
                os.link(temporary, path)
            except FileExistsError:
                return  # Another creator wins; read their complete policy below.
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _prepare_policy(path: Path, jobs: list[dict[str, Any]]) -> Any:
    if not path.exists() and not path.is_symlink():
        value = {
            "schema_version": 2,
            "jobs": {job["name"]: {"status": "unconfigured"} for job in jobs},
        }
        _publish_policy(path, value, create_only=True)
    raw = _load_json(path, private=True)
    migrated = raw == {} or (isinstance(raw, dict) and raw.get("schema_version") == 1)
    if migrated:
        if raw != {} and (
            set(raw) != {"schema_version", "jobs"}
            or type(raw["schema_version"]) is not int
            or not isinstance(raw["jobs"], dict)
        ):
            raise ReconcileError("invalid legacy profile cron policy")
        prefixes = {job["name"]: job["deliver_prefix"] for job in jobs}
        configured = {
            name: {"status": "enabled", **_validate_preferences(name, entry, prefixes.get(name))}
            for name, entry in raw.get("jobs", {}).items()
        }
        raw = {
            "schema_version": 2,
            "jobs": {**{job["name"]: {"status": "disabled"} for job in jobs}, **configured},
        }
    entries = _policy_entries(raw)
    validation_errors: list[str] = []
    _validate_policy(raw, jobs, errors=validation_errors)
    if validation_errors:
        return raw  # Diagnose per job below; never rewrite an invalid policy.
    missing = {job["name"] for job in jobs} - entries.keys()
    if missing or migrated:
        raw["jobs"].update({name: {"status": "unconfigured"} for name in missing})
        _publish_policy(path, raw)
    return raw


def _policy_entries(raw: Any) -> dict[str, Any]:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "jobs"}
        or type(raw.get("schema_version")) is not int
        or raw["schema_version"] != 2
        or not isinstance(raw.get("jobs"), dict)
    ):
        raise ReconcileError("profile cron policy requires schema_version 2 with a jobs object")
    if any(not _SAFE_NAME.fullmatch(name) for name in raw["jobs"]):
        raise ReconcileError("profile cron policy has unsafe job names")
    return raw["jobs"]


def _validate_preferences(name: str, policy: Any, prefix: str | None) -> dict[str, str]:
    if not isinstance(policy, dict) or set(policy) != {"schedule", "deliver"}:
        raise ReconcileError(f"profile cron policy for {name} must contain schedule and deliver")
    schedule, deliver = policy["schedule"], policy["deliver"]
    if (
        not isinstance(schedule, str)
        or not schedule.strip()
        or len(schedule) > 128
        or any(char in schedule for char in "\r\n\x00")
        or "PROFILE_OWNS" in schedule
        or not _valid_schedule(schedule)
    ):
        raise ReconcileError(f"profile cron policy for {name} has an invalid schedule")
    if (
        not isinstance(deliver, str)
        or len(deliver) > 80
        or any(char in deliver for char in "\r\n\x00")
    ):
        raise ReconcileError(f"profile cron policy for {name} has an invalid delivery route")
    if (
        (prefix == "local" and deliver != "local")
        or (prefix == "telegram:" and not _TELEGRAM_ROUTE.fullmatch(deliver))
        or (prefix is None and deliver != "local" and not _TELEGRAM_ROUTE.fullmatch(deliver))
    ):
        raise ReconcileError(f"profile cron policy for {name} violates delivery ownership")
    return {"schedule": schedule, "deliver": deliver}


def _validate_policy(
    raw: Any, jobs: list[dict[str, Any]], *, errors: list[str] | None = None
) -> dict[str, dict[str, str]]:
    entries = _policy_entries(raw)
    result = {}
    failures = []
    for job in jobs:
        name = job["name"]
        entry = entries.get(name, {"status": "unconfigured"})
        try:
            if (
                not isinstance(entry, dict)
                or "status" not in entry
                or not isinstance(entry["status"], str)
                or entry["status"] not in {"unconfigured", "disabled", "enabled"}
                or set(entry) - {"status", "schedule", "deliver"}
            ):
                raise ReconcileError(f"profile cron policy for {name} has invalid status or fields")
            if entry["status"] == "enabled":
                result[name] = _validate_preferences(
                    name, {k: v for k, v in entry.items() if k != "status"}, job["deliver_prefix"]
                )
        except ReconcileError as exc:
            failures.append(str(exc))
    if errors is not None:
        errors.extend(failures)
    elif failures:
        raise ReconcileError("; ".join(failures))
    return result


def _skills(row: dict[str, str]) -> list[str]:
    return [skill.strip() for skill in row.get("skills", "").split(",") if skill.strip()]


def _definition_matches(
    wanted: dict[str, Any], profile: dict[str, str], row: dict[str, str]
) -> bool:
    return (
        row.get("schedule") == profile["schedule"]
        and row.get("deliver") == profile["deliver"]
        and row.get("script") == wanted["script"]
        and sorted(_skills(row)) == sorted(wanted["skills"])
        and ("no-agent" in row.get("mode", "")) == wanted["no_agent"]
    )


def _assert_observed(
    jobs: list[dict[str, Any]], policy: dict[str, dict[str, str]], rows: list[dict[str, str]]
) -> None:
    by_name: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_name.setdefault(row["name"], []).append(row)
    for wanted in jobs:
        identity = wanted["name"]
        matches = by_name.get(identity, [])
        if len(matches) != 1:
            raise ReconcileError(f"required native job must exist exactly once: {identity}")
        row = matches[0]
        expected = policy[identity]
        if row.get("state") != "active":
            raise ReconcileError(f"required native job is disabled: {identity}")
        if row.get("schedule") != expected["schedule"]:
            raise ReconcileError(
                f"required native job has the wrong profile-owned schedule: {identity}: "
                f"expected {expected['schedule']!r}, observed {row.get('schedule')!r}"
            )
        if row.get("deliver") != expected["deliver"]:
            raise ReconcileError(f"required native job has the wrong delivery route: {identity}")
        if row.get("script") != wanted["script"]:
            raise ReconcileError(f"required native job has the wrong managed script: {identity}")
        if sorted(_skills(row)) != sorted(wanted["skills"]):
            raise ReconcileError(f"required native job has the wrong skills: {identity}")
        observed_no_agent = "no-agent" in row.get("mode", "")
        if observed_no_agent != wanted["no_agent"]:
            raise ReconcileError(f"required native job has the wrong agent mode: {identity}")


def _stale_managed_jobs(
    jobs: list[dict[str, Any]], managed_script_dir: Path, rows: list[dict[str, str]]
) -> list[dict[str, str]]:
    desired_names = {job["name"] for job in jobs}
    return [
        row
        for row in rows
        if row["name"] not in desired_names
        and row.get("script") is not None
        and Path(row["script"]).is_absolute()
        and ".." not in Path(row["script"]).parts
        and Path(row["script"]).parent == managed_script_dir
    ]


def _assert_no_stale_managed_jobs(
    jobs: list[dict[str, Any]], managed_script_dir: Path, rows: list[dict[str, str]]
) -> None:
    stale = _stale_managed_jobs(jobs, managed_script_dir, rows)
    if stale:
        names = sorted((row["name"], row["id"]) for row in stale)
        raise ReconcileError(f"undeclared Nix-managed native jobs remain: {names}")


def _run(command: Sequence[str], env: dict[str, str]) -> str:
    process = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "no diagnostic"
        raise ReconcileError(f"command failed ({process.returncode}): {command[0]}: {detail}")
    return process.stdout


def _cron_options(
    wanted: dict[str, Any], profile: dict[str, str], scripts_root: Path | None = None
) -> list[str]:
    options = [
        "--name",
        str(wanted["name"]),
        "--deliver",
        profile["deliver"],
        "--script",
        str(Path(wanted["script"]).relative_to(scripts_root))
        if scripts_root
        else str(wanted["script"]),
    ]
    for skill in wanted["skills"]:
        options.extend(["--skill", str(skill)])
    return options


def canonicalize_native(
    jobs: list[dict[str, Any]],
    *,
    check_only: bool,
    home: Path | None = None,
    scripts_root: Path | None = None,
) -> None:
    """Use the pinned native job API for fields absent from `cron list/edit`.

    This is a deliberately owned extension, not a second scheduler or store.
    The module runs it under the exact Hermes virtualenv. Native update_job
    retains run history, output, scheduler identity and private state.
    """
    from cron.jobs import list_jobs, update_job, use_cron_store

    if home is not None:
        with use_cron_store(home):
            return canonicalize_native(jobs, check_only=check_only, scripts_root=home / "scripts")

    native = list_jobs(include_disabled=True)
    for wanted in jobs:
        matches = [row for row in native if row["name"] == wanted["name"]]
        if len(matches) > 1:
            raise ReconcileError(f"native cron name collision: {wanted['name']}")
        if not matches:
            continue  # CLI creates the missing native record below.
        row = matches[0]
        observed_script = Path(row.get("script") or ".")
        if scripts_root is not None:
            observed_script = scripts_root / observed_script
        if ".." in observed_script.parts or observed_script.parent != Path(wanted["script"]).parent:
            raise ReconcileError(f"same-named independent job is not adoptable: {wanted['name']}")
        expected = {
            # Payload reset below is limited to positively managed launchers.
            "prompt": "",
            "enabled_toolsets": None,
            "workdir": None,
            "context_from": [],
            "model": None,
            "provider": None,
            "base_url": None,
            "script": str(Path(wanted["script"]).relative_to(scripts_root))
            if scripts_root
            else wanted["script"],
            "skills": wanted["skills"],
            "skill": wanted["skills"][0] if wanted["skills"] else None,
            "no_agent": wanted["no_agent"],
        }
        if wanted["no_agent"]:
            expected.update(provider_snapshot=None, model_snapshot=None)
        # Missing legacy optional fields and their canonical empty form agree.
        wrong = any(row.get(k) != v and not (not row.get(k) and not v) for k, v in expected.items())
        if not wrong:
            continue
        if check_only:
            raise ReconcileError(f"stale native execution contract: {wanted['name']}")
        # Correct payload/mode together while pausing the stale record. The
        # ordinary reconciler resumes only after all declared fields agree.
        updated = update_job(row["id"], {**expected, "enabled": False})
        if updated is None:
            raise ReconcileError("native job disappeared during contract adoption")
    observed = list_jobs(include_disabled=True)
    for wanted in jobs:
        rows = [row for row in observed if row["name"] == wanted["name"]]
        if rows and (
            rows[0].get("prompt")
            or rows[0].get("enabled_toolsets")
            or rows[0].get("workdir")
            or rows[0].get("context_from")
        ):
            raise ReconcileError("native execution contract correction did not read back")


@contextmanager
def _policy_lock(policy_path: Path, *, read_only: bool = False):
    # Stable sidecar, never the replaceable JSON inode. Agent read/modify/rename
    # writers must hold this same lock; never unlink the sidecar.
    path = policy_path.with_name(policy_path.name + ".lock")
    flags = os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT
    try:
        descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileNotFoundError:
        if not read_only:
            raise
        yield
        return
    except OSError as exc:
        raise ReconcileError("cannot safely open policy lock") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ReconcileError("policy lock must be a private regular file")
        fcntl.flock(descriptor, fcntl.LOCK_SH if read_only else fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def reconcile(
    contract_path: Path,
    policy_path: Path,
    hermes: str,
    *,
    status_path: Path | None = None,
    **kwargs,
):
    with _policy_lock(policy_path, read_only=kwargs.get("check_only", False)):
        try:
            policy = _reconcile(contract_path, policy_path, hermes, **kwargs)
            if status_path:
                contract = _load_json(contract_path, private=False)
                jobs, _ = _validate_contract(contract)
                raw = (
                    _load_json(policy_path, private=True)
                    if jobs
                    else {"schema_version": 2, "jobs": {}}
                )
                pending = _unconfigured(raw, jobs)
                _write_status(
                    status_path,
                    "needs-configuration" if pending else ("ready" if policy else "disabled"),
                    "Unconfigured jobs: " + ", ".join(pending)
                    if pending
                    else "Native jobs match the configured policy.",
                    fingerprint=_fingerprint(contract, raw),
                )
            return policy
        except ReconcileError as exc:
            if status_path:
                _write_status(status_path, "error", str(exc))
            raise


def _reconcile(
    contract_path: Path,
    policy_path: Path,
    hermes: str,
    *,
    check_only: bool = False,
    bootstrap: bool = False,
    native_contract: bool = False,
    runner: Callable[[Sequence[str], dict[str, str]], str] = _run,
) -> dict[str, dict[str, str]] | None:
    if bootstrap and check_only:
        raise ReconcileError("bootstrap cannot be combined with read-only checks")
    jobs, managed_script_dir = _validate_contract(_load_json(contract_path, private=False))
    env = dict(os.environ)
    scripts_root = policy_path.parent / "scripts" if native_contract else None
    if scripts_root is not None:
        env["HERMES_HOME"] = str(policy_path.parent)
        try:
            managed_script_dir.relative_to(scripts_root)
        except ValueError as exc:
            raise ReconcileError(
                "managed scripts must be inside the owning profile scripts directory"
            ) from exc
    _retire_old_layout(
        managed_script_dir, hermes, env, runner, check_only=check_only, scripts_root=scripts_root
    )
    try:
        raw_policy = (
            (
                _load_json(policy_path, private=True)
                if check_only
                else _prepare_policy(policy_path, jobs)
            )
            if jobs
            else {"schema_version": 2, "jobs": {}}
        )
        policy_errors: list[str] = []
        policy = _validate_policy(raw_policy, jobs, errors=policy_errors)
    except (ReconcileError, OSError) as exc:
        if check_only:
            raise
        # A matching name alone is not ownership. Missing policy must never
        # suspend an independent user job; only our exact launcher tree counts.

        def managed(row: dict[str, str]) -> bool:
            script = Path(row.get("script") or ".")
            return (
                script.is_absolute()
                and ".." not in script.parts
                and script.parent == managed_script_dir
            )

        initial = parse_cron_list(runner([hermes, "cron", "list", "--all"], env), scripts_root)
        for row in initial:
            if managed(row) and row["state"] == "active":
                runner([hermes, "cron", "pause", row["id"]], env)
        final = parse_cron_list(runner([hermes, "cron", "list", "--all"], env), scripts_root)
        if any(managed(row) and row["state"] == "active" for row in final):
            raise ReconcileError("unconfigured managed jobs remain active after suspension")
        raise PolicyError(str(exc)) from exc
    declared_jobs = jobs
    jobs = [job for job in jobs if job["name"] in policy]
    comparison_policy = policy
    if native_contract:
        from cron.jobs import parse_schedule

        comparison_policy = {
            name: {**value, "schedule": parse_schedule(value["schedule"])["display"]}
            for name, value in policy.items()
        }
        canonicalize_native(jobs, check_only=check_only, home=policy_path.parent)
    initial = parse_cron_list(runner([hermes, "cron", "list", "--all"], env), scripts_root)
    by_name: dict[str, list[dict[str, str]]] = {}
    for row in initial:
        by_name.setdefault(row["name"], []).append(row)
    for wanted in jobs:
        existing = by_name.get(wanted["name"], [])
        if len(existing) > 1:
            raise ReconcileError(f"native cron name collision: {wanted['name']}")
        if existing:
            script = Path(existing[0].get("script") or ".")
            if (
                not script.is_absolute()
                or ".." in script.parts
                or script.parent != managed_script_dir
            ):
                raise ReconcileError(
                    f"same-named independent job is not adoptable: {wanted['name']}"
                )
    if check_only:
        if policy_errors:
            raise ReconcileError("; ".join(policy_errors))
        _assert_no_stale_managed_jobs(declared_jobs, managed_script_dir, initial)
        if any(
            row["name"] not in policy and row["state"] == "active"
            for row in _stale_managed_jobs([], managed_script_dir, initial)
        ):
            raise ReconcileError("unconfigured managed jobs remain active")
        _assert_observed(jobs, comparison_policy, initial)
        return policy
    for stale in _stale_managed_jobs(declared_jobs, managed_script_dir, initial):
        runner([hermes, "cron", "remove", stale["id"]], env)
    for row in _stale_managed_jobs([], managed_script_dir, initial):
        if row["name"] not in policy and row["state"] == "active":
            if row["name"] in {job["name"] for job in declared_jobs}:
                runner([hermes, "cron", "pause", row["id"]], env)
    for wanted in jobs:
        profile = policy[wanted["name"]]
        existing = by_name.get(wanted["name"], [])
        options = _cron_options(wanted, profile, scripts_root)
        if existing:
            row = existing[0]
            if not _definition_matches(wanted, comparison_policy[wanted["name"]], row):
                command = [
                    hermes,
                    "cron",
                    "edit",
                    row["id"],
                    "--schedule",
                    profile["schedule"],
                    *options,
                ]
                if not wanted["skills"]:
                    command.append("--clear-skills")
                command.append("--no-agent" if wanted["no_agent"] else "--agent")
                runner(command, env)
            if row.get("state") != "active":
                runner([hermes, "cron", "resume", row["id"]], env)
        else:
            command = [
                hermes,
                "cron",
                "create",
                profile["schedule"],
                *options,
            ]
            if wanted["no_agent"]:
                command.append("--no-agent")
            runner(command, env)
    final = parse_cron_list(runner([hermes, "cron", "list", "--all"], env), scripts_root)
    _assert_no_stale_managed_jobs(declared_jobs, managed_script_dir, final)
    if any(
        row["name"] not in policy and row["state"] == "active"
        for row in _stale_managed_jobs([], managed_script_dir, final)
    ):
        raise ReconcileError("unconfigured managed jobs remain active after suspension")
    _assert_observed(jobs, comparison_policy, final)
    if native_contract:
        canonicalize_native(jobs, check_only=True, home=policy_path.parent)
    if policy_errors:
        raise PolicyError("; ".join(policy_errors))
    return policy


def _write_status(path: Path, state: str, detail: str, *, fingerprint: str | None = None) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".native-cron-status-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            status = {"schema_version": 1, "state": state, "detail": detail}
            if fingerprint is not None:
                status["fingerprint"] = fingerprint
            json.dump(status, handle)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fingerprint(contract: Any, policy: Any) -> str:
    return hashlib.sha256(json.dumps([contract, policy], sort_keys=True).encode()).hexdigest()


def _retire_old_layout(
    managed_dir: Path,
    hermes: str,
    env: dict[str, str],
    runner: Callable,
    *,
    check_only: bool,
    scripts_root: Path | None = None,
) -> None:
    """Retire only the previous Nix-owned layouts; never touch preference files."""
    if managed_dir.parts[-3:] != ("scripts", "profile", ".nix-managed"):
        return
    scripts = managed_dir.parent.parent

    def legacy(row: dict[str, str]) -> bool:
        path = Path(row.get("script") or ".")
        if not path.is_absolute() or ".." in path.parts:
            return False
        if path.parent == scripts / ".nix-managed":
            return True
        try:
            parts = path.relative_to(scripts / "job-users").parts
        except ValueError:
            return False
        return (
            len(parts) == 3
            and re.fullmatch(r"[1-9][0-9]{0,19}", parts[0]) is not None
            and parts[1] == ".nix-managed"
        )

    rows = parse_cron_list(runner([hermes, "cron", "list", "--all"], env), scripts_root)
    retired = [row for row in rows if legacy(row)]
    if check_only and retired:
        raise ReconcileError("obsolete per-user or legacy profile schedules still exist")
    for row in retired:
        runner([hermes, "cron", "remove", row["id"]], env)
    if retired and any(
        legacy(row)
        for row in parse_cron_list(runner([hermes, "cron", "list", "--all"], env), scripts_root)
    ):
        raise ReconcileError("obsolete managed schedules remain after removal")


def _unconfigured(raw: Any, jobs: list[dict[str, Any]]) -> list[str]:
    entries = _policy_entries(raw)
    return sorted(
        job["name"]
        for job in jobs
        if isinstance(entries.get(job["name"], {"status": "unconfigured"}), dict)
        and entries.get(job["name"], {"status": "unconfigured"}).get("status") == "unconfigured"
    )


def remind(
    contract_path: Path,
    policy_path: Path,
    status_path: Path,
    hermes: str,
    profile: str,
    target: str | Sequence[str],
    *,
    runner: Callable[[Sequence[str], dict[str, str]], str] = _run,
) -> bool:
    """Send one aggregated setup reminder through the profile's native sender."""
    if not _SAFE_NAME.fullmatch(profile):
        raise ReconcileError("reminder profile must be a safe name")
    contract = _load_json(contract_path, private=False)
    jobs, _ = _validate_contract(contract)
    targets = list(dict.fromkeys([target] if isinstance(target, str) else target))
    if not targets or any(not _TELEGRAM_ROUTE.fullmatch(item) for item in targets):
        raise ReconcileError("setup reminders require explicit Telegram chat/topic targets")
    if not jobs:
        return False
    with _policy_lock(policy_path, read_only=True):
        names = _unconfigured(_load_json(policy_path, private=True), jobs)
    if not names:
        return False
    listed: list[str] = []
    for name in names:
        if sum(len(row) + 1 for row in listed) + len(name) > 2200:
            break
        listed.append(f"- {name}")
    if len(listed) < len(names):
        listed.append(f"- ... and {len(names) - len(listed)} more (see the job contract)")
    message = (
        f"{profile}: scheduled-job setup needs attention.\n\n"
        "These unconfigured jobs are waiting for your setup choice:\n"
        + "\n".join(listed)
        + "\n\nSuggested prompt — send this back to me:\n"
        "\"Configure this profile's unconfigured scheduled jobs. Read native-cron-contract.json, "
        "native-cron-policy.json and native-cron-status.json in your profile home. "
        "Check existing preferences and ask me for missing schedules, destinations, or workflow settings. "
        "Keep one shared profile policy, not per-user schedules. Set each chosen job to enabled "
        "with complete preferences, or disabled to opt out of that job. Preserve other entries. "
        "Hold native-cron-policy.json.lock across reading and atomically writing the private schema-2 "
        'policy, then verify readiness and native cron readback before claiming jobs are active."\n\n'
        "I will remind all configured chats daily only about jobs still unconfigured."
    )
    errors = []
    for destination in targets:
        try:
            result = runner(
                [hermes, "send", "--to", destination, "--json", message], dict(os.environ)
            )
            delivery = json.loads(result)
            if (
                not isinstance(delivery, dict)
                or delivery.get("success") is not True
                or delivery.get("skipped")
            ):
                raise ReconcileError("reminder not delivered")
        except (ReconcileError, json.JSONDecodeError):
            errors.append(f"setup reminder delivery failed for {destination}")
    if errors:
        raise ReconcileError("; ".join(errors))
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--hermes", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument(
        "--native-contract",
        action="store_true",
        help="Use pinned Hermes Python API for full execution-contract adoption",
    )
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--remind", action="store_true")
    parser.add_argument("--profile")
    parser.add_argument("--reminder-target", action="append", default=[])
    parser.add_argument("--print-deliver")
    args = parser.parse_args(argv)

    if args.remind:
        if (
            not args.status_file
            or not args.profile
            or not args.reminder_target
            or args.bootstrap
            or args.check
            or args.print_deliver
        ):
            parser.error(
                "--remind requires --profile and --status-file, without reconciliation flags"
            )
        try:
            remind(
                args.contract,
                args.policy,
                args.status_file,
                args.hermes,
                args.profile,
                args.reminder_target,
            )
        except ReconcileError as exc:
            print(f"hermes-native-cron-reconciler: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.print_deliver and not args.check:
        parser.error("--print-deliver requires --check")
    if args.check and (args.bootstrap or args.status_file):
        parser.error("--check cannot be combined with --bootstrap or --status-file")
    try:
        policy = reconcile(
            args.contract,
            args.policy,
            args.hermes,
            check_only=args.check,
            bootstrap=args.bootstrap,
            native_contract=args.native_contract,
            status_path=args.status_file,
        )
        if args.print_deliver:
            if policy is None or args.print_deliver not in policy:
                raise ReconcileError("requested delivery job is not in the required contract")
            print(policy[args.print_deliver]["deliver"])
    except ReconcileError as exc:
        print(f"hermes-native-cron-reconciler: {exc}", file=sys.stderr)
        if args.bootstrap and isinstance(exc, PolicyError):
            return 0  # Keep the interactive gateway available to repair policy.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
