from __future__ import annotations

import copy
import email.message
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import kanban_vikunja_projection as projection
from kanban_vikunja_projection import (
    InputError,
    NativeKanban,
    ProjectionState,
    ProtocolError,
    VikunjaClient,
    action_key,
    canonical_origin,
    comment_event_key,
    completion_event_key,
    parse_link,
    reconcile_kanban_to_vikunja,
    reconcile_vikunja_to_kanban,
    stable_marker,
    validate_config,
    validate_intake_ownership,
)

BOARD = "homelab-devops"
TASK_ID = "t_deadbeef"
PROJECT = 110
AUTO_REF = "nix-config-pr-200-merge"


def config() -> dict:
    return {
        "schema_version": 1,
        "board": BOARD,
        "project_owner": "sandro",
        "edges": {
            "kanban_to_vikunja": {
                "enabled": True,
                "selector": {
                    "include": [PROJECT],
                    "exclude": None,
                    "include_future_projects": False,
                },
                "default_policy": "linkedOnly",
                "project_policies": {"110": {"mode": "linkedOnly", "task_allowlist": []}},
            },
            "vikunja_to_kanban": {
                "enabled": True,
                "selector": {
                    "include": [PROJECT],
                    "exclude": None,
                    "include_future_projects": False,
                },
                "default_policy": "linkedOnly",
                "project_policies": {"110": {"mode": "linkedOnly", "task_allowlist": []}},
                "wake_on_human_comment": True,
            },
        },
    }


def kanban_task(*, status: str = "blocked", action: str = "Merge") -> dict:
    return {
        "id": TASK_ID,
        "title": "Merge nix-config PR 200 after review",
        "body": (
            "Host: alakazam\n\n"
            "Current action: Review and merge only after exact-head approval.\n\n"
            f"Human action: {action}\n"
            "Vikunja project: 110\n"
            "Vikunja assignee: sandro\n"
            "Vikunja done: false\n"
            f"Vikunja automation reference: {'nix-config-issue-200-questions' if action == 'Questions' else AUTO_REF}\n"
        ),
        "status": status,
        "assignee": "system-admin",
    }


def vikunja_task(*, done: bool = False, task_id: int = 71, description: str | None = None) -> dict:
    key = action_key(AUTO_REF, "Merge")
    description = description or (
        "Current action: Review and merge.\n\n"
        f"Hermes Kanban: {BOARD}/{TASK_ID}\n\n"
        f"{stable_marker(key)}"
    )
    return {
        "id": task_id,
        "project_id": PROJECT,
        "title": "Merge nix-config PR 200 after review",
        "description": description,
        "done": done,
        "assignees": [{"username": "sandro", "id": 1}],
        "url": f"https://tasks.example.test/tasks/{task_id}",
        "updated": "2026-08-28T08:00:00Z",
    }


class FakeKanban:
    board = BOARD

    def __init__(self, tasks: list[dict] | None = None) -> None:
        self.tasks = {row["id"]: copy.deepcopy(row) for row in (tasks or [kanban_task()])}
        self.comments: dict[str, list[dict]] = {row: [] for row in self.tasks}
        self.comment_calls: list[tuple[str, str]] = []
        self.unblock_calls: list[str] = []

    def list_tasks(self) -> list[dict]:
        return [copy.deepcopy(row) for row in self.tasks.values()]

    def show(self, task_id: str) -> dict:
        return {
            "task": copy.deepcopy(self.tasks[task_id]),
            "comments": copy.deepcopy(self.comments[task_id]),
        }

    def comment_once(self, task_id: str, body: str, marker: str) -> None:
        if not any(marker in row["body"] for row in self.comments[task_id]):
            self.comments[task_id].append({"author": "kanban-vikunja-reconciler", "body": body})
            self.comment_calls.append((task_id, body))

    def unblock_if_blocked(self, task_id: str) -> None:
        if self.tasks[task_id]["status"] == "blocked":
            self.tasks[task_id]["status"] = "ready"
            self.unblock_calls.append(task_id)


class FakeVikunja:
    def __init__(self) -> None:
        self.project = {"id": PROJECT, "title": "PRs", "owner": {"username": "sandro", "id": 1}}
        self.tasks: dict[int, dict] = {}
        self.comments: dict[int, list[dict]] = {}
        self.created = 0
        self.updated = 0
        self.assigned = 0
        self.lose_create_response = False
        self.schema_validations = 0

    def validate_schema(self) -> None:
        self.schema_validations += 1

    def validate_projects(self, project_ids: list[int], owner: str) -> dict[int, dict]:
        if project_ids != [PROJECT] or owner != "sandro":
            raise InputError("project ownership mismatch")
        return {PROJECT: copy.deepcopy(self.project)}

    def list_visible_projects(self) -> list[dict]:
        return [copy.deepcopy(self.project)]

    def list_tasks(self, project_id: int) -> list[dict]:
        return [
            copy.deepcopy(row) for row in self.tasks.values() if row["project_id"] == project_id
        ]

    def get_task(self, task_id: int) -> dict:
        return copy.deepcopy(self.tasks[task_id])

    def ensure_task(self, desired: dict) -> dict:
        self.validate_schema()
        matches = [
            row
            for row in self.tasks.values()
            if stable_marker(desired["automation_key"]) in row["description"]
        ]
        if len(matches) > 1:
            raise RuntimeError("duplicate stable marker")
        if matches:
            row = matches[0]
            for field in ("title", "description", "done"):
                row[field] = desired[field]
            self.updated += 1
        else:
            task_id = 70 + len(self.tasks) + 1
            row = {
                "id": task_id,
                "project_id": desired["project_id"],
                "title": desired["title"],
                "description": desired["description"],
                "done": desired["done"],
                "assignees": [],
                "url": f"https://tasks.example.test/tasks/{task_id}",
                "updated": "2026-08-28T08:00:00Z",
            }
            self.tasks[task_id] = row
            self.comments[task_id] = []
            self.created += 1
            if self.lose_create_response:
                self.lose_create_response = False
                raise RuntimeError("response lost after commit")
        row["assignees"] = (
            [{"username": desired["assignee"], "id": 1}] if desired["assignee"] else []
        )
        self.assigned += 1
        return copy.deepcopy(row)

    def find_by_marker(self, project_id: int, marker: str) -> list[dict]:
        return [
            copy.deepcopy(row)
            for row in self.tasks.values()
            if row["project_id"] == project_id and marker in row["description"]
        ]

    def list_comments(self, task_id: int) -> list[dict]:
        return copy.deepcopy(self.comments.get(task_id, []))


