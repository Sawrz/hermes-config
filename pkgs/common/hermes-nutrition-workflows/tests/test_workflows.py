from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from hermes_workflow_state import GenerationStore

from hermes_nutrition_workflows import (
    ContractError,
    WorkflowRunner,
    analyze_weight_trend,
    calculate_portions,
    read_private_overlay,
)


class PortionArithmeticTests(unittest.TestCase):
    def test_normalizes_mass_and_volume_and_propagates_uncertainty(self) -> None:
        plan = {
            "portions": 4,
            "ingredients": [
                {
                    "name": "synthetic-oats",
                    "quantity": "0.2",
                    "unit": "kg",
                    "nutrition_per_100g": {
                        "energy_kcal": "380",
                        "protein_g": "13",
                        "carbohydrate_g": "68",
                        "fat_g": "7",
                    },
                    "uncertainty_percent": "2",
                    "provenance": {
                        "source": "synthetic-fixture",
                        "source_id": "food-1",
                        "version": "v1",
                    },
                },
                {
                    "name": "synthetic-liquid",
                    "quantity": "250",
                    "unit": "ml",
                    "density_g_per_ml": "1.04",
                    "nutrition_per_100g": {
                        "energy_kcal": "50",
                        "protein_g": "3",
                        "carbohydrate_g": "5",
                        "fat_g": "2",
                    },
                    "uncertainty_percent": "5",
                    "provenance": {
                        "source": "synthetic-fixture",
                        "source_id": "food-2",
                        "version": "v3",
                    },
                },
            ],
        }

        result = calculate_portions(plan)

        self.assertEqual(result["total_mass_g"], "460.00")
        self.assertEqual(result["totals"]["energy_kcal"], "890.00")
        self.assertEqual(result["per_portion"]["energy_kcal"], "222.50")
        self.assertEqual(result["per_portion"]["protein_g"], "8.45")
        # Conservative linear propagation: sum absolute source ranges / total.
        self.assertEqual(result["uncertainty"]["energy_kcal"], "±2.44%")
        self.assertEqual(
            result["provenance"],
            [
                {"source": "synthetic-fixture", "source_id": "food-1", "version": "v1"},
                {"source": "synthetic-fixture", "source_id": "food-2", "version": "v3"},
            ],
        )

    def test_rejects_float_input_and_volume_without_density(self) -> None:
        base = {
            "portions": 1,
            "ingredients": [
                {
                    "name": "synthetic",
                    "quantity": "100",
                    "unit": "ml",
                    "nutrition_per_100g": {
                        "energy_kcal": "1",
                        "protein_g": "1",
                        "carbohydrate_g": "1",
                        "fat_g": "1",
                    },
                    "uncertainty_percent": "0",
                    "provenance": {"source": "fixture", "source_id": "x", "version": "1"},
                }
            ],
        }
        with self.assertRaisesRegex(ContractError, "density"):
            calculate_portions(base)
        base["ingredients"][0]["quantity"] = 100.0
        with self.assertRaisesRegex(ContractError, "decimal string"):
            calculate_portions(base)
        base["ingredients"][0]["quantity"] = "1" * 65
        base["ingredients"][0]["density_g_per_ml"] = "1"
        with self.assertRaisesRegex(ContractError, "bounded decimal string"):
            calculate_portions(base)


