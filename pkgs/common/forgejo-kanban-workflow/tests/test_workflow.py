from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path

from forgejo_event_journal import stable_event_id
from forgejo_kanban_workflow import (
    ForgejoClient,
    InputError,
    JournalCli,
    MappingStore,
    NativeKanban,
    event_comment,
    evidence_digest,
    execution_key,
    logical_key,
    marker,
    reconcile,
    strict_json,
    validate_event,
)

EVENT_ID = "fj-" + "a" * 64
SECOND_EVENT_ID = "fj-" + "b" * 64
HEAD = "1" * 40


def event(
    *,
    disposition: str = "actionable",
    kind: str = "comment",
    target_kind: str = "pull_request",
    number: int = 7,
    event_seed: str = EVENT_ID,
) -> dict:
    segment = "pulls" if target_kind == "pull_request" else "issues"
    row = {
        "schema_version": 2,
        "event_id": "",
        "repository": "nixos/nix-config",
        "kind": kind,
        "source_key": f"fixture:1:{event_seed.removeprefix('fj-')}",
        "revision": "2026-08-28T05:00:00Z",
        "observed_at": "2026-08-28T05:00:01Z",
        "target": {
            "kind": target_kind,
            "number": number,
            "url": f"https://git.example.test/nixos/nix-config/{segment}/{number}",
        },
        "disposition": disposition,
        "summary": "source event",
        "payload_digest": "2" * 64,
        "sources": ["poll"],
    }
    row["event_id"] = stable_event_id(row)
    return row


class FakeJournal:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.acks: list[tuple[str, str]] = []

    def pending(self) -> list[dict]:
        return [validate_event(dict(row)) for row in self.rows]

    def ack(self, event_id: str, proof_id: str) -> None:
        self.acks.append((event_id, proof_id))


class FakeForgejo:
    def __init__(self, snapshots: dict[str, dict] | None = None) -> None:
        self.snapshots = snapshots or {}
        self.calls: list[str] = []

    def snapshot(self, row: dict) -> dict:
        self.calls.append(row["event_id"])
        return self.snapshots.get(
            row["event_id"],
            {
                "target": row["target"]["kind"],
                "number": row["target"]["number"],
                "url": f"https://git.example.test/nixos/nix-config/pulls/{row['target']['number']}",
                "head": HEAD if row["target"]["kind"] == "pull_request" else None,
                "pull": {"number": row["target"]["number"], "title": "Fix workflow"},
                "reviews": [
                    {
                        "review": {"id": 9, "state": "REQUEST_CHANGES", "commit_id": HEAD},
                        "comments": [{"id": 44}],
                    }
                ],
                "statuses": [],
            },
        )


class JournalCliTests(unittest.TestCase):
    def test_pending_returns_deterministic_bounded_slice_of_large_backlog(self) -> None:
        rows = [event(event_seed=f"fj-{index:064x}") for index in range(501)]

        def runner(*_args, **_kwargs):
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"schema_version": 2, "events": rows}),
                stderr="",
            )

        pending = JournalCli(Path("/journal"), Path("/state"), runner=runner).pending()
        self.assertEqual(len(pending), 500)
        self.assertEqual(pending[0]["event_id"], rows[0]["event_id"])
        self.assertEqual(pending[-1]["event_id"], rows[499]["event_id"])


class FakeKanban:
    board = "homelab-devops"

    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}
        self.comments: dict[str, list[dict]] = {}
        self.created: list[tuple[str, bool]] = []
        self.woken: list[str] = []

    def ensure_task(self, row: dict, snapshot: dict, continuation: bool) -> dict:
        task_id = "t_deadbeef" if not continuation else "t_cafebabe"
        self.tasks[task_id] = {"id": task_id, "status": "ready", "title": "workflow"}
        self.comments.setdefault(task_id, [])
        self.created.append((row["event_id"], continuation))
        return self.show(task_id)

    def show(self, task_id: str) -> dict:
        return {
            "task": self.tasks[task_id],
            "comments": list(self.comments[task_id]),
            "events": [],
            "runs": [],
        }

    def comment_once(self, task_id: str, body: str, event_id: str) -> None:
        if not any(marker(event_id) in row["body"] for row in self.comments[task_id]):
            self.comments[task_id].append({"author": "reconciler", "body": body})

    def wake_actionable(self, task: dict, row: dict) -> None:
        self.woken.append(task["task"]["id"])


