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

SRC = Path(__file__).parents[1] / "src"
STATE_SRC = Path(
    os.environ.get(
        "HERMES_WORKFLOW_STATE_PYTHONPATH",
        Path(__file__).parents[2] / "hermes-workflow-state" / "src",
    )
)
sys.path[:0] = [str(SRC), str(STATE_SRC)]

from hermes_digest_workflow import (  # noqa: E402
    DigestError,
    DigestProtocol,
    MinifluxDigestClient,
    canonical_route,
    miniflux_entry_candidate,
    normalize_candidates,
    secure_read,
    validate_enrichment,
)
from hermes_workflow_state import (  # noqa: E402
    canonical_json_bytes,
    validate_preference_contract,
    validate_preferences,
)


def candidate(entry_id: int, *, published: str = "2026-08-27T10:00:00Z", title: str | None = None):
    return {
        "entry_id": entry_id,
        "source_id": 7,
        "source_title": "Example",
        "title": title or f"Story {entry_id}",
        "url": f"https://example.test/{entry_id}",
        "published_at": published,
        "updated_at": published,
        "media_type": "article",
        "summary": f"Summary {entry_id}",
    }


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.transition_now = dt.datetime(2026, 8, 27, 12, tzinfo=dt.timezone.utc)
        self.protocol = DigestProtocol(self.root / "state", clock=lambda: self.transition_now)
        self.prefs = {
            "enabled": True,
            "cadence_minutes": 240,
            "edition": "morning",
            "language": "en",
            "topics": [],
            "max_candidates": 10,
            "max_prompt_chars": 12000,
            "transcripts": "selective",
            "category_ids": ["3", "8"],
            "media_types": ["article", "podcast"],
            "excluded_source_ids": ["99"],
            "timezone": "Europe/Berlin",
            "quiet_hours_start": "22:00",
            "quiet_hours_end": "06:00",
            "consolidation": "topic",
            "style": "concise",
            "max_extracted_chars": 12000,
            "max_transcript_chars": 18000,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def record_rendered(self, batch_id, rendered, claim_id, *, now):
        self.transition_now = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
        self.protocol.record_rendered(batch_id, rendered, claim_id)

    def fail(self, batch_id, claim_id, *, error, now):
        self.transition_now = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
        self.protocol.fail(batch_id, claim_id, error=error)

    def test_edition_preference_contract_uses_shared_bounded_cli_schema(self):
        contract_path = Path(__file__).parents[1] / "contracts" / "preferences.json"
        contract = json.loads(contract_path.read_text())
        validate_preference_contract(contract)
        normalized = validate_preferences(contract, self.prefs)
        self.assertEqual(normalized["values"], self.prefs)
        with self.assertRaisesRegex(Exception, "unauthorized properties"):
            validate_preferences(contract, {**self.prefs, "delivery_target": "attacker"})

    def test_not_due_empty_duplicate_and_already_processed_are_noops(self):
        now = "2026-08-27T12:00:00Z"
        self.assertEqual(
            self.protocol.claim(now, self.prefs, [], "telegram:123:45"), {"status": "empty"}
        )
        disabled = dict(self.prefs, enabled=False)
        self.assertEqual(
            self.protocol.claim(now, disabled, [candidate(1)], "telegram:123:45"),
            {"status": "disabled"},
        )
        first = self.protocol.claim(now, self.prefs, [candidate(1)], "telegram:123:45")
        self.assertEqual(first["status"], "claimed")
        self.record_rendered(first["batch_id"], "Digest body", first["claim_id"], now=now)
        state = self.protocol.inspect()
        self.assertEqual(state["batches"][-1]["state"], "rendered")
        self.assertEqual(state["batches"][-1]["rendered_content"], "Digest body")
        self.assertEqual(
            self.protocol.claim(now, self.prefs, [candidate(1)], "telegram:123:45")["status"],
            "already_processed",
        )
        later = "2026-08-27T13:00:00Z"
        self.assertEqual(
            self.protocol.claim(later, self.prefs, [candidate(2)], "telegram:123:45")["status"],
            "not_due",
        )

    def test_reordering_is_same_batch_and_deduplicates_entries(self):
        rows = [candidate(2), candidate(1), candidate(2)]
        one = normalize_candidates(rows, max_candidates=10, max_prompt_chars=12000)
        two = normalize_candidates(list(reversed(rows)), max_candidates=10, max_prompt_chars=12000)
        self.assertEqual(one, two)
        self.assertEqual([row["entry_id"] for row in one], [1, 2])

    def test_conflicting_same_entry_revision_fails_closed(self):
        with self.assertRaisesRegex(DigestError, "conflicting revisions"):
            normalize_candidates([candidate(1), candidate(1, title="Changed")], 10, 12000)

    def test_bounds_candidate_envelope(self):
        rows = [candidate(i) for i in range(1, 21)]
        normalized = normalize_candidates(rows, max_candidates=3, max_prompt_chars=12000)
        self.assertEqual(len(normalized), 3)
        oversized = candidate(1)
        oversized["summary"] = "x" * 4000
        packed = normalize_candidates([oversized], 10, 2000)
        self.assertEqual(len(packed), 1)
        self.assertTrue(packed[0]["summary"].endswith("…"))
        self.assertLessEqual(len(canonical_json_bytes(packed)), 2000)

    def test_concurrent_claim_has_one_winner(self):
        barrier = threading.Barrier(6)
        results = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            result = DigestProtocol(self.root / "state").claim(
                "2026-08-27T12:00:00Z", self.prefs, [candidate(1)], "telegram:123:45"
            )
            with lock:
                results.append(result["status"])

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("claimed"), 1)
        self.assertEqual(results.count("in_flight"), 5)

    def test_changed_candidate_set_does_not_start_a_second_live_session(self):
        first = self.protocol.claim(
            "2026-08-27T12:00:00Z", self.prefs, [candidate(1)], "telegram:123:45"
        )
        self.assertEqual(first["preferences"]["transcripts"], "selective")
        second = self.protocol.claim(
            "2026-08-27T12:01:00Z",
            self.prefs,
            [candidate(1), candidate(2)],
            "telegram:123:45",
        )
        self.assertEqual(second["status"], "in_flight")

    def test_restart_preserves_one_live_claim(self):
        first = self.protocol.claim(
            "2026-08-27T12:00:00Z", self.prefs, [candidate(1)], "telegram:123:45", claim_ttl=60
        )
        restarted = DigestProtocol(self.root / "state", clock=lambda: self.transition_now)
        self.assertEqual(
            restarted.claim(
                "2026-08-27T12:00:30Z", self.prefs, [candidate(1)], "telegram:123:45", claim_ttl=60
            )["status"],
            "in_flight",
        )
        self.transition_now = dt.datetime(2026, 8, 27, 12, 0, 30, tzinfo=dt.timezone.utc)
        restarted.record_rendered(first["batch_id"], "Digest", first["claim_id"])
        self.assertEqual(restarted.inspect()["batches"][-1]["state"], "rendered")

    def test_expired_old_batch_cannot_publish_after_new_batch_claim(self):
        old = self.protocol.claim(
            "2026-08-27T12:00:00Z", self.prefs, [candidate(1)], "telegram:123:45", claim_ttl=60
        )
        new = self.protocol.claim(
            "2026-08-27T12:01:01Z", self.prefs, [candidate(2)], "telegram:123:45", claim_ttl=60
        )
        self.assertEqual(new["status"], "claimed")
        with self.assertRaisesRegex(DigestError, "only a claimed batch"):
            self.record_rendered(
                old["batch_id"], "stale output", old["claim_id"], now="2026-08-27T12:01:01Z"
            )

    def test_expired_claim_cannot_render_or_fail_without_another_claim(self):
        rendered = self.protocol.claim(
            "2026-08-27T12:00:00Z", self.prefs, [candidate(1)], "telegram:123:45", claim_ttl=60
        )
        with self.assertRaisesRegex(DigestError, "expired"):
            self.record_rendered(
                rendered["batch_id"],
                "stale output",
                rendered["claim_id"],
                now="2026-08-27T12:01:01Z",
            )

        failed = self.protocol.claim(
            "2026-08-27T12:02:00Z", self.prefs, [candidate(2)], "telegram:123:45", claim_ttl=60
        )
        with self.assertRaisesRegex(DigestError, "expired"):
            self.fail(
                failed["batch_id"],
                failed["claim_id"],
                error="late failure",
                now="2026-08-27T12:03:01Z",
            )

    def test_mixed_processed_and_new_candidates_never_replay_old_entries(self):
        prefs = dict(self.prefs, cadence_minutes=60, max_candidates=1)
        first = self.protocol.claim(
            "2026-08-27T12:00:00Z", prefs, [candidate(1), candidate(2)], "telegram:123:45"
        )
        self.assertEqual([row["entry_id"] for row in first["candidates"]], [2])
        self.record_rendered(
            first["batch_id"], "Digest", first["claim_id"], now="2026-08-27T12:00:00Z"
        )
        second = self.protocol.claim(
            "2026-08-27T13:02:00Z", prefs, [candidate(1), candidate(2)], "telegram:123:45"
        )
        self.assertEqual(second["status"], "claimed")
        self.assertEqual([row["entry_id"] for row in second["candidates"]], [1])

    def test_retention_is_bounded_and_old_processed_entries_remain_tombstoned(self):
        prefs = dict(
            self.prefs,
            cadence_minutes=60,
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
        )
        start = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        for index in range(1, 131):
            now = (start + dt.timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M:%SZ")
            claim = self.protocol.claim(now, prefs, [candidate(index)], "telegram:123:45")
            self.assertEqual(claim["status"], "claimed")
            self.record_rendered(claim["batch_id"], f"Digest {index}", claim["claim_id"], now=now)
        later = (start + dt.timedelta(hours=132)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(
            self.protocol.claim(later, prefs, [candidate(1)], "telegram:123:45")["status"],
            "already_processed",
        )
        mixed = self.protocol.claim(later, prefs, [candidate(1), candidate(131)], "telegram:123:45")
        self.assertEqual(mixed["status"], "claimed")
        self.assertEqual([row["entry_id"] for row in mixed["candidates"]], [131])
        generations = [
            path
            for path in (self.root / "state" / "generations").iterdir()
            if path.name.startswith("gen-")
        ]
        self.assertLessEqual(len(generations), 3)

    def test_failed_newest_candidates_do_not_starve_older_candidates(self):
        prefs = dict(self.prefs, max_candidates=1)
        first = self.protocol.claim(
            "2026-08-27T12:00:00Z", prefs, [candidate(1), candidate(2)], "telegram:123:45"
        )
        self.assertEqual([row["entry_id"] for row in first["candidates"]], [2])
        self.fail(
            first["batch_id"],
            first["claim_id"],
            error="privacy violation",
            now="2026-08-27T12:00:00Z",
        )
        second = self.protocol.claim(
            "2026-08-27T12:01:00Z", prefs, [candidate(1), candidate(2)], "telegram:123:45"
        )
        self.assertEqual(second["status"], "claimed")
        self.assertEqual([row["entry_id"] for row in second["candidates"]], [1])

    def test_expired_claim_is_reclaimable_without_consuming_candidates(self):
        first = self.protocol.claim(
            "2026-08-27T12:00:00Z", self.prefs, [candidate(1)], "telegram:123:45", claim_ttl=60
        )
        result = self.protocol.claim(
            "2026-08-27T12:01:01Z", self.prefs, [candidate(1)], "telegram:123:45", claim_ttl=60
        )
        self.assertEqual(result["status"], "claimed")
        self.assertEqual(result["batch_id"], first["batch_id"])
        self.assertNotEqual(result["claim_id"], first["claim_id"])
        with self.assertRaisesRegex(DigestError, "batch claim does not match"):
            self.record_rendered(
                first["batch_id"], "late", first["claim_id"], now="2026-08-27T12:01:01Z"
            )
        self.record_rendered(
            result["batch_id"], "recovered", result["claim_id"], now="2026-08-27T12:01:01Z"
        )
        self.assertEqual(
            self.protocol.claim(
                "2026-08-27T16:02:00Z", self.prefs, [candidate(1)], "telegram:123:45"
            )["status"],
            "already_processed",
        )

    def test_batch_eviction_does_not_replay_failed_entries(self):
        prefs = dict(
            self.prefs,
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
        )
        start = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        for index in range(1, 131):
            now = (start + dt.timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")
            claim = self.protocol.claim(now, prefs, [candidate(index)], "telegram:123:45")
            self.assertEqual(claim["status"], "claimed")
            self.fail(claim["batch_id"], claim["claim_id"], error="rejected", now=now)
        later = (start + dt.timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(
            self.protocol.claim(later, self.prefs, [candidate(1)], "telegram:123:45")["status"],
            "already_processed",
        )

    def test_no_custom_delivery_acknowledgement_surface_exists(self):
        self.assertFalse(hasattr(self.protocol, "request_delivery"))
        self.assertFalse(hasattr(self.protocol, "acknowledge"))

    def test_route_is_explicit_and_origin_is_forbidden(self):
        self.assertEqual(canonical_route("telegram:123:45"), "telegram:123:45")
        for value in ("origin", "telegram", "telegram:123", "all", "local", "telegram:abc:1"):
            with self.assertRaises(DigestError):
                canonical_route(value)

    def test_candidate_urls_require_one_canonical_identity(self):
        valid = candidate(1)
        for url in (
            "https://EXAMPLE.test/1",
            "https://example.test:443/1",
            "https://example.test/%7euser",
            "https://example.test/%7Euser",
            "https://example.test/a/../1",
            "https://example.test/1#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(DigestError):
                normalize_candidates([{**valid, "url": url}], 10, 12000)

    def test_enrichment_states_and_untrusted_content(self):
        base = candidate(1)
        good = {
            "candidate": base,
            "state": "partial",
            "extracted_text": "Page text",
            "transcript": None,
            "errors": ["transcript_unavailable"],
        }
        normalized = validate_enrichment(good)
        self.assertEqual(normalized["state"], "partial")
        self.assertTrue(normalized["untrusted"])
        metadata = {
            "candidate": base,
            "state": "metadata_only",
            "extracted_text": None,
            "transcript": None,
            "errors": ["extraction_failed"],
        }
        self.assertEqual(validate_enrichment(metadata)["state"], "metadata_only")
        failed = {
            "candidate": base,
            "state": "failed",
            "extracted_text": None,
            "transcript": None,
            "errors": [],
        }
        with self.assertRaises(DigestError):
            validate_enrichment(failed)

    def test_miniflux_mapping_preserves_provenance_and_strips_markup(self):
        entry = {
            "id": 9,
            "title": "Episode",
            "url": "https://example.test/episode",
            "published_at": "2026-08-27T10:00:00Z",
            "changed_at": "2026-08-27T10:01:00.013556Z",
            "content": "<p>Hello <strong>world</strong></p>",
            "feed": {"id": 4, "title": "Private feed", "user_id": 12},
            "enclosures": [{"mime_type": "audio/mpeg"}],
        }
        row = miniflux_entry_candidate(entry, 12)
        self.assertEqual(row["summary"], "Hello world")
        self.assertEqual(row["media_type"], "podcast")
        self.assertEqual(
            (row["entry_id"], row["source_id"], row["source_title"]), (9, 4, "Private feed")
        )
        entry["feed"]["user_id"] = 13
        with self.assertRaisesRegex(DigestError, "current-user ownership"):
            miniflux_entry_candidate(entry, 12)
        del entry["feed"]["user_id"]
        with self.assertRaisesRegex(DigestError, "current-user ownership"):
            miniflux_entry_candidate(entry, 12)

    def test_miniflux_pagination_probes_exact_cap_and_rejects_above(self):
        def raw_entry(number):
            return {
                "id": number,
                "title": f"Entry {number}",
                "url": f"https://example.test/{number}",
                "published_at": "2026-08-27T10:00:00Z",
                "changed_at": "2026-08-27T10:00:00Z",
                "content": "summary",
                "feed": {"id": 4, "title": "Feed", "user_id": 12},
                "enclosures": [],
            }

        class FakeClient(MinifluxDigestClient):
            def __init__(self, count):
                self.count = count
                self.offsets = []

            def request(self, path):
                if path == "/v1/me":
                    return {"id": 12, "is_admin": False}
                offset = int(path.rsplit("offset=", 1)[1])
                self.offsets.append(offset)
                return {
                    "entries": [
                        raw_entry(i) for i in range(offset + 1, min(offset + 101, self.count + 1))
                    ]
                }

        exact = FakeClient(500)
        self.assertEqual(len(exact.collect([3])), 500)
        self.assertEqual(exact.offsets[-1], 500)
        with self.assertRaisesRegex(DigestError, "exceeds 500"):
            FakeClient(501).collect([3])

    def test_digest_policy_scopes_miniflux_filters_content_and_honors_quiet_hours(self):
        class ScopedClient(MinifluxDigestClient):
            def __init__(self):
                self.paths = []

            def request(self, path):
                self.paths.append(path)
                if path == "/v1/me":
                    return {"id": 12, "is_admin": False}
                return {"entries": []}

        client = ScopedClient()
        self.assertEqual(client.collect([8, 3]), [])
        entry_paths = [path for path in client.paths if path.startswith("/v1/entries")]
        self.assertEqual(len(entry_paths), 2)
        self.assertTrue(any("category_id=3" in path for path in entry_paths))
        self.assertTrue(any("category_id=8" in path for path in entry_paths))
        with self.assertRaisesRegex(DigestError, "selected category"):
            client.collect([])

        selected = self.protocol.claim(
            "2026-08-27T12:00:00Z",
            self.prefs,
            [
                candidate(1),
                {**candidate(2), "media_type": "video"},
                {**candidate(3), "source_id": 99},
            ],
            "telegram:123:45",
        )
        self.assertEqual([row["entry_id"] for row in selected["candidates"]], [1])
        quiet = self.protocol.claim(
            "2026-08-27T21:00:00Z",
            self.prefs,
            [candidate(4)],
            "telegram:123:45",
        )
        self.assertEqual(quiet, {"status": "quiet_hours"})

        enrichment = validate_enrichment(
            {
                "candidate": candidate(5),
                "state": "complete",
                "extracted_text": "x" * 12000,
                "transcript": "y" * 18000,
                "errors": [],
            },
            self.prefs,
        )
        self.assertEqual(len(enrichment["extracted_text"]), 12000)
        with self.assertRaisesRegex(DigestError, "max_extracted_chars"):
            validate_enrichment(
                {
                    "candidate": candidate(5),
                    "state": "complete",
                    "extracted_text": "x" * 12001,
                    "transcript": None,
                    "errors": [],
                },
                self.prefs,
            )

    def test_cli_quiet_hours_do_not_wake_agent(self):
        prefs = self.root / "prefs.json"
        candidates = self.root / "candidates.json"
        prefs.write_text(json.dumps(self.prefs))
        candidates.write_text(json.dumps([candidate(1)]))
        command = [
            sys.executable,
            str(SRC / "hermes_digest_workflow.py"),
            "gate",
            "--state-dir",
            str(self.root / "quiet-cli-state"),
            "--preferences",
            str(prefs),
            "--candidates",
            str(candidates),
            "--route",
            "telegram:123:45",
            "--now",
            "2026-08-31T20:27:00Z",
        ]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SRC), str(STATE_SRC)]))
        result = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"wakeAgent": False})

    def test_runtime_files_require_regular_mode_0400(self):
        path = self.root / "credential"
        path.write_text("secret")
        path.chmod(0o400)
        self.assertEqual(secure_read(path, "credential"), "secret")
        path.chmod(0o600)
        with self.assertRaisesRegex(DigestError, "0400 regular"):
            secure_read(path, "credential")

    def test_cli_noop_is_silent_and_claim_outputs_one_bounded_envelope(self):
        prefs = self.root / "prefs.json"
        candidates = self.root / "candidates.json"
        prefs.write_text(json.dumps(dict(self.prefs, enabled=False)))
        candidates.write_text(json.dumps([candidate(1)]))
        claim_now = "2099-08-27T12:00:00Z"
        command = [
            sys.executable,
            str(SRC / "hermes_digest_workflow.py"),
            "gate",
            "--state-dir",
            str(self.root / "cli-state"),
            "--preferences",
            str(prefs),
            "--candidates",
            str(candidates),
            "--route",
            "telegram:123:45",
            "--now",
            claim_now,
        ]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SRC), str(STATE_SRC)]))
        result = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"wakeAgent": False})
        prefs.write_text(json.dumps(self.prefs))
        result = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["wakeAgent"])
        context = envelope["context"]
        self.assertEqual(context["status"], "claimed")
        self.assertEqual(len(context["candidates"]), 1)
        self.assertEqual(context["state_dir"], str((self.root / "cli-state").absolute()))

        rendered = self.root / "rendered.txt"
        rendered.write_text("Digest body")
        rendered.chmod(0o600)
        transition = subprocess.run(
            [
                sys.executable,
                str(SRC / "hermes_digest_workflow.py"),
                "rendered",
                "--state-dir",
                context["state_dir"],
                "--batch-id",
                context["batch_id"],
                "--claim-id",
                context["claim_id"],
                "--rendered-file",
                str(rendered),
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(transition.returncode, 0, transition.stderr)
        self.assertEqual(json.loads(transition.stdout), {"status": "rendered"})
        again = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
        self.assertEqual(json.loads(again.stdout), {"wakeAgent": False})

    def test_disabled_live_gate_does_not_open_service_files_or_create_state(self):
        prefs = self.root / "disabled.json"
        prefs.write_text(json.dumps(dict(self.prefs, enabled=False)))
        state = self.root / "disabled-state"
        command = [
            sys.executable,
            str(SRC / "hermes_digest_workflow.py"),
            "live-gate",
            "--state-dir",
            str(state),
            "--preferences",
            str(prefs),
            "--route",
            "telegram:123:45",
            "--now",
            "2026-08-27T12:00:00Z",
            "--endpoint-file",
            str(self.root / "missing-endpoint"),
            "--credential-file",
            str(self.root / "missing-credential"),
        ]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SRC), str(STATE_SRC)]))
        result = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"wakeAgent": False})
        self.assertFalse(state.exists())

    def test_terminal_transitions_reject_worker_supplied_time(self):
        parser = __import__("hermes_digest_workflow").parser()
        common = [
            "--state-dir",
            str(self.root / "state"),
            "--batch-id",
            "batch-" + "a" * 64,
            "--claim-id",
            "claim-" + "b" * 32,
            "--now",
            "2000-01-01T00:00:00Z",
        ]
        with self.assertRaises(SystemExit):
            parser.parse_args(["rendered", *common, "--rendered-file", str(self.root / "body")])
        with self.assertRaises(SystemExit):
            parser.parse_args(["fail", *common, "--error", "late"])


if __name__ == "__main__":
    unittest.main()