class ConfigurationTests(unittest.TestCase):
    def test_cli_passes_source_config_to_both_reconcilers(self) -> None:
        for direction in ("kanban-to-vikunja", "vikunja-to-kanban"):
            with self.subTest(direction=direction), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config_path = root / "config.json"
                config_path.write_text(json.dumps(config()))
                kanban = FakeKanban()
                kanban.tasks = {}
                vikunja = FakeVikunja()
                with (
                    mock.patch.object(projection, "NativeKanban", return_value=kanban),
                    mock.patch.object(projection, "VikunjaClient", return_value=vikunja),
                    mock.patch("builtins.print"),
                ):
                    result = projection.main(
                        [
                            "--config",
                            str(config_path),
                            "--actor-username",
                            "system-admin",
                            "--endpoint",
                            "https://tasks.example.test/api/v2",
                            "--credential-file",
                            str(root / "credential"),
                            "--hermes",
                            "/bin/hermes",
                            "--hermes-home",
                            str(root / "hermes"),
                            "--state-root",
                            str(root / "state"),
                            direction,
                        ]
                    )
                self.assertEqual(result, 0)
                self.assertEqual(vikunja.created, 0)

    def test_vikunja_v2_markdown_updates_use_rich_text_put(self) -> None:
        client = VikunjaClient.__new__(VikunjaClient)
        client._allowed("PUT", "/tasks/71?format=markdown")
        with self.assertRaises(InputError):
            client._allowed("PATCH", "/tasks/71")

    def test_project_110_is_permanently_include_mode_linked_only(self) -> None:
        normalized = validate_config(config())
        for edge in normalized["edges"].values():
            self.assertEqual(edge["selected_projects"], [110])
            self.assertEqual(edge["project_policies"]["110"]["mode"], "linkedOnly")

    def test_selectors_require_exactly_one_mode_and_future_guard(self) -> None:
        for mutation in ("both", "neither", "unguarded-exclude"):
            with self.subTest(mutation=mutation):
                value = config()
                selector = value["edges"]["kanban_to_vikunja"]["selector"]
                if mutation == "both":
                    selector["exclude"] = [9]
                elif mutation == "neither":
                    selector["include"] = None
                else:
                    selector["include"] = None
                    selector["exclude"] = [9]
                with self.assertRaises(InputError):
                    validate_config(value)

    def test_overlapping_intake_ownership_is_rejected(self) -> None:
        with self.assertRaisesRegex(InputError, "overlapping enabled intake owners"):
            validate_intake_ownership(
                [
                    {
                        "name": "first",
                        "direction": "vikunja-to-kanban",
                        "enabled": True,
                        "projects": [110],
                    },
                    {
                        "name": "second",
                        "direction": "vikunja-to-kanban",
                        "enabled": True,
                        "projects": [110],
                    },
                ]
            )

    def test_project_110_rejects_project_wide_or_allowlist_but_may_be_explicitly_excluded(
        self,
    ) -> None:
        attacks = ["projectWide", "taskAllowlist"]
        for mode in attacks:
            with self.subTest(mode=mode):
                value = config()
                value["edges"]["vikunja_to_kanban"]["project_policies"]["110"] = {
                    "mode": mode,
                    "task_allowlist": [71] if mode == "taskAllowlist" else [],
                }
                with self.assertRaises(InputError):
                    validate_config(value)
        value = config()
        selector = value["edges"]["vikunja_to_kanban"]["selector"]
        selector.update(include=None, exclude=[], include_future_projects=True)
        normalized = validate_config(value)
        self.assertEqual(normalized["edges"]["vikunja_to_kanban"]["selected_projects"], [110])
        selector["exclude"] = [110]
        normalized = validate_config(value)
        self.assertEqual(normalized["edges"]["vikunja_to_kanban"]["selected_projects"], [])

    def test_exclude_mode_selects_visible_nonexcluded_projects_with_future_acknowledgement(
        self,
    ) -> None:
        value = config()
        edge = value["edges"]["vikunja_to_kanban"]
        edge["selector"] = {
            "include": None,
            "exclude": [9],
            "include_future_projects": True,
        }
        edge["project_policies"]["110"] = {"mode": "linkedOnly", "task_allowlist": []}
        normalized = validate_config(value)

        class VisibleVikunja(FakeVikunja):
            def list_visible_projects(self) -> list[dict]:
                return [
                    {"id": 9, "title": "Excluded", "owner": {"username": "sandro"}},
                    self.project,
                    {"id": 120, "title": "Future", "owner": {"username": "sandro"}},
                ]

            def validate_projects(self, project_ids: list[int], owner: str) -> dict[int, dict]:
                self.assertions = (project_ids, owner)
                rows = {row["id"]: row for row in self.list_visible_projects()}
                return {project_id: rows[project_id] for project_id in project_ids}

        vikunja = VisibleVikunja()
        self.assertEqual(
            projection._selected_projects(normalized, "vikunja_to_kanban", vikunja), [110, 120]
        )
        self.assertEqual(vikunja.assertions, ([110, 120], "sandro"))