class ValidationTests(unittest.TestCase):
    def test_forgejo_actions_requires_positive_execution_evidence(self) -> None:
        from forgejo_kanban_workflow import executed_ci_success

        status = {"status": "success", "target_url": "/nixos/nix-config/actions/runs/23/jobs/0"}
        for description in (
            None,
            "",
            "Has been skipped",
            "Has been cancelled",
            "Unknown result",
            "Successful",
            "Successful in unknown",
            "Successful in 3s (skipped)",
            "Not Successful in 3s",
            "Successful in -3s",
            "Successful in 3s extra",
        ):
            with self.subTest(description=description):
                self.assertFalse(executed_ci_success({**status, "description": description}))
        # Minimal offline fixtures from PR221-Review-48009557 live readback.
        for description in (
            "Has succeeded",
            "Successful in 3s",
            "Successful in 52s",
            "Successful in 1m14s",
        ):
            with self.subTest(description=description):
                self.assertTrue(executed_ci_success({**status, "description": description}))
                for state in ("pending", "failure", "error", "warning"):
                    self.assertFalse(
                        executed_ci_success({**status, "status": state, "description": description})
                    )
        self.assertFalse(
            executed_ci_success({**status, "status": "pending", "description": "Has succeeded"})
        )
        self.assertTrue(
            executed_ci_success({"status": "success", "description": "External check passed"})
        )

    def test_readiness_requires_exact_head_review_and_all_required_contexts(self) -> None:
        from forgejo_kanban_workflow import pr_readiness

        snapshot = {
            "head": HEAD,
            "pull": {"state": "open", "draft": False, "merged": False, "mergeable": True},
            "branch": {"enable_status_check": True, "status_check_contexts": ["CI / checks"]},
            "statuses": [{"id": 1, "context": "CI / checks", "status": "success"}],
            "reviews": [
                {
                    "review": {
                        "id": 4,
                        "commit_id": HEAD,
                        "state": "APPROVED",
                        "stale": False,
                        "dismissed": False,
                        "user": {"login": "reviewer"},
                    },
                    "comments": [],
                }
            ],
        }
        self.assertTrue(pr_readiness(snapshot, reviewer="reviewer")["ready"])
        for field, value in (
            ("commit_id", "2" * 40),
            ("stale", True),
            ("state", "REQUEST_CHANGES"),
        ):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(snapshot))
                changed["reviews"][0]["review"][field] = value
                self.assertFalse(pr_readiness(changed, reviewer="reviewer")["ready"])
        snapshot["statuses"] = []
        self.assertFalse(pr_readiness(snapshot, reviewer="reviewer")["ready"])

    def test_readiness_rejects_failed_rerun_and_unresolved_old_inline_findings(self) -> None:
        from forgejo_kanban_workflow import pr_readiness

        snapshot = {
            "head": HEAD,
            "pull": {"state": "open", "draft": False, "merged": False, "mergeable": True},
            "branch": {"enable_status_check": True, "status_check_contexts": ["CI / *"]},
            "statuses": [
                {"id": 3, "context": "CI / tests", "status": "pending"},
                {"id": 1, "context": "CI / tests", "status": "success"},
            ],
            "reviews": [
                {
                    "review": {
                        "id": 4,
                        "commit_id": HEAD,
                        "state": "APPROVED",
                        "stale": False,
                        "dismissed": False,
                        "user": {"login": "reviewer"},
                    },
                    "comments": [{"id": 10, "commit_id": "2" * 40, "resolver": None}],
                }
            ],
        }
        result = pr_readiness(snapshot, reviewer="reviewer")
        self.assertFalse(result["ready"])
        self.assertIn("unresolved-inline:10", result["reasons"])
        self.assertIn("unsuccessful-ci:CI / *", result["reasons"])
        snapshot["statuses"][0]["status"] = "success"
        snapshot["reviews"][0]["comments"][0]["resolver"] = {"login": "reviewer"}
        self.assertTrue(pr_readiness(snapshot, reviewer="reviewer")["ready"])
        snapshot["branch"]["status_check_contexts"] = []
        self.assertFalse(pr_readiness(snapshot, reviewer="reviewer")["ready"])

    def test_readiness_does_not_discard_another_reviewers_request_for_changes(self) -> None:
        from forgejo_kanban_workflow import pr_readiness

        def review(identity, user, state, head=HEAD):
            return {
                "review": {
                    "id": identity,
                    "commit_id": head,
                    "state": state,
                    "stale": head != HEAD,
                    "dismissed": False,
                    "user": {"login": user},
                },
                "comments": [],
            }

        snapshot = {
            "head": HEAD,
            "pull": {"state": "open", "draft": False, "merged": False, "mergeable": True},
            "branch": {"enable_status_check": True, "status_check_contexts": ["CI"]},
            "statuses": [{"id": 1, "context": "CI", "status": "success"}],
            "reviews": [
                review(1, "human", "REQUEST_CHANGES", "2" * 40),
                review(2, "reviewer", "APPROVED"),
            ],
        }
        self.assertFalse(pr_readiness(snapshot, reviewer="reviewer")["ready"])
        snapshot["reviews"].append(review(3, "human", "APPROVED"))
        self.assertTrue(pr_readiness(snapshot, reviewer="reviewer")["ready"])

    def test_strict_json_rejects_duplicate_keys_and_non_finite_numbers(self) -> None:
        with self.assertRaises(InputError):
            strict_json('{"x":1,"x":2}')
        with self.assertRaises(InputError):
            strict_json('{"x":NaN}')

    def test_event_binds_target_identity(self) -> None:
        row = validate_event(event())
        self.assertEqual(row["logical_key"], "forgejo:nixos/nix-config:pull_request:7")
        wrong = event()
        wrong["target"]["url"] = "https://git.example.test/issues/7"
        with self.assertRaises(InputError):
            validate_event(wrong)

    def test_event_rejects_unbound_source_transport_and_digest(self) -> None:
        wrong = event()
        wrong["sources"] = ["webhook"]
        with self.assertRaises(InputError):
            validate_event(wrong)
        wrong = event()
        wrong["payload_digest"] = "not-a-digest"
        with self.assertRaises(InputError):
            validate_event(wrong)

    def test_event_rejects_id_not_bound_to_payload(self) -> None:
        wrong = event()
        wrong["event_id"] = "fj-" + "c" * 64
        with self.assertRaises(InputError):
            validate_event(wrong)

    def test_logical_and_execution_identities_remain_distinct(self) -> None:
        logical = logical_key("issue", 3)
        self.assertEqual(execution_key(logical, EVENT_ID, False), logical)
        self.assertEqual(execution_key(logical, EVENT_ID, True), logical + ":epoch:" + EVENT_ID)

    def test_marker_is_exact(self) -> None:
        self.assertEqual(marker(EVENT_ID), "nix-config-forgejo-event:" + EVENT_ID)
        with self.assertRaises(InputError):
            marker("fj-short")


