"""Isolated native/Forgejo contracts; no external service or model is called."""

import copy
import unittest

from kanban_vikunja_projection import InputError, parse_human_action
from forgejo_kanban_workflow import pr_actionable_digest
from pr_projection_lifecycle import PrLifecycle

HEAD = "a" * 40
MERGE = "b" * 40
URL = "https://forge.example.test/nixos/nix-config/pulls/200"
ANCHOR = "homelab-devops/t_aaaaaaaa"


def snapshot():
    return {
        "head": HEAD,
        "url": URL,
        "comments": [],
        "pull": {"state": "open", "merged": False, "draft": False, "mergeable": True},
        "branch": {"enable_status_check": True, "status_check_contexts": ["test"]},
        "statuses": [{"id": 1, "context": "test", "status": "success"}],
        "reviews": [
            {
                "review": {
                    "id": 1,
                    "user": {"login": "reviewer"},
                    "state": "APPROVED",
                    "commit_id": HEAD,
                    "stale": False,
                    "dismissed": False,
                },
                "comments": [],
            }
        ],
    }


class Native:
    def __init__(self):
        self.rows = {}
        self.sequence = 0

    def list_workflow_tasks(self):
        return [copy.deepcopy(r["task"]) for r in self.rows.values()]

    def show(self, task_id):
        return copy.deepcopy(self.rows[task_id])

    def phase(self, phase, parents=(), **proof):
        self.sequence += 1
        seq = self.sequence
        task_id = f"t_{seq:08x}"
        owner = "code-reviewer" if phase == "review" else "system-admin"
        row = {
            "task": {
                "id": task_id,
                "assignee": owner,
                "status": "done",
                "body": f"PR projection anchor: {ANCHOR}\nPR workflow phase: {phase}\n"
                f"PR head: {HEAD}\nForgejo authority: {URL}\n",
            },
            "parents": list(parents),
            "comments": [],
            "runs": [
                {
                    "id": seq,
                    "profile": owner,
                    "status": "done",
                    "metadata": {
                        "pr_workflow": {
                            "anchor": ANCHOR,
                            "phase": phase,
                            "head": HEAD,
                            "result": "success",
                            "evidence": ["isolated-fixture"],
                            **proof,
                        }
                    },
                }
            ],
        }
        self.rows[task_id] = row
        return task_id


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.source = snapshot()
        self.native = Native()
        self.anchor = {
            "id": "t_aaaaaaaa",
            "assignee": "system-admin",
            "title": "PR 200",
            "body": f"PR workflow: native-v1\nForgejo authority: {URL}\n"
            "Human action: Merge\nVikunja project: 110\nVikunja assignee: none\n"
            "Vikunja done: false\nVikunja automation reference: nix-config-pr-200-merge\n\n"
            "## Why\nPreserve this explanation.\n## Risk\nPreserve rollback instructions.\n",
        }
        self.action = parse_human_action(self.anchor)
        self.forge = type(
            "Forge", (), {"readiness_snapshot": lambda _, n: copy.deepcopy(self.source)}
        )()
        self.provider = PrLifecycle(self.forge, self.native, "homelab-devops")

    def resolve(self):
        return self.provider.resolve(self.anchor, self.action)

    def reviewed(self):
        implementation = self.native.phase("implementation")
        review = self.native.phase("review", [implementation], review_id=1)
        return self.native.phase(
            "handoff", [review], review_id=1, actionable_digest=pr_actionable_digest(self.source)
        )

    def test_exact_head_handoff_required_and_story_preserved(self):
        self.assertIsNone(self.resolve()[1]["assignee"])
        self.reviewed()
        task, action = self.resolve()
        self.assertEqual(action["assignee"], "sandro")
        self.assertFalse(action["done"])
        self.assertIn("Preserve this explanation", task["body"])
        self.assertIn("Preserve rollback instructions", task["body"])

    def test_skipped_required_check_withdraws_merge_assignment_but_optional_skip_does_not(self):
        self.reviewed()
        self.assertEqual(self.resolve()[1]["assignee"], "sandro")
        required = self.source["statuses"][0]
        required["description"] = "Has been skipped"
        task, action = self.resolve()
        self.assertIsNone(action["assignee"])
        self.assertIn("unexecuted-ci:test", task["body"])
        # The latest attempt wins, including a skipped rerun after a success.
        required["description"] = "Successful in 3s"
        required["target_url"] = "/nixos/nix-config/actions/runs/2306/jobs/2"
        self.source["statuses"].append({**required, "id": 2, "description": "Has been skipped"})
        self.assertIsNone(self.resolve()[1]["assignee"])
        self.source["statuses"].append({**required, "id": 3})
        self.source["statuses"].append(
            {
                "id": 4,
                "context": "optional wiki",
                "status": "success",
                "description": "Has been skipped",
            }
        )
        self.assertEqual(self.resolve()[1]["assignee"], "sandro")
        self.source["branch"]["status_check_contexts"] = ["*"]
        self.assertIsNone(self.resolve()[1]["assignee"])

    def test_reported_branch_policy_is_blocked_until_explicit_required_contexts_exist(self):
        self.reviewed()
        self.source["branch"] = {
            "name": "master",
            "protected": True,
            "required_approvals": 0,
            "enable_status_check": False,
            "status_check_contexts": None,
            "effective_branch_protection_name": "",
        }
        task, action = self.resolve()
        self.assertIsNone(action["assignee"])
        self.assertIn("required-ci-policy-missing", task["body"])
        self.source["branch"].update(enable_status_check=True, status_check_contexts=["test"])
        self.assertEqual(self.resolve()[1]["assignee"], "sandro")

    def test_review_without_completed_implementation_parent_is_not_ready(self):
        review = self.native.phase("review", review_id=1)
        self.native.phase("handoff", [review], review_id=1)
        self.assertIsNone(self.resolve()[1]["assignee"])

    def test_head_change_and_active_unclassified_followup_withdraw_assignment(self):
        self.reviewed()
        self.source["head"] = "c" * 40
        self.assertIsNone(self.resolve()[1]["assignee"])
        self.source["head"] = HEAD
        followup = self.native.phase("implementation")
        self.native.rows[followup]["task"].update(
            status="running", body=f"Forgejo authority: {URL}"
        )
        self.assertIsNone(self.resolve()[1]["assignee"])

    def test_secret_human_wait_is_not_merge_readiness(self):
        gate = self.native.phase("implementation")
        self.native.rows[gate]["task"]["status"] = "blocked"
        self.native.rows[gate]["task"]["body"] += "Human decision: secret\nHuman gate: secret-1\n"
        task, action = self.resolve()
        self.assertEqual(action["assignee"], "sandro")
        self.assertIn("Human-Wait: secret", task["body"])
        self.assertIn("Merge ready: false", task["body"])

    def test_merged_needs_ordered_rebuild_verify_and_optional_human_evidence(self):
        self.reviewed()
        self.source["pull"].update(state="closed", merged=True, merge_commit_sha=MERGE)
        outcome = self.native.phase(
            "outcome",
            outcome="merged",
            merge_commit=MERGE,
            rebuild_required=True,
            manual_verification_required=True,
        )
        verify = self.native.phase("verify", [outcome], merge_commit=MERGE)
        self.assertFalse(self.resolve()[1]["done"])
        rebuild = self.native.phase("rebuild", [outcome], merge_commit=MERGE)
        self.assertFalse(self.resolve()[1]["done"])
        del self.native.rows[verify]
        verify = self.native.phase("verify", [rebuild], merge_commit=MERGE)
        self.assertFalse(self.resolve()[1]["done"])
        self.native.phase("manual-verify", [verify], merge_commit=MERGE, human_evidence="comment:7")
        self.assertTrue(self.resolve()[1]["done"])

    def test_no_rebuild_requires_explicit_outcome_and_verification(self):
        self.source["pull"].update(state="closed", merged=True, merge_commit_sha=MERGE)
        outcome = self.native.phase(
            "outcome",
            outcome="merged",
            merge_commit=MERGE,
            rebuild_required=False,
            manual_verification_required=False,
        )
        self.assertFalse(self.resolve()[1]["done"])
        self.native.phase("verify", [outcome], merge_commit=MERGE)
        self.assertTrue(self.resolve()[1]["done"])

    def test_closed_without_merge_requires_recorded_outcome(self):
        self.source["pull"].update(state="closed")
        self.assertFalse(self.resolve()[1]["done"])
        self.native.phase(
            "outcome", outcome="closed", rebuild_required=False, manual_verification_required=False
        )
        self.assertTrue(self.resolve()[1]["done"])

    def test_failed_latest_run_invalidates_old_success(self):
        self.reviewed()
        row = list(self.native.rows.values())[-1]
        row["runs"].append({"id": 999, "status": "failed", "profile": "system-admin"})
        self.assertIsNone(self.resolve()[1]["assignee"])

    def test_multiple_human_gates_are_rejected(self):
        for i in range(2):
            task_id = self.native.phase("implementation")
            self.native.rows[task_id]["task"]["status"] = "blocked"
            self.native.rows[task_id]["task"]["body"] += (
                f"Human decision: secret\nHuman gate: gate-{i}\n"
            )
        with self.assertRaises(InputError):
            self.resolve()

    def test_meaningful_comment_edit_requires_new_handoff_but_timestamp_churn_does_not(self):
        self.source["comments"] = [
            {"id": 1, "user": {"login": "sandro"}, "body": "Scope", "updated_at": "old"}
        ]
        self.reviewed()
        self.assertEqual(self.resolve()[1]["assignee"], "sandro")
        self.source["comments"][0]["updated_at"] = "new"
        self.assertEqual(self.resolve()[1]["assignee"], "sandro")
        self.source["comments"][0]["body"] = "New requirement"
        self.assertIsNone(self.resolve()[1]["assignee"])

    def test_native_comment_updates_human_request_without_body_mutation(self):
        task_id = self.native.phase("implementation")
        envelope = self.native.rows[task_id]
        envelope["task"]["status"] = "blocked"
        original = envelope["task"]["body"]
        envelope["comments"] = [
            {
                "author": "system-admin",
                "created_at": 1234,
                "body": f"PR workflow update: native-v1\nPR head: {HEAD}\nHuman decision: secret\nHuman gate: secret-2\n",
            }
        ]
        task, action = self.resolve()
        self.assertEqual(action["assignee"], "sandro")
        self.assertIn("PR human requested at: 1234", task["body"])
        self.assertEqual(envelope["task"]["body"], original)
        envelope["comments"][0]["author"] = "unrelated-profile"
        self.assertIsNone(self.resolve()[1]["assignee"])

    def test_branch_approval_minimum_and_newer_review_are_not_bypassed(self):
        self.reviewed()
        self.source["branch"]["required_approvals"] = 1
        self.assertIsNone(self.resolve()[1]["assignee"])
        self.source["reviews"][0]["review"]["official"] = True
        self.assertEqual(self.resolve()[1]["assignee"], "sandro")
        self.source["reviews"].append(copy.deepcopy(self.source["reviews"][0]))
        self.source["reviews"][-1]["review"]["id"] = 2
        self.assertIsNone(self.resolve()[1]["assignee"])