class IdentityTests(unittest.TestCase):
    def test_stable_reciprocal_reference_round_trip(self) -> None:
        key = action_key("nix-config-issue-200-questions", "Questions")
        marker = stable_marker(key)
        self.assertEqual(
            parse_link(f"Hermes Kanban: {BOARD}/{TASK_ID}\n\n{marker}"), (BOARD, TASK_ID, key)
        )
        self.assertNotEqual(action_key(AUTO_REF, "Merge"), key)
        with self.assertRaises(InputError):
            action_key(AUTO_REF, "Questions")

    def test_link_rejects_duplicates_controls_and_forged_marker(self) -> None:
        key = action_key(AUTO_REF, "Merge")
        samples = [
            f"Hermes Kanban: {BOARD}/{TASK_ID}\nHermes Kanban: {BOARD}/{TASK_ID}\n{stable_marker(key)}",
            f"Hermes Kanban: {BOARD}/{TASK_ID}\n{stable_marker(key)}\n{stable_marker(key)}",
            f"Hermes Kanban: ../escape/{TASK_ID}\n{stable_marker(key)}",
            f"Hermes Kanban: {BOARD}/{TASK_ID}\nAutomation reference: forged",
            f"Hermes Kanban: {BOARD}/{TASK_ID}\r\n{stable_marker(key)}",
        ]
        for sample in samples:
            with self.subTest(sample=sample), self.assertRaises(InputError):
                parse_link(sample)

    def test_comment_edits_have_distinct_event_identity(self) -> None:
        first = {"id": 4, "updated": "2026-08-28T08:00:00Z", "comment": "first"}
        second = {"id": 4, "updated": "2026-08-28T08:00:00Z", "comment": "edited"}
        self.assertNotEqual(comment_event_key(71, first), comment_event_key(71, second))
        self.assertNotEqual(
            completion_event_key(vikunja_task(done=False)),
            completion_event_key(vikunja_task(done=True)),
        )


