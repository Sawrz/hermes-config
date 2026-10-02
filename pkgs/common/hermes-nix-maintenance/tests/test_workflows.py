from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from hermes_nix_maintenance import (
    IntentLedger,
    WorkflowError,
    build_issue_intent,
    container_pending_gate,
    digest,
    validate_curation,
    validate_inventory,
)
from nix_config_wiki_sync import (
    PlannedAction,
    SourcePage,
    WikiPage,
    WikiSyncError,
    action_request,
    apply_actions,
    manifest,
    path_to_page_title,
    plan_sync,
    quote_page_title,
)

ROOT = Path(os.environ.get("HERMES_CONFIG_ROOT", Path(__file__).resolve().parents[4]))
PACKAGE = ROOT / "pkgs/common/hermes-nix-maintenance"


def inventory() -> dict:
    return {
        "schema_version": 1,
        "repository": "nixos/nix-config",
        "source_commit": "a" * 40,
        "period": "2026-08",
        "candidates": [
            {
                "service": "jellyfin",
                "image": "lscr.io/linuxserver/jellyfin",
                "current": "10.11.6",
                "target": "10.11.11",
                "hosts": ["abra"],
                "enabled": True,
                "source_status": "registry-confirmed",
            },
            {
                "service": "immich",
                "image": "ghcr.io/immich-app/immich-server",
                "current": "v2.4.1",
                "target": "v3.1.0",
                "hosts": ["abra"],
                "enabled": True,
                "source_status": "release-confirmed",
            },
        ],
    }


def curation(inv: dict, decisions: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "repository": inv["repository"],
        "source_commit": inv["source_commit"],
        "period": inv["period"],
        "inventory_sha256": digest(inv),
        "decisions": decisions
        if decisions is not None
        else [
            {
                "service": "jellyfin",
                "image": "lscr.io/linuxserver/jellyfin",
                "decision": "include",
                "reason": "low-risk patch update",
            },
            {
                "service": "immich",
                "image": "ghcr.io/immich-app/immich-server",
                "decision": "needs-human",
                "reason": "major migration requires a runbook decision",
            },
        ],
    }


