from __future__ import annotations

import copy
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

SOURCE = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SOURCE))

import hermes_workflow_state as state_module  # noqa: E402
from hermes_workflow_state import (  # noqa: E402
    DirectionalWork,
    GenerationStore,
    LockUnavailable,
    ProtocolError,
    RetryPolicy,
    SelectedGeneration,
    StateAbsent,
    exclusive_lock,
    load_json,
    render_metrics,
    validate_preferences,
)


def documents(value: str) -> dict[str, object]:
    return {
        "cursor.json": {"position": value},
        "pending.json": {"events": [{"id": value}]},
        "receipts.json": {"receipts": []},
    }


def lock_holder(root: str, ready: multiprocessing.Queue) -> None:
    with exclusive_lock(Path(root), timeout=1):
        ready.put(True)
        time.sleep(1)


class GenerationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def store(self, root: Path | None = None, fault=None) -> GenerationStore:
        return GenerationStore(root or self.root, protocol="fixture", schema_version=1, fault=fault)

    def test_absent_state_is_distinct_from_partial_state(self) -> None:
        with self.assertRaises(StateAbsent):
            self.store().read()
        self.root.mkdir()
        (self.root / "generations").mkdir()
        with self.assertRaisesRegex(ProtocolError, "selector"):
            self.store().read()

    def test_publish_and_read_complete_generation(self) -> None:
        generation = self.store().publish(documents("one"))
        selected = self.store().read()
        self.assertEqual(selected.generation, generation)
        self.assertEqual(selected.documents, documents("one"))
        self.assertRegex(generation, r"^g-[0-9a-f]{64}$")

    def test_identical_publication_is_idempotent(self) -> None:
        first = self.store().publish(documents("one"))
        second = self.store().publish(documents("one"))
        self.assertEqual(first, second)
        generations = [
            p.name for p in (self.root / "generations").iterdir() if p.name.startswith("g-")
        ]
        self.assertEqual(generations, [first])

    def test_retry_existing_generation_fsyncs_parent_before_selector(self) -> None:
        def fail(label: str) -> None:
            if label == "after_generation_publish":
                raise RuntimeError("injected crash")

        with self.assertRaises(RuntimeError):
            self.store(fault=fail).publish(documents("one"))
        calls: list[Path] = []
        original = state_module._fsync_dir

        def record(path: Path) -> None:
            calls.append(path)
            original(path)

        with mock.patch.object(state_module, "_fsync_dir", side_effect=record):
            self.store().publish(documents("one"))
        generations = self.root / "generations"
        self.assertIn(generations, calls)
        self.assertLess(calls.index(generations), calls.index(self.root))

    def test_crash_boundaries_select_complete_old_or_new_generation(self) -> None:
        old_generation = self.store().publish(documents("old"))
        baseline = Path(self.temp.name) / "baseline"
        shutil.copytree(self.root, baseline)
        boundaries = [
            "after_document:cursor.json",
            "after_document:pending.json",
            "after_document:receipts.json",
            "after_manifest",
            "after_generation_fsync",
            "after_generation_publish",
            "after_selector_replace",
            "after_selector_fsync",
        ]
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                case = Path(self.temp.name) / ("case-" + boundary.replace(":", "-"))
                shutil.copytree(baseline, case)

                def fail(label: str, expected: str = boundary) -> None:
                    if label == expected:
                        raise RuntimeError("injected crash")

                with self.assertRaisesRegex(RuntimeError, "injected crash"):
                    self.store(case, fail).publish(documents("new"))
                selected = self.store(case).read()
                expected = (
                    "new"
                    if boundary in {"after_selector_replace", "after_selector_fsync"}
                    else "old"
                )
                self.assertEqual(selected.documents, documents(expected))
                if expected == "old":
                    self.assertEqual(selected.generation, old_generation)

    def test_update_recovers_complete_unselected_first_generation(self) -> None:
        def fail(label: str) -> None:
            if label == "after_generation_publish":
                raise RuntimeError("injected crash")

        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.store(fault=fail).publish(documents("first"))

        observed: list[SelectedGeneration | None] = []

        def transform(current: SelectedGeneration | None) -> dict[str, object]:
            observed.append(current)
            return documents("second")

        selected = self.store().update(transform)
        first = observed[0]
        assert first is not None
        self.assertEqual(first.documents, documents("first"))
        self.assertEqual(selected.documents, documents("second"))
        self.assertEqual(self.store().read().documents, documents("second"))

    def test_update_retries_every_pre_rename_first_publication_crash(self) -> None:
        boundaries = [
            "after_document:cursor.json",
            "after_document:pending.json",
            "after_document:receipts.json",
            "after_manifest",
            "after_generation_fsync",
        ]
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                case = Path(self.temp.name) / ("retry-" + boundary.replace(":", "-"))

                def fail(label: str, expected: str = boundary) -> None:
                    if label == expected:
                        raise RuntimeError("injected crash")

                with self.assertRaisesRegex(RuntimeError, "injected crash"):
                    self.store(case, fail).publish(documents("first"))
                with self.assertRaisesRegex(ProtocolError, "selector"):
                    self.store(case).read()
                self.assertEqual(
                    {entry.name for entry in case.iterdir() if entry.name != ".lock"},
                    {"generations"},
                )
                self.assertEqual(list((case / "generations").iterdir()), [])

                observed: list[SelectedGeneration | None] = []

                def transform(current: SelectedGeneration | None) -> dict[str, object]:
                    observed.append(current)
                    return documents("retry")

                selected = self.store(case).update(transform)
                self.assertEqual(observed, [None])
                self.assertEqual(selected.documents, documents("retry"))
                self.assertEqual(self.store(case).read().documents, documents("retry"))

    def test_update_retries_real_process_death_at_every_pre_rename_boundary(self) -> None:
        boundaries = [
            "after_temporary_generation",
            "after_document_stage:cursor.json",
            "after_document:cursor.json",
            "after_document_stage:pending.json",
            "after_document:pending.json",
            "after_document_stage:receipts.json",
            "after_document:receipts.json",
            "after_manifest_stage",
            "after_manifest",
            "after_generation_fsync",
        ]
        child = """
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_workflow_state import GenerationStore

root = Path(sys.argv[2])
boundary = sys.argv[3]
payload = json.loads(sys.argv[4])

def die(label):
    if label == boundary:
        os._exit(73)

GenerationStore(root, protocol="fixture", schema_version=1, fault=die).publish(payload)
"""
        payload = json.dumps(documents("first"))
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                case = Path(self.temp.name) / ("process-death-" + boundary.replace(":", "-"))
                crashed = subprocess.run(
                    [sys.executable, "-c", child, str(SOURCE), str(case), boundary, payload],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(crashed.returncode, 73, crashed.stderr + crashed.stdout)
                with self.assertRaisesRegex(ProtocolError, "selector"):
                    self.store(case).read()
                residue = list((case / "generations").iterdir())
                self.assertEqual(len(residue), 1)
                self.assertRegex(residue[0].name, r"^\.tmp-[0-9a-f]{32}$")

                selected = self.store(case).update(lambda current: documents("retry"))

                self.assertEqual(selected.documents, documents("retry"))
                self.assertEqual(self.store(case).read().documents, documents("retry"))
                self.assertFalse(
                    any(p.name.startswith(".tmp-") for p in (case / "generations").iterdir())
                )

    def test_update_retries_process_death_at_every_temporary_cleanup_boundary(self) -> None:
        publisher = """
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_workflow_state import GenerationStore

root = Path(sys.argv[2])
payload = json.loads(sys.argv[3])

def die(label):
    if label == "after_generation_fsync":
        os._exit(73)

GenerationStore(root, protocol="fixture", schema_version=1, fault=die).publish(payload)
"""
        recoverer = """
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_workflow_state import GenerationStore

root = Path(sys.argv[2])
boundary = sys.argv[3]
payload = json.loads(sys.argv[4])

def die(label):
    if label == boundary:
        os._exit(74)

GenerationStore(root, protocol="fixture", schema_version=1, fault=die).update(
    lambda current: payload
)
"""
        boundaries = [
            "before_cleanup_tombstone_rename",
            "after_cleanup_tombstone_rename",
            "after_cleanup_tombstone_fsync",
            "before_cleanup_tombstone_open",
            "after_cleanup_tombstone_validation",
            "after_cleanup_unlink:cursor.json",
            "after_cleanup_unlink:manifest.json",
            "after_cleanup_unlink:pending.json",
            "after_cleanup_unlink:receipts.json",
            "after_cleanup_tombstone_remove",
            "after_cleanup_fsync",
        ]
        payload = json.dumps(documents("first"))
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                case = Path(self.temp.name) / ("cleanup-death-" + boundary.replace(":", "-"))
                crashed_publisher = subprocess.run(
                    [sys.executable, "-c", publisher, str(SOURCE), str(case), payload],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    crashed_publisher.returncode,
                    73,
                    crashed_publisher.stderr + crashed_publisher.stdout,
                )
                crashed_recoverer = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        recoverer,
                        str(SOURCE),
                        str(case),
                        boundary,
                        payload,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    crashed_recoverer.returncode,
                    74,
                    crashed_recoverer.stderr + crashed_recoverer.stdout,
                )
                with self.assertRaisesRegex(ProtocolError, "selector"):
                    self.store(case).read()

                selected = self.store(case).update(lambda current: documents("retry"))

                self.assertEqual(selected.documents, documents("retry"))
                self.assertEqual(self.store(case).read().documents, documents("retry"))
                residue = [
                    path.name
                    for path in (case / "generations").iterdir()
                    if path.name.startswith((".tmp-", ".cleanup-"))
                ]
                self.assertEqual(residue, [])

    def test_cleanup_tombstone_recovery_rejects_unsafe_or_malformed_layouts(self) -> None:
        tombstone_name = ".cleanup-" + "a" * 32

        def make_case(name: str) -> tuple[Path, Path]:
            case = Path(self.temp.name) / ("cleanup-tombstone-" + name)
            generations = case / "generations"
            generations.mkdir(parents=True)
            return case, generations

        cases = []

        case, generations = make_case("malformed-document")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        (tombstone / "pending.json").write_text('{"events":', encoding="utf-8")
        cases.append(case)

        case, generations = make_case("incompatible-manifest")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        (tombstone / "manifest.json").write_bytes(
            state_module.canonical_json_bytes(
                {"protocol": "other", "schema_version": 1, "documents": {}}
            )
        )
        cases.append(case)

        case, generations = make_case("malformed-manifest-metadata")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        (tombstone / "manifest.json").write_bytes(
            state_module.canonical_json_bytes(
                {
                    "protocol": "fixture",
                    "schema_version": 1,
                    "documents": {"pending.json": {"sha256": "bad", "size": -1}},
                }
            )
        )
        cases.append(case)

        case, generations = make_case("surviving-document-mismatch")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        pending_raw = state_module.canonical_json_bytes({"events": []})
        (tombstone / "pending.json").write_bytes(pending_raw)
        (tombstone / "manifest.json").write_bytes(
            state_module.canonical_json_bytes(
                {
                    "protocol": "fixture",
                    "schema_version": 1,
                    "documents": {"pending.json": {"sha256": "0" * 64, "size": len(pending_raw)}},
                }
            )
        )
        cases.append(case)

        case, generations = make_case("manifest-deletion-gap")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        (tombstone / "pending.json").write_bytes(pending_raw)
        receipts_raw = state_module.canonical_json_bytes({"receipts": []})
        (tombstone / "manifest.json").write_bytes(
            state_module.canonical_json_bytes(
                {
                    "protocol": "fixture",
                    "schema_version": 1,
                    "documents": {
                        "pending.json": {
                            "sha256": state_module._sha256(pending_raw),
                            "size": len(pending_raw),
                        },
                        "receipts.json": {
                            "sha256": state_module._sha256(receipts_raw),
                            "size": len(receipts_raw),
                        },
                    },
                }
            )
        )
        cases.append(case)

        case, generations = make_case("manifest-aggregate-overflow")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        (tombstone / "manifest.json").write_bytes(
            state_module.canonical_json_bytes(
                {
                    "protocol": "fixture",
                    "schema_version": 1,
                    "documents": {
                        f"part{index}.json": {
                            "sha256": "0" * 64,
                            "size": state_module.MAX_DOCUMENT_BYTES,
                        }
                        for index in range(9)
                    },
                }
            )
        )
        cases.append(case)

        case, generations = make_case("reserved-manifest-row")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        (tombstone / "manifest.json").write_bytes(
            state_module.canonical_json_bytes(
                {
                    "protocol": "fixture",
                    "schema_version": 1,
                    "documents": {"manifest.json": {"sha256": "0" * 64, "size": 1}},
                }
            )
        )
        cases.append(case)

        case, generations = make_case("boolean-schema-version")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        (tombstone / "manifest.json").write_bytes(
            state_module.canonical_json_bytes(
                {
                    "protocol": "fixture",
                    "schema_version": True,
                    "documents": {"pending.json": {"sha256": "0" * 64, "size": 1}},
                }
            )
        )
        cases.append(case)

        case, generations = make_case("manifest-with-staged-document")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        (tombstone / ".pending.json.tmp").write_text("partial", encoding="utf-8")
        (tombstone / "manifest.json").write_bytes(
            state_module.canonical_json_bytes(
                {
                    "protocol": "fixture",
                    "schema_version": 1,
                    "documents": {"pending.json": {"sha256": "0" * 64, "size": 1}},
                }
            )
        )
        cases.append(case)

        case, generations = make_case("staged-manifest-without-documents")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        (tombstone / ".manifest.json.tmp").write_text("partial", encoding="utf-8")
        cases.append(case)

        case, generations = make_case("staged-document-before-finalized-document")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        (tombstone / ".a.json.tmp").write_text("partial", encoding="utf-8")
        (tombstone / "z.json").write_bytes(state_module.canonical_json_bytes({"z": 1}))
        cases.append(case)

        case, generations = make_case("manifest-free-aggregate-overflow")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        large_raw = state_module.canonical_json_bytes(
            {"payload": "x" * (state_module.MAX_DOCUMENT_BYTES - 32)}
        )
        for index in range(9):
            (tombstone / f"part{index}.json").write_bytes(large_raw)
        cases.append(case)

        case, generations = make_case("unsafe-child")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        (tombstone / "unexpected").write_text("data", encoding="utf-8")
        cases.append(case)

        case, generations = make_case("symlink-child")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        outside = Path(self.temp.name) / "cleanup-tombstone-outside"
        outside.write_text("data", encoding="utf-8")
        (tombstone / "pending.json").symlink_to(outside)
        cases.append(case)

        case, generations = make_case("special-child")
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        os.mkfifo(tombstone / "pending.json")
        cases.append(case)

        case, generations = make_case("symlink-tombstone")
        outside_directory = Path(self.temp.name) / "cleanup-tombstone-outside-directory"
        outside_directory.mkdir()
        (generations / tombstone_name).symlink_to(outside_directory, target_is_directory=True)
        cases.append(case)

        case, generations = make_case("special-tombstone")
        os.mkfifo(generations / tombstone_name)
        cases.append(case)

        case, generations = make_case("unsafe-name")
        (generations / ".cleanup-unsafe").mkdir()
        cases.append(case)

        case, generations = make_case("multiple")
        (generations / tombstone_name).mkdir()
        (generations / (".cleanup-" + "b" * 32)).mkdir()
        cases.append(case)

        case, generations = make_case("mixed")
        (generations / tombstone_name).mkdir()
        (generations / (".tmp-" + "b" * 32)).mkdir()
        cases.append(case)

        for case in cases:
            with self.subTest(case=case.name):
                with self.assertRaisesRegex(
                    ProtocolError,
                    "selector|cleanup|ambiguous|unsafe|symlink|regular file|malformed|staged|publication",
                ):
                    self.store(case).update(lambda _current: documents("retry"))

    def test_temporary_cleanup_rejects_unreachable_staged_layouts(self) -> None:
        layouts = {
            "staged-manifest-without-documents": {".manifest.json.tmp": b"partial"},
            "staged-document-before-finalized-document": {
                ".a.json.tmp": b"partial",
                "z.json": state_module.canonical_json_bytes({"z": 1}),
            },
        }
        for name, files in layouts.items():
            with self.subTest(name=name):
                case = Path(self.temp.name) / ("temporary-unreachable-" + name)
                temporary = case / "generations" / (".tmp-" + "a" * 32)
                temporary.mkdir(parents=True)
                for filename, raw in files.items():
                    (temporary / filename).write_bytes(raw)
                with self.assertRaisesRegex(ProtocolError, "staged.*document|publication order"):
                    self.store(case).update(lambda _current: documents("retry"))

    def test_manifest_free_tombstone_still_respects_generated_manifest_bound(self) -> None:
        case = Path(self.temp.name) / "cleanup-tombstone-generated-manifest-bound"
        tombstone = case / "generations" / (".cleanup-" + "a" * 32)
        tombstone.mkdir(parents=True)
        for index in range(4):
            (tombstone / f"part{index}.json").write_bytes(
                state_module.canonical_json_bytes({"part": index})
            )

        updater_called = False

        def updater(_current: Any) -> dict[str, Any]:
            nonlocal updater_called
            updater_called = True
            return documents("retry")

        with mock.patch.object(state_module, "MAX_MANIFEST_BYTES", 256):
            with self.assertRaisesRegex(ProtocolError, "manifest.*byte limit"):
                self.store(case).update(updater)
        self.assertFalse(updater_called)

    def test_temporary_generation_replacement_before_tombstone_rename_is_rejected(
        self,
    ) -> None:
        case = Path(self.temp.name) / "temporary-generation-replacement"
        generations = case / "generations"
        generations.mkdir(parents=True)
        temporary = generations / (".tmp-" + "a" * 32)
        temporary.mkdir()
        (temporary / "pending.json").write_bytes(state_module.canonical_json_bytes({"events": []}))
        replacement = Path(self.temp.name) / "replacement-temporary-generation"
        replacement.mkdir()
        replacement_file = replacement / "pending.json"
        replacement_file.write_text("must survive", encoding="utf-8")
        orphan = generations / ".attacker-moved-temporary"
        replaced = False

        def replace_temporary(label: str) -> None:
            nonlocal replaced
            if label == "before_cleanup_tombstone_rename":
                temporary.rename(orphan)
                replacement.rename(temporary)
                replaced = True

        with self.assertRaisesRegex(ProtocolError, "temporary generation changed"):
            self.store(case, replace_temporary).update(lambda _current: documents("retry"))
        self.assertTrue(replaced)
        self.assertEqual((temporary / "pending.json").read_text(encoding="utf-8"), "must survive")

    def test_cleanup_tombstone_replacement_before_open_is_rejected(self) -> None:
        case = Path(self.temp.name) / "cleanup-tombstone-replacement-before-open"
        generations = case / "generations"
        generations.mkdir(parents=True)
        tombstone_name = ".cleanup-" + "a" * 32
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        (tombstone / "pending.json").write_bytes(state_module.canonical_json_bytes({"events": []}))
        replacement = Path(self.temp.name) / "replacement-cleanup-tombstone"
        replacement.mkdir()
        replacement_file = replacement / "pending.json"
        replacement_file.write_bytes(state_module.canonical_json_bytes({"replacement": True}))
        orphan = generations / ".attacker-moved-cleanup"
        replaced = False

        def replace_tombstone(label: str) -> None:
            nonlocal replaced
            if label == "before_cleanup_tombstone_open":
                tombstone.rename(orphan)
                replacement.rename(tombstone)
                replaced = True

        with self.assertRaisesRegex(ProtocolError, "cleanup tombstone changed before removal"):
            self.store(case, replace_tombstone).update(lambda _current: documents("retry"))
        self.assertTrue(replaced)
        self.assertTrue((tombstone / "pending.json").exists())
        self.assertFalse((case / "current.json").exists())

    def test_cleanup_tombstone_path_replacement_cannot_escape_generation_directory(self) -> None:
        case = Path(self.temp.name) / "cleanup-tombstone-path-replacement"
        generations = case / "generations"
        generations.mkdir(parents=True)
        tombstone = generations / (".cleanup-" + "a" * 32)
        tombstone.mkdir()
        (tombstone / "pending.json").write_bytes(state_module.canonical_json_bytes({"events": []}))
        outside = Path(self.temp.name) / "cleanup-outside"
        outside.mkdir()
        outside_file = outside / "pending.json"
        outside_file.write_text("must survive", encoding="utf-8")
        orphan = generations / ".attacker-moved-tombstone"
        replaced = False

        def replace_path(label: str) -> None:
            nonlocal replaced
            if label == "after_cleanup_tombstone_validation":
                tombstone.rename(orphan)
                tombstone.symlink_to(outside, target_is_directory=True)
                replaced = True

        with self.assertRaisesRegex(ProtocolError, "cleanup.*changed|symlink"):
            self.store(case, replace_path).update(lambda _current: documents("retry"))
        self.assertTrue(replaced)
        self.assertEqual(outside_file.read_text(encoding="utf-8"), "must survive")

    def test_cleanup_tombstone_parent_replacement_is_rejected_before_open(self) -> None:
        case = Path(self.temp.name) / "cleanup-tombstone-parent-replacement"
        generations = case / "generations"
        generations.mkdir(parents=True)
        tombstone_name = ".cleanup-" + "a" * 32
        tombstone = generations / tombstone_name
        tombstone.mkdir()
        replacement = Path(self.temp.name) / "attacker-generations"
        replacement.mkdir()
        replacement_tombstone = replacement / tombstone_name
        replacement_tombstone.mkdir()
        outside_file = replacement_tombstone / "pending.json"
        outside_file.write_text("must survive", encoding="utf-8")
        orphan = case / "original-generations"
        replaced = False

        def replace_parent(label: str) -> None:
            nonlocal replaced
            if label == "before_cleanup_tombstone_open":
                generations.rename(orphan)
                replacement.rename(generations)
                replaced = True

        with self.assertRaisesRegex(ProtocolError, "cleanup.*parent.*changed"):
            self.store(case, replace_parent).update(lambda _current: documents("retry"))
        self.assertTrue(replaced)
        replacement_file = generations / tombstone_name / "pending.json"
        self.assertEqual(replacement_file.read_text(encoding="utf-8"), "must survive")

    def test_cleanup_tombstone_child_replacement_is_not_deleted(self) -> None:
        case = Path(self.temp.name) / "cleanup-tombstone-child-replacement"
        generations = case / "generations"
        generations.mkdir(parents=True)
        tombstone = generations / (".cleanup-" + "a" * 32)
        tombstone.mkdir()
        child = tombstone / "pending.json"
        child.write_bytes(state_module.canonical_json_bytes({"events": []}))
        replaced = False

        def replace_child(label: str) -> None:
            nonlocal replaced
            if label == "after_cleanup_tombstone_validation":
                child.unlink()
                child.write_bytes(state_module.canonical_json_bytes({"replacement": True}))
                replaced = True

        with self.assertRaisesRegex(ProtocolError, "cleanup.*child.*changed"):
            self.store(case, replace_child).update(lambda _current: documents("retry"))
        self.assertTrue(replaced)
        self.assertTrue(child.exists())

    def test_cleanup_tombstone_parent_replacement_after_open_is_rejected(self) -> None:
        case = Path(self.temp.name) / "cleanup-tombstone-parent-replacement-after-open"
        generations = case / "generations"
        generations.mkdir(parents=True)
        tombstone = generations / (".cleanup-" + "a" * 32)
        tombstone.mkdir()
        replacement = Path(self.temp.name) / "attacker-generations-after-open"
        replacement.mkdir()
        sentinel = replacement / "sentinel"
        sentinel.write_text("must survive", encoding="utf-8")
        orphan = case / "original-generations-after-open"
        replaced = False

        def replace_parent(label: str) -> None:
            nonlocal replaced
            if label == "after_cleanup_tombstone_validation":
                generations.rename(orphan)
                replacement.rename(generations)
                replaced = True

        with self.assertRaisesRegex(ProtocolError, "cleanup.*parent.*changed"):
            self.store(case, replace_parent).update(lambda _current: documents("retry"))
        self.assertTrue(replaced)
        self.assertEqual((generations / "sentinel").read_text(encoding="utf-8"), "must survive")
        self.assertFalse((case / "current.json").exists())

    def test_update_retries_process_death_during_document_stage_write(self) -> None:
        child = """
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import hermes_workflow_state as state

root = Path(sys.argv[2])
payload = json.loads(sys.argv[3])

def die_during_write(path, raw, mode=0o640):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    os.write(fd, raw[:max(1, len(raw) // 2)])
    os._exit(73)

state._write_new = die_during_write
state.GenerationStore(root, protocol="fixture", schema_version=1).publish(payload)
"""
        case = Path(self.temp.name) / "process-death-during-document-stage"
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(SOURCE), str(case), json.dumps(documents("first"))],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(crashed.returncode, 73, crashed.stderr + crashed.stdout)
        selected = self.store(case).update(lambda current: documents("retry"))
        self.assertEqual(selected.documents, documents("retry"))
        self.assertEqual(self.store(case).read().documents, documents("retry"))

    def test_update_does_not_recover_ambiguous_unselected_generations(self) -> None:
        def fail(label: str) -> None:
            if label == "after_generation_publish":
                raise RuntimeError("injected crash")

        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.store(fault=fail).publish(documents("first"))
        source = next((self.root / "generations").glob("g-*"))
        duplicate = self.root / "generations" / ("g-" + "f" * 64)
        shutil.copytree(source, duplicate)
        with self.assertRaisesRegex(ProtocolError, "selector"):
            self.store().update(lambda _current: documents("second"))

    def test_empty_retry_recovery_still_rejects_partial_and_unsafe_layouts(self) -> None:
        cases = {
            "extra-root-entry": lambda root: (root / "unexpected.json").write_text("{}"),
            "partial-generation": lambda root: (root / "generations" / ("g-" + "a" * 64)).mkdir(),
            "unsafe-generation-name": lambda root: (root / "generations" / "partial").mkdir(),
            "stranded-temporary": lambda root: (root / "generations" / ".tmp-stranded").mkdir(),
            "symlinked-generation": lambda root: (
                root / "generations" / ("g-" + "b" * 64)
            ).symlink_to(root / "generations", target_is_directory=True),
            "special-generation": lambda root: os.mkfifo(root / "generations" / ("g-" + "c" * 64)),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                case = Path(self.temp.name) / ("unsafe-" + name)
                (case / "generations").mkdir(parents=True)
                mutate(case)
                with self.assertRaisesRegex(ProtocolError, "selector|manifest|ambiguous|unsafe"):
                    self.store(case).update(lambda _current: documents("retry"))

    def test_temporary_recovery_rejects_invalid_ambiguous_and_mixed_residue(self) -> None:
        temporary_name = ".tmp-" + "a" * 32

        def make_case(name: str) -> tuple[Path, Path]:
            case = Path(self.temp.name) / ("temporary-" + name)
            generations = case / "generations"
            generations.mkdir(parents=True)
            return case, generations

        cases = []

        case, generations = make_case("malformed")
        temporary = generations / temporary_name
        temporary.mkdir()
        (temporary / "cursor.json").write_text('{"position":', encoding="utf-8")
        cases.append(case)

        case, generations = make_case("symlink-child")
        temporary = generations / temporary_name
        temporary.mkdir()
        outside = Path(self.temp.name) / "temporary-outside.json"
        outside.write_text("{}", encoding="utf-8")
        (temporary / "cursor.json").symlink_to(outside)
        cases.append(case)

        case, generations = make_case("symlink-temporary")
        target = Path(self.temp.name) / "temporary-outside-directory"
        target.mkdir()
        (generations / temporary_name).symlink_to(target, target_is_directory=True)
        cases.append(case)

        case, generations = make_case("special-temporary")
        os.mkfifo(generations / temporary_name)
        cases.append(case)

        case, generations = make_case("special-child")
        temporary = generations / temporary_name
        temporary.mkdir()
        os.mkfifo(temporary / "cursor.json")
        cases.append(case)

        case, generations = make_case("mismatched-manifest")
        temporary = generations / temporary_name
        temporary.mkdir()
        (temporary / "cursor.json").write_bytes(
            state_module.canonical_json_bytes({"position": "one"})
        )
        (temporary / "manifest.json").write_bytes(state_module.canonical_json_bytes({"version": 1}))
        cases.append(case)

        case, generations = make_case("ambiguous")
        for suffix in ("a", "b"):
            temporary = generations / (".tmp-" + suffix * 32)
            temporary.mkdir()
            (temporary / "cursor.json").write_text('{"position":"one"}', encoding="utf-8")
        cases.append(case)

        case, generations = make_case("mixed")
        temporary = generations / temporary_name
        temporary.mkdir()
        (temporary / "cursor.json").write_text('{"position":"one"}', encoding="utf-8")
        complete_root = Path(self.temp.name) / "complete-source"
        generation = self.store(complete_root).publish(documents("complete"))
        shutil.copytree(complete_root / "generations" / generation, generations / generation)
        cases.append(case)

        for case in cases:
            with self.subTest(case=case.name):
                with self.assertRaisesRegex(
                    ProtocolError, "selector|temporary|ambiguous|unsafe|symlink|regular file"
                ):
                    self.store(case).update(lambda _current: documents("retry"))

    def test_first_publication_crash_is_absent_or_complete_never_partial(self) -> None:
        for boundary in ["after_manifest", "after_generation_publish", "after_selector_replace"]:
            with self.subTest(boundary=boundary):
                case = Path(self.temp.name) / ("first-" + boundary)

                def fail(label: str, expected: str = boundary) -> None:
                    if label == expected:
                        raise RuntimeError("injected crash")

                with self.assertRaisesRegex(RuntimeError, "injected crash"):
                    self.store(case, fail).publish(documents("first"))
                if boundary == "after_selector_replace":
                    self.assertEqual(self.store(case).read().documents, documents("first"))
                else:
                    with self.assertRaises(ProtocolError):
                        self.store(case).read()

    def test_selected_corruption_fails_closed_without_fallback(self) -> None:
        self.store().publish(documents("old"))
        selected = self.store().publish(documents("new"))
        (self.root / "generations" / selected / "pending.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "digest"):
            self.store().read()

    def test_incompatible_selector_fails_closed(self) -> None:
        self.store().publish(documents("one"))
        selector = json.loads((self.root / "current.json").read_text())
        selector["schema_version"] = 99
        (self.root / "current.json").write_text(json.dumps(selector), encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "schema"):
            self.store().read()

    def test_selector_schema_version_requires_exact_json_integer(self) -> None:
        self.store().publish(documents("one"))
        selector = json.loads((self.root / "current.json").read_text())
        malformed_versions = [True, False, 1.0, "1", None, [], {}, 0, 2, -1]
        for index, schema_version in enumerate(malformed_versions):
            with self.subTest(schema_version=schema_version):
                case = Path(self.temp.name) / f"selector-schema-{index}"
                shutil.copytree(self.root, case)
                bad = dict(selector, schema_version=schema_version)
                (case / "current.json").write_bytes(state_module.canonical_json_bytes(bad))
                with self.assertRaisesRegex(ProtocolError, "selector schema"):
                    self.store(case).read()

    def test_manifest_schema_version_requires_exact_json_integer(self) -> None:
        generation = self.store().publish(documents("one"))
        generation_dir = self.root / "generations" / generation
        manifest = json.loads((generation_dir / "manifest.json").read_text())
        selector = json.loads((self.root / "current.json").read_text())
        malformed_versions = [True, False, 1.0, "1", None, [], {}, 0, 2, -1]
        for index, schema_version in enumerate(malformed_versions):
            with self.subTest(schema_version=schema_version):
                case = Path(self.temp.name) / f"manifest-schema-{index}"
                shutil.copytree(self.root, case)
                bad_manifest = dict(manifest, schema_version=schema_version)
                manifest_raw = state_module.canonical_json_bytes(bad_manifest)
                manifest_sha256 = state_module._sha256(manifest_raw)
                bad_generation = "g-" + manifest_sha256
                source = case / "generations" / generation
                source.rename(case / "generations" / bad_generation)
                (case / "generations" / bad_generation / "manifest.json").write_bytes(manifest_raw)
                bad_selector = dict(
                    selector,
                    generation=bad_generation,
                    manifest_sha256=manifest_sha256,
                )
                (case / "current.json").write_bytes(state_module.canonical_json_bytes(bad_selector))
                with self.assertRaisesRegex(ProtocolError, "manifest.*incompatible"):
                    self.store(case).read()

    def test_selector_generation_is_strictly_validated(self) -> None:
        self.store().publish(documents("one"))
        selector = json.loads((self.root / "current.json").read_text())
        attacks = [
            "../escape",
            "/absolute",
            "g-nothex",
            "g-" + "a" * 64 + "/child",
            "g-" + "a" * 63 + "\n",
        ]
        for index, attack in enumerate(attacks):
            with self.subTest(attack=repr(attack)):
                case = Path(self.temp.name) / f"selector-{index}"
                shutil.copytree(self.root, case)
                bad = dict(selector, generation=attack)
                (case / "current.json").write_text(json.dumps(bad), encoding="utf-8")
                with self.assertRaises(ProtocolError):
                    self.store(case).read()

    def test_symlinked_protocol_objects_are_rejected(self) -> None:
        self.store().publish(documents("one"))
        selected = self.store().read().generation
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        target = self.root / "generations" / selected / "pending.json"
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaisesRegex(ProtocolError, "symlink"):
            self.store().read()

    def test_duplicate_json_keys_are_rejected(self) -> None:
        path = Path(self.temp.name) / "duplicate.json"
        path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "duplicate"):
            load_json(path)

    def test_non_finite_json_numbers_are_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            state_module.canonical_json_bytes({"value": float("nan")})
        path = Path(self.temp.name) / "nan.json"
        path.write_text('{"value":NaN}', encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "non-finite"):
            load_json(path)

    def test_publish_rejects_oversized_document_before_state_write(self) -> None:
        oversized = {"payload": "x" * state_module.MAX_DOCUMENT_BYTES}
        with self.assertRaisesRegex(ProtocolError, "document.*byte limit"):
            self.store().publish({"payload.json": oversized})
        self.assertFalse((self.root / "current.json").exists())
        self.assertFalse((self.root / "generations").exists())

    def test_publish_rejects_oversized_complete_generation_before_state_write(self) -> None:
        documents = {
            f"part{index}.json": {"payload": "x" * (state_module.MAX_DOCUMENT_BYTES // 2)}
            for index in range(17)
        }
        with self.assertRaisesRegex(ProtocolError, "generation.*byte limit"):
            self.store().publish(documents)
        self.assertFalse((self.root / "current.json").exists())

    def test_read_rejects_oversized_selector_manifest_and_document_before_parsing(self) -> None:
        generation = self.store().publish(documents("one"))
        generation_dir = self.root / "generations" / generation
        cases = [
            (self.root / "current.json", state_module.MAX_SELECTOR_BYTES, "selector"),
            (generation_dir / "manifest.json", state_module.MAX_MANIFEST_BYTES, "manifest"),
            (generation_dir / "pending.json", state_module.MAX_DOCUMENT_BYTES, "document"),
        ]
        for index, (target, limit, label) in enumerate(cases):
            with self.subTest(label=label):
                case = Path(self.temp.name) / f"oversized-read-{index}"
                shutil.copytree(self.root, case)
                relative = target.relative_to(self.root)
                (case / relative).write_bytes(b" " * (limit + 1))
                with self.assertRaisesRegex(ProtocolError, f"{label}.*byte limit"):
                    self.store(case).read()

    def test_manifest_announced_sizes_cannot_exceed_generation_bound(self) -> None:
        generation = self.store().publish(documents("one"))
        generation_dir = self.root / "generations" / generation
        manifest_path = generation_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["documents"]["pending.json"]["size"] = state_module.MAX_DOCUMENT_BYTES + 1
        raw = state_module.canonical_json_bytes(manifest)
        manifest_path.write_bytes(raw)
        selector_path = self.root / "current.json"
        selector = json.loads(selector_path.read_text())
        selector["manifest_sha256"] = state_module._sha256(raw)
        selector["generation"] = "g-" + selector["manifest_sha256"]
        replacement = self.root / "generations" / selector["generation"]
        generation_dir.rename(replacement)
        selector_path.write_bytes(state_module.canonical_json_bytes(selector))
        with self.assertRaisesRegex(ProtocolError, "document.*byte limit"):
            self.store().read()

    def test_cleanup_keeps_selected_and_rejects_symlink_entries(self) -> None:
        first = self.store().publish(documents("one"))
        second = self.store().publish(documents("two"))
        removed = self.store().cleanup(keep_previous=0)
        self.assertIn(first, removed)
        self.assertTrue((self.root / "generations" / second).is_dir())
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = self.root / "generations" / ("g-" + "a" * 64)
        # pathlib's / operator above must remain confined even when a symlink is introduced.
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ProtocolError, "symlink"):
            self.store().cleanup()
        self.assertTrue(outside.is_dir())

    def test_lock_contention_times_out_without_entering(self) -> None:
        ready: multiprocessing.Queue = multiprocessing.Queue()
        process = multiprocessing.Process(target=lock_holder, args=(str(self.root), ready))
        process.start()
        self.assertTrue(ready.get(timeout=2))
        try:
            with self.assertRaises(LockUnavailable), exclusive_lock(self.root, timeout=0.05):
                self.fail("contended lock entered")
        finally:
            process.join(timeout=3)
            if process.is_alive():
                process.kill()

    def test_reader_holds_shared_lock_across_selector_and_generation(self) -> None:
        self.store().publish(documents("old"))
        reader_entered = threading.Event()
        release_reader = threading.Event()
        publish_done = threading.Event()
        original = state_module._read_regular

        def blocked_read(path: Path, *args) -> bytes:
            if path.name == "manifest.json" and not reader_entered.is_set():
                reader_entered.set()
                self.assertTrue(release_reader.wait(timeout=2))
            return original(path, *args)

        reader = threading.Thread(target=self.store().read)
        publisher = threading.Thread(
            target=lambda: (self.store().publish(documents("new")), publish_done.set())
        )
        with mock.patch.object(state_module, "_read_regular", side_effect=blocked_read):
            reader.start()
            self.assertTrue(reader_entered.wait(timeout=2))
            publisher.start()
            time.sleep(0.05)
            self.assertFalse(publish_done.is_set())
            release_reader.set()
            reader.join(timeout=2)
            publisher.join(timeout=2)
        self.assertTrue(publish_done.is_set())


class DirectionalStateTests(unittest.TestCase):
    def test_lost_response_requires_reconciliation_not_blind_retry(self) -> None:
        work = DirectionalWork.create("event-1", ["kanban", "source-reply"])
        work.start("kanban", "attempt-1")
        work.lost_response("kanban", "timeout")
        self.assertEqual(work.edges["kanban"].state, "ambiguous")
        self.assertEqual(work.retryable_edges(RetryPolicy(max_attempts=3)), [])
        work.reconcile("kanban", observed=True, proof="card:t_12345678")
        self.assertEqual(work.edges["kanban"].state, "confirmed")

    def test_negative_readback_retries_only_failed_edge(self) -> None:
        work = DirectionalWork.create("event-1", ["kanban", "source-reply"])
        work.start("kanban", "attempt-1")
        work.confirm("kanban", "card:t_12345678")
        work.start("source-reply", "attempt-2")
        work.lost_response("source-reply", "timeout")
        work.reconcile("source-reply", observed=False, proof="readback:none")
        self.assertEqual(work.retryable_edges(RetryPolicy(max_attempts=3)), ["source-reply"])
        self.assertEqual(work.edges["kanban"].state, "confirmed")

    def test_permanent_and_exhausted_failures_do_not_retry(self) -> None:
        work = DirectionalWork.create("event-1", ["a", "b"])
        work.start("a", "attempt-a")
        work.fail("a", transient=False, error="forbidden")
        work.start("b", "attempt-b")
        work.fail("b", transient=True, error="unavailable")
        self.assertEqual(work.retryable_edges(RetryPolicy(max_attempts=1)), [])

    def test_confirm_requires_durable_nonempty_proof(self) -> None:
        work = DirectionalWork.create("event-1", ["edge"])
        work.start("edge", "attempt")
        with self.assertRaisesRegex(ProtocolError, "proof"):
            work.confirm("edge", "")

    def test_round_trip_rejects_unknown_or_incompatible_fields(self) -> None:
        work = DirectionalWork.create("event-1", ["edge"])
        encoded = work.to_document()
        self.assertEqual(DirectionalWork.from_document(encoded).to_document(), encoded)
        for bad in [dict(encoded, schema_version=99), dict(encoded, shadow_state={})]:
            with self.assertRaises(ProtocolError):
                DirectionalWork.from_document(bad)

    def test_round_trip_rejects_impossible_edge_state(self) -> None:
        encoded = DirectionalWork.create("event-1", ["edge"]).to_document()
        encoded["edges"]["edge"].update(state="confirmed", proof=None, attempts=0)
        with self.assertRaisesRegex(ProtocolError, "invariant"):
            DirectionalWork.from_document(encoded)

    def test_retry_delay_is_bounded_exponential(self) -> None:
        policy = RetryPolicy(max_attempts=5, initial_delay_seconds=2, maximum_delay_seconds=10)
        self.assertEqual([policy.delay_seconds(n) for n in range(1, 6)], [2, 4, 8, 10, 10])


class PreferenceAndMetricsTests(unittest.TestCase):
    contract: ClassVar[dict[str, Any]] = {
        "schema_version": 1,
        "name": "digest-edition",
        "preference_version": 3,
        "properties": {
            "enabled": {"type": "boolean", "default": False},
            "cadence_minutes": {"type": "integer", "minimum": 60, "maximum": 1440, "default": 240},
            "language": {"type": "string", "enum": ["de", "en"], "default": "en"},
            "topics": {"type": "string-list", "max_items": 4, "max_length": 20, "default": []},
        },
    }

    def test_preferences_apply_defaults_and_reject_scope_expansion(self) -> None:
        self.assertEqual(
            validate_preferences(self.contract, {"enabled": True})["values"],
            {"enabled": True, "cadence_minutes": 240, "language": "en", "topics": []},
        )
        for values in [
            {"destination": "attacker"},
            {"cadence_minutes": 1},
            {"language": "fr"},
            {"topics": ["a", "b", "c", "d", "e"]},
            {"enabled": 1},
        ]:
            with self.subTest(values=values), self.assertRaises(ProtocolError):
                validate_preferences(self.contract, values)

    def test_preference_contract_requires_safe_disable_control(self) -> None:
        bad = copy.deepcopy(self.contract)
        del bad["properties"]["enabled"]
        with self.assertRaisesRegex(ProtocolError, "enabled"):
            validate_preferences(bad, {})

    def test_preference_enum_text_is_bounded(self) -> None:
        bad = copy.deepcopy(self.contract)
        bad["properties"]["language"]["enum"] = ["x" * 1000000]
        bad["properties"]["language"]["default"] = "x" * 1000000
        with self.assertRaisesRegex(ProtocolError, "enum"):
            validate_preferences(bad, {})

    def test_metrics_have_fixed_names_and_no_labels(self) -> None:
        rendered = render_metrics(
            {
                "publications_total": 2,
                "reconciliations_total": 3,
                "retries_total": 1,
                "corruptions_total": 0,
                "lock_contentions_total": 4,
            }
        )
        self.assertNotIn("{", rendered)
        self.assertIn("hermes_workflow_state_publications_total 2\n", rendered)
        with self.assertRaises(ProtocolError):
            render_metrics({"event_123": 1})
        with self.assertRaises(ProtocolError):
            render_metrics({"retries_total": -1})

    def test_cli_validate_preview_apply_show_and_readback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            contract = root / "contract.json"
            requested = root / "requested.json"
            contract.write_text(json.dumps(self.contract), encoding="utf-8")
            requested.write_text(json.dumps({"enabled": True, "language": "de"}), encoding="utf-8")
            cli = SOURCE / "hermes_workflow_preferences.py"
            base = [
                sys.executable,
                str(cli),
                "--state-dir",
                str(root / "state"),
                "--contract",
                str(contract),
            ]

            validated = subprocess.run(
                base + ["validate", "--input", str(requested)],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertTrue(json.loads(validated.stdout)["values"]["enabled"])
            preview = subprocess.run(
                base + ["preview", "--input", str(requested)],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(
                json.loads(preview.stdout)["changes"]["enabled"], {"from": False, "to": True}
            )
            subprocess.run(
                base
                + [
                    "apply",
                    "--input",
                    str(requested),
                    "--actor",
                    "operator",
                    "--reason",
                    "enable edition",
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            shown = subprocess.run(base + ["show"], check=True, text=True, capture_output=True)
            self.assertTrue(json.loads(shown.stdout)["values"]["enabled"])
            readback = subprocess.run(
                base + ["readback", "--input", str(requested)],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertTrue(json.loads(readback.stdout)["matches"])

    def test_cli_invalid_apply_is_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            contract = root / "contract.json"
            valid = root / "valid.json"
            invalid = root / "invalid.json"
            contract.write_text(json.dumps(self.contract), encoding="utf-8")
            valid.write_text(json.dumps({"enabled": False}), encoding="utf-8")
            invalid.write_text(json.dumps({"cadence_minutes": 1}), encoding="utf-8")
            cli = SOURCE / "hermes_workflow_preferences.py"
            base = [
                sys.executable,
                str(cli),
                "--state-dir",
                str(root / "state"),
                "--contract",
                str(contract),
            ]
            subprocess.run(
                base
                + ["apply", "--input", str(valid), "--actor", "operator", "--reason", "baseline"],
                check=True,
                capture_output=True,
            )
            selector_before = (root / "state" / "current.json").read_bytes()
            result = subprocess.run(
                base + ["apply", "--input", str(invalid), "--actor", "operator", "--reason", "bad"],
                check=False,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / "state" / "current.json").read_bytes(), selector_before)

    def test_cli_disable_is_a_managed_audited_transition(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            contract = root / "contract.json"
            requested = root / "requested.json"
            contract.write_text(json.dumps(self.contract), encoding="utf-8")
            requested.write_text(json.dumps({"enabled": True, "language": "de"}), encoding="utf-8")
            cli = SOURCE / "hermes_workflow_preferences.py"
            base = [
                sys.executable,
                str(cli),
                "--state-dir",
                str(root / "state"),
                "--contract",
                str(contract),
            ]
            applied = subprocess.run(
                base
                + ["apply", "--input", str(requested), "--actor", "operator", "--reason", "on"],
                check=True,
                text=True,
                capture_output=True,
            )
            generation = json.loads(applied.stdout)["generation"]
            disabled = subprocess.run(
                base
                + [
                    "disable",
                    "--expected-generation",
                    generation,
                    "--actor",
                    "operator",
                    "--reason",
                    "pause safely",
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            result = json.loads(disabled.stdout)
            self.assertFalse(result["preferences"]["values"]["enabled"])
            self.assertEqual(result["changes"], ["enabled"])
            self.assertEqual(result["lifecycle"], "disabled")
            shown = json.loads(
                subprocess.run(base + ["show"], check=True, text=True, capture_output=True).stdout
            )
            self.assertFalse(shown["values"]["enabled"])
            self.assertEqual(shown["values"]["language"], "de")

    def test_cli_backup_confirmed_remove_and_validated_restore_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            contract = root / "contract.json"
            requested = root / "requested.json"
            backup = root / "backup.json"
            alternate = root / "alternate.json"
            corrupt = root / "corrupt.json"
            downgrade = root / "downgrade.json"
            contract.write_text(json.dumps(self.contract), encoding="utf-8")
            requested.write_text(json.dumps({"enabled": True, "language": "de"}), encoding="utf-8")
            cli = SOURCE / "hermes_workflow_preferences.py"
            base = [
                sys.executable,
                str(cli),
                "--state-dir",
                str(root / "state"),
                "--contract",
                str(contract),
            ]

            active = json.loads(
                subprocess.run(
                    base
                    + ["apply", "--input", str(requested), "--actor", "operator", "--reason", "on"],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout
            )
            subprocess.run(
                base + ["backup", "--output", str(backup)], check=True, capture_output=True
            )
            envelope = json.loads(backup.read_text())
            self.assertEqual(envelope["payload"]["source_generation"], active["generation"])
            self.assertEqual(envelope["payload"]["preferences"]["values"]["language"], "de")

            disabled = json.loads(
                subprocess.run(
                    base
                    + [
                        "disable",
                        "--expected-generation",
                        active["generation"],
                        "--actor",
                        "operator",
                        "--reason",
                        "pause",
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout
            )
            alternate_envelope = copy.deepcopy(envelope)
            alternate_envelope["payload"]["preferences"]["values"]["language"] = "en"
            alternate_envelope["sha256"] = state_module._sha256(
                state_module.canonical_json_bytes(alternate_envelope["payload"])
            )
            alternate.write_bytes(state_module.canonical_json_bytes(alternate_envelope))
            wrong_backup = subprocess.run(
                base
                + [
                    "remove",
                    "--expected-generation",
                    disabled["generation"],
                    "--confirm-removal",
                    self.contract["name"],
                    "--backup",
                    str(alternate),
                    "--actor",
                    "operator",
                    "--reason",
                    "wrong backup",
                ],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(wrong_backup.returncode, 0)
            self.assertIn("current preferences", wrong_backup.stderr)
            rejected = subprocess.run(
                base
                + [
                    "remove",
                    "--expected-generation",
                    disabled["generation"],
                    "--confirm-removal",
                    "wrong-name",
                    "--backup",
                    str(backup),
                    "--actor",
                    "operator",
                    "--reason",
                    "retire",
                ],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            removed = json.loads(
                subprocess.run(
                    base
                    + [
                        "remove",
                        "--expected-generation",
                        disabled["generation"],
                        "--confirm-removal",
                        self.contract["name"],
                        "--backup",
                        str(backup),
                        "--actor",
                        "operator",
                        "--reason",
                        "retire",
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout
            )
            self.assertEqual(removed["lifecycle"], "removed")
            shown = json.loads(
                subprocess.run(base + ["show"], check=True, text=True, capture_output=True).stdout
            )
            self.assertEqual(shown["lifecycle"], "removed")
            self.assertNotIn("values", shown)

            wrong_restore = subprocess.run(
                base
                + [
                    "restore",
                    "--backup",
                    str(alternate),
                    "--expected-generation",
                    removed["generation"],
                    "--actor",
                    "operator",
                    "--reason",
                    "wrong backup",
                ],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(wrong_restore.returncode, 0)
            self.assertIn("tombstone", wrong_restore.stderr)

            corrupt.write_bytes(backup.read_bytes() + b" ")
            selector_before = (root / "state" / "current.json").read_bytes()
            bad_restore = subprocess.run(
                base
                + [
                    "restore",
                    "--backup",
                    str(corrupt),
                    "--expected-generation",
                    removed["generation"],
                    "--actor",
                    "operator",
                    "--reason",
                    "bad",
                ],
                capture_output=True,
            )
            self.assertNotEqual(bad_restore.returncode, 0)
            self.assertEqual((root / "state" / "current.json").read_bytes(), selector_before)

            downgraded = copy.deepcopy(envelope)
            downgraded["payload"]["preference_version"] = self.contract["preference_version"] - 1
            downgraded["sha256"] = state_module._sha256(
                state_module.canonical_json_bytes(downgraded["payload"])
            )
            downgrade.write_bytes(state_module.canonical_json_bytes(downgraded))
            bad_version = subprocess.run(
                base
                + [
                    "restore",
                    "--backup",
                    str(downgrade),
                    "--expected-generation",
                    removed["generation"],
                    "--actor",
                    "operator",
                    "--reason",
                    "downgrade",
                ],
                capture_output=True,
            )
            self.assertNotEqual(bad_version.returncode, 0)
            self.assertEqual((root / "state" / "current.json").read_bytes(), selector_before)

            restored = json.loads(
                subprocess.run(
                    base
                    + [
                        "restore",
                        "--backup",
                        str(backup),
                        "--expected-generation",
                        removed["generation"],
                        "--actor",
                        "operator",
                        "--reason",
                        "recover",
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout
            )
            self.assertEqual(restored["lifecycle"], "restored")
            self.assertTrue(restored["preferences"]["values"]["enabled"])
            self.assertEqual(restored["preferences"]["values"]["language"], "de")

    def test_cli_rejects_stale_expected_generation_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            contract = root / "contract.json"
            requested = root / "requested.json"
            contract.write_text(json.dumps(self.contract), encoding="utf-8")
            requested.write_text(json.dumps({"enabled": True}), encoding="utf-8")
            cli = SOURCE / "hermes_workflow_preferences.py"
            base = [
                sys.executable,
                str(cli),
                "--state-dir",
                str(root / "state"),
                "--contract",
                str(contract),
            ]
            first = json.loads(
                subprocess.run(
                    base
                    + [
                        "apply",
                        "--input",
                        str(requested),
                        "--actor",
                        "operator",
                        "--reason",
                        "one",
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout
            )
            requested.write_text(json.dumps({"enabled": False}), encoding="utf-8")
            second = json.loads(
                subprocess.run(
                    base
                    + [
                        "apply",
                        "--input",
                        str(requested),
                        "--expected-generation",
                        first["generation"],
                        "--actor",
                        "operator",
                        "--reason",
                        "two",
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout
            )
            selector_before = (root / "state" / "current.json").read_bytes()
            stale = subprocess.run(
                base
                + [
                    "disable",
                    "--expected-generation",
                    first["generation"],
                    "--actor",
                    "operator",
                    "--reason",
                    "stale",
                ],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("concurrently", stale.stderr)
            self.assertEqual((root / "state" / "current.json").read_bytes(), selector_before)
            self.assertNotEqual(first["generation"], second["generation"])

    def test_cli_serializes_concurrent_apply_through_readback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            contract = root / "contract.json"
            first = root / "first.json"
            second = root / "second.json"
            contract.write_text(json.dumps(self.contract), encoding="utf-8")
            first.write_text(json.dumps({"enabled": True}), encoding="utf-8")
            second.write_text(json.dumps({"enabled": False, "language": "de"}), encoding="utf-8")
            cli = SOURCE / "hermes_workflow_preferences.py"
            base = [
                sys.executable,
                str(cli),
                "--state-dir",
                str(root / "state"),
                "--contract",
                str(contract),
            ]
            results: list[subprocess.CompletedProcess] = []

            def apply(path: Path, reason: str) -> None:
                results.append(
                    subprocess.run(
                        base
                        + [
                            "apply",
                            "--input",
                            str(path),
                            "--actor",
                            "operator",
                            "--reason",
                            reason,
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                )

            threads = [
                threading.Thread(target=apply, args=(first, "first")),
                threading.Thread(target=apply, args=(second, "second")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
            self.assertEqual(
                [result.returncode for result in results],
                [0, 0],
                [result.stderr for result in results],
            )

    def test_cli_bounds_retained_generations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            contract = root / "contract.json"
            request = root / "request.json"
            contract.write_text(json.dumps(self.contract), encoding="utf-8")
            cli = SOURCE / "hermes_workflow_preferences.py"
            base = [
                sys.executable,
                str(cli),
                "--state-dir",
                str(root / "state"),
                "--contract",
                str(contract),
            ]
            for index in range(6):
                request.write_text(json.dumps({"enabled": bool(index % 2)}), encoding="utf-8")
                subprocess.run(
                    base
                    + [
                        "apply",
                        "--input",
                        str(request),
                        "--actor",
                        "operator",
                        "--reason",
                        f"change {index}",
                    ],
                    check=True,
                    capture_output=True,
                )
            generations = [
                p for p in (root / "state" / "generations").iterdir() if p.name.startswith("g-")
            ]
            self.assertLessEqual(len(generations), 2)

    def test_cli_rejects_semantically_corrupt_selected_audit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            contract = root / "contract.json"
            requested = root / "requested.json"
            contract.write_text(json.dumps(self.contract), encoding="utf-8")
            requested.write_text(json.dumps({"enabled": True}), encoding="utf-8")
            store = GenerationStore(
                root / "state",
                protocol="preferences-digest-edition",
                schema_version=3,
            )
            store.publish(
                {
                    "preferences.json": validate_preferences(self.contract, {"enabled": True}),
                    "audit.json": {"schema_version": 1, "actor": "operator"},
                }
            )
            cli = SOURCE / "hermes_workflow_preferences.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(cli),
                    "--state-dir",
                    str(root / "state"),
                    "--contract",
                    str(contract),
                    "show",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("audit", result.stderr)


if __name__ == "__main__":
    unittest.main()