class MappingTests(unittest.TestCase):
    def test_mapping_survives_restart_and_preserves_task_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mapping"
            first = validate_event(event())
            store = MappingStore(root)
            store.put(first, "t_deadbeef", continuation=False)
            restarted = MappingStore(root)
            row = restarted.read()[first["logical_key"]]
            self.assertEqual(row["task_ids"], ["t_deadbeef"])
            second = validate_event(event(event_seed=SECOND_EVENT_ID))
            restarted.put(second, "t_cafebabe", continuation=True)
            row = MappingStore(root).read()[first["logical_key"]]
            self.assertEqual(row["task_ids"], ["t_deadbeef", "t_cafebabe"])
            self.assertEqual(row["execution_epoch"], second["event_id"])

    def test_mapping_rejects_symlinked_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside"
            outside.mkdir()
            root = Path(tmp) / "mapping"
            root.symlink_to(outside, target_is_directory=True)
            with self.assertRaises((InputError, OSError, RuntimeError)):
                MappingStore(root).put(validate_event(event()), "t_deadbeef", continuation=False)


class ReconcileTests(unittest.TestCase):
    def test_all_f10_event_kinds_use_one_logical_native_route(self) -> None:
        kinds = ["issue", "comment", "pull_request", "review", "status"]
        rows = [
            event(kind=kind, event_seed="fj-" + f"{index:x}" * 64)
            for index, kind in enumerate(kinds, 1)
        ]
        kanban = FakeKanban()
        with tempfile.TemporaryDirectory() as tmp:
            reconcile(FakeJournal(rows), FakeForgejo(), kanban, MappingStore(Path(tmp) / "state"))
        self.assertEqual(kanban.created, [(rows[0]["event_id"], False)])
        self.assertEqual(len(kanban.comments["t_deadbeef"]), len(rows))

    def test_evidence_without_route_is_zero_wake_and_zero_kanban_mutation(self) -> None:
        journal = FakeJournal([event(disposition="evidence", kind="status")])
        forgejo = FakeForgejo()
        kanban = FakeKanban()
        with tempfile.TemporaryDirectory() as tmp:
            result = reconcile(journal, forgejo, kanban, MappingStore(Path(tmp) / "state"))
        self.assertEqual(result["noop"], 1)
        self.assertEqual(kanban.created, [])
        self.assertEqual(kanban.woken, [])
        self.assertTrue(journal.acks[0][1].startswith("evidence-no-route:"))

    def test_actionable_event_creates_one_native_route_comments_and_wakes(self) -> None:
        journal = FakeJournal([event()])
        kanban = FakeKanban()
        with tempfile.TemporaryDirectory() as tmp:
            state = MappingStore(Path(tmp) / "state")
            result = reconcile(journal, FakeForgejo(), kanban, state)
            self.assertEqual(result["created"], 1)
            self.assertEqual(kanban.woken, ["t_deadbeef"])
            comment = kanban.comments["t_deadbeef"][0]["body"]
            self.assertIn("Formal reviews inspected: 1; inline comments inspected: 1", comment)
            self.assertIn("Exact PR head: " + HEAD, comment)
            self.assertEqual(
                state.read()[logical_key("pull_request", 7)]["current_task_id"], "t_deadbeef"
            )
        self.assertTrue(journal.acks[0][1].startswith("kanban:homelab-devops:t_deadbeef:"))

    def test_replay_is_idempotent_across_restart(self) -> None:
        row = event()
        kanban = FakeKanban()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            first = FakeJournal([row])
            reconcile(first, FakeForgejo(), kanban, MappingStore(root))
            second = FakeJournal([row])
            reconcile(second, FakeForgejo(), kanban, MappingStore(root))
        self.assertEqual(kanban.created, [(row["event_id"], False)])
        self.assertEqual(len(kanban.comments["t_deadbeef"]), 1)

    def test_actionable_event_after_done_creates_distinct_continuation_epoch(self) -> None:
        kanban = FakeKanban()
        first = event()
        second = event(event_seed=SECOND_EVENT_ID)
        with tempfile.TemporaryDirectory() as tmp:
            store = MappingStore(Path(tmp) / "state")
            reconcile(FakeJournal([first]), FakeForgejo(), kanban, store)
            kanban.tasks["t_deadbeef"]["status"] = "done"
            result = reconcile(FakeJournal([second]), FakeForgejo(), kanban, store)
            mapping = store.read()[logical_key("pull_request", 7)]
        self.assertEqual(result["continued"], 1)
        self.assertEqual(mapping["task_ids"], ["t_deadbeef", "t_cafebabe"])

    def test_evidence_on_existing_route_does_not_enter_live_comment_bridge(self) -> None:
        actionable = event()
        evidence = event(disposition="evidence", kind="status", event_seed=SECOND_EVENT_ID)
        kanban = FakeKanban()
        with tempfile.TemporaryDirectory() as tmp:
            store = MappingStore(Path(tmp) / "state")
            reconcile(FakeJournal([actionable]), FakeForgejo(), kanban, store)
            kanban.woken.clear()
            reconcile(FakeJournal([evidence]), FakeForgejo(), kanban, store)
        self.assertEqual(kanban.woken, [])
        self.assertEqual(len(kanban.comments["t_deadbeef"]), 1)

    def test_concurrent_same_event_converges_on_mapping(self) -> None:
        # Native idempotency is represented by the fake returning one task id.
        row = event()
        kanban = FakeKanban()
        errors: list[Exception] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"

            def worker() -> None:
                try:
                    reconcile(FakeJournal([row]), FakeForgejo(), kanban, MappingStore(root))
                except (InputError, RuntimeError, OSError, KeyError) as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            mapping = MappingStore(root).read()[logical_key("pull_request", 7)]
        self.assertEqual(errors, [])
        self.assertEqual(mapping["task_ids"], ["t_deadbeef"])
        self.assertEqual(len(kanban.created), 1)


