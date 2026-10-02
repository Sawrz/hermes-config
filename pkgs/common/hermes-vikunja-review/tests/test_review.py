from __future__ import annotations

import datetime as dt
import email.message
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from hermes_workflow_state import GenerationStore

from hermes_vikunja_review import (
    ContractError,
    FixtureClient,
    MutationPlan,
    VikunjaClient,
    apply_mutation,
    collect_review,
    effective_config,
    plan_mutation,
    recurrence_semantics,
    render_review,
    run_review,
    validate_config,
    verify_no_agent_evidence,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 6, 0, tzinfo=UTC)


def task(
    task_id: int,
    *,
    project_id: int = 20,
    title: str | None = None,
    due_date: str = "2026-08-28T08:00:00Z",
    repeat_after: int = 0,
    repeat_mode: int = 0,
    updated: str = "2026-08-27T10:00:00Z",
) -> dict[str, object]:
    return {
        "id": task_id,
        "title": title or f"Task {task_id}",
        "project_id": project_id,
        "due_date": due_date,
        "start_date": "0001-01-01T00:00:00Z",
        "done": False,
        "priority": 2,
        "repeat_after": repeat_after,
        "repeat_mode": repeat_mode,
        "updated": updated,
    }


def config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "api_version": "v2",
        "timezone": "Europe/Berlin",
        "inbox_project_id": 10,
        "limits": {
            "per_page": 2,
            "max_pages": 3,
            "max_tasks": 5,
            "timeout_seconds": 10,
        },
        "reviews": {
            "daily": {
                "sections": [
                    {"name": "Due", "role": "due", "saved_filter_id": 1},
                    {"name": "Starting", "role": "starting", "saved_filter_id": 2},
                ]
            },
            "weekly": {
                "sections": [
                    {"name": "Inbox", "role": "inbox", "saved_filter_id": 3},
                    {"name": "Waiting", "role": "waiting", "saved_filter_id": 4},
                ]
            },
        },
    }


def saved_filter(filter_id: int, expression: str = "done = false") -> dict[str, object]:
    return {
        "id": filter_id,
        "title": f"filter-{filter_id}",
        "filters": {
            "filter": expression,
            "filter_include_nulls": False,
            "s": "",
            "sort_by": ["due_date", "priority", "id"],
            "order_by": ["asc", "desc", "asc"],
        },
    }


