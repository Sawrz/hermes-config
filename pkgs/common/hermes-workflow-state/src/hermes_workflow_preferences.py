#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from hermes_workflow_state import (
    MAX_DOCUMENT_BYTES,
    GenerationStore,
    ProtocolError,
    StateAbsent,
    _read_regular,
    _sha256,
    canonical_json_bytes,
    load_json,
    validate_preference_contract,
    validate_preferences,
)


def _read_requested(path: Path) -> Any:
    return load_json(path)


def _store(args: argparse.Namespace, contract: dict[str, Any]) -> GenerationStore:
    return GenerationStore(
        args.state_dir,
        protocol="preferences-" + contract["name"],
        schema_version=contract["preference_version"],
        lock_timeout=args.lock_timeout,
    )


def _decode_selected(
    selected: Any | None, contract: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    if selected is None:
        return None, validate_preferences(contract, {})
    if set(selected.documents) != {"audit.json", "preferences.json"}:
        raise ProtocolError("selected preference generation has an unexpected document set")
    audit = selected.documents["audit.json"]
    audit_fields = {
        "schema_version",
        "actor",
        "reason",
        "changed_at",
        "previous_generation",
        "changed_keys",
    }
    if not isinstance(audit, dict) or set(audit) != audit_fields or audit["schema_version"] != 1:
        raise ProtocolError("selected preference audit is malformed")
    for field, maximum in (("actor", 100), ("reason", 200)):
        value = audit[field]
        if (
            not isinstance(value, str)
            or not value
            or len(value) > maximum
            or any(ord(ch) < 32 for ch in value)
        ):
            raise ProtocolError(f"selected preference audit {field} is malformed")
    try:
        changed_at = dt.datetime.fromisoformat(audit["changed_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ProtocolError("selected preference audit timestamp is malformed") from exc
    if changed_at.tzinfo is None or not audit["changed_at"].endswith("Z"):
        raise ProtocolError("selected preference audit timestamp is not canonical UTC")
    previous = audit["previous_generation"]
    if previous is not None and (
        not isinstance(previous, str) or not re.fullmatch(r"g-[0-9a-f]{64}", previous)
    ):
        raise ProtocolError("selected preference audit previous generation is malformed")
    changed_keys = audit["changed_keys"]
    if (
        not isinstance(changed_keys, list)
        or changed_keys != sorted(set(changed_keys))
        or not all(isinstance(key, str) and key in contract["properties"] for key in changed_keys)
    ):
        raise ProtocolError("selected preference audit changed keys are malformed")
    current = selected.documents["preferences.json"]
    expected = {"schema_version", "values"}
    if not isinstance(current, dict) or set(current) != expected:
        raise ProtocolError("selected preference document is malformed")
    validated = validate_preferences(contract, current["values"])
    if current != validated:
        raise ProtocolError("selected preferences are not canonical for the active contract")
    return selected.generation, current


def _current(store: GenerationStore, contract: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    try:
        selected = store.read()
    except StateAbsent:
        selected = None
    return _decode_selected(selected, contract)


def _changes(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    return {
        key: {"from": old["values"][key], "to": new["values"][key]}
        for key in sorted(new["values"])
        if old["values"][key] != new["values"][key]
    }


def _audit_text(value: str, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(ch) < 32 for ch in value)
    ):
        raise ProtocolError(f"audit {label} must be bounded text without controls")
    return value


def _check_expected(expected: str | None, actual: str | None) -> None:
    if expected is not None and expected != (actual or "absent"):
        raise ProtocolError("preference generation changed concurrently")


def _backup_payload(
    contract: dict[str, Any], generation: str, preferences: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": contract["name"],
        "preference_version": contract["preference_version"],
        "source_generation": generation,
        "preferences": preferences,
    }


def _load_backup(path: Path, contract: dict[str, Any]) -> tuple[dict[str, Any], str]:
    raw = _read_regular(path, MAX_DOCUMENT_BYTES, "preference backup")
    try:
        envelope = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("preference backup JSON is malformed") from exc
    if raw != canonical_json_bytes(envelope):
        raise ProtocolError("preference backup is not canonical or is corrupt")
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "sha256"}:
        raise ProtocolError("preference backup envelope is malformed")
    payload = envelope["payload"]
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "name",
        "preference_version",
        "source_generation",
        "preferences",
    }:
        raise ProtocolError("preference backup payload is malformed")
    digest = _sha256(canonical_json_bytes(payload))
    if envelope["sha256"] != digest:
        raise ProtocolError("preference backup digest is invalid")
    if (
        payload["schema_version"] != 1
        or payload["name"] != contract["name"]
        or payload["preference_version"] != contract["preference_version"]
    ):
        raise ProtocolError("preference backup is incompatible or a downgrade")
    if not isinstance(payload["source_generation"], str) or not re.fullmatch(
        r"g-[0-9a-f]{64}", payload["source_generation"]
    ):
        raise ProtocolError("preference backup source generation is malformed")
    validated = validate_preferences(contract, payload["preferences"].get("values", {}))
    if payload["preferences"] != validated:
        raise ProtocolError("preference backup values are not canonical")
    return payload, digest


def _write_backup(path: Path, envelope: dict[str, Any]) -> None:
    if not path.is_absolute():
        raise ProtocolError("preference backup output must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_json_bytes(envelope))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _decode_tombstone(selected: Any, contract: dict[str, Any]) -> dict[str, Any] | None:
    if set(selected.documents) != {"audit.json", "tombstone.json"}:
        return None
    tombstone = selected.documents["tombstone.json"]
    if not isinstance(tombstone, dict) or set(tombstone) != {
        "schema_version",
        "name",
        "preference_version",
        "removed_at",
        "previous_generation",
        "backup_sha256",
    }:
        raise ProtocolError("selected preference tombstone is malformed")
    if (
        tombstone["schema_version"] != 1
        or tombstone["name"] != contract["name"]
        or tombstone["preference_version"] != contract["preference_version"]
        or not isinstance(tombstone["backup_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", tombstone["backup_sha256"])
    ):
        raise ProtocolError("selected preference tombstone is incompatible")
    return tombstone


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Bounded crash-safe workflow preference control")
    result.add_argument("--state-dir", type=Path, required=True)
    result.add_argument("--contract", type=Path, required=True)
    result.add_argument("--lock-timeout", type=float, default=10.0)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    for name in ("validate", "preview", "readback"):
        command = commands.add_parser(name)
        command.add_argument("--input", type=Path, required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--input", type=Path, required=True)
    for command in (apply, commands.add_parser("disable")):
        command.add_argument("--expected-generation")
        command.add_argument("--actor", required=True)
        command.add_argument("--reason", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    remove = commands.add_parser("remove")
    remove.add_argument("--expected-generation", required=True)
    remove.add_argument("--confirm-removal", required=True)
    remove.add_argument("--backup", type=Path, required=True)
    remove.add_argument("--actor", required=True)
    remove.add_argument("--reason", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--expected-generation", required=True)
    restore.add_argument("--actor", required=True)
    restore.add_argument("--reason", required=True)
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    contract = validate_preference_contract(load_json(args.contract))
    store = _store(args, contract)
    if args.command in {"show", "backup"}:
        try:
            selected = store.read()
        except StateAbsent:
            selected = None
        if selected is not None and _decode_tombstone(selected, contract) is not None:
            if args.command == "backup":
                raise ProtocolError("removed preferences cannot be backed up; restore first")
            return {"generation": selected.generation, "lifecycle": "removed"}
        generation, current = _decode_selected(selected, contract)
        if args.command == "show":
            return {"generation": generation, "lifecycle": "active", **current}
        if generation is None:
            raise ProtocolError("absent preferences cannot be backed up")
        payload = _backup_payload(contract, generation, current)
        envelope = {"payload": payload, "sha256": _sha256(canonical_json_bytes(payload))}
        _write_backup(args.output, envelope)
        return {"generation": generation, "backup": str(args.output), "sha256": envelope["sha256"]}

    if args.command in {"remove", "restore"}:
        _audit_text(args.actor, "actor", 100)
        _audit_text(args.reason, "reason", 200)
        backup, backup_digest = _load_backup(args.backup, contract)
        if args.command == "remove" and args.confirm_removal != contract["name"]:
            raise ProtocolError("removal confirmation must exactly match the preference name")
        operation: dict[str, Any] = {}

        def lifecycle_transform(selected: Any | None) -> dict[str, Any]:
            if selected is None:
                raise ProtocolError("preferences are absent")
            tombstone = _decode_tombstone(selected, contract)
            if tombstone is None:
                previous_generation, previous = _decode_selected(selected, contract)
            else:
                previous_generation, previous = selected.generation, None
            _check_expected(args.expected_generation, previous_generation)
            now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
            if args.command == "remove":
                if previous is None:
                    raise ProtocolError("preferences are already removed")
                if previous["values"]["enabled"]:
                    raise ProtocolError("preferences must be disabled before removal")
                backup_preferences = backup["preferences"]
                disabled_backup = validate_preferences(
                    contract,
                    {**backup_preferences["values"], "enabled": False},
                )
                selected_audit = selected.documents["audit.json"]
                backup_matches_current = (
                    backup_preferences == previous
                    and backup["source_generation"] == previous_generation
                ) or (
                    disabled_backup == previous
                    and selected_audit["previous_generation"] == backup["source_generation"]
                )
                if not backup_matches_current:
                    raise ProtocolError("removal backup does not match the current preferences")
                audit = {
                    "schema_version": 1,
                    "actor": args.actor,
                    "reason": args.reason,
                    "changed_at": now,
                    "previous_generation": previous_generation,
                    "changed_keys": [],
                }
                return {
                    "audit.json": audit,
                    "tombstone.json": {
                        "schema_version": 1,
                        "name": contract["name"],
                        "preference_version": contract["preference_version"],
                        "removed_at": now,
                        "previous_generation": previous_generation,
                        "backup_sha256": backup_digest,
                    },
                }
            if tombstone is not None and backup_digest != tombstone["backup_sha256"]:
                raise ProtocolError("restore backup does not match the selected tombstone")
            restored = backup["preferences"]
            audit = {
                "schema_version": 1,
                "actor": args.actor,
                "reason": args.reason,
                "changed_at": now,
                "previous_generation": previous_generation,
                "changed_keys": sorted(contract["properties"]),
            }
            operation["restored"] = restored
            return {"audit.json": audit, "preferences.json": restored}

        selected = store.update(lifecycle_transform)
        if args.command == "remove":
            if _decode_tombstone(selected, contract) is None:
                raise ProtocolError("preference removal readback did not select a tombstone")
            store.cleanup(keep_previous=1)
            return {
                "generation": selected.generation,
                "lifecycle": "removed",
                "backup_sha256": backup_digest,
            }
        selected_generation, readback = _decode_selected(selected, contract)
        if readback != operation["restored"]:
            raise ProtocolError("preference restore readback did not match the validated backup")
        store.cleanup(keep_previous=1)
        return {
            "generation": selected_generation,
            "lifecycle": "restored",
            "preferences": readback,
            "backup_sha256": backup_digest,
        }

    requested = (
        validate_preferences(contract, _read_requested(args.input))
        if args.command in {"validate", "preview", "readback", "apply"}
        else None
    )
    if args.command == "validate":
        assert requested is not None
        return requested

    if args.command == "preview":
        assert requested is not None
        generation, current = _current(store, contract)
        return {
            "generation": generation,
            "changes": _changes(current, requested),
            "requested": requested,
        }
    if args.command == "readback":
        generation, current = _current(store, contract)
        return {"generation": generation, "matches": current == requested, "current": current}
    if args.command in {"apply", "disable"}:
        _audit_text(args.actor, "actor", 100)
        _audit_text(args.reason, "reason", 200)
        operation: dict[str, Any] = {}

        def transform(selected: Any | None) -> dict[str, Any]:
            previous_generation, previous = _decode_selected(selected, contract)
            _check_expected(args.expected_generation, previous_generation)
            target = requested
            if args.command == "disable":
                target = validate_preferences(
                    contract,
                    {**previous["values"], "enabled": False},
                )
            assert target is not None
            audit = {
                "schema_version": 1,
                "actor": args.actor,
                "reason": args.reason,
                "changed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                "previous_generation": previous_generation,
                "changed_keys": sorted(_changes(previous, target)),
            }
            operation["changes"] = audit["changed_keys"]
            operation["requested"] = target
            return {"preferences.json": target, "audit.json": audit}

        selected = store.update(transform)
        selected_generation, readback = _decode_selected(selected, contract)
        if readback != operation["requested"]:
            raise ProtocolError(
                "preference publication readback did not match the applied generation"
            )
        cleanup_error = None
        try:
            store.cleanup(keep_previous=1)
        except (ProtocolError, OSError) as exc:
            # The selected generation is already durable. Report retention
            # cleanup separately so callers do not retry a successful apply.
            cleanup_error = str(exc)
        result = {
            "generation": selected_generation,
            "changes": operation["changes"],
            "preferences": readback,
            "retention_cleanup": "pass" if cleanup_error is None else "failed",
            "lifecycle": "disabled" if args.command == "disable" else "active",
        }
        if cleanup_error is not None:
            result["retention_error"] = cleanup_error
        return result
    raise ProtocolError("unsupported preference command")


def main() -> int:
    args = parser().parse_args()
    try:
        sys.stdout.buffer.write(canonical_json_bytes(run(args)))
        return 0
    except (ProtocolError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