class ContainerMechanicsTests(unittest.TestCase):
    def test_intent_retains_exact_inputs_not_only_hashes(self):
        inv = validate_inventory(inventory())
        cur = validate_curation(curation(inv), inv)
        intent = build_issue_intent(inv, cur)
        self.assertEqual(intent.get("inputs"), {"inventory": inv, "curation": cur})

    def test_pending_native_gate_is_silent_until_exact_intent_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = IntentLedger(Path(tmp) / "state")
            self.assertIsNone(ledger.pending_context())
            self.assertEqual(
                container_pending_gate(ledger),
                {"wakeAgent": False, "reason": "no-pending-intent"},
            )
            inv = validate_inventory(inventory())
            cur = validate_curation(curation(inv), inv)
            intent = build_issue_intent(inv, cur)
            assert intent is not None
            ledger.stage(intent)
            context = ledger.pending_context()
            self.assertEqual(context["intent"]["intent_sha256"], intent["intent_sha256"])
            gate = container_pending_gate(ledger)
            self.assertTrue(gate["wakeAgent"])
            self.assertEqual(gate["context"], context)

    def test_exact_target_inventory_and_curation_build_one_intent(self) -> None:
        inv = validate_inventory(inventory())
        cur = validate_curation(curation(inv), inv)
        intent = build_issue_intent(inv, cur)
        assert intent is not None
        self.assertEqual(intent["repository"], "nixos/nix-config")
        self.assertEqual(intent["source_commit"], "a" * 40)
        self.assertEqual(intent["logical_key"], "nix-config-container-update:2026-08")
        self.assertEqual(len(intent["decisions"]), 2)

    def test_wrong_repo_stale_commit_and_unknown_curation_fail(self) -> None:
        wrong = inventory()
        wrong["repository"] = "other/repo"
        with self.assertRaisesRegex(WorkflowError, "exactly"):
            validate_inventory(wrong)
        inv = validate_inventory(inventory())
        stale = curation(inv)
        stale["source_commit"] = "b" * 40
        with self.assertRaisesRegex(WorkflowError, "does not match"):
            validate_curation(stale, inv)
        unknown = curation(inv)
        unknown["decisions"][0]["service"] = "not-enabled"
        with self.assertRaisesRegex(WorkflowError, "unknown"):
            validate_curation(unknown, inv)

    def test_empty_or_excluded_inventory_is_a_silent_noop(self) -> None:
        inv = validate_inventory(inventory())
        cur = validate_curation(
            curation(
                inv,
                [
                    {
                        "service": "jellyfin",
                        "image": "lscr.io/linuxserver/jellyfin",
                        "decision": "exclude",
                        "reason": "unsupported tag family",
                    }
                ],
            ),
            inv,
        )
        self.assertIsNone(build_issue_intent(inv, cur))
        with tempfile.TemporaryDirectory() as tmp:
            result = IntentLedger(Path(tmp) / "state").stage(None)
        self.assertEqual(result, {"wakeAgent": False, "changed": False, "reason": "empty-curation"})

    def test_pending_ack_and_noop_are_crash_safe_and_exact_bound(self) -> None:
        inv = validate_inventory(inventory())
        cur = validate_curation(curation(inv), inv)
        intent = build_issue_intent(inv, cur)
        assert intent is not None
        with tempfile.TemporaryDirectory() as tmp:
            ledger = IntentLedger(Path(tmp) / "state")
            first = ledger.stage(intent)
            self.assertFalse(first["wakeAgent"])
            self.assertTrue(first["changed"])
            repeated = ledger.stage(intent)
            self.assertEqual(repeated["reason"], "pending")
            self.assertFalse(repeated["changed"])
            ledger.begin(intent["intent_sha256"], "attempt-1")
            ledger.lost_response(intent["intent_sha256"], "response timeout")
            weak_proof = {
                "repository": "nixos/nix-config",
                "source_commit": intent["source_commit"],
                "intent_sha256": intent["intent_sha256"],
                "target_kind": "issue",
                "target_number": 200,
                "target_url": "https://git.example.test/nixos/nix-config/issues/200?wrong=1",
                "readback_sha256": "f" * 64,
            }
            with self.assertRaisesRegex(WorkflowError, "canonical issue target"):
                ledger.acknowledge(intent["intent_sha256"], weak_proof)
            proof = {
                **weak_proof,
                "target_url": "https://git.example.test/nixos/nix-config/issues/200",
            }
            acknowledged = ledger.acknowledge(intent["intent_sha256"], proof)
            self.assertEqual(acknowledged["state"], "acknowledged")
            self.assertEqual(ledger.stage(intent)["reason"], "acknowledged")

    def test_corrupt_selected_state_fails_closed(self) -> None:
        inv = validate_inventory(inventory())
        cur = validate_curation(curation(inv), inv)
        intent = build_issue_intent(inv, cur)
        assert intent is not None
        with tempfile.TemporaryDirectory() as tmp:
            ledger = IntentLedger(Path(tmp) / "state")
            corrupt = IntentLedger._initial(intent)
            corrupt["state"] = "acknowledged"
            ledger.store.publish({"intent.json": corrupt})
            with self.assertRaisesRegex(WorkflowError, "invariant"):
                ledger.stage(intent)

    def test_only_failed_edge_retries_and_new_intent_cannot_replace_pending(self) -> None:
        inv = validate_inventory(inventory())
        cur = validate_curation(curation(inv), inv)
        intent = build_issue_intent(inv, cur)
        assert intent is not None
        with tempfile.TemporaryDirectory() as tmp:
            ledger = IntentLedger(Path(tmp) / "state")
            ledger.stage(intent)
            ledger.begin(intent["intent_sha256"], "attempt-1")
            failed = ledger.transient_failure(intent["intent_sha256"], "HTTP 503")
            self.assertEqual(failed["state"], "transient-failed")
            retry = ledger.begin(intent["intent_sha256"], "attempt-2")
            self.assertEqual(retry["attempts"], 2)
            changed_inventory = {**inv, "period": "2026-09"}
            changed = build_issue_intent(
                changed_inventory, validate_curation(curation(changed_inventory), changed_inventory)
            )
            with self.assertRaisesRegex(WorkflowError, "still pending"):
                ledger.stage(changed)