class ContractTests(unittest.TestCase):
    def test_weekly_is_the_only_inbox_owner(self) -> None:
        valid = validate_config(config())
        self.assertEqual(valid["reviews"]["weekly"]["sections"][0]["role"], "inbox")

        for mutate in (
            lambda c: c["reviews"]["daily"]["sections"].append(
                {"name": "Inbox", "role": "inbox", "saved_filter_id": 8}
            ),
            lambda c: c["reviews"]["weekly"]["sections"].pop(0),
            lambda c: c["reviews"]["weekly"]["sections"].append(
                {"name": "Inbox 2", "role": "inbox", "saved_filter_id": 8}
            ),
        ):
            case = json.loads(json.dumps(config()))
            mutate(case)
            with self.subTest(case=case), self.assertRaisesRegex(ContractError, "Inbox"):
                validate_config(case)

    def test_config_is_bounded_and_rejects_implicit_authority(self) -> None:
        bad_cases = []
        for path, value in (
            (("api_version",), "v1"),
            (("limits", "max_pages"), 100),
            (("limits", "max_tasks"), 10000),
            (("limits", "timeout_seconds"), 0),
        ):
            case = json.loads(json.dumps(config()))
            target = case
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            bad_cases.append(case)
        bad_cases.append({**config(), "credential": "secret"})
        for case in bad_cases:
            with self.subTest(case=case), self.assertRaises(ContractError):
                validate_config(case)

    def test_saved_filter_ids_are_unique_across_daily_and_weekly(self) -> None:
        case = config()
        case["reviews"]["weekly"]["sections"][1]["saved_filter_id"] = 1
        with self.assertRaisesRegex(ContractError, "unique"):
            validate_config(case)

    def test_runtime_preferences_can_only_narrow_declarative_sections(self) -> None:
        preferences = {
            "schema_version": 1,
            "values": {
                "enabled": True,
                "daily_hour": 7,
                "weekly_hour": 7,
                "weekly_weekday": "sunday",
                "timezone": "Europe/Berlin",
                "render_style": "compact",
                "daily_sections": ["Due"],
                "weekly_sections": ["Inbox"],
            },
        }
        narrowed = effective_config(config(), preferences, "daily")
        self.assertEqual([s["name"] for s in narrowed["reviews"]["daily"]["sections"]], ["Due"])
        self.assertEqual(
            [s["name"] for s in narrowed["reviews"]["weekly"]["sections"]], ["Inbox", "Waiting"]
        )

    def test_runtime_preferences_reject_scope_and_schedule_expansion(self) -> None:
        baseline = {
            "schema_version": 1,
            "values": {
                "enabled": True,
                "daily_hour": 7,
                "weekly_hour": 7,
                "weekly_weekday": "sunday",
                "timezone": "Europe/Berlin",
                "render_style": "compact",
                "daily_sections": ["Due"],
                "weekly_sections": ["Inbox"],
            },
        }
        bad_cases = []
        for key, value in (
            ("daily_hour", 2),
            ("weekly_weekday", "monday"),
            ("daily_sections", ["Attacker Project"]),
            ("weekly_sections", ["Waiting"]),
        ):
            case = json.loads(json.dumps(baseline))
            case["values"][key] = value
            bad_cases.append(case)
        bad_cases.append(
            {"schema_version": 1, "values": {**baseline["values"], "destination": "other"}}
        )
        for case in bad_cases:
            with self.subTest(case=case), self.assertRaises(ContractError):
                effective_config(config(), case, "daily")


