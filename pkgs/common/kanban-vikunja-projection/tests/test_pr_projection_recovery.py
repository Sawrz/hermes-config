"""Offline historical format fixtures and crash recovery, never live task writes."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import kanban_vikunja_projection as p
from test_projection import FakeKanban, FakeVikunja, config, kanban_task


class LegacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = p.ProjectionState(Path(self.tmp.name), "kanban-to-vikunja")
        self.legacy = json.loads(
            (Path(__file__).parent / "fixtures/legacy-pr-task.json").read_text()
        )["task"]
        self.native = kanban_task()
        self.native["body"] = self.native["body"].replace("200", "172") + (
            "PR workflow: native-v1\nForgejo authority: https://git.wrzalek.com/nixos/nix-config/pulls/172\n"
            "Vikunja legacy task: 1587\nVikunja legacy creator: bot-system-admin\n"
            "Vikunja legacy root: t_05367b21\n"
        )
        self.kanban = FakeKanban([self.native])
        self.vikunja = FakeVikunja()
        self.vikunja.tasks[1587] = copy.deepcopy(self.legacy)
        self.vikunja.tasks[1587]["url"] = "https://tasks.example.test/tasks/1587"
        self.vikunja.comments[1587] = []

    def run_projection(self):
        return p.reconcile_kanban_to_vikunja(config(), self.kanban, self.vikunja, self.state)

    def test_explicit_legacy_adoption_preserves_story_and_reuses_task(self):
        self.assertEqual(self.run_projection()["failed"], 0)
        self.assertEqual(len(self.vikunja.tasks), 1)
        row = self.vikunja.tasks[1587]
        self.assertIn("Risk and rollback", row["description"])
        self.assertIn("Sandro manual verification", row["description"])
        self.assertEqual(row["description"].count(p.stable_marker("nix-config-pr-172-merge")), 1)
        self.assertFalse(row["done"])
        self.assertEqual(self.run_projection()["noop"], 1)
        self.assertEqual(len(self.state.read()["mappings"]), 1)

    def test_no_implicit_adoption_and_no_title_matching(self):
        self.kanban.tasks[self.native["id"]]["body"] = self.native["body"].replace(
            "Vikunja legacy task: 1587\n", ""
        )
        with self.assertRaises(p.InputError):
            self.run_projection()
        self.assertEqual(self.state.read()["pending"], {})
        self.assertEqual(self.vikunja.tasks[1587]["description"], self.legacy["description"])

    def test_foreign_creator_lineage_or_duplicate_marker_fail_before_intent(self):
        for field, value in [
            ("created_by", {"username": "foreign"}),
            ("description", self.legacy["description"].replace("t_05367b21", "t_11111111")),
            (
                "description",
                self.legacy["description"] + "\n" + p.stable_marker("nix-config-pr-172-merge"),
            ),
        ]:
            with self.subTest(field=field, value=value):
                self.vikunja.tasks[1587] = {**self.legacy, field: value}
                with self.assertRaises(p.InputError):
                    self.run_projection()
                self.assertEqual(self.state.read()["pending"], {})

    def test_response_lost_after_adoption_is_recovered_without_duplicate(self):
        real = self.vikunja.ensure_task

        def lose(desired):
            real(desired)
            raise RuntimeError("isolated lost response")

        self.vikunja.ensure_task = lose
        self.assertEqual(self.run_projection()["failed"], 1)
        self.assertEqual(len(self.state.read()["pending"]), 1)
        self.vikunja.ensure_task = real
        self.assertEqual(self.run_projection()["failed"], 0)
        self.assertEqual(len(self.vikunja.tasks), 1)
        self.assertEqual(self.state.read()["pending"], {})

    def test_verified_absence_creates_one_task(self):
        self.kanban.tasks[self.native["id"]]["body"] = "\n".join(
            line
            for line in self.native["body"].splitlines()
            if not line.startswith("Vikunja legacy ")
        )
        self.vikunja.tasks.clear()
        self.assertEqual(self.run_projection()["failed"], 0)
        self.assertEqual(len(self.vikunja.tasks), 1)
        self.assertEqual(self.run_projection()["noop"], 1)

    def test_missing_explicit_legacy_marker_cannot_create_replacement(self):
        self.vikunja.tasks.clear()
        with self.assertRaises(p.InputError):
            self.run_projection()
        self.assertEqual(self.vikunja.tasks, {})
        self.assertEqual(self.state.read()["pending"], {})

    def test_two_anchors_cannot_claim_one_pr(self):
        other = copy.deepcopy(self.native)
        other["id"] = "t_12341234"
        self.kanban.tasks[other["id"]] = other
        with self.assertRaises(p.InputError):
            self.run_projection()
        self.assertEqual(self.state.read()["pending"], {})
        self.assertEqual(self.vikunja.tasks[1587]["description"], self.legacy["description"])

    def test_typed_client_adoption_put_preserves_unowned_fields_and_refuses_drift(self):
        from pr_legacy_adoption import legacy_identity, preserve_record

        desired = p.desired_task(self.native, p.parse_human_action(self.native), self.kanban.board)
        row = {**self.legacy, "due_date": "2026-10-01T12:00:00Z"}
        proof = legacy_identity(self.native, row, desired)
        desired = {
            **preserve_record(desired, row, adopting=True),
            "legacy": proof,
            "target_id": 1587,
        }
        client = p.VikunjaClient("https://tasks.example.test", Path("/unused"), token="fixture")
        client._schema_validated = True
        client.owner_ids[110] = 1
        client.find_by_marker = lambda *args: [client._task_url(copy.deepcopy(row))]
        client.get_task = lambda task_id: client._task_url(copy.deepcopy(row))
        writes = []

        def put(path, payload):
            writes.append((path, payload))
            row.update(payload)
            if path.endswith("/assignees/bulk"):
                row["assignees"] = [
                    {**entry, "username": "sandro"} for entry in payload["assignees"]
                ]

        client.put = put
        actual = client.ensure_task(desired)
        p.verify_vikunja_task(actual, desired)
        self.assertEqual(writes[0][0], "/tasks/1587?format=markdown")
        self.assertEqual(writes[0][1]["due_date"], "2026-10-01T12:00:00Z")
        self.assertEqual(writes[1][0], "/tasks/1587/assignees/bulk")
        row["description"] = self.legacy["description"] + "\nChanged after intent"
        writes.clear()
        with self.assertRaises(p.InputError):
            client.ensure_task(desired)
        self.assertEqual(writes, [])
        client.find_by_marker = lambda *args: []
        with self.assertRaises(p.InputError):
            client.ensure_task(desired)
        self.assertEqual(writes, [])


class HumanRoutingTests(unittest.TestCase):
    def test_only_explicit_matching_gate_wakes_and_never_completes(self):
        anchor = kanban_task()
        anchor["body"] += "PR workflow: native-v1\n"
        child = {
            "id": "t_12345678",
            "status": "blocked",
            "assignee": "system-admin",
            "body": "PR projection anchor: homelab-devops/t_deadbeef\nPR head: abc\nHuman gate: secret-1\n",
        }
        kanban = FakeKanban([anchor, child])
        projected = {
            "description": "PR human continuation: homelab-devops/t_12345678\n"
            "PR human gate: secret-1\nCurrent PR head: abc\n"
        }
        route = p._human_route(kanban, anchor["id"], projected)
        p._wake_human_route(kanban, route)
        self.assertEqual(kanban.unblock_calls, [child["id"]])
        self.assertEqual(kanban.tasks[anchor["id"]]["status"], "blocked")
        kanban.tasks[child["id"]]["status"] = "blocked"
        kanban.tasks[child["id"]]["body"] = child["body"].replace("secret-1", "secret-2")
        p._wake_human_route(kanban, route)
        self.assertEqual(kanban.unblock_calls, [child["id"]])
        self.assertEqual(kanban.tasks[child["id"]]["status"], "blocked")
        self.assertFalse(
            p._human_route(kanban, anchor["id"], {"description": "no human action"})["wake"]
        )