class FakeWikiClient:
    def __init__(self, pages: dict[str, str] | None = None, corrupt_readback: bool = False):
        self.pages = dict(pages or {})
        self.corrupt_readback = corrupt_readback

    def create_page(self, title: str, content: str, message: str) -> None:
        self.pages[title] = content

    def patch_page(self, title: str, content: str, message: str) -> None:
        self.pages[title] = content

    def delete_page(self, title: str) -> None:
        self.pages.pop(title, None)

    def get_page(self, title: str) -> WikiPage | None:
        if title not in self.pages:
            return None
        content = self.pages[title]
        if self.corrupt_readback:
            content += "corrupt"
        return WikiPage(title=title, content=content)


class WikiPublicationTests(unittest.TestCase):
    def test_nested_page_is_one_encoded_exact_target(self) -> None:
        self.assertEqual(path_to_page_title("docs/Runbooks/Ops.md"), "Runbooks/Ops")
        self.assertEqual(quote_page_title("Runbooks/Ops"), "Runbooks%2FOps")

    def test_create_update_delete_require_exact_readback(self) -> None:
        commit = "c" * 40
        source = SourcePage("docs/Home.md", "Home", "raw", "published")
        source2 = SourcePage("docs/Runbooks/Ops.md", "Runbooks/Ops", "raw2", "published2")
        pages = {
            "Home": WikiPage("Home", "old"),
            "Stale": WikiPage("Stale", "stale"),
        }
        actions = plan_sync(
            {"Home": source, "Runbooks/Ops": source2}, pages, commit, "nixos/nix-config"
        )
        client = FakeWikiClient({"Home": "old", "Stale": "stale"})
        proof = apply_actions(client, actions)
        self.assertEqual(len(proof), 3)
        self.assertEqual(client.pages, {"Home": "published", "Runbooks/Ops": "published2"})
        self.assertEqual(
            {row["result"] for row in proof}, {"exact-content-readback", "exact-absence-readback"}
        )

    def test_http_success_without_exact_content_is_failure(self) -> None:
        page = SourcePage("docs/Home.md", "Home", "raw", "published")
        action = PlannedAction(
            "create",
            "Home",
            "docs/Home.md",
            "missing",
            action_request("create", page, "Home", "d" * 40, "nixos/nix-config"),
        )
        with self.assertRaisesRegex(WikiSyncError, "readback failed"):
            apply_actions(FakeWikiClient(corrupt_readback=True), [action])

    def test_dry_run_manifest_has_no_publication_proof(self) -> None:
        data = manifest([], "e" * 40, "nixos/nix-config", 0, 0, "dry-run", {}, [])
        self.assertEqual(data["publication_proof"], [])
        self.assertEqual(data["counts"]["total"], 0)


class OwnershipCollisionTests(unittest.TestCase):
    def test_s63_does_not_reimplement_domain_integration_packages(self) -> None:
        source = (PACKAGE / "src/hermes_nix_maintenance.py").read_text()
        forbidden_implementations = [
            "sqlite3",
            "/kanban.db",
            "ntfy_event_collector",
            "requests.post",
            "ForgejoKanban",
            "VikunjaClient",
            "PrometheusClient",
        ]
        for marker in forbidden_implementations:
            self.assertNotIn(marker, source)

    def test_nix_config_pull_reuses_r62_script_only_boundary(self) -> None:
        schedules = json.loads(
            (ROOT / "pkgs/common/hermes-repository-sync/schedule-templates.json").read_text()
        )
        job = schedules["jobs"]["nix-config-pull"]
        self.assertTrue(job["no_agent"])
        self.assertEqual(job["deliver"], "local")
        self.assertEqual(job["skills"], [])
        self.assertEqual(job["enabled_toolsets"], [])


if __name__ == "__main__":
    unittest.main()
