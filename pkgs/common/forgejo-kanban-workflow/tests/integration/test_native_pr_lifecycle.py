"""Pinned native create/dispatch/complete contract; worker launches are counted, not run."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import forgejo_kanban_workflow as w

from test_native_intake import NativeFixture


class NativePrLifecycleTests(NativeFixture):
    def prepare_handoff(self):
        from kanban_vikunja_projection import NativeKanban, parse_human_action
        from pr_projection_lifecycle import PrLifecycle
        from forgejo_kanban_workflow import pr_actionable_digest

        reviewer_home = self.home / "profiles/code-reviewer"
        reviewer_home.mkdir()
        (reviewer_home / "config.yaml").write_text("{}\n")
        head = "a" * 40
        url = "https://git.example.test/nixos/nix-config/pulls/200"
        anchor = self.kanban._run(
            [
                "create",
                "Permanent PR anchor",
                "--assignee",
                "system-admin",
                "--initial-status",
                "blocked",
                "--body",
                f"PR workflow: native-v1\nForgejo authority: {url}\n"
                "Human action: Merge\nVikunja project: 110\nVikunja assignee: none\nVikunja done: false\n"
                "Vikunja automation reference: nix-config-pr-200-merge\n",
                "--json",
            ],
            expect_json=True,
        )
        anchor_id = anchor["id"]
        anchor_ref = "default/" + anchor_id

        def create(phase, parent=None):
            args = [
                "create",
                phase,
                "--assignee",
                "code-reviewer" if phase == "review" else "system-admin",
                "--body",
                f"PR projection anchor: {anchor_ref}\nPR workflow phase: {phase}\n"
                f"PR head: {head}\nForgejo authority: {url}\n",
                "--idempotency-key",
                f"fixture-{phase}-{head}",
                "--json",
            ]
            if parent:
                args += ["--parent", parent]
            first = self.kanban._run(args, expect_json=True)
            self.assertEqual(self.kanban._run(args, expect_json=True)["id"], first["id"])
            return first["id"]

        def complete(task_id, phase):
            proof = {
                "anchor": anchor_ref,
                "phase": phase,
                "head": head,
                "result": "success",
                "evidence": ["isolated-native-fixture"],
            }
            if phase != "implementation":
                proof["review_id"] = 1
            if phase == "handoff":
                proof["actionable_digest"] = pr_actionable_digest(snapshot)
            self.kanban._run(
                [
                    "complete",
                    task_id,
                    "--summary",
                    "Isolated phase evidence",
                    "--metadata",
                    json.dumps({"pr_workflow": proof}),
                ]
            )

        implementation = create("implementation")
        self.assertEqual([r[0] for r in self.dispatch().spawned], [implementation])
        review = create("review", implementation)
        self.assertEqual(self.kanban.show(review)["task"]["status"], "todo")
        self.assertEqual(list(self.dispatch().spawned), [])
        complete(implementation, "implementation")
        self.assertEqual([r[0] for r in self.dispatch().spawned], [review])
        handoff = create("handoff", review)
        self.assertEqual(self.kanban.show(handoff)["task"]["status"], "todo")
        complete(review, "review")
        self.assertEqual([r[0] for r in self.dispatch().spawned], [handoff])
        snapshot = {
            "url": url,
            "head": head,
            "comments": [],
            "pull": {"state": "open", "merged": False, "draft": False, "mergeable": True},
            "branch": {"enable_status_check": True, "status_check_contexts": ["test"]},
            "statuses": [{"id": 1, "context": "test", "status": "success"}],
            "reviews": [
                {
                    "review": {
                        "id": 1,
                        "state": "APPROVED",
                        "commit_id": head,
                        "stale": False,
                        "dismissed": False,
                        "user": {"login": "reviewer"},
                    },
                    "comments": [],
                }
            ],
        }
        complete(handoff, "handoff")
        forge = type("Forge", (), {"readiness_snapshot": lambda _, n: snapshot})()
        native = NativeKanban(self.cli, "default", self.home)
        provider = PrLifecycle(forge, native, "default")
        for _ in range(2):
            task = native.show(anchor_id)["task"]
            self.assertEqual(
                provider.resolve(task, parse_human_action(task))[1]["assignee"], "sandro"
            )
            self.assertEqual(list(self.dispatch().spawned), [])
        self.assertEqual(self.spawned, [implementation, review, handoff])
        self.assertEqual(native.show(anchor_id)["task"]["status"], "blocked")

        return head, url, anchor_id, anchor_ref, snapshot, native, provider

    def test_review_handoff_uses_native_dependencies_and_projection_never_spawns(self):
        head, url, anchor_id, anchor_ref, snapshot, native, provider = self.prepare_handoff()
        from kanban_vikunja_projection import parse_human_action

        human = self.kanban._run(
            [
                "create",
                "Secret request",
                "--assignee",
                "system-admin",
                "--body",
                f"PR projection anchor: {anchor_ref}\nPR workflow phase: implementation\nPR head: {head}\nForgejo authority: {url}\n",
                "--json",
            ],
            expect_json=True,
        )["id"]
        self.dispatch()
        self.kanban._run(
            [
                "comment",
                human,
                f"PR workflow update: native-v1\nPR head: {head}\nHuman decision: secret\nHuman gate: secret-1\n",
                "--author",
                "system-admin",
            ]
        )
        self.kanban._run(
            ["block", human, "Need an authorized human action", "--kind", "needs_input"]
        )
        task = native.show(anchor_id)["task"]
        projected, action = provider.resolve(task, parse_human_action(task))
        self.assertEqual(action["assignee"], "sandro")
        self.assertIn("Human-Wait: secret", projected["body"])
        self.assertIn("Merge ready: false", projected["body"])
        self.assertIn("PR human requested at:", projected["body"])
        self.assertEqual(list(self.dispatch().spawned), [])

    def outcome_fixture(self, merged=True):
        from kanban_vikunja_projection import parse_human_action

        head, url, anchor_id, anchor_ref, snapshot, native, provider = self.prepare_handoff()
        pull = snapshot["pull"]
        pull.update(
            number=200,
            state="closed",
            merged=merged,
            head={"sha": head},
            merge_commit_sha="b" * 40 if merged else None,
            updated_at="2026-09-17T10:00:00Z",
            title="Outcome fixture",
            labels=[],
        )
        snapshot.update(target="pull_request", number=200)
        self.source = type(
            "Source",
            (),
            {
                "origin": "https://git.example.test",
                "pages": lambda _, path, query=None: [pull] if path.endswith("/pulls") else [],
                "snapshot": lambda _, event: copy.deepcopy(snapshot),
            },
        )()

        def projection():
            task = native.show(anchor_id)["task"]
            return provider.resolve(task, parse_human_action(task))

        return head, anchor_ref, snapshot, projection

    def test_merge_creates_one_outcome_then_human_wait_rebuild_and_verification(self):
        head, anchor_ref, snapshot, projection = self.outcome_fixture()
        self.assertFalse(projection()[1]["done"])
        self.poll()
        spawned = list(self.dispatch().spawned)
        self.assertEqual(len(spawned), 1)
        outcome = spawned[0][0]
        self.assertEqual(self.journal.pending(), [])
        task = self.kanban.show(outcome)["task"]
        self.assertIn("PR workflow phase: outcome", task["body"])
        self.assertIn("PR projection anchor: " + anchor_ref, task["body"])
        self.assertNotIn("Vikunja", task["body"])
        self.assertIsNone(projection()[1]["assignee"])
        self.kanban._run(
            [
                "comment",
                outcome,
                f"PR workflow update: native-v1\nPR head: {head}\nMerge commit: {'b' * 40}\n"
                "Human decision: rebuild\nHuman gate: rebuild-1\n",
                "--author",
                "system-admin",
            ]
        )
        self.kanban._run(["block", outcome, "Rebuild needs authorization", "--kind", "needs_input"])
        self.assertIn("Human-Wait: rebuild", projection()[0]["body"])
        self.assertEqual(projection()[1]["assignee"], "sandro")
        for revision in ("2026-09-17T11:00:00Z", "2026-09-17T12:00:00Z"):
            snapshot["pull"]["updated_at"] = revision
            before = self.kanban.show(outcome)["comments"]
            self.poll()
            self.assertEqual(list(self.dispatch().spawned), [])
            self.assertEqual(self.kanban.show(outcome)["comments"], before)
            self.assertEqual(self.kanban.show(outcome)["task"]["status"], "blocked")
        # Explicit fixture authorization; no production approval is inferred from merge.
        self.kanban._run(["unblock", outcome])
        self.dispatch()
        proof = {
            "anchor": anchor_ref,
            "head": head,
            "result": "success",
            "evidence": ["isolated outcome fixture"],
            "merge_commit": "b" * 40,
        }

        def complete(task_id, phase, **extra):
            self.kanban._run(
                [
                    "complete",
                    task_id,
                    "--summary",
                    "Isolated phase",
                    "--metadata",
                    json.dumps({"pr_workflow": {**proof, "phase": phase, **extra}}),
                ]
            )

        complete(
            outcome,
            "outcome",
            outcome="merged",
            rebuild_required=True,
            manual_verification_required=False,
        )
        self.assertFalse(projection()[1]["done"])
        parent = outcome
        for phase in ("rebuild", "verify"):
            child = self.kanban._run(
                [
                    "create",
                    phase,
                    "--assignee",
                    "system-admin",
                    "--body",
                    f"PR projection anchor: {anchor_ref}\nPR workflow phase: {phase}\n"
                    f"PR head: {head}\nForgejo authority: {snapshot['url']}\n",
                    "--parent",
                    parent,
                    "--json",
                ],
                expect_json=True,
            )["id"]
            self.assertEqual([r[0] for r in self.dispatch().spawned], [child])
            complete(child, phase)
            parent = child
        self.assertTrue(projection()[1]["done"])
        # A later actionable event can already route to verification. Outcome
        # replay must preserve that newer mapping as well as the native tasks.
        logical = w.logical_key("pull_request", 200)
        self.mappings.put(
            {
                "logical_key": logical,
                "target": {"kind": "pull_request", "number": 200},
                "event_id": "fj-" + "e" * 64,
            },
            parent,
            continuation=True,
        )
        self.kanban._run(["archive", outcome])
        snapshot["pull"]["updated_at"] = "2026-09-17T13:00:00Z"
        self.poll()
        self.assertEqual(list(self.dispatch().spawned), [])
        self.assertTrue(projection()[1]["done"])
        self.assertEqual(self.mappings.read()[logical]["current_task_id"], parent)

    def test_close_recovers_lost_create_response_before_ack_without_duplicate(self):
        head, anchor_ref, snapshot, projection = self.outcome_fixture(merged=False)
        original = self.kanban._run

        def lose_response(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == "create":
                raise RuntimeError("fixture response lost after native create")
            return result

        with patch.object(self.kanban, "_run", side_effect=lose_response):
            with self.assertRaisesRegex(RuntimeError, "response lost"):
                self.poll()
        self.assertEqual(len(self.journal.pending()), 1)
        self.poll()
        spawned = list(self.dispatch().spawned)
        self.assertEqual(len(spawned), 1)
        outcome = spawned[0][0]
        self.kanban._run(
            [
                "complete",
                outcome,
                "--summary",
                "Closed without merge",
                "--metadata",
                json.dumps(
                    {
                        "pr_workflow": {
                            "anchor": anchor_ref,
                            "head": head,
                            "phase": "outcome",
                            "result": "success",
                            "evidence": ["isolated close fixture"],
                            "outcome": "closed",
                            "rebuild_required": False,
                            "manual_verification_required": False,
                        }
                    }
                ),
            ]
        )
        self.assertTrue(projection()[1]["done"])
        self.poll()
        self.assertEqual(list(self.dispatch().spawned), [])

    def test_source_failure_or_changed_outcome_does_not_ack(self):
        _head, _anchor, snapshot, _projection = self.outcome_fixture()
        original = self.kanban._run

        def changed_after_create(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == "create":
                snapshot["pull"].update(state="open", merged=False, merge_commit_sha=None)
            return result

        with patch.object(self.kanban, "_run", side_effect=changed_after_create):
            with self.assertRaisesRegex(RuntimeError, "outcome changed"):
                self.poll()
        self.assertEqual(len(self.journal.pending()), 1)
        # A pre-write source error must also leave the source event pending.
        with patch.object(self.source, "snapshot", side_effect=RuntimeError("source unavailable")):
            with self.assertRaisesRegex(RuntimeError, "source unavailable"):
                self.poll()
        self.assertEqual(len(self.journal.pending()), 2)

    def test_untracked_closed_pr_and_open_timestamp_updates_do_not_create_workers(self):
        _head, anchor_ref, snapshot, _projection = self.outcome_fixture()
        self.kanban._run(["archive", anchor_ref.split("/")[1]])
        self.poll()
        self.assertEqual(list(self.dispatch().spawned), [])
        snapshot["pull"].update(
            state="open", merged=False, merge_commit_sha=None, updated_at="2026-09-17T11:00:00Z"
        )
        self.poll()
        self.assertEqual(list(self.dispatch().spawned), [])

    def test_pending_outcome_survives_prewrite_and_mapping_response_loss_and_concurrent_retry(self):
        self.outcome_fixture()
        original_run = self.kanban._run

        def fail_before_create(args, **kwargs):
            if args[0] == "create":
                raise RuntimeError("fixture before native write")
            return original_run(args, **kwargs)

        with patch.object(self.kanban, "_run", side_effect=fail_before_create):
            with self.assertRaisesRegex(RuntimeError, "before native write"):
                self.poll()
        self.assertEqual(len(self.journal.pending()), 1)
        original_put = self.mappings.put

        def lose_mapping_response(*args, **kwargs):
            original_put(*args, **kwargs)
            raise RuntimeError("fixture after durable mapping")

        with patch.object(self.mappings, "put", side_effect=lose_mapping_response):
            with self.assertRaisesRegex(RuntimeError, "after durable mapping"):
                self.poll()
        self.assertEqual(len(self.journal.pending()), 1)
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: self.poll(), range(2)))
        self.assertEqual(self.journal.pending(), [])
        self.assertEqual(len(list(self.dispatch().spawned)), 1)
        self.assertEqual(
            len(self.mappings.read()[w.logical_key("pull_request", 200)]["task_ids"]), 1
        )

    def test_outcome_waits_for_existing_execution_without_releasing_human_gate(self):
        _head, anchor_ref, snapshot, _projection = self.outcome_fixture()
        gate = self.kanban._run(
            [
                "create",
                "Existing human gate",
                "--assignee",
                "system-admin",
                "--initial-status",
                "blocked",
                "--body",
                f"Forgejo authority: {snapshot['url']}",
                "--json",
            ],
            expect_json=True,
        )["id"]
        self.poll()
        outcome = self.mappings.read()[w.logical_key("pull_request", 200)]["current_task_id"]
        self.assertEqual(self.kanban.show(outcome)["parents"], [gate])
        self.assertNotIn(anchor_ref.split("/")[1], self.kanban.show(outcome)["parents"])
        self.assertEqual(list(self.dispatch().spawned), [])
        self.assertEqual(self.kanban.show(gate)["task"]["status"], "blocked")
        self.kanban._run(["unblock", gate])
        self.assertEqual([r[0] for r in self.dispatch().spawned], [gate])
        self.kanban._run(["complete", gate, "--summary", "Fixture gate resolved"])
        self.assertEqual([r[0] for r in self.dispatch().spawned], [outcome])