class CollectionTests(unittest.TestCase):
    def test_authenticated_saved_filter_and_task_reads_use_exact_v2_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            endpoint = root / "endpoint"
            credential = root / "credential"
            endpoint.write_text("https://todo.example.test/api/v2\n")
            credential.write_text("tk_fixture-secret\n")
            endpoint.chmod(0o400)
            credential.chmod(0o400)
            requests = []

            class Response:
                headers = email.message.Message()

                def __init__(self, payload: object):
                    self.payload = json.dumps(payload).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, _limit: int) -> bytes:
                    return self.payload

            class Opener:
                def open(self, request, timeout):
                    requests.append(
                        (request.full_url, request.get_header("Authorization"), timeout)
                    )
                    if request.full_url.endswith("/filters/1"):
                        return Response(saved_filter(1))
                    return Response(
                        {"items": [], "page": 1, "per_page": 2, "total": 0, "total_pages": 0}
                    )

            client = VikunjaClient(endpoint, credential, timeout=7)
            client.opener = Opener()
            self.assertEqual(client.get("/filters/1")["id"], 1)
            self.assertEqual(client.get("/tasks", {"page": "1", "per_page": "2"})["items"], [])
            self.assertEqual(
                requests,
                [
                    ("https://todo.example.test/api/v2/filters/1", "Bearer tk_fixture-secret", 7),
                    (
                        "https://todo.example.test/api/v2/tasks?page=1&per_page=2",
                        "Bearer tk_fixture-secret",
                        7,
                    ),
                ],
            )

    def fixture(
        self, pages: dict[int, list[dict[str, object]]], *, filter_ids=(1, 2)
    ) -> FixtureClient:
        responses: dict[tuple[str, tuple[tuple[str, str], ...]], object] = {}
        for filter_id in filter_ids:
            responses[(f"/filters/{filter_id}", ())] = saved_filter(filter_id)
            for page, items in pages.items():
                responses[
                    (
                        "/tasks",
                        (
                            ("filter", "done = false"),
                            ("filter_include_nulls", "false"),
                            ("order_by", "asc"),
                            ("order_by", "desc"),
                            ("order_by", "asc"),
                            ("page", str(page)),
                            ("per_page", "2"),
                            ("sort_by", "due_date"),
                            ("sort_by", "priority"),
                            ("sort_by", "id"),
                        ),
                    )
                ] = {
                    "$schema": "https://example.test/api/v2/schemas/paginated-task",
                    "items": items,
                    "page": page,
                    "per_page": 2,
                    "total": sum(len(rows) for rows in pages.values()),
                    "total_pages": len(pages),
                }
        return FixtureClient(responses)

    def test_collects_all_v2_pages_and_deduplicates_section_overlap(self) -> None:
        client = self.fixture({1: [task(2), task(1)], 2: [task(3)]})
        result = collect_review(client, validate_config(config()), "daily", NOW)
        self.assertEqual([row["id"] for row in result["sections"][0]["tasks"]], [1, 2, 3])
        self.assertEqual(result["sections"][1]["tasks"], [])
        self.assertTrue(all(method == "GET" for method, _path, _params in client.calls))
        self.assertEqual(len([call for call in client.calls if call[1] == "/tasks"]), 4)

    def test_cap_exhaustion_fails_instead_of_returning_partial_review(self) -> None:
        case = config()
        case["limits"]["max_pages"] = 1
        client = self.fixture({1: [task(1), task(2)], 2: [task(3)]})
        with self.assertRaisesRegex(ContractError, "page cap"):
            collect_review(client, validate_config(case), "daily", NOW)

    def test_malformed_pagination_fails_closed(self) -> None:
        client = self.fixture({1: [task(1)]})
        key = next(key for key in client.responses if key[0] == "/tasks")
        client.responses[key] = {"items": [task(1)], "page": 1}
        with self.assertRaisesRegex(ContractError, "pagination"):
            collect_review(client, validate_config(config()), "daily", NOW)

    def test_daily_saved_filter_leaking_inbox_task_fails_visibly(self) -> None:
        client = self.fixture({1: [task(1, project_id=10)]})
        with self.assertRaisesRegex(ContractError, "daily review.*Inbox"):
            collect_review(client, validate_config(config()), "daily", NOW)

    def test_weekly_inbox_section_rejects_non_inbox_tasks(self) -> None:
        client = self.fixture({1: [task(1, project_id=20)]}, filter_ids=(3, 4))
        with self.assertRaisesRegex(ContractError, "Inbox section"):
            collect_review(client, validate_config(config()), "weekly", NOW)

    def test_filter_identity_and_query_are_read_back_before_task_collection(self) -> None:
        client = self.fixture({1: [task(1)]})
        client.responses[("/filters/1", ())] = saved_filter(99)
        with self.assertRaisesRegex(ContractError, "identity"):
            collect_review(client, validate_config(config()), "daily", NOW)
        self.assertEqual([path for _method, path, _params in client.calls], ["/filters/1"])

    def test_saved_filter_v1_search_field_maps_to_v2_q_parameter(self) -> None:
        client = self.fixture({1: [task(1)]})
        client.responses[("/filters/1", ())]["filters"]["s"] = "needle"
        original_key = next(
            key for key in client.responses if key[0] == "/tasks" and ("page", "1") in key[1]
        )
        searched_key = (
            "/tasks",
            tuple(list(original_key[1][:-3]) + [("q", "needle")] + list(original_key[1][-3:])),
        )
        client.responses[searched_key] = client.responses[original_key]
        result = collect_review(client, validate_config(config()), "daily", NOW)
        self.assertEqual(result["sections"][0]["tasks"][0]["id"], 1)
        self.assertIn(
            ("q", "needle"),
            next(params for _method, path, params in client.calls if path == "/tasks"),
        )


