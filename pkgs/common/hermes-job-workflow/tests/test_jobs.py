from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
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

from hermes_job_workflow import (  # noqa: E402
    ActiveResearchGate,
    JobError,
    JobProtocol,
    ProjectionPlanner,
    collect_employer_json,
    collect_feed_entries,
    collect_greenhouse_pages,
    collect_lever_pages,
    collect_miniflux_entries,
    load_private_cv,
    normalize_records,
    ranking_envelope,
    validate_preferences,
)

NOW = "2026-08-28T08:00:00Z"


def raw_job(
    job_id: str = "123",
    *,
    source_kind: str = "ats",
    official: bool = True,
    state: str = "active",
    title: str = "Staff Platform Engineer",
    observed_at: str = NOW,
):
    return {
        "source_kind": source_kind,
        "source_name": "Example Careers",
        "source_url": "https://jobs.example.test/",
        "source_namespace": "ats:example",
        "source_service": "example-careers",
        "requisition_id": job_id,
        "aliases": [],
        "canonical_url": f"https://jobs.example.test/jobs/{job_id}",
        "employer": "Example GmbH",
        "title": title,
        "location": "Berlin, Germany",
        "description": "Build reliable platforms.",
        "published_at": "2026-08-20T10:00:00Z",
        "observed_at": observed_at,
        "source_state": state,
        "official": official,
        "provenance": [
            {
                "url": f"https://jobs.example.test/jobs/{job_id}",
                "observed_at": observed_at,
                "kind": source_kind,
            }
        ],
    }


def preferences(**overrides):
    value = {
        "enabled": True,
        "active_research_enabled": True,
        "active_cadence_minutes": 1440,
        "max_active_candidates": 8,
        "revalidate_after_minutes": 1440,
        "closed_retention_days": 180,
        "projection": "manual",
    }
    value.update(overrides)
    return value


class NormalizationTests(unittest.TestCase):
    def test_official_source_wins_and_provenance_is_merged(self):
        feed = raw_job(source_kind="feed", official=False, title="Platform Engineer")
        feed["description"] = "Short feed summary"
        ats = raw_job()
        rows = normalize_records([feed, ats])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Staff Platform Engineer")
        self.assertTrue(rows[0]["official"])
        self.assertEqual({p["kind"] for p in rows[0]["provenance"]}, {"feed", "ats"})
        self.assertRegex(rows[0]["job_id"], r"^job-[0-9a-f]{64}$")

    def test_requisition_identity_is_url_independent_and_aliases_are_explicit(self):
        moved = raw_job()
        moved["canonical_url"] = "https://jobs.example.test/new/123"
        moved["provenance"] = [{"url": moved["canonical_url"], "observed_at": NOW, "kind": "ats"}]
        original, relocated = normalize_records([raw_job()]), normalize_records([moved])
        self.assertEqual(original[0]["job_id"], relocated[0]["job_id"])
        other = raw_job("legacy-123", source_kind="feed", official=False)
        other["source_namespace"] = "feed:miniflux"
        other["source_service"] = "example-feed"
        other["aliases"] = ["ats:example/example-careers/Example GmbH/123"]
        self.assertEqual(len(normalize_records([raw_job(), other])), 1)

    def test_conflicting_equal_authority_revision_fails_closed(self):
        one = raw_job(title="One")
        two = raw_job(title="Two")
        with self.assertRaisesRegex(JobError, "conflicting equal-precedence"):
            normalize_records([one, two])

    def test_urls_are_canonical_safe_and_official_is_not_inferred(self):
        for url in (
            "http://jobs.example.test/jobs/123",
            "https://JOBS.example.test/jobs/123",
            "https://user@jobs.example.test/jobs/123",
            "https://jobs.example.test/jobs/../123",
            "https://jobs.example.test/jobs/123#apply",
        ):
            value = raw_job()
            value["canonical_url"] = url
            with self.subTest(url=url), self.assertRaises(JobError):
                normalize_records([value])
        feed = raw_job(source_kind="feed", official=False)
        self.assertFalse(normalize_records([feed])[0]["official"])

    def test_unknown_fields_and_unbounded_content_are_rejected(self):
        with self.assertRaises(JobError):
            normalize_records([{**raw_job(), "cv": "private"}])
        with self.assertRaises(JobError):
            normalize_records([{**raw_job(), "description": "x" * 20001}])


