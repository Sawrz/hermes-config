"""Prepare a managed-source change request. No credentials, network, or writes."""

import argparse
import hashlib
import json
from pathlib import Path
import re


def plan(manifest, skill, request, known_task=None, current_task=None, resource_kind="skills"):
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 2
        or not isinstance(manifest.get("profile"), str)
        or not manifest["profile"]
        or not isinstance(manifest.get("profile_declarations"), list)
        or not all(isinstance(v, dict) for v in manifest["profile_declarations"])
        or not isinstance(manifest.get("skills"), dict)
        or not isinstance(manifest.get("native_jobs"), dict)
        or resource_kind not in ("skills", "native_jobs")
    ):
        raise ValueError("Missing/malformed managed-skill inventory; do not edit or route")
    for name, entry in manifest["skills"].items():
        if (
            not re.fullmatch(r"[a-zA-Z0-9_-]+", name)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("source"), dict)
            or entry["source"].get("kind") not in ("inline", "external", "repository-directory")
            or not isinstance(entry.get("declarations"), list)
            or not all(isinstance(v, dict) for v in entry["declarations"])
            or not isinstance(entry.get("category"), str)
            or not isinstance(entry.get("provider"), str)
        ):
            raise ValueError("Malformed skill entry; do not assume unlisted skills are local")
    for name, entry in manifest["native_jobs"].items():
        if (
            not re.fullmatch(r"[a-zA-Z0-9._-]+", name)
            or not isinstance(entry, dict)
            or entry.get("source") != {"kind": "native-job", "path": None}
            or not isinstance(entry.get("declarations"), list)
            or not all(isinstance(v, dict) for v in entry["declarations"])
            or not isinstance(entry.get("provider"), str)
            or not entry["provider"]
        ):
            raise ValueError("Malformed managed job entry; do not assume local ownership")
    if skill not in manifest[resource_kind]:
        return {
            "action": "inspect_provenance",
            "resource": f"{resource_kind}/{skill}",
            "note": "Unlisted, not permission to edit: confirm native provenance, contract and user authority",
        }
    entry = manifest[resource_kind][skill]
    change_target = (
        request.get("change_target", "implementation")
        if isinstance(request, dict)
        else "implementation"
    )
    if change_target not in ("implementation", "selection"):
        raise ValueError("Change target must be implementation or selection")
    origins = (
        manifest["profile_declarations"]
        if change_target == "selection"
        else [entry["source"]]
        if entry["source"].get("repository")
        else entry["declarations"]
    )
    routes = {
        origin["repository"]: origin["route"]
        for origin in origins
        if origin.get("repository") and origin.get("route")
    }
    requested_repository = request.get("repository") if isinstance(request, dict) else None
    if requested_repository is not None:
        if requested_repository not in routes:
            raise ValueError("Requested repository does not own this managed source")
        route = routes[requested_repository]
    elif len(routes) == 1:
        route = next(iter(routes.values()))
    else:
        raise ValueError("Managed resource needs an explicit, actionable repository owner")
    if not isinstance(route, dict) or any(
        not isinstance(route.get(k), str) or not route[k].strip()
        for k in ("board", "assignee", "repository")
    ):
        raise ValueError("Missing/malformed change route; do not edit or route")
    mode = route.get("mode", "kanban")
    if mode not in ("kanban", "manual"):
        raise ValueError("Unknown route mode; do not edit or route")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+", route["repository"]) or any(
        not re.fullmatch(r"[a-zA-Z0-9_-]+", route[k]) for k in ("board", "assignee")
    ):
        raise ValueError("Invalid route identity or repository owner/name; URLs are not accepted")
    if (
        not isinstance(request, dict)
        or any(
            not isinstance(request.get(k), str) or not request[k].strip()
            for k in ("change", "reason")
        )
        or any(
            not isinstance(request.get(k), list)
            or not request[k]
            or not all(isinstance(v, str) and v.strip() for v in request[k])
            for k in ("evidence", "acceptance")
        )
    ):
        raise ValueError("Request needs change, reason, nonempty evidence and acceptance")
    # Adding the explicit default must not invalidate existing Kanban request keys.
    identity_route = {k: v for k, v in route.items() if k != "mode"} if mode == "kanban" else route
    identity_parts = [identity_route, resource_kind, skill, request["change"].strip()]
    if change_target == "selection":
        identity_parts += [change_target, manifest["profile"]]
    identity = json.dumps(identity_parts, sort_keys=True)
    key = "managed-resource-" + hashlib.sha256(identity.encode()).hexdigest()
    if mode == "manual" and (known_task is not None or current_task is not None):
        raise ValueError("Manual handoff does not accept Kanban task references")
    if known_task is not None:
        task = known_task.get("task", {})
        if (
            task.get("assignee") != route["assignee"]
            or f"Managed-resource change key: {key}" not in task.get("body", "").splitlines()
            or not isinstance(task.get("id"), str)
            or task.get("status") in ("done", "archived")
        ):
            raise ValueError(
                "Known task is not an active matching request; inspect native task before routing"
            )
        return {
            "action": "implement" if task["id"] == current_task else "reuse",
            "task_id": task["id"],
            "board": route["board"],
            "idempotency_key": key,
        }
    if current_task:
        raise ValueError("Read current task with kanban_show before considering another task")
    body = "\n".join(
        [
            f"Managed-resource change key: {key}",
            f"Repository: {route['repository']}",
            f"Resource: {resource_kind}/{skill}",
            f"Change target: {change_target}",
            "Selected source ownership: " + json.dumps(origins, sort_keys=True),
            "Source/declaration: " + json.dumps(entry, sort_keys=True),
            f"Requested change: {request['change']}",
            f"Reason: {request['reason']}",
            "Evidence: " + json.dumps(request["evidence"]),
            "Acceptance: " + json.dumps(request["acceptance"]),
            "Implement this request in the canonical repository and publish a PR under its change policy.",
            "Use the owner's existing repository checkout/access; do not provision access for the requester.",
            "Do not route this same request to yourself again. Never edit or shadow delivered resources.",
            "Respect declared source ownership versus explicitly delegated runtime preferences/state and user authority.",
        ]
    )
    if mode == "manual":
        return {
            "action": "handoff",
            "repository": route["repository"],
            "assignee": route["assignee"],
            "idempotency_key": key,
            "body": body,
            "note": "Return this request to the current caller; no task has been created or dispatched.",
        }
    return {
        "action": "create",
        "arguments": {
            "title": f"Managed {resource_kind}/{skill}: {request['change'][:100]}",
            "board": route["board"],
            "assignee": route["assignee"],
            "body": body,
            "idempotency_key": key,
            "workspace_kind": "scratch",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    resource = parser.add_mutually_exclusive_group(required=True)
    resource.add_argument("--skill")
    resource.add_argument("--job", help="Managed native job launcher/contract, not private cadence")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument(
        "--known-task", type=Path, help="Fresh native kanban_show readback from configured board"
    )
    parser.add_argument("--current-task", help="Currently assigned task id")
    args = parser.parse_args()
    try:
        result = plan(
            json.loads(args.manifest.read_text()),
            args.skill or args.job,
            json.loads(args.request.read_text()),
            json.loads(args.known_task.read_text()) if args.known_task else None,
            args.current_task,
            "skills" if args.skill else "native_jobs",
        )
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"Cannot safely prepare request: {error}\n")
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
