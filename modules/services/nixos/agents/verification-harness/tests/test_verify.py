from __future__ import annotations

import argparse
import copy
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify_module = load_module("verification_harness", ROOT / "verify.py")
collect_module = load_module("verification_collector", ROOT / "collect.py")


class VerificationHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = verify_module.load_json(ROOT / "contract.json")
        cls.healthy = verify_module.load_json(ROOT / "fixtures" / "healthy-no-change.json")
        cls.positive = verify_module.load_json(
            ROOT / "fixtures" / "real-change-positive-control.json"
        )

    def assert_passes(self, bundle: dict) -> None:
        self.assertEqual(verify_module.verify(bundle, self.contract), [])

    def assert_rejected(self, bundle: dict, expected: str) -> None:
        errors = verify_module.verify(bundle, self.contract)
        self.assertTrue(errors, "unsafe bundle was accepted")
        self.assertTrue(
            any(expected in error for error in errors),
            f"expected {expected!r} in {errors!r}",
        )

    def test_healthy_fixture_passes(self) -> None:
        self.assert_passes(copy.deepcopy(self.healthy))

    def test_operator_authored_gate_result_is_not_execution_evidence(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        bundle["evidence"]["after"]["gate_execution"] = None
        bundle["evidence"]["gate_result"] = {"changed_events": 0, "wakeAgent": False}
        errors = verify_module.verify(bundle, self.contract)
        self.assertTrue(
            any("executed gate evidence" in error for error in errors),
            errors,
        )

    def test_executed_gate_evidence_is_bound_to_output_parser_and_scheduler(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        bundle["evidence"]["after"]["gate_execution"] = {
            "correlation_id": bundle["correlation_id"],
            "execution_id": "execution-healthy-1",
            "parser_result": {"changed_events": 0, "wakeAgent": False},
            "raw_output": '{"changed_events":0,"wakeAgent":false}\n',
            "raw_output_sha256": "0" * 64,
            "scheduler_consumed_decision": {"changed_events": 0, "wakeAgent": False},
            "script_identity": "/nix/store/example-gate/bin/gate",
            "script_sha256": bundle["artifact"]["gate_script_sha256"],
        }
        self.assert_rejected(bundle, "raw_output_sha256")

    def test_positive_control_passes(self) -> None:
        self.assert_passes(copy.deepcopy(self.positive))

    def test_declared_counts_cannot_override_native_evidence(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        bundle["observations"]["model_attempts"] = 1
        self.assert_rejected(bundle, "declared observations do not match")

    def test_no_change_native_model_attempt_is_rejected(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        bundle["evidence"]["after"]["model_events"] = [
            {
                "event_id": "unexpected-model-attempt",
                "correlation_id": bundle["correlation_id"],
                "job_id": bundle["artifact"]["job_id"],
                "received_at": "2026-08-28T10:00:30Z",
                "completed_at": None,
                "outcome": "interrupted",
                "input_tokens": 0,
                "output_tokens": 0,
            }
        ]
        bundle["observations"]["model_attempts"] = 1
        self.assert_rejected(bundle, "model_attempts mismatch")

    def test_no_change_native_token_use_is_rejected(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        bundle["evidence"]["after"]["model_events"] = [
            {
                "event_id": "unexpected-model-attempt",
                "correlation_id": bundle["correlation_id"],
                "job_id": bundle["artifact"]["job_id"],
                "received_at": "2026-08-28T10:00:20Z",
                "completed_at": "2026-08-28T10:00:30Z",
                "outcome": "error",
                "input_tokens": 1,
                "output_tokens": 0,
            }
        ]
        bundle["observations"]["model_attempts"] = 1
        bundle["observations"]["input_tokens"] = 1
        self.assert_rejected(bundle, "input_tokens mismatch")

    def test_no_change_delivery_sink_event_is_rejected(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        bundle["evidence"]["after"]["delivery_events"] = [
            {
                "event_id": "delivery-1",
                "correlation_id": bundle["correlation_id"],
                "job_id": bundle["artifact"]["job_id"],
                "received_at": "2026-08-28T10:00:30Z",
            }
        ]
        bundle["observations"]["deliveries"] = 1
        self.assert_rejected(bundle, "deliveries mismatch")

    def test_no_change_external_mutation_is_rejected(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        bundle["evidence"]["after"]["mutation_probes"]["kanban"] = "0" * 64
        self.assert_rejected(bundle, "external mutation probes changed")

    def test_cross_profile_write_is_rejected(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        bundle["evidence"]["after"]["profile_isolation"]["unrelated-profile"] = "0" * 64
        self.assert_rejected(bundle, "profile isolation changed")

    def test_missing_artifact_identity_is_rejected(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        bundle["artifact"]["nix_generation"] = ""
        self.assert_rejected(bundle, "artifact.nix_generation is required")

    def test_artifact_must_match_native_snapshots(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        bundle["artifact"]["git_revision"] = "forged"
        self.assert_rejected(bundle, "artifact does not match bundle artifact")

    def test_artifact_cannot_self_assert_a_different_hermes_build(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        for target in (
            bundle["artifact"],
            bundle["evidence"]["before"]["artifact"],
            bundle["evidence"]["after"]["artifact"],
        ):
            target["hermes_version"] = "not-hermes"
            target["hermes_source_revision"] = "f" * 40
            target["package_store_path"] = "/nix/store/fake-hermes"
        self.assert_rejected(bundle, "does not match the frozen inventory")

    def test_artifact_requires_real_git_and_nix_path_shapes(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        for target in (
            bundle["artifact"],
            bundle["evidence"]["before"]["artifact"],
            bundle["evidence"]["after"]["artifact"],
        ):
            target["git_revision"] = "not-a-sha"
            target["nix_generation"] = "/tmp/generation"
        self.assert_rejected(bundle, "artifact.git_revision must be")
        self.assert_rejected(bundle, "artifact.nix_generation must be")

    def test_mixed_correlation_is_rejected(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        bundle["evidence"]["after"]["correlation_id"] = "33333333-3333-4333-8333-333333333333"
        self.assert_rejected(bundle, "correlation_id does not match")

    def test_snapshot_outside_window_is_rejected(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        bundle["evidence"]["after"]["captured_at"] = "2026-08-28T10:02:00Z"
        self.assert_rejected(bundle, "captured_at is outside the window")

    def test_positive_control_requires_real_tokens(self) -> None:
        bundle = copy.deepcopy(self.positive)
        bundle["evidence"]["after"]["model_events"][0]["input_tokens"] = 0
        bundle["observations"]["input_tokens"] = 0
        self.assert_rejected(bundle, "input_tokens must be at least 1")

    def test_success_without_physical_provider_attempt_is_rejected(self) -> None:
        bundle = copy.deepcopy(self.positive)
        bundle["evidence"]["after"]["model_events"] = []
        bundle["observations"]["model_attempts"] = 0
        bundle["observations"]["model_successes"] = 0
        bundle["observations"]["input_tokens"] = 0
        bundle["observations"]["output_tokens"] = 0
        self.assert_rejected(bundle, "model_successes must be at least 1")

    def test_interrupted_provider_attempt_is_not_a_success(self) -> None:
        bundle = copy.deepcopy(self.positive)
        event = bundle["evidence"]["after"]["model_events"][0]
        event.update(completed_at=None, outcome="interrupted", input_tokens=0, output_tokens=0)
        bundle["observations"].update(model_successes=0, input_tokens=0, output_tokens=0)
        self.assert_rejected(bundle, "model_successes must be at least 1")

    def test_gate_error_fail_safe_fixture_passes(self) -> None:
        bundle = copy.deepcopy(self.positive)
        bundle["fixture"] = "gate_error_fail_safe"
        self.assert_passes(bundle)

    def _failed_bundle(self, fixture: str, status: str, native_status: str, error: str) -> dict:
        bundle = copy.deepcopy(self.healthy)
        bundle["fixture"] = fixture
        bundle["execution_status"] = status
        bundle["wake_agent"] = None
        bundle["error"] = error
        gate = bundle["evidence"]["after"]["gate_execution"]
        gate["parser_result"]["wakeAgent"] = None
        gate["scheduler_consumed_decision"]["wakeAgent"] = None
        raw = '{"changed_events":0,"wakeAgent":null}\n'
        gate["raw_output"] = raw
        gate["raw_output_sha256"] = verify_module.hashlib.sha256(raw.encode()).hexdigest()
        bundle["evidence"]["after"]["scheduler"][0]["status"] = native_status
        bundle["evidence"]["after"]["scheduler"][0]["error"] = error
        return bundle

    def test_malformed_gate_fail_safe_fixture_passes(self) -> None:
        bundle = copy.deepcopy(self.positive)
        bundle["fixture"] = "malformed_gate_fail_closed"
        self.assert_passes(bundle)

    def test_timeout_fail_closed_fixture_passes(self) -> None:
        bundle = copy.deepcopy(self.positive)
        bundle["fixture"] = "timeout_fail_closed"
        self.assert_passes(bundle)

    def test_kill_restart_fail_closed_fixture_passes(self) -> None:
        self.assert_passes(
            self._failed_bundle(
                "kill_restart_fail_closed",
                "failed",
                "unknown",
                "scheduler owner exited before terminal state",
            )
        )

    def test_timeout_cannot_be_reported_as_failed_without_native_wake(self) -> None:
        bundle = self._failed_bundle(
            "timeout_fail_closed", "failed", "failed", "script timeout after 1s"
        )
        self.assert_rejected(bundle, "execution_status mismatch")

    def test_kill_fixture_requires_visible_error(self) -> None:
        bundle = self._failed_bundle(
            "kill_restart_fail_closed", "failed", "unknown", "owner exited"
        )
        bundle["error"] = ""
        self.assert_rejected(bundle, "requires a non-empty error")

    def test_missing_external_probe_is_rejected(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        del bundle["evidence"]["after"]["mutation_probes"]["prometheus"]
        self.assert_rejected(bundle, "mutation probes must contain exactly")

    def test_duplicate_json_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"schema_version":"one","schema_version":"two"}', encoding="utf-8")
            with self.assertRaises(verify_module.DuplicateKeyError):
                verify_module.load_json(path)

    def test_event_timestamps_runtime_binding_and_session_correlation_are_required(self) -> None:
        stale = copy.deepcopy(self.positive)
        stale["evidence"]["after"]["model_events"][0]["received_at"] = "2026-08-28T09:59:59Z"
        self.assert_rejected(stale, "outside the correlated window")

        wrong_artifact = copy.deepcopy(self.positive)
        wrong_artifact["evidence"]["after"]["delivery_events"][0]["artifact_sha256"] = "0" * 64
        self.assert_rejected(wrong_artifact, "not bound to the tested runtime artifact")

        wrong_session = copy.deepcopy(self.positive)
        wrong_session["evidence"]["after"]["model_events"][0]["session_id"] = "stale-session"
        self.assert_rejected(wrong_session, "not bound to a new Hermes session")

    def test_delivery_event_requires_run_correlation(self) -> None:
        bundle = copy.deepcopy(self.healthy)
        bundle["evidence"]["after"]["delivery_events"] = [
            {
                "event_id": "delivery-1",
                "correlation_id": "33333333-3333-4333-8333-333333333333",
                "job_id": bundle["artifact"]["job_id"],
                "received_at": "2026-08-28T10:00:30Z",
            }
        ]
        bundle["observations"]["deliveries"] = 1
        self.assert_rejected(bundle, "not correlated")


class NativeCollectorTests(unittest.TestCase):
    def test_gate_execution_record_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.json"
            path.write_text(
                '{"correlation_id":"corr","correlation_id":"corr",'
                '"execution_id":"exec-1","parser_result":{"changed_events":0,"wakeAgent":false},'
                '"raw_output":"{}\\n","raw_output_sha256":"' + "a" * 64 + '",'
                '"scheduler_consumed_decision":{"changed_events":0,"wakeAgent":false},'
                '"script_identity":"/nix/store/gate/bin/gate","script_sha256":"' + "b" * 64 + '"}'
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                collect_module.read_gate_execution(
                    path,
                    {
                        "gate_script_identity": "/nix/store/gate/bin/gate",
                        "gate_script_sha256": "b" * 64,
                    },
                    "corr",
                    [{"id": "exec-1"}],
                )

    def test_collector_uses_supported_cli_and_hashes_probes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            executable = Path(sys.executable)
            generation = root / "generation"
            generation.mkdir()
            config = root / "config.yaml"
            gate = root / "gate.py"
            config.write_text("telemetry: true\n", encoding="utf-8")
            gate.write_text("print('ok')\n", encoding="utf-8")
            probe = root / "probe.json"
            profile = root / "other-profile"
            probe.write_text('{"value":1}\n', encoding="utf-8")
            profile.mkdir()
            (profile / "state").write_text("unchanged", encoding="utf-8")
            args = argparse.Namespace(
                hermes_home=home,
                job_id="job-1",
                profile="system-admin",
                correlation_id="11111111-1111-4111-8111-111111111111",
                git_revision="a" * 40,
                hermes_source_revision="b" * 40,
                hermes_executable=executable,
                nix_generation=generation,
                profile_config=config,
                gate_script=gate,
                model_log=None,
                delivery_log=None,
                external_probe=[
                    f"kanban={probe}",
                    f"forgejo={probe}",
                    f"ntfy={probe}",
                    f"prometheus={probe}",
                ],
                isolation_profile=[f"other={profile}"],
            )

            def fake_command(argv: list[str], _home: Path) -> str:
                if "runs" in argv:
                    return "e1  completed  job=job-1  source=manual  2026-08-28T10:00:01Z"
                if "sessions" in argv:
                    return (
                        "Preview                                            Last Active   Src    ID\n"
                        + "─" * 95
                        + "\nfixture                                            now           cron   s1"
                    )
                return "Hermes Agent 0.20.0"

            with mock.patch.object(collect_module, "command_output", side_effect=fake_command):
                snapshot = collect_module.capture(args)
            self.assertEqual(snapshot["scheduler"][0]["id"], "e1")
            self.assertEqual(snapshot["sessions"], [{"id": "s1"}])
            self.assertEqual(
                snapshot["schema_version"], "hermes.verification.supported-snapshot.v2"
            )
            self.assertEqual(
                set(snapshot["mutation_probes"]), {"kanban", "forgejo", "ntfy", "prometheus"}
            )
            self.assertEqual(set(snapshot["profile_isolation"]), {"other"})

    def test_delivery_log_schema_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.jsonl"
            path.write_text('{"event_id":"only-one-field"}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                collect_module.read_delivery_events(path, {"profile": "system-admin"})

    def test_model_log_schema_requires_terminal_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.jsonl"
            path.write_text(
                '{"event_id":"e","correlation_id":"c","job_id":"j","received_at":"t"}\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                collect_module.read_model_events(path, {"profile": "system-admin"})

    def test_pinned_multiline_version_is_normalized(self) -> None:
        output = "Hermes Agent v0.20.0 (2026.8.3)\nInstall directory: /nix/store/example"
        self.assertEqual(collect_module.parse_hermes_version(output), "0.20.0")

    def test_unknown_version_shape_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            collect_module.parse_hermes_version("version unknown")

    def test_canonical_hash_does_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            tree = root / "tree"
            outside.write_text("secret", encoding="utf-8")
            tree.mkdir()
            os.symlink(outside, tree / "link")
            first = collect_module.canonical_hash(tree)
            outside.write_text("changed", encoding="utf-8")
            second = collect_module.canonical_hash(tree)
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
