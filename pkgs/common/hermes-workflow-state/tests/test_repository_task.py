"""Review routing binds repository, PR and reviewed head without touching state."""

import json
from pathlib import Path
import tempfile
import unittest

from hermes_repository_instance import (
    RepositoryStore,
    activate_instance,
    instance_scope,
    load_instance,
)
from hermes_repository_task import review_handoff
from hermes_workflow_state import ProtocolError


class ReviewRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = self.root / "registry"
        self.registry.mkdir()
        self.contract = json.loads((Path(__file__).parent / "fixtures/instance.json").read_text())
        self.contract["review_executor"] = "paperclip"
        self.contract["roles"] = {"implementer": "homelab-cto", "reviewer": "code-reviewer"}
        for name in ("nix-config", "hermes-config"):
            contract = self.contract | {
                "id": name,
                "namespace": name,
                "repository": f"nixos/{name}",
            }
            (self.registry / f"{name}.json").write_text(json.dumps(contract))

    def test_overlapping_pr_numbers_and_changed_heads_are_distinct(self):
        first = review_handoff(self.registry, "nixos/nix-config", 7, "a" * 40)
        other = review_handoff(self.registry, "nixos/hermes-config", 7, "a" * 40)
        changed = review_handoff(self.registry, "nixos/nix-config", 7, "b" * 40)
        self.assertEqual(first, review_handoff(self.registry, "nixos/nix-config", 7, "a" * 40))
        self.assertEqual(len({item["idempotency_key"] for item in (first, other, changed)}), 3)
        self.assertFalse(first["dispatched"])
        self.assertTrue(first["url"].endswith("/nixos/nix-config/pulls/7"))

    def test_wrong_repository_or_head_is_rejected(self):
        for repository, head in (("other/project", "a" * 40), ("nixos/nix-config", "main")):
            with self.assertRaises(ProtocolError):
                review_handoff(self.registry, repository, 7, head)

    def test_colliding_registry_is_rejected(self):
        (self.registry / "duplicate.json").write_bytes(
            (self.registry / "nix-config.json").read_bytes()
        )
        with self.assertRaisesRegex(ProtocolError, "colliding"):
            review_handoff(self.registry, "nixos/nix-config", 7, "a" * 40)

    def test_paperclip_cannot_reinterpret_existing_hermes_state(self):
        state = self.root / "state"
        state.mkdir()
        pending = state / "pending.json"
        pending.write_text('{"pending":true}')
        with instance_scope(load_instance(self.registry / "nix-config.json")):
            with self.assertRaisesRegex(ProtocolError, "cannot open Hermes workflow state"):
                RepositoryStore(state, protocol="fixture", schema_version=1)
        self.assertEqual(pending.read_text(), '{"pending":true}')
        self.assertEqual(list(state.iterdir()), [pending])

    def test_legacy_adapter_cannot_activate_paperclip_routing(self):
        with self.assertRaisesRegex(ProtocolError, "requires hermes review routing"):
            activate_instance(self.registry / "nix-config.json", review_executor="hermes")