class NativeKanbanTests(unittest.TestCase):
    def test_outcome_rejects_ambiguous_anchor_and_wrong_owner_before_writing(self):
        url = "https://git.example.test/nixos/nix-config/pulls/7"
        snapshot = {
            "url": url,
            "number": 7,
            "head": HEAD,
            "pull": {"state": "closed", "merged": True, "merge_commit_sha": "2" * 40},
        }
        anchor = {
            "id": "t_aaaaaaaa",
            "assignee": "system-admin",
            "status": "blocked",
            "body": f"PR workflow: native-v1\nForgejo authority: {url}\n",
        }
        for rows in (
            [anchor, {**anchor, "id": "t_bbbbbbbb"}],
            [{**anchor, "assignee": "code-reviewer"}],
        ):
            calls = []

            def runner(command, **kwargs):
                calls.append(command[4:])
                return subprocess.CompletedProcess(command, 0, json.dumps(rows), "")

            native = NativeKanban(
                Path("/fixture/hermes"), "homelab-devops", Path("/fixture/home"), runner
            )
            with self.assertRaisesRegex(InputError, "ambiguous or wrongly owned"):
                native.ensure_outcome(snapshot)
            self.assertEqual(calls, [["list", "--archived", "--json"]])

    def test_cli_uses_explicit_board_and_json_envelopes(self) -> None:
        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            payload = {
                "task": {"id": "t_deadbeef", "status": "ready"},
                "comments": [],
                "events": [],
                "runs": [],
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        adapter = NativeKanban(
            Path("/nix/store/hermes/bin/hermes"), "homelab-devops", Path("/var/lib/hermes"), runner
        )
        shown = adapter.show("t_deadbeef")
        self.assertEqual(shown["task"]["id"], "t_deadbeef")
        self.assertEqual(
            calls[0][:5],
            ["/nix/store/hermes/bin/hermes", "kanban", "--board", "homelab-devops", "show"],
        )

    def test_comment_response_loss_reconciles_by_marker(self) -> None:
        comments: list[dict] = []
        lost = {"once": False}

        def runner(command, **kwargs):
            action = command[4]
            if action == "show":
                payload = {
                    "task": {"id": "t_deadbeef", "status": "ready"},
                    "comments": comments,
                    "events": [],
                    "runs": [],
                }
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
            if action == "comment":
                comments.append({"author": command[8], "body": command[6]})
                if not lost["once"]:
                    lost["once"] = True
                    return subprocess.CompletedProcess(command, 1, "", "response lost")
                return subprocess.CompletedProcess(command, 0, "Comment added\n", "")
            raise AssertionError(command)

        adapter = NativeKanban(
            Path("/nix/store/hermes/bin/hermes"), "homelab-devops", Path("/var/lib/hermes"), runner
        )
        body = marker(EVENT_ID) + "\nevidence"
        adapter.comment_once("t_deadbeef", body, EVENT_ID)
        adapter.comment_once("t_deadbeef", body, EVENT_ID)
        self.assertEqual(len(comments), 1)

    def test_review_task_needs_no_nonexistent_reopen_transition(self) -> None:
        def runner(command, **kwargs):
            raise AssertionError(command)

        adapter = NativeKanban(
            Path("/nix/store/hermes/bin/hermes"),
            "homelab-devops",
            Path("/var/lib/hermes"),
            runner,
        )
        adapter.wake_actionable(
            {"task": {"id": "t_deadbeef", "status": "review"}},
            {"event_id": EVENT_ID},
        )

    def test_wrong_show_envelope_fails_closed(self) -> None:
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, json.dumps({"id": "t_deadbeef"}), "")

        adapter = NativeKanban(
            Path("/nix/store/hermes/bin/hermes"), "homelab-devops", Path("/var/lib/hermes"), runner
        )
        with self.assertRaises(RuntimeError):
            adapter.show("t_deadbeef")

    def test_untrusted_marker_comment_cannot_suppress_authenticated_exact_comment(self) -> None:
        body = marker(EVENT_ID) + "\nevidence"
        comments = [{"author": "attacker", "body": "preseed " + marker(EVENT_ID)}]

        def runner(command, **kwargs):
            if command[4] == "show":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "task": {"id": "t_deadbeef", "status": "ready"},
                            "comments": comments,
                            "events": [],
                            "runs": [],
                        }
                    ),
                    "",
                )
            comments.append({"author": command[8], "body": command[6]})
            return subprocess.CompletedProcess(command, 0, "Comment added\n", "")

        adapter = NativeKanban(
            Path("/nix/store/hermes/bin/hermes"), "homelab-devops", Path("/var/lib/hermes"), runner
        )
        adapter.comment_once("t_deadbeef", body, EVENT_ID)
        self.assertEqual(comments[-1], {"author": "forgejo-kanban-reconciler", "body": body})


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int) -> bytes:
        return self.raw


