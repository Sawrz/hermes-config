"""Explicit legacy identity adoption within the existing projection transaction."""

import re
from hermes_repository_instance import instance

from kanban_vikunja_projection import InputError, marker_re, TASK_RE, digest

ARCHIVE = "\n\n## Preserved legacy record\n\n"


def setting(body, name):
    rows = [line[len(name) + 2 :] for line in body.splitlines() if line.startswith(name + ": ")]
    if len(rows) != 1 or not rows[0]:
        raise InputError(f"legacy adoption requires exactly one {name}")
    return rows[0]


def legacy_identity(task, row, desired):
    body, description = task["body"], str(row.get("description") or "")
    if (
        setting(body, "PR workflow") != "native-v1"
        or task.get("assignee") != instance().implementer
    ):
        raise InputError(
            "legacy adoption requires a native PR anchor owned by its configured implementer"
        )
    if (
        setting(body, "Vikunja legacy task") != str(row.get("id"))
        or row.get("project_id") != desired["project_id"]
    ):
        raise InputError("legacy adoption targets a different task/project")
    if (row.get("created_by") or {}).get("username") != setting(body, "Vikunja legacy creator"):
        raise InputError("legacy task creator differs from explicit adoption intent")
    root = setting(body, "Vikunja legacy root")
    if not TASK_RE.fullmatch(root):
        raise InputError("legacy native root is invalid")
    roots = re.findall(r"^- Kanban lineage root: `([^`]+)`$", description, re.M)
    if roots != [root]:
        raise InputError("legacy task native lineage differs from explicit intent")
    key = desired["automation_key"]
    markers = [
        m.group(1) for line in description.splitlines() if (m := marker_re().fullmatch(line))
    ]
    if markers != [key] or "Hermes Kanban:" in description:
        raise InputError("legacy marker is duplicated, different or already claimed")
    url = setting(body, "Forgejo authority")
    number = re.fullmatch(re.escape(instance().namespace) + r"-pr-([1-9][0-9]*)-merge", key)
    if not number or not url.endswith(f"/{instance().repository}/pulls/" + number[1]):
        raise InputError("legacy adoption lacks canonical PR authority")
    links = re.findall(
        rf"\[[^\]]*\]\((https://[^\s)]+/{re.escape(instance().repository)}/pulls/[0-9]+)\)",
        description,
    )
    if set(links) != {url}:
        raise InputError("legacy task belongs to a different or ambiguous PR")
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "creator": row["created_by"]["username"],
        "description_digest": digest(description),
    }


def preserve_record(desired, row, *, adopting=False):
    description = str(row.get("description") or "")
    if adopting:
        record = "\n".join(
            line for line in description.splitlines() if not marker_re().fullmatch(line)
        ).rstrip()
    elif ARCHIVE in description:
        # Our record is before the one canonical reciprocal footer.
        record = description.split(ARCHIVE, 1)[1].split("\n\n---\n\nAction type:", 1)[0]
        record = record.split("\n\n* * *\n\nAction type:", 1)[0].rstrip()
    else:
        return desired
    result = dict(desired)
    text = result["description"]
    footer = text.index("\n\n---\n\nAction type:")
    result["description"] = text[:footer] + ARCHIVE + record + text[footer:]
    result["digest"] = digest({k: v for k, v in result.items() if k != "digest"})
    return result


def validate_pending_legacy(row, proof):
    if (
        row.get("id") != proof["id"]
        or row.get("project_id") != proof["project_id"]
        or (row.get("created_by") or {}).get("username") != proof["creator"]
        or digest(str(row.get("description") or "")) != proof["description_digest"]
    ):
        raise InputError("legacy task changed after durable adoption intent")
