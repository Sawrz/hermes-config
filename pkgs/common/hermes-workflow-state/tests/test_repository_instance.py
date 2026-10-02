"""Explicit authority and scope bindings, using disposable generations only."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hermes_repository_instance import RepositoryInstance, RepositoryStore, instance, instance_scope
from hermes_workflow_state import GenerationStore, ProtocolError

FIXTURE = Path(__file__).parent / "fixtures/instance.json"


def config(**changes):
    value = json.loads(FIXTURE.read_text())
    value.update(changes)
    return RepositoryInstance.parse(value)


class InstanceTests(unittest.TestCase):
    def test_missing_authority_has_no_installation_defaults(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ProtocolError):
            instance()
        data = json.loads(FIXTURE.read_text())
        for key in data:
            with self.subTest(key=key), self.assertRaises(ProtocolError):
                RepositoryInstance.parse({k: v for k, v in data.items() if k != key})
        for changes in [
            {"repository": "../foreign"},
            {"default_branch": "main^{commit}"},
            {"roles": {"implementer": "same", "reviewer": "same"}},
            {"forgejo_origin": "https://user:secret@example.test"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                config(**changes)

    def test_distinct_instance_roles_sources_and_targets(self):
        second = config(
            id="second",
            repository="widgets/widget",
            default_branch="trunk",
            namespace="widget",
            board="engineering",
            tenant="engineering",
            roles={"implementer": "builder", "reviewer": "auditor"},
            forgejo_origin="https://code.example.test",
            automation_authors=["robot"],
            projection={
                "origin": "https://tasks.example.test",
                "project": 72,
                "owner": "human",
                "title": "Decisions",
            },
        )
        with instance_scope(second):
            self.assertEqual(instance().repository, "widgets/widget")
            self.assertEqual(instance().phase_owners()["review"], "auditor")
            self.assertEqual(instance().human_project, 72)
            instance().check_origin("https://code.example.test", "forgejo")
            with self.assertRaises(ProtocolError):
                instance().check_origin("https://git.example.test", "forgejo")

    def test_state_binding_cannot_be_replaced_or_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with instance_scope(config()):
                store = RepositoryStore(root, protocol="fixture", schema_version=1)
                generation = store.publish({"pending.json": {"id": "preserved", "ack": False}})
            for changes in [
                {"id": "other"},
                {"repository": "widgets/widget"},
                {"forgejo_origin": "https://other.example.test"},
                {"namespace": "other"},
                {"default_branch": "trunk"},
                {"roles": {"implementer": "builder", "reviewer": "auditor"}},
                {
                    "projection": {
                        "origin": "https://vikunja.example.test",
                        "project": 72,
                        "owner": "human",
                        "title": "Other",
                    }
                },
            ]:
                with instance_scope(config(**changes)), self.subTest(changes=changes):
                    other = RepositoryStore(root, protocol="fixture", schema_version=1)
                    for action in [
                        other.read,
                        lambda: other.publish({"pending.json": {}}),
                        other.cleanup,
                    ]:
                        with self.assertRaises(ProtocolError):
                            action()
            with instance_scope(config()):
                self.assertEqual(store.read().generation, generation)
                self.assertEqual(store.read().documents["pending.json"]["id"], "preserved")

    def test_legacy_adoption_requires_pin_and_domain_evidence_and_preserves_every_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = GenerationStore(root, protocol="fixture", schema_version=1)
            docs = {
                "pending.json": {"intent": "unchanged"},
                "acks.json": ["old-ack"],
                "mapping.json": {"task": 89},
            }
            pin = old.publish(docs)

            def validate(value):
                if value != docs:
                    raise ProtocolError("legacy domain identity differs")

            with instance_scope(config()):
                store = RepositoryStore(
                    root, protocol="fixture", schema_version=1, legacy_validator=validate
                )
                with self.assertRaises(ProtocolError):
                    store.read()
            with instance_scope(config(legacy_generations={"fixture": pin})):
                # A pin alone does not skip the adapter's schema/source validator.
                with self.assertRaises(ProtocolError):
                    RepositoryStore(root, protocol="fixture", schema_version=1).read()
                bad = RepositoryStore(
                    root,
                    protocol="fixture",
                    schema_version=1,
                    legacy_validator=lambda _: (_ for _ in ()).throw(ProtocolError("bad source")),
                )
                with self.assertRaises(ProtocolError):
                    bad.publish({"replacement.json": {}})
                self.assertEqual(old.read().generation, pin)
                store = RepositoryStore(
                    root, protocol="fixture", schema_version=1, legacy_validator=validate
                )
                adopted = store.read()
                self.assertEqual(adopted.documents, docs)
                self.assertNotEqual(adopted.generation, pin)
                self.assertEqual(store.read(), adopted)
                self.assertIn("instance.json", old.read().documents)

    def test_crash_during_legacy_adoption_retries_same_data(self):
        for fault_stage in ["after_generation_publish", "after_selector_replace"]:
            with self.subTest(stage=fault_stage), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                old = GenerationStore(root, protocol="fixture", schema_version=1)
                docs = {"pending.json": {"intent": "keep"}}
                pin = old.publish(docs)
                with instance_scope(config(legacy_generations={"fixture": pin})):

                    def fault(stage):
                        if stage == fault_stage:
                            raise RuntimeError("fixture crash")

                    store = RepositoryStore(
                        root,
                        protocol="fixture",
                        schema_version=1,
                        legacy_validator=lambda value: self.assertEqual(value, docs),
                        fault=fault,
                    )
                    try:
                        store.read()
                    except RuntimeError:
                        pass
                    recovered = RepositoryStore(
                        root,
                        protocol="fixture",
                        schema_version=1,
                        legacy_validator=lambda value: self.assertEqual(value, docs),
                    ).read()
                    self.assertEqual(recovered.documents, docs)


if __name__ == "__main__":
    unittest.main()
