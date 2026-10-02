"""Shared boards must preserve repository ownership and independent receipts."""

import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hermes_repository_instance import RepositoryInstance, instance_scope, repository_registry
from hermes_workflow_state import ProtocolError
from kanban_vikunja_projection import (
    InputError,
    ProjectionState,
    reconcile_kanban_to_vikunja,
    reconcile_vikunja_to_kanban,
)
from test_projection import FakeKanban, FakeVikunja, config, kanban_task


class RepositoryRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        registry = self.root / "registry"
        registry.mkdir()
        original = json.loads(Path(os.environ["HERMES_REPOSITORY_INSTANCE"]).read_text())
        other = {
            **original,
            "id": "hermes-config",
            "namespace": "hermes-config",
            "repository": "nixos/hermes-config",
            "default_branch": "main",
        }
        self.contracts = [original, other]
        self.instances = [RepositoryInstance.parse(row) for row in self.contracts]
        for row in self.contracts:
            (registry / (row["id"] + ".json")).write_text(json.dumps(row))
        environment = patch.dict(os.environ, {"HERMES_REPOSITORY_INSTANCES": str(registry)})
        environment.start()
        self.addCleanup(environment.stop)
        tasks = []
        ids = iter(["t_deadbeef", "t_deadbe00", "t_cafebabe", "t_cafeba00"])
        for owner in self.instances:
            for action in ("Merge", "Questions"):
                task = kanban_task(action=action)
                task["id"] = next(ids)
                task["body"] = task["body"].replace("nix-config-", owner.namespace + "-")
                tasks.append(task)
        self.kanban = FakeKanban(tasks)
        self.vikunja = FakeVikunja()

    def stores(self, owner):
        return (
            ProjectionState(self.root / owner.namespace / "out", "kanban-to-vikunja"),
            ProjectionState(self.root / owner.namespace / "in", "vikunja-to-kanban"),
        )

    def test_overlapping_issue_and_pr_numbers_keep_separate_mappings(self):
        for owner in self.instances:
            with instance_scope(owner):
                outgoing, incoming = self.stores(owner)
                result = reconcile_kanban_to_vikunja(config(), self.kanban, self.vikunja, outgoing)
                self.assertEqual(result["created_or_updated"], 2)
                mappings = outgoing.read()["mappings"]
                self.assertEqual(len(mappings), 2)
                self.assertTrue(
                    all(
                        row["automation_key"].startswith(owner.namespace + "-")
                        for row in mappings.values()
                    )
                )
        self.assertEqual(self.vikunja.created, 4)
        for owner in self.instances:
            with instance_scope(owner):
                outgoing, incoming = self.stores(owner)
                before = copy.deepcopy(outgoing.read())
                result = reconcile_kanban_to_vikunja(config(), self.kanban, self.vikunja, outgoing)
                self.assertEqual(result, {"created_or_updated": 0, "noop": 2, "failed": 0})
                reconcile_vikunja_to_kanban(config(), self.kanban, self.vikunja, incoming, outgoing)
                self.assertEqual(outgoing.read()["mappings"], before["mappings"])
        self.assertEqual(self.vikunja.created, 4)
        self.assertEqual(self.vikunja.updated, 0)
        self.assertEqual(self.kanban.unblock_calls, [])

    def test_mapped_native_task_cannot_change_repository(self):
        with instance_scope(self.instances[0]):
            outgoing, _ = self.stores(self.instances[0])
            reconcile_kanban_to_vikunja(config(), self.kanban, self.vikunja, outgoing)
            before = copy.deepcopy(outgoing.read())
            self.kanban.tasks["t_deadbeef"]["body"] = self.kanban.tasks["t_deadbeef"][
                "body"
            ].replace("nix-config-pr-200-merge", "hermes-config-pr-200-merge")
            with self.assertRaisesRegex(InputError, "changed repository ownership"):
                reconcile_kanban_to_vikunja(config(), self.kanban, self.vikunja, outgoing)
            self.assertEqual(outgoing.read()["mappings"], before["mappings"])

    def test_mapped_human_task_cannot_change_repository(self):
        with instance_scope(self.instances[0]):
            outgoing, incoming = self.stores(self.instances[0])
            reconcile_kanban_to_vikunja(config(), self.kanban, self.vikunja, outgoing)
            before = list(self.kanban.comment_calls)
            self.vikunja.tasks[71]["description"] = self.vikunja.tasks[71]["description"].replace(
                "nix-config-pr-200-merge", "hermes-config-pr-200-merge"
            )
            with self.assertRaisesRegex(InputError, "changed repository ownership"):
                reconcile_vikunja_to_kanban(config(), self.kanban, self.vikunja, incoming, outgoing)
            self.assertEqual(self.kanban.comment_calls, before)
            self.assertEqual(self.kanban.unblock_calls, [])

    def test_unknown_repository_reference_is_not_silently_dropped(self):
        with instance_scope(self.instances[0]):
            outgoing, _ = self.stores(self.instances[0])
            self.kanban.tasks["t_deadbeef"]["body"] = self.kanban.tasks["t_deadbeef"][
                "body"
            ].replace("nix-config-pr-200-merge", "unknown-pr-200-merge")
            with self.assertRaisesRegex(InputError, "unknown or ambiguous"):
                reconcile_kanban_to_vikunja(config(), self.kanban, self.vikunja, outgoing)
            self.assertEqual(self.vikunja.created, 0)

    def test_registry_rejects_collision_and_wrong_selected_contract(self):
        with instance_scope(replace(self.instances[0], repository="wrong/repository")):
            with self.assertRaisesRegex(ProtocolError, "differs from its registry"):
                repository_registry()
        other = dict(self.contracts[1], namespace=self.instances[0].namespace)
        (self.root / "registry" / "hermes-config.json").write_text(json.dumps(other))
        with instance_scope(self.instances[0]):
            with self.assertRaisesRegex(ProtocolError, "colliding namespace"):
                repository_registry()


if __name__ == "__main__":
    unittest.main()
