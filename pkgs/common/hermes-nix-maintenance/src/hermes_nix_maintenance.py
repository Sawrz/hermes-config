#!/usr/bin/env python3
"""Deterministic mechanics for explicitly configured Nix repositories.

This module deliberately stops at typed source collection and durable intent
publication. Forgejo/Kanban delivery belongs to F10/F11, Kanban/Vikunja delivery
belongs to V20, Prometheus incident routing belongs to M30, and profile roots or
activation belong to P70/H80.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hermes_repository_instance import (
    instance,
    instance_argument,
    activate_instance,
    RepositoryStore,
)
from hermes_workflow_state import ProtocolError, StateAbsent, exclusive_lock

SCHEMA_VERSION = 1
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PERIOD_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class WorkflowError(ProtocolError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    if not path.is_absolute():
        path = path.resolve()
    try:
        if path.is_symlink() or not path.is_file():
            raise WorkflowError(f"input must be a regular non-symlink file: {path}")
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"cannot load JSON input: {path}") from exc


def canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkflowError("value is not canonical JSON data") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise WorkflowError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _text(value: Any, label: str, maximum: int = 500) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(ch) < 32 for ch in value)
    ):
        raise WorkflowError(f"{label} must be bounded text without control characters")
    return value


def _sha(value: Any, label: str = "source commit") -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise WorkflowError(f"{label} must be an exact lowercase 40-hex commit")
    return value


def validate_exact_target(repository: Any, source_commit: Any) -> tuple[str, str]:
    if repository != instance().repository:
        raise WorkflowError(f"repository target must be exactly {instance().repository}")
    return instance().repository, _sha(source_commit)


def refresh_container_inventory(repo: Path, target: str) -> dict[str, Any]:
    """Evaluate effective host image tuples from one immutable Git revision.

    Host URL and tag overrides are evaluated together; module defaults are never
    rewritten or inferred with text parsing. A locked git flake retains the exact
    target even if the reference cache is promoted during evaluation.
    """
    validate_exact_target(instance().repository, target)
    repo = repo.resolve()
    reference = "git+" + repo.as_uri() + "?rev=" + target
    expression = r"""
      let
        flake = builtins.getFlake REFERENCE;
        hosts = if flake ? nixosConfigurations && builtins.isAttrs flake.nixosConfigurations && flake.nixosConfigurations != {} then flake.nixosConfigurations
                else throw "unsupported inventory: nixosConfigurations missing";
        roots = flake.sourceRepositories or (throw "inventory requires explicit source ownership");
        locate = path: let
          entries = builtins.filter (entry: (builtins.substring 0 (builtins.stringLength "${entry.root}/") path == "${entry.root}/")) (builtins.attrValues roots);
        in if builtins.length entries != 1 then throw "ambiguous or unknown image definition owner" else
          let entry = builtins.head entries; in {
            inherit (entry) repository revision;
            path = builtins.substring (builtins.stringLength "${entry.root}/") (-1) path;
          };
        strip = value: if builtins.isAttrs value && (value._type or "") == "override" then strip value.content else value;
        unique = files: builtins.foldl' (result: file: if builtins.elem file result then result else result ++ [ file ]) [] files;
        ownerOf = option: matches: let
          files = unique (if matches == [] then option.declarations else map (d: d.file) matches);
        in if builtins.length files != 1 then throw "ambiguous effective image definition" else locate (builtins.head files);
        splitReference = reference: let
          digest = builtins.match "(.+)@([^@]+)" reference;
          tag = builtins.match "(.+):([^/:]+)" reference;
        in if digest != null then { image = builtins.elemAt digest 0; current_tag = builtins.elemAt digest 1; }
           else if tag != null then { image = builtins.elemAt tag 0; current_tag = builtins.elemAt tag 1; }
           else throw "inventory requires an explicit image tag or digest";
        rows = builtins.concatMap (host:
          let containers = if hosts.${host}.config ? custom.containers then hosts.${host}.config.custom.containers
                           else throw "unsupported inventory: custom.containers missing";
          in builtins.concatMap (service:
            let
              config = containers.${service};
              options = hosts.${host}.options.custom.containers.${service};
              referenceRow = component: reference: option:
                { inherit host service component;
                  change_route = ownerOf option (builtins.filter (d: strip d.value == reference) option.definitionsWithLocations);
                } // splitReference reference;
            in if !(config.enable or false) then []
            else if config ? images && builtins.isAttrs config.images && config.images != {} then
              map (component: {
                inherit host service component;
                image = config.images.${component}.url;
                current_tag = config.images.${component}.tag;
                change_route = ownerOf options.images (builtins.filter (d:
                  builtins.isAttrs (strip d.value) && (strip d.value) ? ${component}
                  && ((strip ((strip d.value).${component})).tag or null) != null
                  && strip ((strip ((strip d.value).${component})).tag) == config.images.${component}.tag
                ) options.images.definitionsWithLocations);
              }) (builtins.attrNames config.images)
            else if service == "wolf" then
              [ (referenceRow "main" config.image options.image)
                (referenceRow "ui" config.uiImage options.uiImage) ]
              ++ (if config.deviceProfileUi != null && config.deviceProfiles != {} then [
                ({ inherit host service; component = "ui-sdk";
                   change_route = ownerOf options.deviceProfileUi (builtins.filter
                     (d: ((strip d.value).sdkImage or null) == config.deviceProfileUi.sdkImage)
                     options.deviceProfileUi.definitionsWithLocations);
                 } // splitReference config.deviceProfileUi.sdkImage)
              ] else [])
              ++ (if config.drop.enable then [
                (referenceRow "drop-base" config.drop.baseImage options.drop.baseImage)
              ] else [])
            else throw "unsupported inventory: enabled container ${service} has no images"
          ) (builtins.attrNames containers)
        ) (builtins.attrNames hosts);
      in rows
    """.replace("REFERENCE", json.dumps(reference))
    process = subprocess.run(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "eval",
            "--json",
            "--no-write-lock-file",
            "--expr",
            expression,
        ],
        text=True,
        capture_output=True,
        timeout=300,
    )
    if process.returncode:
        raise WorkflowError("container inventory Nix evaluation failed: " + process.stderr[-1500:])
    if len(process.stdout) > 4 * 1024 * 1024:
        raise WorkflowError("container inventory exceeds the bounded output limit")
    try:
        rows = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError("container inventory evaluation did not return JSON") from exc
    if not isinstance(rows, list) or len(rows) > 10000:
        raise WorkflowError("container inventory component list is invalid")
    seen = set()
    for row in rows:
        _exact(
            row,
            {"host", "service", "component", "image", "current_tag", "change_route"},
            "evaluated component",
        )
        for field in ("host", "service", "component", "image", "current_tag"):
            _text(row[field], field, 500)
        route = _exact(
            row["change_route"], {"repository", "revision", "path"}, "image definition route"
        )
        for field in route:
            _text(route[field], field, 500)
        if route["path"].startswith("/") or ".." in Path(route["path"]).parts:
            raise WorkflowError("image definition route escapes its repository")
        identity = (row["host"], row["service"], row["component"])
        if identity in seen:
            raise WorkflowError("duplicate evaluated container component")
        seen.add(identity)
    return {
        "schema_version": 2,
        "repository": instance().repository,
        "source_commit": target,
        "components": rows,
    }


def _registry_tags(image: str) -> list[str]:
    _text(image, "registry image", 500)
    if not re.fullmatch(r"[a-z0-9][a-z0-9.:_/-]+", image) or ".." in image:
        raise WorkflowError("registry image is not an untagged repository name")
    process = subprocess.run(
        ["skopeo", "list-tags", "--no-creds", "--tls-verify=true", "docker://" + image],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if process.returncode:
        raise WorkflowError(
            "registry tag retrieval failed for " + image + ": " + process.stderr[-800:]
        )
    if len(process.stdout) > 4 * 1024 * 1024:
        raise WorkflowError("registry tag response exceeds limit")
    document = json.loads(process.stdout)
    tags = document.get("Tags")
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise WorkflowError("registry tags have an invalid schema")
    return tags


def _release_version(text: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]+(?:[.][0-9]+)+", text):
        raise WorkflowError("curated version capture must identify a numeric release")
    return tuple(int(part) for part in text.split("."))


def _github_release_tags(source: Mapping[str, Any]) -> list[str]:
    from forgejo_event_journal import NoRedirect

    base = source.get("api_base", "https://api.github.com").rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    repository = source.get("repo", "")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
    ):
        raise WorkflowError("release API configuration is invalid")
    tags = []
    for page in range(1, 21):
        request = urllib.request.Request(
            f"{base}/repos/{repository}/releases?per_page=100&page={page}",
            headers={
                "Accept": "application/json",
                "User-Agent": "hermes-nix-maintenance",
            },
        )
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            payload = response.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise WorkflowError("release response exceeds limit")
        releases = json.loads(payload)
        if not isinstance(releases, list) or any(not isinstance(row, dict) for row in releases):
            raise WorkflowError("release response has invalid schema")
        for release in releases:
            if not release.get("draft") and not release.get("prerelease"):
                tags.append(_text(release.get("tag_name"), "release tag", 128))
        if len(releases) < 100:
            return tags
    raise WorkflowError("release pagination bound exhausted")


def discover_container_updates(
    inventory: Mapping[str, Any], feeds: Mapping[str, Any]
) -> dict[str, Any]:
    components = feeds.get("components")
    if not isinstance(components, dict):
        raise WorkflowError("feed registry components are invalid")
    required = {row["service"] + "." + row["component"] for row in inventory["components"]}
    if required - set(components):
        raise WorkflowError(
            "feed coverage is incomplete: " + ", ".join(sorted(required - set(components)))
        )
    updates = []
    queried = {}
    for row in inventory["components"]:
        key = row["service"] + "." + row["component"]
        policy = components[key]
        source = policy.get("source", {})
        if source.get("status", "curated") != "curated":
            raise WorkflowError("source requires reviewed curation: " + key)
        if (
            source.get("type") == "manual"
            or policy.get("update_policy") in {"manual-only", "compatibility-only"}
            or policy.get("current_tag_semantics")
            in {"floating", "branch", "custom-local", "digest", "unsupported", "manual"}
        ):
            continue
        if policy.get("image") != row["image"]:
            raise WorkflowError("feed image differs from effective deployment image: " + key)
        if source.get("type") not in {"registry_tags", "github_releases"}:
            raise WorkflowError("unsupported release source requires curation: " + key)
        if source.get("image", row["image"]) != row["image"]:
            raise WorkflowError("feed source image differs from deployment: " + key)
        try:
            pattern = re.compile(_text(source.get("tag_regex"), "curated tag regex", 1000))
            excluded = re.compile(
                source.get(
                    "exclude_regex",
                    r"(?i)(dev|nightly|canary|unstable|snapshot|alpha|beta|rc|preview|edge)",
                )
            )
        except re.error as exc:
            raise WorkflowError("invalid curated tag expression: " + key) from exc
        current = pattern.fullmatch(row["current_tag"])
        if source["type"] == "github_releases":
            current = re.search(r"(?P<version>[0-9]+(?:[.][0-9]+)+)", row["current_tag"])
        if current is None or "version" not in current.groupdict():
            raise WorkflowError("current tag is outside the curated family: " + key)
        current_version = _release_version(current.group("version"))
        if row["image"] not in queried:
            queried[row["image"]] = _registry_tags(row["image"])
        available = queried[row["image"]]
        tags = available if source["type"] == "registry_tags" else _github_release_tags(source)
        candidates = []
        for tag in tags:
            match = pattern.fullmatch(tag)
            if match is not None and not excluded.search(tag):
                version = _release_version(match.group("version"))
                if version > current_version:
                    candidate = tag
                    if source["type"] == "github_releases":
                        start, end = current.span("version")
                        candidate = (
                            row["current_tag"][:start]
                            + match.group("version")
                            + row["current_tag"][end:]
                        )
                        if candidate not in available:
                            raise WorkflowError(
                                "release target is absent from registry tag family: " + key
                            )
                    candidates.append((version, candidate))
        if candidates:
            candidate = max(candidates)[1]
            updates.append({**row, "candidate_tag": candidate, "policy": policy})
    return {"inventory": dict(inventory), "feeds_sha256": digest(feeds), "updates": updates}


def validate_inventory(value: Any) -> dict[str, Any]:
    value = _exact(
        value,
        {"schema_version", "repository", "source_commit", "period", "candidates"},
        "container inventory",
    )
    if value["schema_version"] != 1 or not PERIOD_RE.fullmatch(str(value["period"])):
        raise WorkflowError("container inventory schema or period is invalid")
    validate_exact_target(value["repository"], value["source_commit"])
    candidates = value["candidates"]
    if not isinstance(candidates, list) or len(candidates) > 256:
        raise WorkflowError("container inventory candidates must be a bounded list")
    identities: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for row in candidates:
        row = _exact(
            row,
            {"service", "image", "current", "target", "hosts", "enabled", "source_status"},
            "container candidate",
        )
        service = _text(row["service"], "service", 128)
        image = _text(row["image"], "image", 300)
        current = _text(row["current"], "current", 128)
        target = _text(row["target"], "target", 128)
        if not SAFE_ID_RE.fullmatch(service):
            raise WorkflowError("container service identity is unsafe")
        identity = f"{service}:{image}"
        if identity in identities:
            raise WorkflowError("container inventory contains duplicate service/image identity")
        identities.add(identity)
        if type(row["enabled"]) is not bool or not row["enabled"]:
            raise WorkflowError("container inventory may only emit enabled services")
        hosts = row["hosts"]
        if not isinstance(hosts, list) or not hosts or len(hosts) != len(set(hosts)):
            raise WorkflowError("container hosts must be a non-empty unique list")
        for host in hosts:
            if not SAFE_ID_RE.fullmatch(_text(host, "host", 128)):
                raise WorkflowError("container host identity is unsafe")
        source_status = _text(row["source_status"], "source status", 100)
        if source_status not in {
            "registry-confirmed",
            "release-confirmed",
            "insufficient-evidence",
        }:
            raise WorkflowError("container source status is unsupported")
        normalized.append(
            {
                "service": service,
                "image": image,
                "current": current,
                "target": target,
                "hosts": sorted(hosts),
                "enabled": True,
                "source_status": source_status,
            }
        )
    return {
        **value,
        "candidates": sorted(normalized, key=lambda row: (row["service"], row["image"])),
    }


def validate_curation(value: Any, inventory: Mapping[str, Any]) -> dict[str, Any]:
    value = _exact(
        value,
        {
            "schema_version",
            "repository",
            "source_commit",
            "period",
            "inventory_sha256",
            "decisions",
        },
        "container curation",
    )
    if value["schema_version"] != 1 or value["period"] != inventory["period"]:
        raise WorkflowError("curation schema or period does not match inventory")
    validate_exact_target(value["repository"], value["source_commit"])
    if value["source_commit"] != inventory["source_commit"]:
        raise WorkflowError("curation source commit does not match inventory exact target")
    if value["inventory_sha256"] != digest(inventory):
        raise WorkflowError("curation is not bound to the exact inventory")
    decisions = value["decisions"]
    if not isinstance(decisions, list) or len(decisions) > len(inventory["candidates"]):
        raise WorkflowError("curation decisions must be a bounded list")
    allowed = {(row["service"], row["image"]) for row in inventory["candidates"]}
    seen: set[tuple[str, str]] = set()
    normalized: list[dict[str, Any]] = []
    for row in decisions:
        row = _exact(row, {"service", "image", "decision", "reason"}, "curation decision")
        identity = (_text(row["service"], "service", 128), _text(row["image"], "image", 300))
        if identity not in allowed or identity in seen:
            raise WorkflowError("curation decision is unknown or duplicated")
        seen.add(identity)
        decision = row["decision"]
        if decision not in {"include", "exclude", "needs-human"}:
            raise WorkflowError("curation decision is unsupported")
        normalized.append(
            {
                "service": identity[0],
                "image": identity[1],
                "decision": decision,
                "reason": _text(row["reason"], "curation reason", 500),
            }
        )
    return {
        **value,
        "decisions": sorted(normalized, key=lambda row: (row["service"], row["image"])),
    }


def build_issue_intent(
    inventory: Mapping[str, Any], curation: Mapping[str, Any]
) -> dict[str, Any] | None:
    selected = [
        row for row in curation["decisions"] if row["decision"] in {"include", "needs-human"}
    ]
    if not selected:
        return None
    payload = {
        "schema_version": 2,
        "kind": "container-monthly-update-issue",
        "repository": inventory["repository"],
        "source_commit": inventory["source_commit"],
        "period": inventory["period"],
        "inventory_sha256": digest(inventory),
        "curation_sha256": digest(curation),
        "logical_key": f"{instance().namespace}-container-update:{inventory['period']}",
        "decisions": selected,
        "inputs": json.loads(canonical_bytes({"inventory": inventory, "curation": curation})),
    }
    payload["intent_sha256"] = digest(payload)
    return payload


def build_container_intent(report: Mapping[str, Any]) -> dict[str, Any] | None:
    """Apply reviewed per-component policies without discarding host overrides."""
    source = report["inventory"]
    if source.get("schema_version") != 2:
        raise WorkflowError("container discovery schema is incompatible")
    candidates = []
    decisions = []
    details = {}
    for row in report["updates"]:
        route = row.get("change_route")
        if route is None:
            raise WorkflowError(
                "inventory predates repository ownership; refresh the deployment inventory "
                "before new curation, preserving any existing pending intent"
            )
        _exact(route, {"repository", "revision", "path"}, "image definition route")
        _sha(route["revision"], "image definition revision")
        path = _text(route["path"], "image definition path", 500)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(route["repository"])):
            raise WorkflowError("image definition repository is invalid")
        if path.startswith("/") or ".." in Path(path).parts:
            raise WorkflowError("image definition route escapes its repository")
        matching = [
            item
            for item in source["components"]
            if all(item[key] == row[key] for key in ("host", "service", "component"))
        ]
        if len(matching) != 1 or matching[0].get("change_route") != route:
            raise WorkflowError("candidate route differs from its deployment inventory")
        policy = row["policy"]["update_policy"]
        if policy not in {"separate-pr", "separate-pr-with-runbook"}:
            raise WorkflowError("container curation policy is unsupported")
        identity = f"{row['host']}.{row['service']}.{row['component']}"
        candidates.append(
            {
                "service": identity,
                "image": row["image"],
                "current": row["current_tag"],
                "target": row["candidate_tag"],
                "hosts": [row["host"]],
                "enabled": True,
                "source_status": "registry-confirmed",
            }
        )
        decisions.append(
            {
                "service": identity,
                "image": row["image"],
                "decision": "include",
                "reason": f"Apply reviewed {policy} policy in {row['change_route']['repository']} at {row['change_route']['path']} ({row['change_route']['revision']}); inventory remains bound to the deployment revision. Normal planning/research and review remain required.",
            }
        )
        details[(identity, row["image"])] = row
    inventory = validate_inventory(
        {
            "schema_version": 1,
            "repository": source["repository"],
            "source_commit": source["source_commit"],
            "period": report["period"],
            "candidates": candidates,
        }
    )
    curation = validate_curation(
        {
            "schema_version": 1,
            "repository": source["repository"],
            "source_commit": source["source_commit"],
            "period": report["period"],
            "inventory_sha256": digest(inventory),
            "decisions": decisions,
        },
        inventory,
    )
    intent = build_issue_intent(inventory, curation)
    if intent is not None:
        intent["decisions"] = [
            {**decision, "candidate": details[(decision["service"], decision["image"])]}
            for decision in intent["decisions"]
        ]
        intent["intent_sha256"] = digest(
            {key: value for key, value in intent.items() if key != "intent_sha256"}
        )
    return intent


def validate_inventory_generation(documents):
    if not isinstance(documents, dict) or set(documents) not in (
        {"health.json", "report.json"},
        {"health.json"},
    ):
        raise WorkflowError("invalid inventory generation documents")
    health = documents["health.json"]
    if not isinstance(health, dict) or health.get("status") not in {"ok", "error"}:
        raise WorkflowError("invalid inventory generation health")
    # A legacy failure without a source-bearing report is insufficient identity evidence.
    report = documents.get("report.json")
    if not isinstance(report, dict):
        raise WorkflowError("legacy inventory has no source identity evidence")
    build_container_intent(report)
    return documents


def validate_issue_intent(intent: Any) -> dict[str, Any]:
    version = intent.get("schema_version") if isinstance(intent, dict) else None
    intent = _exact(
        intent,
        {
            "schema_version",
            "kind",
            "repository",
            "source_commit",
            "period",
            "inventory_sha256",
            "curation_sha256",
            "logical_key",
            "decisions",
            "intent_sha256",
        }
        | ({"inputs"} if version == 2 else set()),
        "container issue intent",
    )
    if version not in {1, 2} or intent["kind"] != "container-monthly-update-issue":
        raise WorkflowError("container issue intent identity is incompatible")
    validate_exact_target(intent["repository"], intent["source_commit"])
    if not PERIOD_RE.fullmatch(str(intent["period"])):
        raise WorkflowError("container issue intent period is invalid")
    for field in ("inventory_sha256", "curation_sha256", "intent_sha256"):
        if not isinstance(intent[field], str) or not re.fullmatch(r"[0-9a-f]{64}", intent[field]):
            raise WorkflowError(f"{field} must be a lowercase SHA-256 digest")
    if intent["logical_key"] != f"{instance().namespace}-container-update:{intent['period']}":
        raise WorkflowError("container issue logical key is invalid")
    if not isinstance(intent["decisions"], list) or not intent["decisions"]:
        raise WorkflowError("container issue intent requires selected decisions")
    expected_digest = digest(
        {key: value for key, value in intent.items() if key != "intent_sha256"}
    )
    if intent["intent_sha256"] != expected_digest:
        raise WorkflowError("issue intent digest is invalid")
    if version == 2:
        inputs = _exact(intent["inputs"], {"inventory", "curation"}, "intent inputs")
        inventory = validate_inventory(inputs["inventory"])
        curation = validate_curation(inputs["curation"], inventory)
        if (
            digest(inventory) != intent["inventory_sha256"]
            or digest(curation) != intent["curation_sha256"]
            or inventory["source_commit"] != intent["source_commit"]
            or inventory["period"] != intent["period"]
        ):
            raise WorkflowError("intent inputs do not match their exact target and digests")
    return intent


def validate_issue_readback(
    proof: Any, intent: Mapping[str, Any], *, origin: str | None = None
) -> dict[str, Any]:
    proof = _exact(
        proof,
        {
            "repository",
            "source_commit",
            "intent_sha256",
            "target_kind",
            "target_number",
            "target_url",
            "readback_sha256",
        },
        "issue readback proof",
    )
    validate_exact_target(proof["repository"], proof["source_commit"])
    if (
        proof["source_commit"] != intent["source_commit"]
        or proof["intent_sha256"] != intent["intent_sha256"]
    ):
        raise WorkflowError("issue readback proof does not match the exact intent target")
    if (
        proof["target_kind"] != "issue"
        or type(proof["target_number"]) is not int
        or proof["target_number"] < 1
    ):
        raise WorkflowError("issue readback proof target identity is invalid")
    if not isinstance(proof["readback_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", proof["readback_sha256"]
    ):
        raise WorkflowError("issue readback proof digest is invalid")
    parsed = urllib.parse.urlsplit(_text(proof["target_url"], "target URL", 500))
    expected_origin = urllib.parse.urlsplit(origin or instance().forgejo_origin or "")
    expected_path = f"/{instance().repository}/issues/{proof['target_number']}"
    if (
        parsed.scheme != "https"
        or parsed.netloc != expected_origin.netloc
        or expected_origin.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != expected_path
    ):
        raise WorkflowError("issue readback proof URL is not an exact canonical issue target")
    return proof


def _validate_intent_state(value: Any, *, origin: str | None = None) -> dict[str, Any]:
    # Existing schema-1 generations were never an automatic publication grant.
    if isinstance(value, dict):
        value = {"publication_requested": False, **value}
    value = _exact(
        value,
        {
            "schema_version",
            "intent",
            "state",
            "attempts",
            "attempt_id",
            "error",
            "proof",
            "publication_requested",
        },
        "intent state",
    )
    if not isinstance(value["publication_requested"], bool):
        raise WorkflowError("intent publication request must be boolean")
    if value["schema_version"] != 1 or type(value["attempts"]) is not int or value["attempts"] < 0:
        raise WorkflowError("intent state schema or attempt count is invalid")
    intent = validate_issue_intent(value["intent"])
    state = value["state"]
    if state not in {"pending", "in-flight", "ambiguous", "transient-failed", "acknowledged"}:
        raise WorkflowError("intent state is unsupported")
    if value["attempt_id"] is not None:
        _text(value["attempt_id"], "attempt id", 200)
    if value["error"] is not None:
        _text(value["error"], "intent error", 300)
    invariant = {
        "pending": value["attempts"] == 0
        and value["attempt_id"] is None
        and value["error"] is None
        and value["proof"] is None,
        "in-flight": value["attempts"] >= 1
        and value["attempt_id"] is not None
        and value["error"] is None
        and value["proof"] is None,
        "ambiguous": value["attempts"] >= 1
        and value["attempt_id"] is not None
        and value["error"] is not None
        and value["proof"] is None,
        "transient-failed": value["attempts"] >= 1
        and value["attempt_id"] is not None
        and value["error"] is not None
        and value["proof"] is None,
        "acknowledged": value["attempts"] >= 1
        and value["attempt_id"] is not None
        and value["error"] is None
        and value["proof"] is not None,
    }[state]
    if not invariant:
        raise WorkflowError("intent state invariant is invalid")
    if state == "acknowledged":
        validate_issue_readback(value["proof"], intent, origin=origin)
    return value


class IntentLedger:
    """Crash-safe pending/attempt/ambiguous/ack state for one issue intent.

    The ledger never writes Forgejo. F11 consumes the intent and returns exact
    source-bound readback proof, which this ledger records before acknowledgement.
    """

    def __init__(self, root: Path, *, origin: str | None = None):
        self.origin = origin or instance().forgejo_origin
        if self.origin is None:
            raise WorkflowError("maintenance delivery requires an explicit Forgejo binding")
        instance().check_origin(self.origin, "forgejo")
        self.store = RepositoryStore(
            root.resolve(),
            protocol="system-admin-intents",
            schema_version=1,
            legacy_validator=lambda docs: _validate_intent_state(
                _exact(docs, {"intent.json"}, "legacy intents")["intent.json"], origin=self.origin
            ),
        )

    def _read(self) -> dict[str, Any] | None:
        try:
            selected = self.store.read()
        except StateAbsent:
            return None
        return _validate_intent_state(selected.documents["intent.json"], origin=self.origin)

    def pending_context(self) -> dict[str, Any] | None:
        current = self._read()
        if current is None or current["state"] not in {"pending", "transient-failed"}:
            return None
        return {
            "schema_version": 1,
            "action": "deliver-container-update-issue-intent-through-managed-forgejo-kanban",
            "intent": current["intent"],
            "attempts": current["attempts"],
        }

    @staticmethod
    def _initial(intent: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "intent": dict(intent),
            "state": "pending",
            "attempts": 0,
            "attempt_id": None,
            "error": None,
            "proof": None,
        }

    def stage(self, intent: Mapping[str, Any] | None) -> dict[str, Any]:
        if intent is None:
            return {"wakeAgent": False, "changed": False, "reason": "empty-curation"}
        intent = validate_issue_intent(intent)
        expected_digest = intent["intent_sha256"]
        current = self._read()
        if current is not None:
            if current["intent"]["intent_sha256"] == expected_digest:
                return {
                    "wakeAgent": False,
                    "changed": False,
                    "reason": current["state"],
                    "intent_sha256": expected_digest,
                }
            if current["state"] != "acknowledged":
                raise WorkflowError("a different issue intent is still pending or ambiguous")
        changed = False

        def publish(selected):
            nonlocal changed
            if selected is not None:
                actual = _validate_intent_state(
                    selected.documents["intent.json"], origin=self.origin
                )
                if actual["intent"]["intent_sha256"] == expected_digest:
                    return selected.documents
                if actual["state"] != "acknowledged":
                    raise WorkflowError("a different issue intent is still pending or ambiguous")
            changed = True
            return {"intent.json": self._initial(intent)}

        self.store.update(publish)
        return {
            "wakeAgent": False,
            "changed": changed,
            "reason": "pending-intent",
            "intent": intent,
        }

    def request_delivery(self) -> dict[str, Any]:
        current = self._read()
        if (
            current is None
            or current["state"] == "acknowledged"
            or current["publication_requested"]
        ):
            return {"wakeAgent": False, "changed": False, "reason": "no-new-request"}

        def request(selected):
            if selected is None:
                raise WorkflowError("intent disappeared before publication request")
            doc = _validate_intent_state(selected.documents["intent.json"], origin=self.origin)
            if doc["intent"]["intent_sha256"] != current["intent"]["intent_sha256"]:
                raise WorkflowError("selected intent changed before publication request")
            doc["publication_requested"] = True
            return {"intent.json": doc}

        self.store.update(request)
        return {"wakeAgent": False, "changed": True, "reason": "publication-requested"}

    def deliver(self, client: Any, *, require_request: bool = False) -> dict[str, Any]:
        with exclusive_lock(self.store.root / "delivery"):
            current = self._read()
            if current is None or current["state"] == "acknowledged":
                return {"wakeAgent": False, "changed": False, "reason": "no-pending-intent"}
            if require_request and not current["publication_requested"]:
                return {"wakeAgent": False, "changed": False, "reason": "not-requested"}
            if client.origin != self.origin:
                raise WorkflowError("issue writer origin differs from the ledger target")
            intent = current["intent"]
            identity = intent["intent_sha256"]
            writable = current["state"] in {"pending", "transient-failed"}

            def before_write() -> None:
                self.begin(identity, uuid.uuid4().hex)

            try:
                proof = client.deliver_issue_intent(
                    intent, allow_write=writable, before_write=before_write
                )
            except Exception:
                selected = self._read()
                if selected is not None and selected["state"] == "in-flight":
                    self.lost_response(
                        identity,
                        "issue write/readback failed after durable attempt; reconcile before replay",
                    )
                raise
            readback_state = self._read()
            if readback_state is None:
                raise WorkflowError("intent disappeared during external readback")
            if readback_state["state"] in {"pending", "transient-failed"}:
                # Readback may find an already-published exact report before any write.
                self.begin(identity, uuid.uuid4().hex)
            self.acknowledge(identity, proof)
            return {"wakeAgent": False, "changed": True, "reason": "acknowledged", "proof": proof}

    def begin(self, intent_sha256: str, attempt_id: str) -> dict[str, Any]:
        _text(attempt_id, "attempt id", 200)

        def transform(current: Any) -> Mapping[str, Any]:
            if current is None:
                raise WorkflowError("cannot begin absent intent")
            doc = _validate_intent_state(current.documents["intent.json"], origin=self.origin)
            if doc["intent"]["intent_sha256"] != intent_sha256:
                raise WorkflowError("attempt does not match selected intent")
            if doc["state"] not in {"pending", "transient-failed"}:
                raise WorkflowError("only pending or transient-failed intent may begin")
            updated = dict(doc)
            updated.update(
                state="in-flight",
                attempts=doc["attempts"] + 1,
                attempt_id=attempt_id,
                error=None,
                proof=None,
            )
            return {"intent.json": updated}

        return self.store.update(transform).documents["intent.json"]

    def lost_response(self, intent_sha256: str, error: str) -> dict[str, Any]:
        _text(error, "lost response", 300)
        return self._transition(
            intent_sha256, {"in-flight"}, state="ambiguous", error=error, proof=None
        )

    def transient_failure(self, intent_sha256: str, error: str) -> dict[str, Any]:
        _text(error, "transient failure", 300)
        return self._transition(
            intent_sha256, {"in-flight"}, state="transient-failed", error=error, proof=None
        )

    def acknowledge(self, intent_sha256: str, proof: Mapping[str, Any]) -> dict[str, Any]:
        current = self._read()
        if current is None or current["intent"]["intent_sha256"] != intent_sha256:
            raise WorkflowError("acknowledgement does not match selected intent")
        proof = validate_issue_readback(proof, current["intent"], origin=self.origin)
        return self._transition(
            intent_sha256,
            {"in-flight", "ambiguous"},
            state="acknowledged",
            error=None,
            proof=proof,
        )

    def _transition(
        self,
        intent_sha256: str,
        allowed: set[str],
        *,
        state: str,
        error: str | None,
        proof: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        def transform(current: Any) -> Mapping[str, Any]:
            if current is None:
                raise WorkflowError("intent state is absent")
            doc = _validate_intent_state(current.documents["intent.json"], origin=self.origin)
            if doc["intent"]["intent_sha256"] != intent_sha256 or doc["state"] not in allowed:
                raise WorkflowError("intent transition does not match selected state")
            updated = dict(doc)
            updated.update(state=state, error=error, proof=proof)
            return {"intent.json": updated}

        return self.store.update(transform).documents["intent.json"]


def container_pending_gate(ledger: IntentLedger) -> dict[str, Any]:
    context = ledger.pending_context()
    return (
        {"wakeAgent": False, "reason": "no-pending-intent"}
        if context is None
        else {"wakeAgent": True, "context": context}
    )


class DocsAudit:
    """One successful commit; pending ranges live only in native Kanban tasks."""

    def __init__(self, root: Path, repo: Path):
        self.repo = repo.resolve()
        self.root = root.resolve()
        self.store = RepositoryStore(
            root.resolve(),
            protocol="nix-config-docs-audit",
            schema_version=1,
            legacy_validator=lambda docs: self.git(
                "merge-base",
                "--is-ancestor",
                _sha(
                    _exact(
                        _exact(docs, {"successful.json"}, "legacy audit")["successful.json"],
                        {"commit"},
                        "audit marker",
                    )["commit"]
                ),
                "refs/heads/" + instance().default_branch,
            ),
        )

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        if result.returncode:
            raise WorkflowError("documentation audit Git history is unavailable or incompatible")
        return result.stdout.strip()

    def successful_commit(self) -> str | None:
        try:
            doc = self.store.read().documents["successful.json"]
        except StateAbsent:
            return None
        return _sha(_exact(doc, {"commit"}, "successful audit marker")["commit"])

    def reconcile(self, kanban: Any) -> dict[str, Any]:
        self.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.root.with_name(self.root.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._reconcile(kanban)

    def identity(self) -> dict[str, Any]:
        cfg = instance()
        return {
            "instance": cfg.identity,
            "repository": cfg.repository,
            "origin": cfg.forgejo_origin,
            "branch": cfg.default_branch,
            "namespace": cfg.namespace,
            "board": cfg.board,
            "profile": cfg.implementer,
        }

    def task_key(self, base: str | None) -> str:
        identity = json.dumps(self.identity(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return instance().namespace + "-docs-audit:v2:" + digest + ":" + (base or "initial")

    def task_scope(self, row: Mapping[str, Any], base: str | None) -> dict[str, Any]:
        try:
            fixed = json.loads(str(row.get("body") or "").splitlines()[0])["docs_audit"]
        except (ValueError, IndexError, KeyError, TypeError) as exc:
            raise WorkflowError("documentation audit opening contract is malformed") from exc
        if (
            not isinstance(fixed, dict)
            or set(fixed) != {"identity", "key", "base", "target"}
            or fixed["identity"] != self.identity()
            or fixed["base"] != base
            or row.get("assignee") != instance().implementer
            or row.get("created_by") != "nix-maintenance-protocol"
            or fixed["key"] != self.task_key(base)
            # Pinned Hermes omits the database key from CLI JSON. Validate the
            # opening key passed to native create; if exposed, cross-check it.
            or ("idempotency_key" in row and row["idempotency_key"] != fixed["key"])
        ):
            raise WorkflowError("documentation audit native identity/owner does not match instance")
        _sha(fixed["target"])
        return fixed

    def _reconcile(self, kanban: Any) -> dict[str, Any]:
        scope = self.scope()
        if scope["base"] == scope["target"]:
            return {"status": "unchanged", "wakeAgent": False}
        key = self.task_key(scope["base"])
        legacy_key = instance().namespace + "-docs-audit:" + (scope["base"] or "initial")
        rows = kanban._run(["list", "--archived", "--json"], expect_json=True)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise WorkflowError("documentation audit listing is malformed")
        matches = []
        for row in rows:
            # A historical key is not sufficient source evidence. Never silently
            # adopt it or create a second card alongside unresolved legacy work.
            if row.get("idempotency_key") == legacy_key:
                raise WorkflowError(
                    "legacy documentation audit requires verified identity adoption"
                )
            body = str(row.get("body") or "")
            if row.get("idempotency_key") == key:
                self.task_scope(row, scope["base"])
                matches.append(row)
                continue
            if not body.startswith('{"docs_audit":'):
                continue
            try:
                fixed = json.loads(body.splitlines()[0])["docs_audit"]
            except (ValueError, KeyError, TypeError) as exc:
                raise WorkflowError("documentation audit opening contract is malformed") from exc
            if not isinstance(fixed, dict):
                raise WorkflowError("documentation audit opening contract is malformed")
            if fixed.get("base") != scope["base"]:
                continue
            if "identity" not in fixed:
                raise WorkflowError(
                    "unbound documentation audit requires verified identity adoption"
                )
            if fixed["identity"] == self.identity() or fixed.get("key") == key:
                self.task_scope(row, scope["base"])
                matches.append(row)
        if len(matches) > 1:
            raise WorkflowError("ambiguous documentation audit lineage")
        if matches:
            task = kanban.show(matches[0]["id"])
            fixed = self.task_scope(task["task"], scope["base"])
            if task["task"]["status"] != "done":
                return {"status": "pending", "task_id": task["task"]["id"], "wakeAgent": False}
            # The immutable target lives in the native opening post, not another
            # pending-work database. Only a successful structured handoff ACKs it.
            runs = sorted(task.get("runs", []), key=lambda r: r["id"])
            latest = runs[-1] if runs else {}
            proof = (
                (latest.get("metadata") or {}).get("docs_audit")
                if latest.get("status") == "done"
                else None
            )
            if (
                not isinstance(proof, dict)
                or proof.get("identity") != fixed["identity"]
                or proof.get("base") != fixed["base"]
                or proof.get("target") != fixed["target"]
                or latest.get("profile") != instance().implementer
                or proof.get("key") != fixed["key"]
                or proof.get("task_id") != task["task"]["id"]
            ):
                raise WorkflowError("completed documentation audit lacks exact-range handoff proof")
            if not isinstance(proof.get("findings"), list) or proof.get("result") != "success":
                raise WorkflowError("documentation audit result/follow-up handoff is missing")
            for finding in proof["findings"]:
                if not isinstance(finding, dict) or not re.fullmatch(
                    r"t_[0-9a-f]+", str(finding.get("task_id", ""))
                ):
                    raise WorkflowError("each audit finding requires a durable follow-up card")
                followup = kanban.show(finding["task_id"])
                if not followup["task"].get("body"):
                    raise WorkflowError("audit follow-up lacks its durable specification")
            target = _sha(fixed["target"])
            self.git("merge-base", "--is-ancestor", target, scope["target"])
            self.store.publish({"successful.json": {"commit": target}})
            return self._reconcile(kanban)
        if not scope["paths"]:
            self.store.publish({"successful.json": {"commit": scope["target"]}})
            return {"status": "no-relevant-change", "wakeAgent": False}
        header = {
            "docs_audit": {
                "identity": self.identity(),
                "key": key,
                "base": scope["base"],
                "target": scope["target"],
            }
        }
        body = (
            json.dumps(header, sort_keys=True)
            + "\n"
            + (
                f"Semantic code-to-documentation audit of {instance().repository}. "
                "Load the managed nix-maintenance-protocol skill. "
                "Read the repository at the FIXED target commit above, never moving HEAD. "
                "First run: audit the complete relevant tree. Otherwise use git diff/log base..target, "
                "including code-only changes and affected documentation. No merge, deployment or cleanup. "
                "Record results and create durable follow-up cards for every finding before completion. "
                "Copy the exact identity/key/base/target from the opening contract into "
                "metadata.docs_audit={identity,key,base,target,task_id,result,findings:[{task_id}]}; "
                "task_id must be this native task; result must be success; explain coverage in the completion summary. "
                "findings=[] is valid only after a successful audit. "
                "Failures/blockers leave this card pending; do not complete with partial coverage.\n"
                f"Read-only source cache: {self.repo}\n"
                "Changed paths:\n" + "\n".join(scope["paths"]) + "\nGit log:\n" + scope["log"]
            )
        )
        task = kanban._run(
            [
                "create",
                f"Audit {instance().repository} documentation against code",
                "--body",
                body,
                "--assignee",
                instance().implementer,
                "--workspace",
                "scratch",
                "--created-by",
                "nix-maintenance-protocol",
                "--idempotency-key",
                key,
                "--skill",
                "nix-maintenance-protocol",
                "--json",
            ],
            expect_json=True,
        )
        observed = kanban.show(task["id"])
        self.task_scope(observed["task"], scope["base"])
        if observed["task"].get("body") != body:
            raise WorkflowError("documentation task readback mismatch")
        return {"status": "pending", "task_id": task["id"], "wakeAgent": False}

    def scope(self) -> dict[str, Any]:
        if self.git("rev-parse", "--is-shallow-repository") != "false":
            raise WorkflowError("documentation audit requires complete Git history")
        target = _sha(self.git("rev-parse", f"refs/heads/{instance().default_branch}^{{commit}}"))
        base = self.successful_commit()
        if base:
            self.git("merge-base", "--is-ancestor", base, target)
            paths = self.git("diff", "--name-only", base, target, "--").splitlines()
            log = self.git("log", "--format=%H %s", f"{base}..{target}", "--")
        else:
            paths = self.git("ls-tree", "-r", "--name-only", target).splitlines()
            log = self.git("log", "-1", "--format=%H %s", target, "--")
        # A code change can invalidate docs without touching a documentation path.
        # Only unambiguously non-semantic editor/ignore files are excluded.
        paths = [p for p in paths if Path(p).name not in {".gitignore", ".editorconfig"}]
        return {"base": base, "target": target, "paths": paths, "log": log}


def repository_source(registry: Path) -> Path:
    from hermes_repository_sync import _checkout_state, load_job

    if instance().repository_job is None:
        raise WorkflowError("maintenance requires an explicit repository registry selection")
    job = load_job(registry, instance().repository_job, False)
    if (
        job.repository != instance().repository
        or job.ref != "refs/heads/" + instance().default_branch
    ):
        raise WorkflowError("source differs from the configured repository/default branch")
    _checkout_state(job, job.destination)
    return job.destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    instance_argument(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("container-refresh")
    refresh.add_argument("--registry", type=Path, required=True)
    refresh.add_argument("--feeds", type=Path, required=True)
    refresh.add_argument("--state-dir", type=Path, required=True)
    refresh.add_argument("--period", default=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"))
    apply = sub.add_parser("container-curation-apply")
    apply.add_argument("--snapshot-state-dir", type=Path, required=True)
    apply.add_argument("--state-dir", type=Path, required=True)
    deliver = sub.add_parser("container-deliver")
    deliver.add_argument("--state-dir", type=Path, required=True)
    deliver.add_argument(
        "--endpoint-file",
        type=Path,
        default=Path("/run/hermes-credentials/services/forgejo/endpoint"),
    )
    deliver.add_argument(
        "--credential-file",
        type=Path,
        default=Path("/run/hermes-credentials/services/forgejo/credential"),
    )
    deliver.add_argument("--endpoint")
    deliver.add_argument("--require-request", action="store_true")
    request = sub.add_parser("container-request-delivery")
    request.add_argument("--state-dir", type=Path, required=True)
    plan = sub.add_parser("container-plan")
    plan.add_argument("--inventory", type=Path, required=True)
    plan.add_argument("--curation", type=Path, required=True)
    plan.add_argument("--state-dir", type=Path, required=True)
    inventory = sub.add_parser("container-validate-inventory")
    inventory.add_argument("--inventory", type=Path, required=True)
    pending = sub.add_parser("container-pending")
    pending.add_argument("--state-dir", type=Path, required=True)
    docs = sub.add_parser("docs-gate")
    docs.add_argument("--registry", type=Path, required=True)
    docs.add_argument("--hermes", type=Path, required=True)
    docs.add_argument("--board", required=True)
    docs.add_argument("--state-dir", type=Path, required=True)

    begin = sub.add_parser("container-begin")
    begin.add_argument("--state-dir", type=Path, required=True)
    begin.add_argument("--intent-sha256", required=True)
    begin.add_argument("--attempt-id", required=True)

    lost = sub.add_parser("container-lost-response")
    lost.add_argument("--state-dir", type=Path, required=True)
    lost.add_argument("--intent-sha256", required=True)
    lost.add_argument("--error", required=True)

    failed = sub.add_parser("container-transient-failure")
    failed.add_argument("--state-dir", type=Path, required=True)
    failed.add_argument("--intent-sha256", required=True)
    failed.add_argument("--error", required=True)

    acknowledge = sub.add_parser("container-acknowledge")
    acknowledge.add_argument("--state-dir", type=Path, required=True)
    acknowledge.add_argument("--intent-sha256", required=True)
    acknowledge.add_argument("--proof", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    activate_instance(args.instance_config)
    try:
        if args.command == "container-refresh":
            store = RepositoryStore(
                args.state_dir.resolve(),
                protocol="container-inventory",
                schema_version=1,
                legacy_validator=validate_inventory_generation,
            )
            refresh_result: dict[str, Any] = {"wakeAgent": False, "changed": False}
            failures = []

            def refresh_snapshot(current):
                try:
                    if not PERIOD_RE.fullmatch(args.period):
                        raise WorkflowError("container reporting period is invalid")
                    repo = repository_source(args.registry)
                    target = _sha(
                        subprocess.check_output(
                            [
                                "git",
                                "-C",
                                str(repo),
                                "rev-parse",
                                f"refs/heads/{instance().default_branch}^{{commit}}",
                            ],
                            text=True,
                        ).strip()
                    )
                    report = discover_container_updates(
                        refresh_container_inventory(repo, target), load_json(args.feeds)
                    )
                    report["period"] = args.period
                    documents = {"report.json": report, "health.json": {"status": "ok"}}
                    refresh_result["changed"] = current is None or current.documents != documents
                    refresh_result["updates"] = len(report["updates"])
                    return documents
                except Exception as exc:
                    failures.append(exc)
                    documents = dict(current.documents) if current else {}
                    documents["health.json"] = {"status": "error", "reason": type(exc).__name__}
                    return documents

            store.update(refresh_snapshot)
            if failures:
                raise WorkflowError(
                    "container refresh failed; previous data retained but unavailable for curation"
                ) from failures[0]
            result = refresh_result
        elif args.command == "container-validate-inventory":
            validate_inventory(load_json(args.inventory))
            result = None
        elif args.command == "container-request-delivery":
            result = IntentLedger(args.state_dir).request_delivery()
        elif args.command == "container-deliver":
            from forgejo_kanban_workflow import ForgejoClient, canonical_origin

            ledger = IntentLedger(args.state_dir)
            try:
                ledger.store.read()
            except StateAbsent:
                result = {"wakeAgent": False, "changed": False, "reason": "no-pending-intent"}
            else:
                ledger.origin = canonical_origin(
                    args.endpoint or args.endpoint_file.read_text().strip()
                )
                current = ledger._read()
                if current is None:
                    raise WorkflowError("intent disappeared during delivery")
                if current["state"] == "acknowledged" or (
                    args.require_request and not current["publication_requested"]
                ):
                    result = {"wakeAgent": False, "changed": False, "reason": "no-pending-intent"}
                else:
                    result = ledger.deliver(
                        ForgejoClient(ledger.origin, args.credential_file),
                        require_request=args.require_request,
                    )
        elif args.command == "container-curation-apply":
            selected = RepositoryStore(
                args.snapshot_state_dir,
                protocol="container-inventory",
                schema_version=1,
                legacy_validator=validate_inventory_generation,
            ).read()
            if selected.documents["health.json"].get("status") != "ok":
                raise WorkflowError("latest inventory refresh failed; refusing stale curation")
            result = IntentLedger(args.state_dir).stage(
                build_container_intent(selected.documents["report.json"])
            )
        elif args.command == "container-plan":
            inventory = validate_inventory(load_json(args.inventory))
            curation = validate_curation(load_json(args.curation), inventory)
            result = IntentLedger(args.state_dir).stage(build_issue_intent(inventory, curation))
        elif args.command == "container-pending":
            result = container_pending_gate(IntentLedger(args.state_dir))
        elif args.command == "docs-gate":
            from forgejo_kanban_workflow import NativeKanban

            home = Path(os.environ["HERMES_HOME"])
            repo = repository_source(args.registry)
            result = DocsAudit(args.state_dir, repo).reconcile(
                NativeKanban(args.hermes, args.board, home)
            )
        elif args.command == "container-begin":
            result = IntentLedger(args.state_dir).begin(args.intent_sha256, args.attempt_id)
        elif args.command == "container-lost-response":
            result = IntentLedger(args.state_dir).lost_response(args.intent_sha256, args.error)
        elif args.command == "container-transient-failure":
            result = IntentLedger(args.state_dir).transient_failure(args.intent_sha256, args.error)
        else:
            result = IntentLedger(args.state_dir).acknowledge(
                args.intent_sha256, load_json(args.proof)
            )
        if result is not None:
            print(json.dumps(result, sort_keys=True))
        return 0
    except (WorkflowError, ProtocolError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
