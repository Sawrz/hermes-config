"""Run the same lifecycle implementation with two explicit contracts."""

import copy
from dataclasses import replace
import tempfile
from pathlib import Path
import unittest

from hermes_repository_instance import instance, instance_scope
from hermes_workflow_state import ProtocolError
from kanban_vikunja_projection import ProjectionState, parse_human_action
from forgejo_kanban_workflow import pr_actionable_digest
from pr_projection_lifecycle import PrLifecycle
from test_pr_lifecycle import Native, snapshot, HEAD


class InstanceLifecycleTests(unittest.TestCase):
    def test_legacy_projection_requires_source_evidence_and_exact_project(self):
        from kanban_vikunja_projection import validate_legacy_projection, _empty_state, InputError

        with self.assertRaisesRegex(InputError, "no board/project identity"):
            validate_legacy_projection({"edge.json": _empty_state()})
        state = _empty_state()
        state["pending"]["fixture"] = {
            "kind": "ensure_task",
            "source": instance().board + "/t_aaaaaaaa",
            "desired": {"project_id": instance().human_project},
        }
        self.assertEqual(validate_legacy_projection({"edge.json": state}), state)
        state["pending"]["fixture"]["desired"]["project_id"] = 999
        with self.assertRaisesRegex(InputError, "contradicts"):
            validate_legacy_projection({"edge.json": state})

    def test_second_instance_review_handoff_head_change_and_state_isolation(self):
        first = instance()
        second = replace(
            first,
            identity="widgets",
            repository="acme/widgets",
            default_branch="trunk",
            namespace="widgets",
            workflow_key_prefix="forgejo-widgets",
            implementer="builder",
            reviewer="auditor",
            board="engineering",
            tenant="engineering",
            human_project=72,
            human_owner="alex",
            human_project_title="Decisions",
            forgejo_origin="https://code.example.test",
        )
        for cfg in (first, second):
            with (
                self.subTest(repository=cfg.repository),
                instance_scope(cfg),
                tempfile.TemporaryDirectory() as tmp,
            ):
                source = snapshot()
                source["url"] = f"{cfg.forgejo_origin}/{cfg.repository}/pulls/200"
                native = Native()
                impl = native.phase("implementation")
                review = native.phase("review", [impl], review_id=1)
                native.phase(
                    "handoff", [review], review_id=1, actionable_digest=pr_actionable_digest(source)
                )
                anchor_ref = cfg.board + "/t_aaaaaaaa"
                for row in native.rows.values():
                    phase = row["runs"][0]["metadata"]["pr_workflow"]["phase"]
                    owner = cfg.phase_owners()[phase]
                    row["task"]["assignee"] = owner
                    row["task"]["body"] = (
                        f"PR projection anchor: {anchor_ref}\nPR workflow phase: {phase}\nPR head: {HEAD}\nForgejo authority: {source['url']}\n"
                    )
                    row["runs"][0]["profile"] = owner
                    row["runs"][0]["metadata"]["pr_workflow"]["anchor"] = anchor_ref
                anchor = {
                    "id": "t_aaaaaaaa",
                    "assignee": cfg.implementer,
                    "title": "PR 200",
                    "body": f"PR workflow: native-v1\nForgejo authority: {source['url']}\nHuman action: Merge\nVikunja project: {cfg.human_project}\nVikunja assignee: none\nVikunja done: false\nVikunja automation reference: {cfg.namespace}-pr-200-merge\n",
                }
                action = parse_human_action(anchor)
                self.assertEqual(action["project_id"], cfg.human_project)
                forge = type(
                    "Forge", (), {"readiness_snapshot": lambda _, n: copy.deepcopy(source)}
                )()
                provider = PrLifecycle(forge, native, cfg.board)
                self.assertEqual(provider.resolve(anchor, action)[1]["assignee"], cfg.human_owner)
                source["head"] = "b" * 40
                self.assertIsNone(provider.resolve(anchor, action)[1]["assignee"])
                state = ProjectionState(Path(tmp), "kanban-to-vikunja")
                state.read()
                # Write the empty selected state using its normal update path.
                state.store.publish({"edge.json": state.read()})
                with instance_scope(second if cfg == first else first):
                    with self.assertRaises(ProtocolError):
                        ProjectionState(Path(tmp), "kanban-to-vikunja").read()
