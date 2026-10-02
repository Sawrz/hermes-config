"""Read-only lifecycle policy for the existing Kanban-to-Vikunja writer.

Native tools own execution and phase receipts; Forgejo owns PR/review/CI facts.
There is no second scheduler, projection writer or lifecycle database here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from forgejo_kanban_workflow import ForgejoClient, pr_readiness, pr_actionable_digest
from kanban_vikunja_projection import InputError

from hermes_repository_instance import instance

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GATE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
HUMAN_DECISIONS = {"secret", "manual", "rebuild", "verify", "rollback", "continue"}


def field(body: str, name: str) -> str | None:
    rows = [line[len(name) + 2 :] for line in body.splitlines() if line.startswith(name + ": ")]
    if len(rows) > 1:
        raise InputError(f"duplicate PR workflow field: {name}")
    return rows[0] if rows else None


def phase_state(envelope: Mapping[str, Any]) -> tuple[str, int]:
    """Native worker comments update a request without editing immutable task bodies."""
    task = envelope["task"]
    body = str(task.get("body") or "")
    created = task.get("created_at", 0)
    for comment in envelope.get("comments", []):
        update = str(comment.get("body") or "")
        if comment.get("author") == task.get("assignee") and update.startswith(
            "PR workflow update: native-v1\n"
        ):
            allowed = {
                "PR workflow update",
                "PR head",
                "Human decision",
                "Human gate",
                "Merge commit",
            }
            if any(line.split(": ", 1)[0] not in allowed for line in update.splitlines() if line):
                raise InputError("native workflow update contains unsupported fields")
            retained = [line for line in body.splitlines() if line.split(": ", 1)[0] not in allowed]
            body = "\n".join(retained) + "\n" + update
            created = comment.get("created_at", 0)
    if type(created) is not int or created < 0:
        raise InputError("native human request timestamp is invalid")
    return body, created


def receipt(envelope: Mapping[str, Any], phase: str, anchor: str, head: str) -> dict | None:
    task = envelope["task"]
    if task.get("assignee") != instance().phase_owners()[phase]:
        raise InputError("PR phase is assigned to the wrong native profile")
    runs = envelope.get("runs")
    if not isinstance(runs, list):
        raise InputError("native PR phase lacks run readback")
    if not runs or task.get("status") not in {"done", "archived"}:
        return None
    if any(not isinstance(run, dict) or type(run.get("id")) is not int for run in runs):
        raise InputError("native PR run identity is malformed")
    latest = max(runs, key=lambda run: run["id"])
    if latest.get("status") != "done" or latest.get("profile") != instance().phase_owners()[phase]:
        return None
    proof = (latest.get("metadata") or {}).get("pr_workflow")
    if not isinstance(proof, dict):
        return None
    if any(
        proof.get(k) != v
        for k, v in {"anchor": anchor, "phase": phase, "head": head, "result": "success"}.items()
    ):
        return None
    evidence = proof.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(not isinstance(x, str) or not x.strip() for x in evidence)
    ):
        raise InputError("PR phase receipt lacks durable evidence")
    return {
        **proof,
        "task_id": task["id"],
        "run_id": latest["id"],
        "parents": envelope.get("parents", []),
    }


def follows(proof: dict, parent: dict) -> bool:
    return parent["task_id"] in proof["parents"] and proof["run_id"] > parent["run_id"]


def one(rows: list[dict], label: str) -> dict | None:
    if len(rows) > 1:
        if label == "human gate" or len({r["run_id"] for r in rows}) != len(rows):
            raise InputError(f"ambiguous completed PR {label} receipts")
        return max(rows, key=lambda r: r["run_id"])
    return rows[0] if rows else None


class PrLifecycle:
    def __init__(self, forgejo: ForgejoClient, kanban: Any, board: str) -> None:
        self.forgejo, self.kanban, self.board = forgejo, kanban, board

    def resolve(self, task: Mapping[str, Any], action: Mapping[str, Any]) -> tuple[dict, dict]:
        body = str(task.get("body") or "")
        if action["action_type"] != "Merge" or field(body, "PR workflow") is None:
            return dict(task), dict(action)
        if (
            field(body, "PR workflow") != "native-v1"
            or task.get("assignee") != instance().implementer
        ):
            raise InputError("invalid native PR projection anchor")
        match = re.fullmatch(
            re.escape(instance().namespace) + r"-pr-([1-9][0-9]*)-merge", action["automation_key"]
        )
        if not match:
            raise InputError("PR projection lacks its canonical PR identity")
        anchor = f"{self.board}/{task['id']}"
        snapshot = self.forgejo.readiness_snapshot(int(match[1]))
        url, head, pull = snapshot["url"], snapshot["head"], snapshot["pull"]
        if field(body, "Forgejo authority") != url:
            raise InputError("PR projection anchor targets a different Forgejo lineage")
        proofs: dict[str, list[dict]] = {p: [] for p in instance().phase_owners()}
        active, gates = [], []
        for candidate in self.kanban.list_workflow_tasks():
            if candidate["id"] == task["id"]:
                continue
            candidate_body = str(candidate.get("body") or "")
            linked = field(candidate_body, "PR projection anchor") == anchor
            same_pr = field(candidate_body, "Forgejo authority") == url
            if not linked and not same_pr:
                continue
            envelope = self.kanban.show(candidate["id"])
            current = envelope["task"]
            if current != candidate:
                raise RuntimeError("native PR continuation changed during readback")
            if current.get("status") not in {"done", "archived"}:
                active.append(candidate["id"])
            if not linked:
                continue  # New F11 work already invalidates readiness, even before classification.
            phase = field(candidate_body, "PR workflow phase")
            if phase is None or phase not in instance().phase_owners() or not same_pr:
                raise InputError("PR continuation has an invalid phase or Forgejo authority")
            proof = receipt(envelope, phase, anchor, head)
            if proof:
                proofs[phase].append(proof)
            candidate_body, requested_at = phase_state(envelope)
            if field(candidate_body, "PR head") != head:
                continue
            decision, gate = (
                field(candidate_body, "Human decision"),
                field(candidate_body, "Human gate"),
            )
            if decision and current.get("status") == "blocked":
                if decision not in HUMAN_DECISIONS or not gate or not GATE_RE.fullmatch(gate):
                    raise InputError("native human gate is malformed")
                if current.get("assignee") != instance().implementer:
                    raise InputError("human continuation must belong to the configured implementer")
                if pull.get("merged") is True and field(candidate_body, "Merge commit") != pull.get(
                    "merge_commit_sha"
                ):
                    continue
                if (
                    decision in {"rebuild", "verify", "rollback", "continue"}
                    and pull.get("merged") is not True
                ):
                    continue
                gates.append(
                    {
                        "task_id": candidate["id"],
                        "kind": decision,
                        "gate": gate,
                        "requested_at": requested_at,
                    }
                )

        review_candidates = []
        for proof in proofs["review"]:
            if not any(follows(proof, parent) for parent in proofs["implementation"]):
                continue
            matches = [
                r["review"]
                for r in snapshot["reviews"]
                if r["review"]["id"] == proof.get("review_id")
            ]
            if len(matches) == 1:
                review_candidates.append((proof, matches[0]))
        review_candidates.sort(key=lambda pair: pair[1]["id"])
        readiness = {"ready": False, "reasons": ["native-technical-review-missing"]}
        handoff = None
        if review_candidates:
            review_proof, review = review_candidates[-1]
            readiness = pr_readiness(snapshot, reviewer=review["user"]["login"])
            if readiness.get("review_id") != review["id"]:
                readiness = {
                    "ready": False,
                    "reasons": [*readiness["reasons"], "review-receipt-outdated"],
                }
            handoff = one(
                [
                    p
                    for p in proofs["handoff"]
                    if follows(p, review_proof)
                    and p.get("review_id") == review["id"]
                    and p.get("actionable_digest") == pr_actionable_digest(snapshot)
                ],
                "handoff",
            )
        ready = readiness["ready"] and handoff is not None and not active
        phase, assignee, done = "Agent work", None, False
        reasons = list(readiness["reasons"])
        if active:
            reasons.append("native-agent-work-pending")
        if handoff is None:
            reasons.append("exact-head-handoff-missing")
        if ready:
            phase, assignee = "Human review / merge decision", instance().human_owner

        if pull.get("state") == "closed":
            outcome_name = "merged" if pull.get("merged") is True else "closed"
            merge = pull.get("merge_commit_sha") if outcome_name == "merged" else None
            if outcome_name == "merged" and (
                not isinstance(merge, str) or not SHA_RE.fullmatch(merge)
            ):
                raise InputError("merged PR lacks an exact merge commit")
            outcome = one(
                [
                    p
                    for p in proofs["outcome"]
                    if p.get("outcome") == outcome_name and p.get("merge_commit") == merge
                ],
                "outcome",
            )
            phase, assignee, reasons = "PR outcome reconciliation", None, ["outcome-proof-missing"]
            if outcome:
                if any(
                    type(outcome.get(k)) is not bool
                    for k in ("rebuild_required", "manual_verification_required")
                ):
                    raise InputError(
                        "outcome lacks explicit rebuild/manual verification requirements"
                    )
                if outcome_name == "closed":
                    done = (
                        not active
                        and not outcome["rebuild_required"]
                        and not outcome["manual_verification_required"]
                    )
                    phase, reasons = "Closed without merge", []
                else:
                    parent = outcome
                    phase, reasons = "Rebuild", ["rebuild-proof-missing"]
                    if outcome["rebuild_required"]:
                        parent = one(
                            [
                                p
                                for p in proofs["rebuild"]
                                if follows(p, outcome) and p.get("merge_commit") == merge
                            ],
                            "rebuild",
                        )
                    if parent:
                        phase, reasons = "Verification", ["verification-proof-missing"]
                        verified = one(
                            [
                                p
                                for p in proofs["verify"]
                                if follows(p, parent) and p.get("merge_commit") == merge
                            ],
                            "verification",
                        )
                        if verified:
                            done = not active
                            if outcome["manual_verification_required"]:
                                phase, reasons = (
                                    "Manual verification",
                                    ["manual-verification-proof-missing"],
                                )
                                manual = one(
                                    [
                                        p
                                        for p in proofs["manual-verify"]
                                        if follows(p, verified)
                                        and p.get("merge_commit") == merge
                                        and isinstance(p.get("human_evidence"), str)
                                        and p["human_evidence"].strip()
                                    ],
                                    "manual verification",
                                )
                                done = done and manual is not None
                            if done:
                                phase, reasons = "Verified", []
        gate = one(gates, "human gate")
        if gate:
            phase, assignee, done = "Human-Wait: " + gate["kind"], instance().human_owner, False
        # Keep the human narrative; initial routing flags are not current UI facts.
        narrative = "\n".join(
            line
            for line in body.splitlines()
            if not line.startswith(("PR workflow: ", "Human action: ", "Vikunja "))
        ).rstrip()
        rendered = [
            narrative,
            "",
            "## Current projection",
            f"Current phase: {phase}",
            "Current owner: " + (assignee or instance().implementer),
            "Merge ready: " + str(bool(ready)).lower(),
            f"Current PR head: {head}",
        ]
        if gate:
            rendered += [
                f"PR human continuation: {self.board}/{gate['task_id']}",
                f"PR human gate: {gate['gate']}",
                f"PR human requested at: {gate['requested_at']}",
            ]
        if pull.get("merged"):
            rendered.append("Current merge commit: " + str(pull.get("merge_commit_sha")))
        if reasons:
            rendered.append("Outstanding evidence: " + ", ".join(sorted(set(reasons))))
        rendered += [
            "",
            "Merge, rebuild and verification require their own authorization and evidence.",
        ]
        return {**task, "body": "\n".join(rendered)}, {**action, "assignee": assignee, "done": done}
