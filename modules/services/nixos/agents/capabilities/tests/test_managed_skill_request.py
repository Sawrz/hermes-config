"""Repository ownership and native task identity survive multi-repository routing."""

import copy
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).parents[2] / "skills/nix-managed-resource-changes/scripts/change_request.py"
spec = importlib.util.spec_from_file_location("request", SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class RequestTests(unittest.TestCase):
    def setUp(self):
        route = {
            "repository": "nixos/hermes-config",
            "board": "homelab-devops",
            "assignee": "system-admin",
            "mode": "kanban",
        }
        self.manifest = {
            "schema_version": 2,
            "profile": "alpha",
            "native_jobs": {},
            "skills": {
                "example": {
                    "category": "ops",
                    "provider": "example",
                    "source": {
                        "kind": "repository-directory",
                        "path": "skills/example",
                        "repository": "nixos/hermes-config",
                        "route": route,
                    },
                    "declarations": [],
                }
            },
        }
        self.request = {
            "change": "Clarify restore check",
            "reason": "Ambiguity",
            "evidence": ["Rehearsal"],
            "acceptance": ["Rehearsal passes"],
        }

    def plan(self, **kwargs):
        return helper.plan(self.manifest, "example", self.request, **kwargs)

    def test_reusable_source_routes_to_owner(self):
        plan = self.plan()
        self.assertIn("Repository: nixos/hermes-config", plan["arguments"]["body"])
        self.assertEqual(plan, self.plan())

    def test_wrong_repository_is_rejected(self):
        self.request["repository"] = "nixos/nix-config"
        with self.assertRaisesRegex(ValueError, "does not own"):
            self.plan()

    def test_same_resource_in_other_repository_has_distinct_key(self):
        first = self.plan()["arguments"]["idempotency_key"]
        source = self.manifest["skills"]["example"]["source"]
        source["repository"] = source["route"]["repository"] = "nixos/nix-config"
        self.assertNotEqual(first, self.plan()["arguments"]["idempotency_key"])

    def test_profile_selection_routes_to_deployment_declaration(self):
        skill = self.manifest["skills"]["example"]
        skill["source"] = {"kind": "inline", "path": None}
        skill["declarations"] = [
            {
                "repository": "nixos/nix-config",
                "path": "hosts/host.nix",
                "route": {
                    "repository": "nixos/nix-config",
                    "board": "homelab-devops",
                    "assignee": "system-admin",
                },
            }
        ]
        self.assertIn("Repository: nixos/nix-config", self.plan()["arguments"]["body"])

    def test_ambiguous_ownership_requires_explicit_selection(self):
        skill = self.manifest["skills"]["example"]
        source = skill["source"]
        skill["source"] = {"kind": "inline", "path": None}
        other = copy.deepcopy(source)
        other["repository"] = other["route"]["repository"] = "nixos/nix-config"
        skill["declarations"] = [source, other]
        with self.assertRaisesRegex(ValueError, "explicit"):
            self.plan()

    def test_existing_request_reused_and_wrong_task_rejected(self):
        plan = self.plan()
        task = {
            "task": {
                "id": "task-1",
                "assignee": "system-admin",
                "body": plan["arguments"]["body"],
                "status": "running",
            }
        }
        self.assertEqual(self.plan(known_task=task)["action"], "reuse")
        self.assertEqual(self.plan(known_task=task, current_task="task-1")["action"], "implement")
        task["task"]["body"] = "Another repository has issue #1"
        with self.assertRaises(ValueError):
            self.plan(known_task=task)

    def test_manual_route_does_not_dispatch(self):
        self.manifest["skills"]["example"]["source"]["route"]["mode"] = "manual"
        self.assertEqual(self.plan()["action"], "handoff")
        with self.assertRaises(ValueError):
            self.plan(known_task={"task": {}})

    def test_missing_manifest_fails_closed(self):
        for value in [None, {}, {"schema_version": 1}]:
            with self.assertRaises(ValueError):
                helper.plan(value, "example", self.request)

    def test_unknown_skill_requires_provenance_inspection(self):
        self.assertEqual(
            helper.plan(self.manifest, "local", self.request)["action"], "inspect_provenance"
        )

    def test_invalid_evidence_is_rejected(self):
        self.request["evidence"] = []
        with self.assertRaises(ValueError):
            self.plan()


if __name__ == "__main__":
    unittest.main()