class ProjectionTests(unittest.TestCase):
    def make_stores(self, root: Path) -> tuple[ProjectionState, ProjectionState]:
        return ProjectionState(root / "k2v", "kanban-to-vikunja"), ProjectionState(
            root / "v2k", "vikunja-to-kanban"
        )

    def test_create_readback_mapping_and_reciprocal_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k2v, _ = self.make_stores(Path(tmp))
            kanban = FakeKanban()
            vikunja = FakeVikunja()
            result = reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            self.assertEqual(result, {"created_or_updated": 1, "noop": 0, "failed": 0})
            self.assertEqual(vikunja.created, 1)
            row = next(iter(vikunja.tasks.values()))
            self.assertIn(f"Hermes Kanban: {BOARD}/{TASK_ID}", row["description"])
            self.assertEqual(row["assignees"][0]["username"], "sandro")
            self.assertIn("Vikunja task ID: 71", kanban.comment_calls[0][1])
            mapping = k2v.read()["mappings"][f"{BOARD}/{TASK_ID}"]
            self.assertEqual(mapping["vikunja_task_id"], 71)
            self.assertEqual(mapping["action_type"], "Merge")
            vikunja.tasks[71]["description"] = vikunja.tasks[71]["description"].replace(
                "\n---\n", "\n* * *\n"
            )
            self.assertEqual(
                reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v),
                {"created_or_updated": 0, "noop": 1, "failed": 0},
            )
            self.assertEqual(vikunja.updated, 0)
            duplicate = copy.deepcopy(mapping)
            duplicate["kanban_task_id"] = "t_cafebabe"
            with self.assertRaisesRegex(InputError, "duplicate target or automation"):
                k2v.change(
                    lambda persisted: persisted["mappings"].__setitem__(
                        f"{BOARD}/t_cafebabe", duplicate
                    )
                )

    def test_merge_lifecycle_updates_one_mapped_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k2v, _ = self.make_stores(Path(tmp))
            source = kanban_task()
            source["body"] = source["body"].replace(
                "Vikunja assignee: sandro", "Vikunja assignee: none"
            )
            kanban, vikunja = FakeKanban([source]), FakeVikunja()
            reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            for owner, done, action in (
                ("sandro", False, "Review the tested head and decide whether to merge."),
                ("none", False, "New head: CI and review pending."),
                ("sandro", False, "Merged: authorized host rebuild pending."),
                ("none", False, "Live verification pending."),
                ("none", True, "Merge, rebuild and live verification recorded."),
            ):
                with self.subTest(action=action):
                    task = kanban.tasks[TASK_ID]
                    task["body"] = (
                        f"Current action: {action}\n\nHuman action: Merge\n"
                        f"Vikunja project: {PROJECT}\nVikunja assignee: {owner}\n"
                        f"Vikunja done: {str(done).lower()}\n"
                        f"Vikunja automation reference: {AUTO_REF}\n"
                    )
                    result = reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
                    self.assertEqual(result["failed"], 0)
                    self.assertEqual(vikunja.created, 1)
                    self.assertEqual(vikunja.tasks[71]["done"], done)
                    self.assertEqual(
                        [row["username"] for row in vikunja.tasks[71]["assignees"]],
                        [] if owner == "none" else [owner],
                    )
                    self.assertIn(action, vikunja.tasks[71]["description"])
                    self.assertEqual(
                        reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v),
                        {"created_or_updated": 0, "noop": 1, "failed": 0},
                    )
            self.assertEqual(len(kanban.comment_calls), 1)

    def test_mapped_update_retries_failure_before_or_after_remote_commit(self) -> None:
        for committed in (False, True):
            with self.subTest(committed=committed), tempfile.TemporaryDirectory() as tmp:
                k2v, _ = self.make_stores(Path(tmp))
                kanban, vikunja = FakeKanban(), FakeVikunja()
                reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
                kanban.tasks[TASK_ID]["body"] += "\nVerified new lifecycle step.\n"
                original = vikunja.ensure_task

                def interrupted(desired):
                    if committed:
                        original(desired)
                    raise RuntimeError("simulated transport interruption")

                vikunja.ensure_task = interrupted
                self.assertEqual(
                    reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)["failed"], 1
                )
                vikunja.ensure_task = original
                restarted = ProjectionState(Path(tmp) / "k2v", "kanban-to-vikunja")
                self.assertEqual(
                    reconcile_kanban_to_vikunja(config(), kanban, vikunja, restarted)["failed"], 0
                )
                self.assertEqual(vikunja.created, 1)
                self.assertEqual(restarted.read()["pending"], {})
                self.assertIn("Verified new lifecycle step", vikunja.tasks[71]["description"])

    def test_mapped_task_cannot_be_replaced_by_missing_or_moved_marker(self) -> None:
        for replacement in (None, 72):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as tmp:
                k2v, _ = self.make_stores(Path(tmp))
                kanban, vikunja = FakeKanban(), FakeVikunja()
                reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
                row = vikunja.tasks.pop(71)
                if replacement is not None:
                    row["id"] = replacement
                    row["url"] = f"https://tasks.example.test/tasks/{replacement}"
                    vikunja.tasks[replacement] = row
                with self.assertRaises(InputError):
                    reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
                self.assertEqual(vikunja.created, 1)
                self.assertEqual(vikunja.updated, 0)

    def test_manual_completion_repaired_without_granting_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k2v, _ = self.make_stores(Path(tmp))
            kanban, vikunja = FakeKanban(), FakeVikunja()
            reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            vikunja.tasks[71]["done"] = True
            result = reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            self.assertEqual(result["failed"], 0)
            self.assertFalse(vikunja.tasks[71]["done"])
            self.assertEqual(kanban.unblock_calls, [])

    def test_lost_create_response_recovers_by_marker_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k2v, _ = self.make_stores(Path(tmp))
            kanban = FakeKanban()
            vikunja = FakeVikunja()
            vikunja.lose_create_response = True
            first = reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            self.assertEqual(first["failed"], 1)
            self.assertEqual(vikunja.created, 1)
            restarted = ProjectionState(Path(tmp) / "k2v", "kanban-to-vikunja")
            second = reconcile_kanban_to_vikunja(config(), kanban, vikunja, restarted)
            self.assertEqual(second["failed"], 0)
            self.assertEqual(vikunja.created, 1)
            self.assertEqual(len(kanban.comment_calls), 1)

    def test_marker_collision_never_overwrites_or_adopts_unmapped_human_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k2v, _ = self.make_stores(Path(tmp))
            kanban = FakeKanban()
            vikunja = FakeVikunja()
            key = action_key(AUTO_REF, "Merge")
            collision = vikunja_task(
                task_id=71,
                description=f"Unrelated human task.\n\n{stable_marker(key)}",
            )
            collision["title"] = "Unrelated human task"
            vikunja.tasks[71] = collision
            with self.assertRaises(InputError):
                reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            self.assertEqual(vikunja.tasks[71]["title"], "Unrelated human task")
            self.assertEqual(vikunja.updated, 0)
            self.assertEqual(k2v.read()["mappings"], {})

    def test_pending_edges_recover_after_source_disappears_from_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k2v, v2k = self.make_stores(Path(tmp))
            kanban = FakeKanban()
            vikunja = FakeVikunja()
            vikunja.lose_create_response = True
            self.assertEqual(
                reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)["failed"], 1
            )
            kanban.list_tasks = list
            recovered = reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            self.assertEqual(recovered["failed"], 0)
            self.assertEqual(k2v.read()["pending"], {})

            task_id = next(iter(vikunja.tasks))
            vikunja.comments[task_id] = [
                {
                    "id": 9,
                    "comment": "Human evidence",
                    "updated": "2026-08-28T08:20:00Z",
                    "author": {"username": "sandro"},
                }
            ]
            original = kanban.comment_once
            lose_once = {"value": True}

            def committed_response_lost(task_id: str, body: str, marker: str) -> None:
                original(task_id, body, marker)
                if lose_once["value"]:
                    lose_once["value"] = False
                    raise RuntimeError("response lost after native commit")

            kanban.comment_once = committed_response_lost
            self.assertEqual(
                reconcile_vikunja_to_kanban(config(), kanban, vikunja, v2k, k2v)["failed"], 1
            )
            vikunja.tasks.clear()
            vikunja.comments.clear()
            kanban.comment_once = original
            recovered = reconcile_vikunja_to_kanban(config(), kanban, vikunja, v2k, k2v)
            self.assertEqual(recovered["failed"], 0)
            self.assertEqual(v2k.read()["pending"], {})
            evidence = [
                body for _, body in kanban.comment_calls if "Vikunja comment evidence" in body
            ]
            self.assertEqual(len(evidence), 1)

    def test_no_change_is_zero_mutation_and_zero_wake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k2v, v2k = self.make_stores(Path(tmp))
            kanban = FakeKanban()
            vikunja = FakeVikunja()
            reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            baseline = (
                vikunja.created,
                vikunja.updated,
                vikunja.schema_validations,
                len(kanban.comment_calls),
                len(kanban.unblock_calls),
            )
            result1 = reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            result2 = reconcile_vikunja_to_kanban(config(), kanban, vikunja, v2k, k2v)
            self.assertEqual(result1["noop"], 1)
            self.assertEqual(result2["noop"], 1)
            self.assertEqual(
                baseline,
                (
                    vikunja.created,
                    vikunja.updated,
                    vikunja.schema_validations,
                    len(kanban.comment_calls),
                    len(kanban.unblock_calls),
                ),
            )

    def test_unlinked_project_110_task_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k2v, v2k = self.make_stores(Path(tmp))
            vikunja = FakeVikunja()
            vikunja.tasks[99] = vikunja_task(task_id=99, description="ordinary human task")
            result = reconcile_vikunja_to_kanban(config(), FakeKanban(), vikunja, v2k, k2v)
            self.assertEqual(result, {"evidence": 0, "noop": 1, "failed": 0})

    def test_marker_only_tasks_are_silent_and_do_not_block_mapped_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k2v, v2k = self.make_stores(Path(tmp))
            kanban = FakeKanban()
            vikunja = FakeVikunja()
            reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            mapped = next(iter(vikunja.tasks.values()))
            mapped["done"] = True
            # Match deployed legacy tasks, including a marker that happens to
            # use the same key as a mapped task. Never adopt based on the key.
            vikunja.tasks = {
                1586: vikunja_task(
                    task_id=1586,
                    description="**Automation reference:** `vikunja-orphaned-test-project-removal-guide-v1`",
                ),
                1587: vikunja_task(task_id=1587, description=stable_marker(AUTO_REF)),
                mapped["id"]: mapped,
            }
            before = (vikunja.created, vikunja.updated, len(kanban.comment_calls))
            result = reconcile_vikunja_to_kanban(config(), kanban, vikunja, v2k, k2v)
            self.assertEqual(result, {"evidence": 1, "noop": 0, "failed": 0})
            self.assertEqual((vikunja.created, vikunja.updated), before[:2])
            self.assertEqual(len(kanban.comment_calls), before[2] + 1)
            self.assertEqual(kanban.unblock_calls, [])
            self.assertEqual(
                reconcile_vikunja_to_kanban(config(), kanban, vikunja, v2k, k2v),
                {"evidence": 0, "noop": 1, "failed": 0},
            )

    def test_mapped_task_with_removed_link_still_fails_closed(self) -> None:
        for description in ("ordinary task", stable_marker(AUTO_REF)):
            with self.subTest(description=description), tempfile.TemporaryDirectory() as tmp:
                k2v, v2k = self.make_stores(Path(tmp))
                kanban = FakeKanban()
                vikunja = FakeVikunja()
                reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
                row = next(iter(vikunja.tasks.values()))
                row["description"] = description
                row["done"] = True
                before = len(kanban.comment_calls)
                with self.assertRaisesRegex(InputError, f"Vikunja task {row['id']} lacks"):
                    reconcile_vikunja_to_kanban(config(), kanban, vikunja, v2k, k2v)
                self.assertEqual(len(kanban.comment_calls), before)
                self.assertEqual(kanban.unblock_calls, [])

    def test_explicit_unmapped_link_is_not_silently_ignored(self) -> None:
        for description in (
            f"Hermes Kanban: {BOARD}/{TASK_ID}",
            f"Hermes Kanban: {BOARD}/{TASK_ID}\n{stable_marker(AUTO_REF)}",
        ):
            with self.subTest(description=description), tempfile.TemporaryDirectory() as tmp:
                k2v, v2k = self.make_stores(Path(tmp))
                kanban = FakeKanban()
                vikunja = FakeVikunja()
                vikunja.tasks[99] = vikunja_task(task_id=99, description=description)
                with self.assertRaises(InputError):
                    reconcile_vikunja_to_kanban(config(), kanban, vikunja, v2k, k2v)
                self.assertEqual(kanban.comment_calls, [])
                self.assertEqual(kanban.unblock_calls, [])

    def test_human_comment_updates_one_card_dedupes_edits_and_suppresses_echo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k2v, v2k = self.make_stores(Path(tmp))
            kanban = FakeKanban()
            vikunja = FakeVikunja()
            reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            task_id = next(iter(vikunja.tasks))
            vikunja.comments[task_id] = [
                {
                    "id": 1,
                    "comment": "Please wait for CI.",
                    "updated": "2026-08-28T08:10:00Z",
                    "author": {"username": "sandro"},
                },
                {
                    "id": 2,
                    "comment": "bot echo",
                    "updated": "2026-08-28T08:11:00Z",
                    "author": {"username": "system-admin"},
                },
            ]
            first = reconcile_vikunja_to_kanban(
                config(), kanban, vikunja, v2k, k2v, actor_username="system-admin"
            )
            second = reconcile_vikunja_to_kanban(
                config(), kanban, vikunja, v2k, k2v, actor_username="system-admin"
            )
            self.assertEqual(first["evidence"], 1)
            self.assertEqual(second["evidence"], 0)
            self.assertEqual(kanban.unblock_calls, [TASK_ID])
            evidence_comments = [
                body for _, body in kanban.comment_calls if "Vikunja comment evidence" in body
            ]
            self.assertEqual(len(evidence_comments), 1)
            self.assertIn(
                "not merge, deploy, rebuild, rollback, verification, or closure authorization",
                evidence_comments[0],
            )
            vikunja.comments[task_id][0]["comment"] = (
                "Edited: CI is now green; still wait for review."
            )
            edited = reconcile_vikunja_to_kanban(
                config(), kanban, vikunja, v2k, k2v, actor_username="system-admin"
            )
            self.assertEqual(edited["evidence"], 1)

    def test_completion_is_evidence_only_and_never_unblocks_or_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k2v, v2k = self.make_stores(Path(tmp))
            kanban = FakeKanban()
            vikunja = FakeVikunja()
            reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            task_id = next(iter(vikunja.tasks))
            vikunja.tasks[task_id]["done"] = True
            result = reconcile_vikunja_to_kanban(config(), kanban, vikunja, v2k, k2v)
            self.assertEqual(result["evidence"], 1)
            self.assertEqual(kanban.unblock_calls, [])
            self.assertEqual(kanban.tasks[TASK_ID]["status"], "blocked")
            body = next(body for _, body in kanban.comment_calls if "completion evidence" in body)
            self.assertIn("does not authorize", body)

    def test_owner_assignee_and_mapping_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k2v, v2k = self.make_stores(Path(tmp))
            kanban = FakeKanban()
            vikunja = FakeVikunja()
            vikunja.project["owner"]["username"] = "attacker"
            with self.assertRaises(InputError):
                reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            vikunja.project["owner"]["username"] = "sandro"
            reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            row = next(iter(vikunja.tasks.values()))
            row["description"] = row["description"].replace(TASK_ID, "t_cafebabe")
            with self.assertRaises(InputError):
                reconcile_vikunja_to_kanban(config(), kanban, vikunja, v2k, k2v)

    def test_directional_failures_do_not_replay_confirmed_other_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k2v, v2k = self.make_stores(Path(tmp))
            kanban = FakeKanban()
            vikunja = FakeVikunja()
            reconcile_kanban_to_vikunja(config(), kanban, vikunja, k2v)
            before = (vikunja.created, vikunja.updated)
            task_id = next(iter(vikunja.tasks))
            vikunja.comments[task_id] = [
                {
                    "id": 3,
                    "comment": "input",
                    "updated": "2026-08-28T08:15:00Z",
                    "author": {"username": "sandro"},
                }
            ]
            original = kanban.comment_once

            def transient_failure(task_id: str, body: str, marker: str) -> None:
                raise RuntimeError("transient target failure")

            kanban.comment_once = transient_failure
            failed = reconcile_vikunja_to_kanban(config(), kanban, vikunja, v2k, k2v)
            self.assertEqual(failed["failed"], 1)
            kanban.comment_once = original
            recovered = reconcile_vikunja_to_kanban(config(), kanban, vikunja, v2k, k2v)
            self.assertEqual(recovered["evidence"], 1)
            self.assertEqual(before, (vikunja.created, vikunja.updated))

    def test_concurrent_runs_serialize_without_duplicate_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "k2v"
            kanban = FakeKanban()
            vikunja = FakeVikunja()
            errors: list[Exception] = []

            def worker() -> None:
                try:
                    reconcile_kanban_to_vikunja(
                        config(), kanban, vikunja, ProjectionState(root, "kanban-to-vikunja")
                    )
                except (InputError, RuntimeError, OSError, ProtocolError, KeyError) as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(vikunja.created, 1)
            self.assertEqual(len(kanban.comment_calls), 1)

    def test_confirmed_event_retention_compacts_without_replay_authority_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectionState(Path(tmp) / "v2k", "vikunja-to-kanban")
            old_max_events = projection.MAX_EVENTS
            old_window = projection.COMMENT_REVISION_WINDOW
            projection.MAX_EVENTS = 3
            projection.COMMENT_REVISION_WINDOW = 2
            try:
                for comment_id in range(1, 6):
                    store.confirm_event(
                        f"event-{comment_id}",
                        {"kind": "comment"},
                        vikunja_task_id=71,
                        comment_id=comment_id,
                    )
                persisted = store.read()
                self.assertEqual(len(persisted["events"]), 3)
                self.assertEqual(persisted["comment_revisions"]["71"]["high_id"], 5)
                self.assertEqual(set(persisted["comment_revisions"]["71"]["recent"]), {"4", "5"})
            finally:
                projection.MAX_EVENTS = old_max_events
                projection.COMMENT_REVISION_WINDOW = old_window