class RenderingAndStateTests(unittest.TestCase):
    def test_render_is_deterministic_and_escapes_untrusted_titles(self) -> None:
        review = {
            "kind": "daily",
            "generated_at": "2026-08-28T06:00:00Z",
            "sections": [
                {
                    "name": "Due",
                    "role": "due",
                    "tasks": [task(1, title="[x] Inject\n# heading")],
                }
            ],
        }
        rendered = render_review(review)
        self.assertIn(r"\[x\] Inject \# heading", rendered)
        self.assertNotIn("\n# heading", rendered)
        self.assertEqual(rendered, render_review(review))

    def test_empty_and_unchanged_runs_are_silent_and_restart_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "state"
            empty_client = self._client([])
            self.assertEqual(run_review(empty_client, config(), "daily", state_dir, NOW), "")
            populated = self._client([task(1)])
            first = run_review(populated, config(), "daily", state_dir, NOW)
            self.assertIn("Task 1", first)
            restarted = self._client([task(1)])
            self.assertEqual(run_review(restarted, config(), "daily", state_dir, NOW), "")

    def test_collection_error_does_not_advance_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "state"
            good = self._client([task(1)])
            first = run_review(good, config(), "daily", state_dir, NOW)
            self.assertIn("Task 1", first)
            selector = (state_dir / "current.json").read_bytes()
            broken = FixtureClient({})
            with self.assertRaises(ContractError):
                run_review(broken, config(), "daily", state_dir, NOW)
            self.assertEqual((state_dir / "current.json").read_bytes(), selector)

    def test_legacy_success_state_migrates_to_rendered_without_duplicate_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "state"
            content = render_review(collect_review(self._client([task(1)]), config(), "daily", NOW))
            store = GenerationStore(
                state_dir,
                protocol="vikunja-reviews",
                schema_version=1,
            )
            store.publish(
                {
                    "review.json": {
                        "schema_version": 1,
                        "daily": {
                            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                            "last_success": "2026-08-28T06:00:00Z",
                        },
                        "weekly": {"content_sha256": None, "last_success": None},
                    }
                }
            )

            self.assertEqual(
                run_review(self._client([task(1)]), config(), "daily", state_dir, NOW), ""
            )
            self.assertEqual(
                store.read().documents["review.json"],
                {
                    "schema_version": 2,
                    "daily": {
                        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        "last_rendered": "2026-08-28T06:00:00Z",
                    },
                    "weekly": {"content_sha256": None, "last_rendered": None},
                },
            )

    def _client(self, rows: list[dict[str, object]]) -> FixtureClient:
        responses = {}
        for filter_id in (1, 2):
            responses[(f"/filters/{filter_id}", ())] = saved_filter(filter_id)
            responses[
                (
                    "/tasks",
                    (
                        ("filter", "done = false"),
                        ("filter_include_nulls", "false"),
                        ("order_by", "asc"),
                        ("order_by", "desc"),
                        ("order_by", "asc"),
                        ("page", "1"),
                        ("per_page", "2"),
                        ("sort_by", "due_date"),
                        ("sort_by", "priority"),
                        ("sort_by", "id"),
                    ),
                )
            ] = {
                "$schema": "https://example.test/api/v2/schemas/paginated-task",
                "items": rows,
                "page": 1,
                "per_page": 2,
                "total": len(rows),
                "total_pages": 1 if rows else 0,
            }
        return FixtureClient(responses)


class NoAgentEvidenceTests(unittest.TestCase):
    def evidence(self, outcome: str = "changed") -> dict[str, object]:
        return {
            "schema_version": "vikunja.no-agent.evidence.v1",
            "job": {
                "no_agent": True,
                "script": "/nix/store/example/bin/hermes-vikunja-review run --kind daily",
                "prompt": None,
                "skills": [],
            },
            "run": {
                "outcome": outcome,
                "execution_status": "success",
                "scheduler_executions": 1,
                "agent_sessions": 0,
                "model_attempts": 0,
                "model_successes": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "deliveries": 1 if outcome == "changed" else 0,
            },
        }

    def test_script_only_changed_and_silent_evidence(self) -> None:
        for outcome in ("changed", "empty", "unchanged"):
            verify_no_agent_evidence(self.evidence(outcome))

    def test_model_use_and_wrong_delivery_fail_evidence(self) -> None:
        for mutate in (
            lambda e: e["job"].update({"no_agent": False}),
            lambda e: e["job"].update({"prompt": "summarize"}),
            lambda e: e["run"].update({"model_attempts": 1}),
            lambda e: e["run"].update({"input_tokens": 1}),
            lambda e: e["run"].update({"deliveries": 0}),
        ):
            evidence = self.evidence()
            mutate(evidence)
            with self.subTest(evidence=evidence), self.assertRaises(ContractError):
                verify_no_agent_evidence(evidence)