class CollectorTests(unittest.TestCase):
    def test_passive_feed_mapping_is_non_official_and_bounded(self):
        rows = collect_feed_entries(
            [
                {
                    "id": "feed-1",
                    "url": "https://jobs.example.test/jobs/123",
                    "employer": "Example GmbH",
                    "title": "Platform Engineer",
                    "location": "Berlin",
                    "description": "Feed summary",
                    "published_at": "2026-08-20T10:00:00Z",
                }
            ],
            source_name="Example jobs feed",
            source_url="https://jobs.example.test/feed.xml",
            observed_at=NOW,
        )
        self.assertEqual(rows[0]["source_kind"], "feed")
        self.assertFalse(rows[0]["official"])
        with self.assertRaisesRegex(JobError, "record bound"):
            collect_feed_entries(
                [
                    {
                        "id": str(i),
                        "url": f"https://jobs.example.test/{i}",
                        "employer": "Example",
                        "title": "Role",
                        "location": "",
                        "description": "",
                        "published_at": None,
                    }
                    for i in range(501)
                ],
                source_name="Feed",
                source_url="https://jobs.example.test/feed.xml",
                observed_at=NOW,
            )

    def test_miniflux_entry_mapping_preserves_feed_provenance(self):
        rows = collect_miniflux_entries(
            [
                {
                    "content": "Build reliable AI systems",
                    "content_truncated": False,
                    "feed_id": 31,
                    "feed_title": "Example Careers",
                    "feed_url": "https://jobs.example.test/feed.xml",
                    "id": 41,
                    "published_at": "2026-08-20T10:00:00Z",
                    "site_url": "https://jobs.example.test/",
                    "title": "Senior AI Engineer",
                    "url": "https://jobs.example.test/jobs/41",
                }
            ],
            observed_at=NOW,
        )
        self.assertEqual(rows[0]["source_kind"], "feed")
        self.assertEqual(rows[0]["requisition_id"], "41")
        self.assertEqual(rows[0]["source_name"], "Example Careers")
        self.assertFalse(rows[0]["official"])

    def test_greenhouse_pagination_maps_official_records_and_stops(self):
        pages = [
            {
                "jobs": [
                    {
                        "id": 1,
                        "title": "One",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                        "location": {"name": "Remote"},
                        "content": "First",
                        "updated_at": "2026-08-27T10:00:00Z",
                    }
                ]
            },
            {
                "jobs": [
                    {
                        "id": 2,
                        "title": "Two",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                        "location": {"name": "Berlin"},
                        "content": "Second",
                        "updated_at": "2026-08-27T11:00:00Z",
                    }
                ]
            },
            {"jobs": []},
        ]
        rows = collect_greenhouse_pages(pages, board="acme", employer="Acme", observed_at=NOW)
        self.assertEqual(sorted(row["requisition_id"] for row in rows), ["1", "2"])
        self.assertTrue(all(row["official"] for row in rows))

    def test_lever_pagination_and_employer_json_are_bounded(self):
        pages = [
            [
                {
                    "id": "a",
                    "text": "Engineer",
                    "hostedUrl": "https://jobs.lever.co/acme/a",
                    "categories": {"location": "Remote"},
                    "descriptionPlain": "Role",
                    "createdAt": 1787800000000,
                }
            ],
            [],
        ]
        rows = collect_lever_pages(pages, site="acme", employer="Acme", observed_at=NOW)
        self.assertEqual(rows[0]["requisition_id"], "a")
        employer_rows = collect_employer_json(
            [
                {
                    "id": "x",
                    "title": "SRE",
                    "url": "https://careers.acme.test/jobs/x",
                    "location": "Remote",
                    "description": "Operate systems",
                    "published_at": None,
                    "state": "active",
                }
            ],
            source_name="Acme careers",
            source_url="https://careers.acme.test/",
            employer="Acme",
            observed_at=NOW,
        )
        self.assertEqual(employer_rows[0]["source_kind"], "employer")
        with self.assertRaisesRegex(JobError, "page bound"):
            collect_greenhouse_pages(
                [{"jobs": []}] * 21, board="acme", employer="Acme", observed_at=NOW
            )
        with self.assertRaisesRegex(JobError, "record bound"):
            collect_employer_json(
                [
                    {
                        "id": str(i),
                        "title": "SRE",
                        "url": f"https://careers.acme.test/jobs/{i}",
                        "location": "",
                        "description": "",
                        "published_at": None,
                        "state": "active",
                    }
                    for i in range(501)
                ],
                source_name="Acme careers",
                source_url="https://careers.acme.test/",
                employer="Acme",
                observed_at=NOW,
            )


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.protocol = JobProtocol(self.root / "state")

    def tearDown(self):
        self.temp.cleanup()

    def test_active_timeout_unknown_recovery_and_explicit_closed_transitions(self):
        active = self.protocol.ingest(NOW, [raw_job()], preferences())
        job_id = active["jobs"][0]["job_id"]
        self.assertEqual(active["jobs"][0]["status"], "active")

        timeout = self.protocol.revalidate(
            "2026-08-29T09:00:00Z",
            [{"job_id": job_id, "outcome": "timeout", "record": None, "reason": "deadline"}],
            preferences(),
        )
        self.assertEqual(timeout["jobs"][0]["status"], "unknown")
        self.assertEqual(timeout["jobs"][0]["last_error"], "timeout:deadline")

        recovered = self.protocol.revalidate(
            "2026-08-29T10:00:00Z",
            [
                {
                    "job_id": job_id,
                    "outcome": "active",
                    "record": raw_job(observed_at="2026-08-29T10:00:00Z"),
                    "reason": None,
                }
            ],
            preferences(),
        )
        self.assertEqual(recovered["jobs"][0]["status"], "active")

        closed_record = raw_job(state="closed", observed_at="2026-08-30T10:00:00Z")
        closed = self.protocol.revalidate(
            "2026-08-30T10:00:00Z",
            [
                {
                    "job_id": job_id,
                    "outcome": "closed",
                    "record": closed_record,
                    "reason": "official_gone",
                }
            ],
            preferences(),
        )
        self.assertEqual(closed["jobs"][0]["status"], "closed")
        self.assertEqual(closed["jobs"][0]["closed_at"], "2026-08-30T10:00:00Z")

    def test_bot_protection_and_malformed_response_never_close(self):
        first = self.protocol.ingest(NOW, [raw_job()], preferences())
        job_id = first["jobs"][0]["job_id"]
        for outcome in ("bot_protection", "error"):
            state = self.protocol.revalidate(
                "2026-08-29T09:00:00Z",
                [{"job_id": job_id, "outcome": outcome, "record": None, "reason": "blocked"}],
                preferences(),
            )
            self.assertEqual(state["jobs"][0]["status"], "unknown")

    def test_closed_records_are_retained_then_tombstoned_without_identity_reuse(self):
        first = self.protocol.ingest(
            NOW, [raw_job(state="closed")], preferences(closed_retention_days=30)
        )
        job_id = first["jobs"][0]["job_id"]
        retained = self.protocol.prune(
            "2026-09-20T08:00:00Z", preferences(closed_retention_days=30)
        )
        self.assertEqual(retained["jobs"][0]["status"], "closed")
        pruned = self.protocol.prune("2026-10-01T08:00:01Z", preferences(closed_retention_days=30))
        self.assertEqual(pruned["jobs"], [])
        self.assertEqual(pruned["tombstones"][0]["job_id"], job_id)
        with self.assertRaisesRegex(JobError, "tombstoned"):
            self.protocol.ingest(
                "2026-10-02T08:00:00Z", [raw_job(observed_at="2026-10-02T08:00:00Z")], preferences()
            )

    def test_dedup_and_restart_preserve_provenance(self):
        feed = raw_job(source_kind="feed", official=False, title="Platform Engineer")
        self.protocol.ingest(NOW, [feed, raw_job()], preferences())
        restarted = JobProtocol(self.root / "state").inspect()
        self.assertEqual(len(restarted["jobs"]), 1)
        self.assertEqual(
            {p["kind"] for p in restarted["jobs"][0]["record"]["provenance"]}, {"feed", "ats"}
        )

    def test_out_of_order_observation_cannot_overwrite_newer_record(self):
        self.protocol.ingest(NOW, [raw_job(title="Current")], preferences())
        stale = raw_job(title="Stale", observed_at="2026-08-28T07:00:00Z")
        state = self.protocol.ingest("2026-08-28T09:00:00Z", [stale], preferences())
        self.assertEqual(state["jobs"][0]["record"]["title"], "Current")
        self.assertEqual(state["jobs"][0]["last_seen_at"], NOW)

    def test_newer_observation_updates_record_and_preserves_provenance(self):
        self.protocol.ingest(NOW, [raw_job()], preferences())
        newer = raw_job(title="Principal Platform Engineer", observed_at="2026-08-28T10:00:00Z")
        newer["provenance"] = [
            {
                "url": newer["canonical_url"],
                "observed_at": newer["observed_at"],
                "kind": "ats",
            }
        ]
        state = self.protocol.ingest("2026-08-28T10:00:00Z", [newer], preferences())
        record = state["jobs"][0]["record"]
        self.assertEqual(record["title"], "Principal Platform Engineer")
        self.assertEqual(len(record["provenance"]), 2)

    def test_duplicate_revalidation_identity_is_rejected(self):
        state = self.protocol.ingest(NOW, [raw_job()], preferences())
        job_id = state["jobs"][0]["job_id"]
        result = {"job_id": job_id, "outcome": "timeout", "record": None, "reason": "deadline"}
        with self.assertRaisesRegex(JobError, "duplicate revalidation"):
            self.protocol.revalidate("2026-08-29T09:00:00Z", [result, result], preferences())

    def test_semantically_invalid_selected_state_fails_closed(self):
        self.protocol.store.publish(
            {
                "jobs.json": {"schema_version": 1, "jobs": [{"status": "closed"}]},
                "tombstones.json": {"schema_version": 1, "tombstones": []},
                "meta.json": {"schema_version": 1, "updated_at": NOW},
            }
        )
        with self.assertRaises(JobError):
            self.protocol.inspect()

    def test_private_cv_never_enters_store_or_collector_output(self):
        marker = "PRIVATE-CV-MARKER-7f91"
        cv = self.root / "cv.md"
        cv.write_text(marker, encoding="utf-8")
        cv.chmod(0o400)
        records = normalize_records([raw_job()])
        envelope = ranking_envelope(records, load_private_cv(cv))
        self.assertEqual(envelope["private_cv"], marker)
        self.protocol.ingest(NOW, records, preferences())
        stored = b"".join(
            path.read_bytes() for path in (self.root / "state").rglob("*") if path.is_file()
        )
        self.assertNotIn(marker.encode(), stored)
        self.assertNotIn("private_cv", json.dumps(self.protocol.inspect()))
        cv.chmod(0o600)
        with self.assertRaisesRegex(JobError, "0400"):
            load_private_cv(cv)


class ActiveGateAndProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_active_gate_zero_token_noop_and_bounded_positive_claim(self):
        passive = JobProtocol(self.root / "passive")
        passive.ingest(NOW, [raw_job(), raw_job("456")], preferences())
        gate = ActiveResearchGate(self.root / "active")
        disabled = gate.claim_from_passive(NOW, preferences(active_research_enabled=False), passive)
        self.assertEqual(disabled, {"wakeAgent": False, "reason": "disabled"})
        empty_protocol = JobProtocol(self.root / "empty")
        empty = gate.claim_from_passive(NOW, preferences(), empty_protocol)
        self.assertEqual(empty, {"wakeAgent": False, "reason": "empty"})
        positive = gate.claim_from_passive(NOW, preferences(max_active_candidates=1), passive)
        self.assertTrue(positive["wakeAgent"])
        self.assertEqual(len(positive["candidates"]), 1)
        self.assertNotIn("queries", positive)
        self.assertEqual(
            gate.claim_from_passive("2026-08-28T08:01:00Z", preferences(), passive),
            {"wakeAgent": False, "reason": "in_flight"},
        )

    def test_active_claim_is_bound_to_exact_durable_passive_generation(self):
        passive = JobProtocol(self.root / "passive")
        passive.ingest(NOW, [raw_job()], preferences())
        claim = ActiveResearchGate(self.root / "active").claim_from_passive(
            NOW, preferences(), passive
        )
        self.assertRegex(claim["passive_generation"], r"^g-[0-9a-f]{64}$")
        self.assertEqual(claim["candidate_ids"], [claim["candidates"][0]["job_id"]])

    def test_active_gate_completion_restart_and_cadence(self):
        def clock():
            return dt.datetime(2026, 8, 28, 8, 0, 30, tzinfo=dt.timezone.utc)

        gate = ActiveResearchGate(self.root / "active", clock=clock)
        claim = gate.claim(NOW, preferences(), [raw_job()])
        self.assertEqual(claim["claim_expires_at"], "2026-08-28T09:00:30Z")
        gate.complete(claim["claim_id"], "research-summary-sha256:" + "a" * 64)
        restarted = ActiveResearchGate(self.root / "active")
        self.assertEqual(
            restarted.claim("2026-08-28T09:00:00Z", preferences(), [raw_job()]),
            {"wakeAgent": False, "reason": "not_due"},
        )

    def test_active_completion_uses_trusted_clock_and_fences_expired_and_successor_claims(self):
        now = [dt.datetime(2026, 8, 28, 8, 0, 0, tzinfo=dt.timezone.utc)]
        gate = ActiveResearchGate(self.root / "active", clock=lambda: now[0])
        first = gate.claim(NOW, preferences(), [raw_job()], claim_ttl_seconds=60)
        now[0] = dt.datetime(2026, 8, 28, 8, 1, 0, tzinfo=dt.timezone.utc)
        with self.assertRaisesRegex(JobError, "expired"):
            gate.complete(first["claim_id"], "research-summary-sha256:" + "a" * 64)

        second = gate.claim(
            "2026-08-28T08:01:00Z", preferences(), [raw_job()], claim_ttl_seconds=60
        )
        self.assertNotEqual(first["claim_id"], second["claim_id"])
        with self.assertRaisesRegex(JobError, "current active claim"):
            gate.complete(first["claim_id"], "research-summary-sha256:" + "b" * 64)

    def test_projection_defaults_manual_and_auto_is_verified_recent_managed_only(self):
        protocol = JobProtocol(self.root / "jobs")
        active = protocol.ingest(NOW, [raw_job()], preferences())["jobs"][0]
        closed = {**active, "status": "closed", "closed_at": NOW}
        unknown = {**active, "status": "unknown", "last_error": "timeout:deadline"}
        self.assertEqual(ProjectionPlanner("off", False).plan([active]), [])
        manual = ProjectionPlanner("manual", False).plan([active])
        self.assertEqual(manual[0]["action"], "manual_review")
        suggest = ProjectionPlanner("suggest", False, now="2026-08-28T08:30:00Z").plan(
            [active, unknown, closed]
        )
        self.assertEqual([row["action"] for row in suggest], ["suggest"])
        self.assertIsNone(suggest[0]["mutation"])
        with self.assertRaisesRegex(JobError, "explicitly authorized"):
            ProjectionPlanner("auto", False).plan([active])
        planner = ProjectionPlanner("auto", True, now="2026-08-28T08:30:00Z")
        auto = planner.plan([active, unknown, closed])
        self.assertEqual([row["action"] for row in auto], ["upsert", "annotate_closed"])
        self.assertEqual(auto[0]["mutation"]["readback"], "exact-managed-fields")
        self.assertEqual(auto[0]["mutation"]["method"], "PUT")
        self.assertEqual(auto[1]["mutation"]["fields"], ["description"])
        rendered = json.dumps(auto)
        for forbidden in ("delete", "complete", "apply", "message"):
            self.assertNotIn(f'"{forbidden}"', rendered)

        stale = json.loads(json.dumps(active))
        stale["last_seen_at"] = "2026-08-20T08:00:00Z"
        unverified = json.loads(json.dumps(active))
        unverified["record"]["official"] = False
        self.assertEqual(planner.plan([unknown, stale, unverified]), [])

    def test_preferences_reject_auto_without_separate_authorization(self):
        validate_preferences(preferences())
        with self.assertRaisesRegex(JobError, "auto projection"):
            validate_preferences(preferences(projection="auto"))
        self.assertEqual(
            validate_preferences(preferences(projection="auto"), auto_authorized=True)[
                "projection"
            ],
            "auto",
        )

    def test_cli_passive_is_always_no_wake_and_active_is_bounded(self):
        trusted_now = (
            dt.datetime.now(dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        records = self.root / "records.json"
        prefs = self.root / "prefs.json"
        records.write_text(json.dumps([raw_job()]), encoding="utf-8")
        prefs.write_text(json.dumps(preferences()), encoding="utf-8")
        cli = SRC / "hermes_job_workflow.py"
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SRC), str(STATE_SRC)]))
        passive = subprocess.run(
            [
                sys.executable,
                str(cli),
                "passive",
                "--state-dir",
                str(self.root / "passive"),
                "--preferences",
                str(prefs),
                "--records",
                str(records),
                "--now",
                NOW,
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(passive.returncode, 0, passive.stderr)
        self.assertEqual(json.loads(passive.stdout), {"wakeAgent": False})
        miniflux_entries = self.root / "miniflux-entries.json"
        miniflux_entries.write_text(
            json.dumps(
                [
                    {
                        "content": "Build reliable AI systems",
                        "content_truncated": False,
                        "feed_id": 31,
                        "feed_title": "Example Careers",
                        "feed_url": "https://jobs.example.test/feed.xml",
                        "id": 41,
                        "published_at": "2026-08-20T10:00:00Z",
                        "site_url": "https://jobs.example.test/",
                        "title": "Senior AI Engineer",
                        "url": "https://jobs.example.test/jobs/41",
                    }
                ]
            ),
            encoding="utf-8",
        )
        miniflux_passive = subprocess.run(
            [
                sys.executable,
                str(cli),
                "miniflux-passive",
                "--state-dir",
                str(self.root / "miniflux-passive"),
                "--preferences",
                str(prefs),
                "--entries",
                str(miniflux_entries),
                "--now",
                NOW,
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(miniflux_passive.returncode, 0, miniflux_passive.stderr)
        self.assertEqual(json.loads(miniflux_passive.stdout), {"wakeAgent": False})
        projection = subprocess.run(
            [
                sys.executable,
                str(cli),
                "projection-plan",
                "--state-dir",
                str(self.root / "miniflux-passive"),
                "--preferences",
                str(prefs),
                "--now",
                trusted_now,
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(projection.returncode, 0, projection.stderr)
        projection_result = json.loads(projection.stdout)
        self.assertEqual(projection_result["mode"], "manual")
        self.assertEqual(projection_result["actions"][0]["action"], "manual_review")
        active = subprocess.run(
            [
                sys.executable,
                str(cli),
                "miniflux-active-gate",
                "--state-dir",
                str(self.root / "active-cli"),
                "--preferences",
                str(prefs),
                "--passive-state-dir",
                str(self.root / "miniflux-passive"),
                "--now",
                trusted_now,
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(active.returncode, 0, active.stderr)
        active_result = json.loads(active.stdout)
        self.assertTrue(active_result["wakeAgent"])
        caller_timed = subprocess.run(
            [
                sys.executable,
                str(cli),
                "active-complete",
                "--state-dir",
                str(self.root / "active-cli"),
                "--claim-id",
                active_result["claim_id"],
                "--proof",
                "research-summary-sha256:" + "a" * 64,
                "--now",
                "2000-01-01T00:00:00Z",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(caller_timed.returncode, 2)
        completed = subprocess.run(
            [
                sys.executable,
                str(cli),
                "active-complete",
                "--state-dir",
                str(self.root / "active-cli"),
                "--claim-id",
                active_result["claim_id"],
                "--proof",
                "research-summary-sha256:" + "a" * 64,
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "completed")


class ManagedArtifactContractTests(unittest.TestCase):
    def test_preference_contract_matches_runtime_and_auto_is_disabled(self):
        contract = json.loads(
            (Path(__file__).parents[1] / "contracts" / "preferences.json").read_text()
        )
        self.assertEqual(set(contract["properties"]), PREFERENCE_KEYS_FOR_TEST)
        self.assertEqual(contract["properties"]["projection"]["default"], "manual")
        self.assertNotIn("auto_projection_authorized", contract["properties"])


PREFERENCE_KEYS_FOR_TEST = {
    "enabled",
    "active_research_enabled",
    "active_cadence_minutes",
    "max_active_candidates",
    "revalidate_after_minutes",
    "closed_retention_days",
    "projection",
}


if __name__ == "__main__":
    unittest.main()