class AdapterTests(unittest.TestCase):
    def test_partial_update_preserves_unmanaged_vikunja_metadata(self) -> None:
        client = VikunjaClient(
            "https://tasks.example.test",
            Path("/credential"),
            token="fixture",
        )
        desired = {
            "automation_key": AUTO_REF,
            "project_id": 110,
            "title": "Updated title",
            "description": f"Hermes Kanban: {BOARD}/{TASK_ID}\n\n{stable_marker(AUTO_REF)}",
            "done": False,
            "assignee": None,
        }
        existing = {
            "id": 71,
            "project_id": 110,
            "title": "Old title",
            "description": desired["description"],
            "done": False,
            "due_date": "2026-09-01T12:00:00Z",
            "labels": [{"id": 4, "title": "manual"}],
            "assignees": [],
        }
        client.validate_schema = lambda: None
        client.find_by_marker = lambda _project, _marker: [existing]
        client.get_task = lambda _task_id: client._task_url(existing)

        def replace_task(path, payload):
            if path.endswith("/assignees/bulk"):
                existing["assignees"] = payload["assignees"]
            else:
                # PUT replaces writable fields; labels use a separate endpoint.
                labels = existing["labels"]
                existing.clear()
                existing.update(id=71, labels=labels, **payload)

        client.put = mock.Mock(side_effect=replace_task)
        result = client.ensure_task(desired)
        client.put.assert_any_call(
            "/tasks/71?format=markdown",
            {
                "title": "Updated title",
                "description": desired["description"],
                "done": False,
                "project_id": 110,
                "due_date": "2026-09-01T12:00:00Z",
            },
        )
        self.assertEqual(result["due_date"], "2026-09-01T12:00:00Z")
        self.assertEqual(result["labels"], [{"id": 4, "title": "manual"}])

    def test_typed_vikunja_create_assign_and_readback_positive_control(self) -> None:
        calls: list[tuple[str, str]] = []
        task: dict | None = None
        schema = {
            "paths": {
                "/projects/{project}/tasks": {"get": {}, "post": {}},
                "/tasks/{projecttask}": {"get": {}, "put": {}},
                "/tasks/{task}/comments": {"get": {}},
                "/tasks/{projecttask}/assignees/bulk": {"put": {}},
            }
        }

        class Response:
            status = 200

            def __init__(self, body):
                self.body = body
                self.headers = {"Content-Type": "application/json"}

            def read(self, _limit):
                return json.dumps(self.body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def opener(request, timeout):
            nonlocal task
            parsed = urllib.parse.urlsplit(request.full_url)
            calls.append((request.method, parsed.path))
            payload = json.loads(request.data) if request.data else None
            if request.method == "GET" and parsed.path == "/api/v2/openapi.json":
                return Response(schema)
            if request.method == "GET" and parsed.path == "/api/v2/projects/110":
                return Response(
                    {"id": 110, "title": "PRs", "owner": {"id": 1, "username": "sandro"}}
                )
            if request.method == "GET" and parsed.path == "/api/v2/projects/110/tasks":
                return Response({"items": [] if task is None else [task], "total_pages": 1})
            if request.method == "POST" and parsed.path == "/api/v2/projects/110/tasks":
                assert isinstance(payload, dict)
                task = {
                    "id": 71,
                    "project_id": 110,
                    "assignees": [],
                    **payload,
                }
                return Response(task)
            if request.method == "PUT" and parsed.path == "/api/v2/tasks/71/assignees/bulk":
                assert task is not None and isinstance(payload, dict)
                task["assignees"] = (
                    [{"id": 1, "username": "sandro"}] if payload["assignees"] else []
                )
                return Response(task)
            if request.method == "PUT" and parsed.path == "/api/v2/tasks/71":
                assert task is not None and isinstance(payload, dict)
                self.assertEqual(urllib.parse.parse_qs(parsed.query), {"format": ["markdown"]})
                self.assertEqual(payload["priority"], 3)
                self.assertEqual(payload["due_date"], "2026-10-01T00:00:00Z")
                self.assertEqual(payload["project_id"], PROJECT)
                task.update(payload)
                return Response(task)
            if request.method == "GET" and parsed.path == "/api/v2/tasks/71":
                return Response(task)
            raise AssertionError((request.method, request.full_url, timeout))

        client = VikunjaClient(
            "https://tasks.example.test/api/v1",
            Path("/credential"),
            token="fixture",
            opener=opener,
        )
        client.validate_projects([110], "sandro")
        desired = {
            "automation_key": AUTO_REF,
            "project_id": 110,
            "title": "Merge nix-config PR 200 after review",
            "description": f"Hermes Kanban: {BOARD}/{TASK_ID}\n\n{stable_marker(AUTO_REF)}",
            "done": False,
            "assignee": "sandro",
        }
        result = client.ensure_task(desired)
        self.assertEqual(result["url"], "https://tasks.example.test/tasks/71")
        self.assertEqual(result["assignees"], [{"id": 1, "username": "sandro"}])
        self.assertIn(("POST", "/api/v2/projects/110/tasks"), calls)
        self.assertIn(("PUT", "/api/v2/tasks/71/assignees/bulk"), calls)
        assert task is not None
        task.update(priority=3, due_date="2026-10-01T00:00:00Z")
        desired.update(description=desired["description"] + "\nNew reviewed head.", assignee=None)
        updated = client.ensure_task(desired)
        self.assertEqual(updated["description"], desired["description"])
        self.assertEqual(updated["assignees"], [])
        self.assertEqual(updated["id"], 71)
        self.assertIn(("PUT", "/api/v2/tasks/71"), calls)

    def test_native_comment_and_unblock_readback_positive_control(self) -> None:
        comments: list[dict] = []
        task = kanban_task()

        def runner(command, **_kwargs):
            args = command[4:]
            if args[:2] == ["show", TASK_ID]:
                return subprocess.CompletedProcess(
                    command, 0, json.dumps({"task": task, "comments": comments}), ""
                )
            if args[0] == "comment":
                comments.append({"author": "kanban-vikunja-reconciler", "body": args[2]})
                return subprocess.CompletedProcess(command, 0, "", "")
            if args[:2] == ["unblock", TASK_ID]:
                task["status"] = "ready"
                return subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError(command)

        adapter = NativeKanban(
            Path("/nix/store/hermes/bin/hermes"), BOARD, Path("/var/lib/hermes"), runner
        )
        body = "kanban-vikunja-event:test\nActionable human comment"
        adapter.comment_once(TASK_ID, body, "kanban-vikunja-event:test")
        adapter.unblock_if_blocked(TASK_ID)
        self.assertEqual(comments, [{"author": "kanban-vikunja-reconciler", "body": body}])
        self.assertEqual(task["status"], "ready")

    def test_live_openapi_operation_names_are_bound_to_dispatch(self) -> None:
        schema = {
            "paths": {
                "/projects/{project}/tasks": {"get": {}, "post": {}},
                "/tasks/{projecttask}": {"get": {}, "put": {}},
                "/tasks/{task}/comments": {"get": {}},
                "/tasks/{projecttask}/assignees/bulk": {"put": {}},
            }
        }
        client = VikunjaClient(
            "https://tasks.example.test",
            Path("/credential"),
            token="fixture",
        )
        client.get = lambda path: schema
        client.validate_schema()
        schema["paths"]["/tasks/{projecttask}/assignees/bulk"] = {}
        invalid = VikunjaClient("https://tasks.example.test", Path("/credential"), token="fixture")
        invalid.get = lambda path: schema
        with self.assertRaises(InputError):
            invalid.validate_schema()

    def test_native_cli_uses_explicit_board_and_json_without_sqlite(self) -> None:
        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            if "list" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps([kanban_task()]), "")
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"task": kanban_task(), "comments": []}), ""
            )

        adapter = NativeKanban(
            Path("/nix/store/hermes/bin/hermes"), BOARD, Path("/var/lib/hermes"), runner
        )
        self.assertEqual(adapter.list_tasks()[0]["id"], TASK_ID)
        self.assertEqual(adapter.show(TASK_ID)["task"]["id"], TASK_ID)
        for command in calls:
            self.assertEqual(
                command[:4], ["/nix/store/hermes/bin/hermes", "kanban", "--board", BOARD]
            )
            self.assertNotIn("sqlite", " ".join(command).lower())

    def test_vikunja_client_paginates_typed_v2_and_refuses_redirects(self) -> None:
        calls: list[str] = []
        pages = {
            1: {"items": [{"id": 1}], "total_pages": 2},
            2: {"items": [{"id": 2}], "total_pages": 2},
        }

        class Response:
            status = 200

            def __init__(self, body):
                self.body = body
                self.headers = {"Content-Type": "application/json"}

            def read(self, _limit):
                return json.dumps(self.body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def opener(request, timeout):
            calls.append(request.full_url)
            page = int(request.full_url.split("page=")[1].split("&")[0])
            return Response(pages[page])

        client = VikunjaClient(
            "https://tasks.example.test/api/v2",
            Path("/credential"),
            opener=opener,
            token="fixture",
        )
        rows = client.pages("/projects/110/tasks?format=markdown")
        self.assertEqual([row["id"] for row in rows], [1, 2])
        self.assertTrue(all(url.startswith("https://tasks.example.test/api/v2/") for url in calls))
        self.assertEqual(
            canonical_origin("https://tasks.example.test/api/v2"), "https://tasks.example.test"
        )
        self.assertEqual(
            canonical_origin("https://tasks.example.test/api/v1"), "https://tasks.example.test"
        )
        with self.assertRaises(InputError):
            canonical_origin("https://user@tasks.example.test/api/v2")
        redirect = urllib.error.HTTPError(
            "https://tasks.example.test", 302, "redirect", email.message.Message(), None
        )
        client._opener = lambda *_args, **_kwargs: (_ for _ in ()).throw(redirect)
        with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
            client.get("/projects/110")


if __name__ == "__main__":
    unittest.main()