class WeightTrendTests(unittest.TestCase):
    @staticmethod
    def measurement(identifier: str, when: str, value: str, **extra: object) -> dict[str, object]:
        result: dict[str, object] = {
            "id": identifier,
            "measured_at": when,
            "value_kg": value,
            "uncertainty_kg": "0.10",
            "context": {
                "device_id": "synthetic-scale",
                "calibration_version": "cal-1",
                "placement": "synthetic-floor",
                "conditions": "morning",
            },
            "provenance": {"source": "synthetic-fixture", "source_id": identifier, "version": "1"},
            "correction_of": None,
        }
        result.update(extra)
        return result

    def test_deduplicates_exact_observations_and_applies_correction(self) -> None:
        records = [
            self.measurement("m1", "2026-01-01T07:00:00Z", "80.00"),
            self.measurement("duplicate", "2026-01-01T07:00:00Z", "80.00"),
            self.measurement("m2", "2026-01-05T07:00:00Z", "79.80"),
            self.measurement("m2-corrected", "2026-01-05T07:00:00Z", "79.70", correction_of="m2"),
            self.measurement("m3", "2026-01-09T07:00:00Z", "79.40"),
        ]

        result = analyze_weight_trend(records)

        self.assertTrue(result["qualified"])
        self.assertEqual(result["effective_count"], 3)
        self.assertEqual(result["duplicate_ids"], ["duplicate"])
        self.assertEqual(result["superseded_ids"], ["m2"])
        self.assertEqual(result["change_kg"], "-0.60")
        self.assertEqual(result["uncertainty_kg"], "±0.20")
        self.assertEqual(result["span_days"], 8)

    def test_calibration_change_disqualifies_trend(self) -> None:
        records = [
            self.measurement("m1", "2026-01-01T07:00:00Z", "80.00"),
            self.measurement("m2", "2026-01-05T07:00:00Z", "79.80"),
            self.measurement(
                "m3",
                "2026-01-09T07:00:00Z",
                "79.50",
                context={
                    "device_id": "synthetic-scale",
                    "calibration_version": "cal-2",
                    "placement": "synthetic-floor",
                    "conditions": "morning",
                },
            ),
        ]

        result = analyze_weight_trend(records)

        self.assertFalse(result["qualified"])
        self.assertEqual(result["reason"], "measurement-context-changed")

    def test_conflicting_duplicate_id_and_missing_correction_fail_closed(self) -> None:
        records = [
            self.measurement("m1", "2026-01-01T07:00:00Z", "80.00"),
            self.measurement("m1", "2026-01-02T07:00:00Z", "81.00"),
        ]
        with self.assertRaisesRegex(ContractError, "duplicate measurement id"):
            analyze_weight_trend(records)
        with self.assertRaisesRegex(ContractError, "correction target"):
            analyze_weight_trend(
                [self.measurement("m2", "2026-01-02T07:00:00Z", "81.00", correction_of="missing")]
            )


class WorkflowGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.overlay = self.root / "private.json"
        nutrition = {
            "energy_kcal": "100",
            "protein_g": "10",
            "carbohydrate_g": "20",
            "fat_g": "5",
        }
        provenance = {
            "source": "synthetic-fixture",
            "source_id": "private-sentinel",
            "version": "1",
        }
        self.private_data = {
            "weight-check-in-and-trend": {
                "measurements": [],
                "data_quality_questions": [],
            },
            "daily-intake-closeout": {
                "entries": [
                    {
                        "name": "private-sentinel-food",
                        "quantity": "150",
                        "unit": "g",
                        "nutrition_per_100g": nutrition,
                        "uncertainty_percent": "10",
                        "provenance": provenance,
                    }
                ],
                "data_quality_questions": [],
            },
            "household-portion-planning": {
                "plan": {
                    "portions": 3,
                    "ingredients": [
                        {
                            "name": "private-sentinel-food",
                            "quantity": "300",
                            "unit": "g",
                            "nutrition_per_100g": nutrition,
                            "uncertainty_percent": "10",
                            "provenance": provenance,
                        }
                    ],
                },
                "data_quality_questions": [],
            },
        }
        self.write_overlay("daily-intake-closeout")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_overlay(self, workflow: str, data: object | None = None) -> None:
        value = {
            "schema_version": 1,
            "workflow": workflow,
            "data": self.private_data[workflow] if data is None else data,
        }
        if self.overlay.exists():
            self.overlay.chmod(0o600)
        self.overlay.write_text(json.dumps(value), encoding="utf-8")
        self.overlay.chmod(0o400)

    def request(self, workflow: str, **changes: object) -> dict[str, object]:
        request: dict[str, object] = {
            "schema_version": 1,
            "workflow": workflow,
            "timezone": "Europe/Berlin",
            "start_date": "2026-01-10",
            "recurrence": {
                "kind": "daily",
                "interval": 1,
                "local_time": "08:00",
                "weekdays": [],
            },
            "dependency": {"present": True, "revision": "synthetic-v1"},
        }
        request.update(changes)
        return request

    @property
    def now(self) -> dt.datetime:
        return dt.datetime(2026, 1, 10, 9, tzinfo=dt.timezone.utc)

    def test_malformed_recurrence_fails_closed_before_private_read(self) -> None:
        self.overlay.unlink()
        request = self.request(
            "daily-intake-closeout",
            recurrence={"kind": "weekly", "interval": 1, "local_time": "08:00", "weekdays": []},
        )
        with self.assertRaisesRegex(ContractError, "weekdays"):
            WorkflowRunner(self.state).run(request, self.overlay, self.now)
        self.assertFalse(self.state.exists())

    def test_noop_gates_do_not_read_private_overlay_or_create_state(self) -> None:
        self.overlay.unlink()
        cases = [
            self.request("daily-intake-closeout", start_date="2026-01-11"),
            self.request(
                "daily-intake-closeout",
                dependency={"present": False, "revision": "synthetic-v1"},
            ),
        ]
        for request in cases:
            with self.subTest(request=request):
                self.assertEqual(
                    WorkflowRunner(self.state).run(request, self.overlay, self.now), ""
                )
        self.assertFalse(self.state.exists())

    def test_exact_script_only_outputs_for_all_three_workflows(self) -> None:
        outputs = {}
        for workflow in (
            "weight-check-in-and-trend",
            "daily-intake-closeout",
            "household-portion-planning",
        ):
            self.write_overlay(workflow)
            outputs[workflow] = WorkflowRunner(self.state / workflow).run(
                self.request(workflow), self.overlay, self.now
            )
        self.assertEqual(outputs["weight-check-in-and-trend"], "Weight check-in is due.\n")
        self.assertEqual(
            outputs["daily-intake-closeout"],
            "Daily intake closeout: 150.00 kcal; protein 15.00 g; carbohydrate 30.00 g; fat 7.50 g; energy uncertainty ±10.00%.\n",
        )
        self.assertEqual(
            outputs["household-portion-planning"],
            "Household portions: 3; per portion 100.00 kcal, protein 10.00 g, carbohydrate 20.00 g, fat 5.00 g; energy uncertainty ±10.00%.\n",
        )

    def test_restart_and_concurrency_emit_once_and_state_contains_no_private_values(self) -> None:
        request = self.request("daily-intake-closeout")
        outputs: list[str] = []
        barrier = threading.Barrier(4)

        def invoke() -> None:
            barrier.wait()
            outputs.append(WorkflowRunner(self.state).run(request, self.overlay, self.now))

        threads = [threading.Thread(target=invoke) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(bool(output) for output in outputs), 1)
        self.assertEqual(WorkflowRunner(self.state).run(request, self.overlay, self.now), "")
        state_bytes = b"".join(
            path.read_bytes() for path in self.state.rglob("*") if path.is_file()
        )
        self.assertNotIn(b"private-sentinel", state_bytes)
        self.assertNotIn(b"150.00", state_bytes)

    def test_legacy_acknowledged_state_migrates_to_rendered_without_duplicate_output(self) -> None:
        request = self.request("daily-intake-closeout")
        store = GenerationStore(
            self.state,
            protocol="nutrition-due-state",
            schema_version=1,
        )
        identity = "daily-intake-closeout:2026-01-10"
        store.publish(
            {
                "due-state.json": {
                    "schema_version": 1,
                    "acknowledged_occurrences": [identity],
                }
            }
        )

        self.assertEqual(WorkflowRunner(self.state).run(request, self.overlay, self.now), "")
        self.assertEqual(
            store.read().documents["due-state.json"],
            {
                "schema_version": 2,
                "rendered_occurrences": [identity],
            },
        )

    def test_private_overlay_requires_regular_mode_0400_file(self) -> None:
        self.overlay.chmod(0o600)
        with self.assertRaisesRegex(ContractError, "0400"):
            read_private_overlay(self.overlay)
        target = self.root / "target.json"
        self.overlay.rename(target)
        os.symlink(target, self.overlay)
        with self.assertRaisesRegex(ContractError, "non-symlink"):
            read_private_overlay(self.overlay)

    def test_cli_noop_is_zero_bytes_and_errors_redact_private_values(self) -> None:
        request_path = self.root / "preferences.json"
        request_path.write_text(
            json.dumps(self.request("daily-intake-closeout", start_date="2026-01-11")),
            encoding="utf-8",
        )
        command = [
            sys.executable,
            "-m",
            "hermes_nutrition_workflows",
            "--preferences",
            str(request_path),
            "--private-overlay",
            str(self.root / "missing-private-sentinel.json"),
            "--state-dir",
            str(self.state),
            "--now",
            "2026-01-10T09:00:00Z",
        ]
        no_op = subprocess.run(command, check=False, capture_output=True)
        self.assertEqual(no_op.returncode, 0)
        self.assertEqual(no_op.stdout, b"")
        self.assertEqual(no_op.stderr, b"")
        self.assertFalse(self.state.exists())

        request_path.write_text(json.dumps(self.request("daily-intake-closeout")), encoding="utf-8")
        self.overlay.chmod(0o600)
        failed_command = list(command)
        failed_command[6] = str(self.overlay)
        failed = subprocess.run(failed_command, check=False, capture_output=True)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, b"")
        self.assertEqual(
            failed.stderr,
            b"nutrition workflow input or state failed validation\n",
        )
        self.assertNotIn(b"private-sentinel", failed.stderr)

    def test_dependency_revision_change_does_not_duplicate_rendered_logical_occurrence(
        self,
    ) -> None:
        request = self.request("daily-intake-closeout")
        runner = WorkflowRunner(self.state)
        self.assertNotEqual(runner.run(request, self.overlay, self.now), "")
        corrected = self.request(
            "daily-intake-closeout",
            dependency={"present": True, "revision": "synthetic-v2"},
        )
        self.assertEqual(runner.run(corrected, self.overlay, self.now), "")

    def test_local_day_dst_gap_and_fold_have_stable_logical_occurrences(self) -> None:
        preferences = self.request(
            "daily-intake-closeout",
            start_date="2026-03-29",
            recurrence={"kind": "daily", "interval": 1, "local_time": "02:30", "weekdays": []},
        )
        # Europe/Berlin 02:30 is absent on spring-forward day; policy advances
        # to the first valid local minute (03:00, 01:00Z).
        self.assertEqual(
            WorkflowRunner(self.state / "gap").run(
                preferences, self.overlay, dt.datetime(2026, 3, 29, 0, 59, tzinfo=dt.timezone.utc)
            ),
            "",
        )
        self.assertNotEqual(
            WorkflowRunner(self.state / "gap").run(
                preferences, self.overlay, dt.datetime(2026, 3, 29, 1, 0, tzinfo=dt.timezone.utc)
            ),
            "",
        )
        fold = self.request(
            "daily-intake-closeout",
            start_date="2026-10-25",
            recurrence={"kind": "daily", "interval": 1, "local_time": "02:30", "weekdays": []},
        )
        runner = WorkflowRunner(self.state / "fold")
        self.assertNotEqual(
            runner.run(
                fold, self.overlay, dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.timezone.utc)
            ),
            "",
        )
        self.assertEqual(
            runner.run(
                fold, self.overlay, dt.datetime(2026, 10, 25, 1, 30, tzinfo=dt.timezone.utc)
            ),
            "",
        )

    def test_prompt_does_not_bypass_private_data_schema(self) -> None:
        data = json.loads(json.dumps(self.private_data["daily-intake-closeout"]))
        data["data_quality_questions"] = ["Resolve the synthetic uncertainty."]
        data["unexpected_private_policy"] = "forbidden"
        self.write_overlay("daily-intake-closeout", data)
        with self.assertRaisesRegex(ContractError, "daily closeout data"):
            WorkflowRunner(self.state).run(
                self.request("daily-intake-closeout"), self.overlay, self.now
            )

    def test_actionable_data_quality_prompt_is_bounded_and_conditional(self) -> None:
        data = json.loads(json.dumps(self.private_data["daily-intake-closeout"]))
        data["data_quality_questions"] = [
            "Confirm the synthetic unit before finalizing the closeout."
        ]
        self.write_overlay("daily-intake-closeout", data)
        output = WorkflowRunner(self.state).run(
            self.request("daily-intake-closeout"), self.overlay, self.now
        )
        self.assertTrue(output.startswith("ACTIONABLE NUTRITION DATA-QUALITY PROMPT\n"))
        self.assertIn("Confirm the synthetic unit", output)
        self.assertLessEqual(len(output.encode("utf-8")), 1200)
        self.assertEqual(
            WorkflowRunner(self.state / "not-due").run(
                self.request("daily-intake-closeout", start_date="2026-01-11"),
                self.overlay,
                self.now,
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