class ForgejoProtocolTests(unittest.TestCase):
    def credential(self, root: Path) -> Path:
        path = root / "credential"
        path.write_text("secret-token\n")
        path.chmod(0o400)
        return path

    def test_snapshot_inspects_every_review_inline_comment_and_rechecks_head(self) -> None:
        calls: list[str] = []

        def opener(request, timeout):
            path = urllib.parse.urlsplit(request.full_url).path
            calls.append(path)
            if path.endswith("/pulls/7"):
                return FakeResponse({"number": 7, "title": "PR", "head": {"sha": HEAD}})
            if path.endswith("/pulls/7/reviews"):
                return FakeResponse([{"id": 9, "state": "APPROVED", "commit_id": HEAD}])
            if path.endswith("/reviews/9/comments"):
                return FakeResponse([{"id": 11, "body": "inline"}])
            if path.endswith(f"/commits/{HEAD}/statuses"):
                return FakeResponse([])
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            client = ForgejoClient("https://git.example.test", self.credential(Path(tmp)), opener)
            snapshot = client.snapshot(validate_event(event()))
        self.assertEqual(snapshot["reviews"][0]["comments"][0]["id"], 11)
        self.assertEqual(calls.count("/api/v1/repos/nixos/nix-config/pulls/7"), 2)

    def test_issue_snapshot_binds_exact_repository_url_and_comments(self) -> None:
        def opener(request, timeout):
            path = urllib.parse.urlsplit(request.full_url).path
            if path.endswith("/issues/4"):
                return FakeResponse({"number": 4, "title": "Issue"})
            if path.endswith("/issues/4/comments"):
                return FakeResponse([{"id": 11, "body": "follow-up"}])
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            client = ForgejoClient("https://git.example.test", self.credential(Path(tmp)), opener)
            snapshot = client.snapshot(
                validate_event(event(target_kind="issue", number=4, kind="issue"))
            )
        self.assertEqual(snapshot["url"], "https://git.example.test/nixos/nix-config/issues/4")
        self.assertEqual(snapshot["comments"][0]["id"], 11)

    def test_snapshot_rejects_head_change(self) -> None:
        heads = iter([HEAD, "3" * 40])

        def opener(request, timeout):
            path = urllib.parse.urlsplit(request.full_url).path
            if path.endswith("/pulls/7"):
                return FakeResponse({"number": 7, "head": {"sha": next(heads)}})
            if path.endswith(("/pulls/7/reviews", f"/commits/{HEAD}/statuses")):
                return FakeResponse([])
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            client = ForgejoClient("https://git.example.test", self.credential(Path(tmp)), opener)
            with self.assertRaises(RuntimeError):
                client.snapshot(validate_event(event()))

    def test_snapshot_rejects_outcome_change_during_read(self) -> None:
        reads = 0

        def opener(request, timeout):
            nonlocal reads
            path = urllib.parse.urlsplit(request.full_url).path
            if path.endswith("/pulls/7"):
                reads += 1
                return FakeResponse(
                    {
                        "number": 7,
                        "head": {"sha": HEAD},
                        "state": "open" if reads == 1 else "closed",
                        "merged": False,
                    }
                )
            return FakeResponse([])

        with tempfile.TemporaryDirectory() as tmp:
            client = ForgejoClient("https://git.example.test", self.credential(Path(tmp)), opener)
            with self.assertRaisesRegex(RuntimeError, "outcome changed"):
                client.snapshot(validate_event(event()))

    def test_guarded_reply_binds_target_marker_body_and_exact_readback_url(self) -> None:
        stored: list[dict] = []

        def opener(request, timeout):
            path = urllib.parse.urlsplit(request.full_url).path
            if path.endswith("/api/v1/user"):
                return FakeResponse({"login": "system-admin-agent"})
            if request.method == "GET" and path.endswith("/issues/4"):
                return FakeResponse({"number": 4, "title": "Issue"})
            if request.method == "POST" and path.endswith("/issues/4/comments"):
                body = json.loads(request.data)
                stored.append(
                    {
                        "id": 88,
                        "body": body["body"],
                        "user": {"login": "system-admin-agent"},
                        "html_url": "https://git.example.test/nixos/nix-config/issues/4#issuecomment-88",
                    }
                )
                return FakeResponse(stored[0])
            if request.method == "GET" and path.endswith("/issues/4/comments"):
                return FakeResponse(stored)
            raise AssertionError((request.method, path))

        with tempfile.TemporaryDirectory() as tmp:
            client = ForgejoClient("https://git.example.test", self.credential(Path(tmp)), opener)
            body = "Handled\n" + marker(EVENT_ID)
            result = client.guarded_reply("issue", 4, body, EVENT_ID)
        self.assertTrue(result["target_verified"])
        self.assertEqual(result["comment_id"], 88)

    def test_guarded_reply_ignores_marker_preseeded_by_another_forgejo_user(self) -> None:
        body = "Handled\n" + marker(EVENT_ID)
        stored = [
            {
                "id": 70,
                "body": body,
                "user": {"login": "attacker"},
                "html_url": "https://git.example.test/nixos/nix-config/issues/4#issuecomment-70",
            }
        ]

        def opener(request, timeout):
            path = urllib.parse.urlsplit(request.full_url).path
            if path.endswith("/api/v1/user"):
                return FakeResponse({"login": "system-admin-agent"})
            if request.method == "GET" and path.endswith("/issues/4"):
                return FakeResponse({"number": 4})
            if request.method == "GET" and path.endswith("/issues/4/comments"):
                return FakeResponse(stored)
            if request.method == "POST" and path.endswith("/issues/4/comments"):
                row = {
                    "id": 71,
                    "body": json.loads(request.data)["body"],
                    "user": {"login": "system-admin-agent"},
                    "html_url": "https://git.example.test/nixos/nix-config/issues/4#issuecomment-71",
                }
                stored.append(row)
                return FakeResponse(row)
            raise AssertionError((request.method, path))

        with tempfile.TemporaryDirectory() as tmp:
            client = ForgejoClient("https://git.example.test", self.credential(Path(tmp)), opener)
            result = client.guarded_reply("issue", 4, body, EVENT_ID)
        self.assertEqual(result["comment_id"], 71)
        self.assertEqual(len(stored), 2)

    def test_guarded_reply_rejects_wrong_target_and_missing_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = ForgejoClient(
                "https://git.example.test",
                self.credential(Path(tmp)),
                lambda *args, **kwargs: FakeResponse({}),
            )
            with self.assertRaises(InputError):
                client.guarded_reply("issue", 4, "no marker", EVENT_ID)

    def test_pending_review_publication_reads_back_state_head_and_inline_comments(self) -> None:
        phase = {"submitted": False}

        def opener(request, timeout):
            path = urllib.parse.urlsplit(request.full_url).path
            if path.endswith("/pulls/7"):
                return FakeResponse({"number": 7, "head": {"sha": HEAD}})
            if path.endswith("/pulls/7/reviews"):
                state = "APPROVED" if phase["submitted"] else "PENDING"
                return FakeResponse(
                    [
                        {
                            "id": 9,
                            "state": state,
                            "commit_id": HEAD,
                            "stale": False,
                            "official": phase["submitted"],
                            "body": "Reviewed" if phase["submitted"] else "Draft",
                        }
                    ]
                )
            if path.endswith("/reviews/9/comments"):
                return FakeResponse([{"id": 55, "body": "finding"}])
            if path.endswith(f"/commits/{HEAD}/statuses"):
                return FakeResponse([])
            if request.method == "POST" and path.endswith("/reviews/9"):
                phase["submitted"] = True
                return FakeResponse({"id": 9, "state": "PENDING"})
            raise AssertionError((request.method, path))

        with tempfile.TemporaryDirectory() as tmp:
            client = ForgejoClient("https://git.example.test", self.credential(Path(tmp)), opener)
            result = client.submit_pending_review(7, 9, "APPROVED", "Reviewed")
        self.assertEqual(result["head"], HEAD)
        self.assertEqual(result["review"]["state"], "APPROVED")
        self.assertEqual(result["comments"][0]["id"], 55)

    def test_pending_review_rejects_wrong_commit_body_and_official_state(self) -> None:
        phase = {"submitted": False}

        def opener(request, timeout):
            path = urllib.parse.urlsplit(request.full_url).path
            if path.endswith("/pulls/7"):
                return FakeResponse({"number": 7, "head": {"sha": HEAD}})
            if path.endswith("/pulls/7/reviews"):
                if not phase["submitted"]:
                    return FakeResponse(
                        [
                            {
                                "id": 9,
                                "state": "PENDING",
                                "commit_id": HEAD,
                                "stale": False,
                                "official": False,
                                "body": "Draft",
                            }
                        ]
                    )
                return FakeResponse(
                    [
                        {
                            "id": 9,
                            "state": "APPROVED",
                            "commit_id": "2" * 40,
                            "stale": False,
                            "official": False,
                            "body": "wrong body",
                        }
                    ]
                )
            if path.endswith(("/reviews/9/comments", f"/commits/{HEAD}/statuses")):
                return FakeResponse([])
            if request.method == "POST" and path.endswith("/reviews/9"):
                phase["submitted"] = True
                return FakeResponse({"id": 9})
            raise AssertionError((request.method, path))

        with tempfile.TemporaryDirectory() as tmp:
            client = ForgejoClient("https://git.example.test", self.credential(Path(tmp)), opener)
            with self.assertRaises(RuntimeError):
                client.submit_pending_review(7, 9, "APPROVED", "Reviewed")

    def test_redirect_is_not_followed_by_fixture_opener_and_auth_never_appears_in_error(
        self,
    ) -> None:
        def opener(request, timeout):
            headers = Message()
            headers["Location"] = "https://evil.test"
            raise urllib.error.HTTPError(request.full_url, 302, "redirect", headers, io.BytesIO())

        with tempfile.TemporaryDirectory() as tmp:
            client = ForgejoClient("https://git.example.test", self.credential(Path(tmp)), opener)
            with self.assertRaisesRegex(RuntimeError, "HTTP 302") as caught:
                client.get("/repos/nixos/nix-config/issues/1")
        self.assertNotIn("secret-token", str(caught.exception))


class EvidenceTests(unittest.TestCase):
    def test_comment_is_bound_to_exact_snapshot_and_not_authority(self) -> None:
        row = validate_event(event())
        snapshot = {
            "head": HEAD,
            "reviews": [{"review": {"id": 1}, "comments": []}],
            "statuses": [],
        }
        body = event_comment(row, snapshot)
        self.assertIn(evidence_digest(snapshot), body)
        self.assertIn(
            "not merge, deploy, rebuild, verification, closure, or human authorization", body
        )


if __name__ == "__main__":
    unittest.main()