class RecurrenceAndMutationTests(unittest.TestCase):
    def test_recurrence_semantics_never_calls_ninety_days_quarterly(self) -> None:
        self.assertEqual(recurrence_semantics(task(1, repeat_mode=1)), "monthly-calendar")
        self.assertEqual(
            recurrence_semantics(task(1, repeat_after=90 * 86400)), "duration-90-days-not-quarterly"
        )
        self.assertEqual(recurrence_semantics(task(1, repeat_after=7 * 86400)), "duration-7-days")

    def test_quarterly_mutation_is_rejected_not_approximated(self) -> None:
        with self.assertRaisesRegex(ContractError, "quarterly.*not representable"):
            plan_mutation(task(1), {"recurrence": "quarterly"})

    def test_interactive_mutation_requires_exact_preview_confirmation_and_readback(self) -> None:
        plan = plan_mutation(task(1), {"done": True})
        self.assertIsInstance(plan, MutationPlan)
        self.assertEqual(plan.patch, {"done": True})
        self.assertEqual(plan.source_updated, "2026-08-27T10:00:00Z")
        self.assertRegex(plan.confirmation, r"^CONFIRM VIKUNJA TASK 1 [0-9a-f]{64}$")
        self.assertEqual(plan.expected_readback, {"done": True})

    def test_mutation_scope_cannot_expand_via_arbitrary_fields(self) -> None:
        for patch in (
            {"project_id": 99},
            {"created_by": {"id": 1}},
            {"recurrence": "90-days"},
            {"done": 1},
        ):
            with self.subTest(patch=patch), self.assertRaises(ContractError):
                plan_mutation(task(1), patch)

    def test_apply_requires_confirmation_fresh_revision_and_exact_readback(self) -> None:
        plan = plan_mutation(task(1), {"done": True})
        client = MutationClient(task(1))
        readback = apply_mutation(client, plan, plan.confirmation)
        self.assertTrue(readback["done"])
        self.assertEqual(
            client.calls,
            [
                ("GET", "/tasks/1"),
                ("PATCH", "/tasks/1", {"done": True}),
                ("GET", "/tasks/1"),
            ],
        )

    def test_bad_confirmation_performs_no_request(self) -> None:
        plan = plan_mutation(task(1), {"done": True})
        client = MutationClient(task(1))
        with self.assertRaisesRegex(ContractError, "confirmation"):
            apply_mutation(client, plan, "CONFIRM SOMETHING ELSE")
        self.assertEqual(client.calls, [])

    def test_stale_preview_and_indeterminate_write_never_retry(self) -> None:
        plan = plan_mutation(task(1), {"done": True})
        stale = MutationClient(task(1, updated="2026-08-28T11:00:00Z"))
        with self.assertRaisesRegex(ContractError, "changed since preview"):
            apply_mutation(stale, plan, plan.confirmation)
        self.assertEqual(stale.calls, [("GET", "/tasks/1")])

        indeterminate = MutationClient(task(1), fail_patch=True)
        with self.assertRaisesRegex(ContractError, "indeterminate"):
            apply_mutation(indeterminate, plan, plan.confirmation)
        self.assertEqual(
            [call[0] for call in indeterminate.calls],
            ["GET", "PATCH"],
        )


class MutationClient:
    def __init__(self, current: dict[str, object], *, fail_patch: bool = False):
        self.current = dict(current)
        self.fail_patch = fail_patch
        self.calls: list[tuple] = []

    def patch(self, path: str, patch: dict[str, object]):
        self.calls.append(("PATCH", path, dict(patch)))
        if self.fail_patch:
            raise RuntimeError("lost response")
        self.current.update(patch)
        return dict(self.current)

    def get(self, path: str):
        self.calls.append(("GET", path))
        return dict(self.current)


if __name__ == "__main__":
    unittest.main()
