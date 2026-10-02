from __future__ import annotations

import copy
import datetime as dt
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import forgejo_event_journal as journal

NOW = "2026-08-28T01:02:03Z"
REVISION = "2026-08-28T01:00:00Z"
ORIGIN = "https://git.example.test"


def issue_payload(number: int = 200, repository: str = journal.instance().repository) -> dict:
    return {
        "action": "updated",
        "repository": {"full_name": repository},
        "sender": {"login": "sandro"},
        "issue": {
            "number": number,
            "updated_at": REVISION,
            "created_at": "2026-08-27T12:00:00Z",
            "state": "open",
            "title": "fixture",
        },
    }


def comment_payload(body: str = "Please fix this", comment_id: int = 2775) -> dict:
    payload = issue_payload()
    payload["comment"] = {
        "id": comment_id,
        "updated_at": REVISION,
        "created_at": REVISION,
        "body": body,
    }
    return payload


class FakeClient:
    origin = ORIGIN

    def __init__(self, full_page: bool = False) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self.full_page = full_page

    def pages(self, path, query=None, limit=50):
        self.calls.append((path, query))
        if path.endswith("/issues"):
            return iter(
                [
                    {
                        "number": 200,
                        "updated_at": REVISION,
                        "created_at": REVISION,
                        "state": "open",
                        "title": "fixture",
                    }
                ]
            )
        if path.endswith("/issues/comments"):
            return iter(
                [
                    {
                        "id": 2775,
                        "issue_url": ORIGIN + "/api/v1/repos/nixos/nix-config/issues/200",
                        "html_url": ORIGIN + "/nixos/nix-config/issues/200#issuecomment-2775",
                        "updated_at": REVISION,
                        "created_at": REVISION,
                        "body": "Please fix this",
                    }
                ]
            )
        if path.endswith("/pulls"):
            return iter(
                [
                    {
                        "number": 199,
                        "updated_at": REVISION,
                        "created_at": REVISION,
                        "state": "open",
                        "head": {"sha": "a" * 40},
                    }
                ]
            )
        if path.endswith("/pulls/199/reviews"):
            return iter(
                [
                    {
                        "id": 44,
                        "submitted_at": REVISION,
                        "state": "REQUEST_CHANGES",
                    }
                ]
            )
        if path.endswith("/commits/" + "a" * 40 + "/statuses"):
            return iter(
                [
                    {
                        "id": 55,
                        "updated_at": REVISION,
                        "context": "Flake Check",
                        "status": "failure",
                    }
                ]
            )
        return iter([])


class JournalTests(unittest.TestCase):
    def event(self, source: str = "poll", disposition: str = "evidence") -> dict:
        return journal.make_event(
            origin=ORIGIN,
            kind="issue",
            source_key="issue:200",
            revision=REVISION,
            target_kind="issue",
            target_number=200,
            disposition=disposition,
            summary="fixture",
            semantic=journal.issue_semantic(issue_payload()["issue"]),
            source=source,
            observed_at=NOW,
        )

    def test_repository_and_exact_origin_fail_closed(self):
        with self.assertRaisesRegex(journal.InputError, "repository"):
            journal.repository_from_payload(issue_payload(repository="other/repo"))
        invalid_origins = [
            "http://git.wrzalek.com",
            "https://user@git.wrzalek.com",
            "https://git.example.test/extra",
            "https://git.example.test?x=1",
            " https://git.example.test",
        ]
        for value in invalid_origins:
            with self.subTest(value=value), self.assertRaises(journal.InputError):
                journal.canonical_origin(value)

    def test_duplicate_reordered_and_polling_repair_converge(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = journal.Journal(Path(td))
            first_poll = self.event("poll")
            poll = self.event("poll")
            first = ledger.ingest([first_poll], source="poll", observed_at=NOW)
            duplicate = ledger.ingest(
                [copy.deepcopy(first_poll), copy.deepcopy(first_poll)],
                source="poll",
                observed_at=NOW,
            )
            repaired = ledger.complete_poll([poll], completed_at="2026-08-28T01:05:00Z")
            self.assertEqual(first["persisted"], 1)
            self.assertEqual(duplicate["deduplicated"], 2)
            self.assertEqual(repaired["deduplicated"], 1)
            pending = ledger.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["sources"], ["poll"])
            self.assertEqual(
                ledger.read()["cursor.json"]["last_completed_poll"], "2026-08-28T01:05:00Z"
            )

    def test_acknowledgement_is_idempotent_and_replay_safe(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = journal.Journal(Path(td))
            event = self.event()
            ledger.ingest([event], source="poll", observed_at=NOW)
            self.assertTrue(
                ledger.acknowledge(
                    event["event_id"], "kanban:t_12345678:handled", acknowledged_at=NOW
                )
            )
            self.assertFalse(
                ledger.acknowledge(
                    event["event_id"], "kanban:t_12345678:handled", acknowledged_at=NOW
                )
            )
            self.assertEqual(ledger.pending(), [])
            ledger.complete_poll([self.event("poll")], completed_at="2026-08-28T01:05:00Z")
            self.assertEqual(ledger.pending(), [])
            with self.assertRaisesRegex(journal.InputError, "different proof"):
                ledger.acknowledge(event["event_id"], "different")

    def test_acknowledged_non_issue_events_are_compacted_after_poll_overlap(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(journal, "MAX_EVENTS", 1):
            ledger = journal.Journal(Path(td))
            first = self.event()
            first.update(kind="comment", source_key="comment:200")
            first["event_id"] = journal.stable_event_id(first)
            ledger.ingest([first], source="poll", observed_at=NOW)
            ledger.acknowledge(first["event_id"], "kanban:t_12345678:handled", acknowledged_at=NOW)
            ledger.complete_poll([], completed_at="2026-08-28T01:10:00Z")
            documents = ledger.read()
            self.assertEqual(documents["journal.json"]["events"], [])
            self.assertEqual(documents["receipts.json"]["receipts"], [])

            replacement = journal.make_event(
                origin=ORIGIN,
                kind="issue",
                source_key="issue:201",
                revision="2026-08-28T01:09:00Z",
                target_kind="issue",
                target_number=201,
                disposition="evidence",
                summary="replacement",
                semantic={"number": 201},
                source="poll",
                observed_at="2026-08-28T01:10:00Z",
            )
            ledger.ingest([replacement], source="poll", observed_at="2026-08-28T01:10:00Z")
            self.assertEqual(ledger.pending(), [replacement])

    def test_durable_before_ack_crash_boundaries_recover(self):
        labels = [
            "after_document:cursor.json",
            "after_manifest",
            "after_generation_fsync",
            "after_generation_publish",
            "after_selector_replace",
            "after_selector_fsync",
        ]
        for label in labels:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as td:
                triggered = False

                def fault(point, *, expected=label):
                    nonlocal triggered
                    if point == expected and not triggered:
                        triggered = True
                        raise OSError("injected crash")

                event = self.event()
                journal.Journal(Path(td)).initialize()
                crashing = journal.Journal(Path(td), fault=fault)
                with self.assertRaisesRegex(OSError, "injected crash"):
                    crashing.ingest([event], source="poll", observed_at=NOW)
                restarted = journal.Journal(Path(td))
                restarted.ingest([event], source="poll", observed_at=NOW)
                self.assertEqual(
                    [row["event_id"] for row in restarted.pending()], [event["event_id"]]
                )
                restarted.store.cleanup(keep_previous=1)
                self.assertLessEqual(len(list((Path(td) / "generations").iterdir())), 2)

    def test_repeated_post_selector_failures_do_not_leak_generations(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal.Journal(root).initialize()
            for _ in range(3):

                def fault(point, *, expected="after_selector_replace"):
                    if point == expected:
                        raise OSError("injected post-selector failure")

                with self.assertRaisesRegex(OSError, "post-selector"):
                    journal.Journal(root, fault=fault).ingest(
                        [self.event(disposition="actionable")], source="poll", observed_at=NOW
                    )
            self.assertLessEqual(len(list((root / "generations").iterdir())), 2)

    def test_malformed_selector_generation_symlink_and_path_attacks_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = journal.Journal(root)
            ledger.ingest([self.event()], source="poll", observed_at=NOW)
            selector = json.loads((root / "current.json").read_text())
            attacks = ["../escape", "/tmp/escape", "g-not-a-digest", "g-" + "A" * 64]
            original = (root / "current.json").read_bytes()
            for attack in attacks:
                with self.subTest(attack=attack):
                    bad = dict(selector)
                    bad["generation"] = attack
                    (root / "current.json").write_text(json.dumps(bad))
                    with self.assertRaises(journal.ProtocolError):
                        ledger.store.read()
            (root / "current.json").write_bytes(original)
            selected = selector["generation"]
            generation = root / "generations" / selected
            moved = root / "real"
            generation.rename(moved)
            generation.symlink_to(moved, target_is_directory=True)
            with self.assertRaisesRegex(journal.ProtocolError, "symlink"):
                ledger.store.read()

    def test_strict_json_rejects_duplicates_and_non_finite_numbers(self):
        for raw in [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}']:
            with self.subTest(raw=raw), self.assertRaises(journal.InputError):
                journal.strict_json(raw)

    def test_poll_repairs_all_pages_and_classifies_only_reasoning_actionable(self):
        since = dt.datetime(2026, 8, 28, 0, 55, tzinfo=dt.timezone.utc)
        rows = journal.poll_events(FakeClient(), since, observed_at=NOW)
        self.assertEqual(
            {row["kind"] for row in rows}, {"issue", "comment", "pull_request", "review", "status"}
        )
        self.assertEqual(
            {row["kind"] for row in rows if row["disposition"] == "actionable"},
            {"issue", "comment", "review"},
        )
        self.assertTrue(all(row["repository"] == journal.instance().repository for row in rows))

    def test_poll_accepts_forgejo16_issue_and_pull_request_comment_urls(self):
        class CommentClient(FakeClient):
            def pages(self, path, query=None, limit=50):
                if path.endswith("/issues/comments"):
                    # Forgejo 16's Comment schema populates only the URL for
                    # the target kind; PR comments have an empty issue_url.
                    return iter(
                        [
                            {
                                "id": comment_id,
                                "issue_url": target if kind == "issue" else "",
                                "pull_request_url": target if kind == "pull_request" else "",
                                "html_url": f"{target}#issuecomment-{comment_id}",
                                "updated_at": REVISION,
                                "created_at": REVISION,
                                "body": "Please fix this",
                            }
                            for kind, comment_id, target in [
                                ("issue", 2775, ORIGIN + "/nixos/nix-config/issues/200"),
                                ("pull_request", 2776, ORIGIN + "/nixos/nix-config/pulls/199"),
                            ]
                        ]
                    )
                return super().pages(path, query, limit)

        since = dt.datetime(2026, 8, 28, 0, 55, tzinfo=dt.timezone.utc)
        rows = journal.poll_events(CommentClient(), since, observed_at=NOW)
        comments = [row for row in rows if row["kind"] == "comment"]
        self.assertEqual(len(comments), 2)
        self.assertEqual({row["target"]["kind"] for row in comments}, {"issue", "pull_request"})
        self.assertEqual({row["target"]["number"] for row in comments}, {199, 200})
        self.assertTrue(all(row["disposition"] == "actionable" for row in comments))

    def test_client_pages_fetches_until_short_page_and_is_repository_confined(self):
        with tempfile.TemporaryDirectory() as td:
            credential = Path(td) / "credential"
            credential.write_text("fixture")
            client = journal.ForgejoClient(ORIGIN, credential)
            calls = []

            def get(path: str, query: dict | None = None):
                self.assertIsNotNone(query)
                assert query is not None
                calls.append(query["page"])
                return [{"id": i} for i in range(50)] if query["page"] == 1 else [{"id": 51}]

            client.get = get
            self.assertEqual(len(list(client.pages("/repos/nixos/nix-config/issues"))), 51)
            self.assertEqual(calls, [1, 2])
            with self.assertRaisesRegex(journal.InputError, "allowlist"):
                journal.ForgejoClient.get(client, "/repos/other/repo/issues")

    def test_client_installs_no_redirect_handler_for_authenticated_requests(self):
        with tempfile.TemporaryDirectory() as td:
            credential = Path(td) / "credential"
            credential.write_text("fixture")
            opener = mock.Mock()
            with mock.patch.object(
                journal.urllib.request, "build_opener", return_value=opener
            ) as build:
                client = journal.ForgejoClient(ORIGIN, credential)
            handler = build.call_args.args[0]
            self.assertIsInstance(handler, journal.NoRedirect)
            self.assertIs(client._opener, opener.open)
            self.assertIsNone(
                handler.redirect_request(
                    urllib.request.Request(ORIGIN + "/api/v1/repos/nixos/nix-config"),
                    None,
                    302,
                    "redirect",
                    {"Location": "https://evil.test/steal"},
                    "https://evil.test/steal",
                )
            )

    def test_service_start_cursor_is_durable_before_first_network_poll(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = journal.Journal(Path(td))
            with mock.patch.object(journal, "utc_now", return_value="2026-08-28T01:00:00Z"):
                first = ledger.bind_initial_poll_since("service-start")
            with mock.patch.object(journal, "utc_now", return_value="2026-08-28T01:05:00Z"):
                retry = ledger.bind_initial_poll_since("service-start")
            self.assertEqual(first, "2026-08-28T01:00:00Z")
            self.assertEqual(retry, first)
            self.assertEqual(
                ledger.read()["cursor.json"]["initial_poll_since"],
                "2026-08-28T01:00:00Z",
            )

    def test_poll_only_contract_has_no_webhook_commands_or_state(self):
        command_action = next(
            action
            for action in journal.parser()._actions
            if isinstance(action, journal.argparse._SubParsersAction)
        )
        self.assertNotIn("receive", command_action.choices)
        self.assertNotIn("hook-spec", command_action.choices)
        documents = journal.empty_documents()
        self.assertFalse(any("webhook" in key for key in documents["health.json"]))
        self.assertFalse(any("webhook" in key for key in documents["metrics.json"]))

    def test_metrics_expose_degraded_paths_and_pending_classes(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = journal.Journal(Path(td))
            evidence = self.event(disposition="evidence")
            actionable = journal.make_event(
                origin=ORIGIN,
                kind="comment",
                source_key="comment:2775",
                revision=REVISION,
                target_kind="issue",
                target_number=200,
                disposition="actionable",
                summary="fix",
                semantic=journal.comment_semantic(comment_payload()["comment"]),
                source="poll",
                observed_at=NOW,
            )
            ledger.ingest([evidence, actionable], source="poll", observed_at=NOW)
            ledger.mark_failure("poll down", source="poll", when=NOW)
            text = journal.prometheus_metrics(ledger.read())
            self.assertIn('pending{disposition="actionable"} 1', text)
            self.assertIn('pending{disposition="evidence"} 1', text)
            self.assertIn("poll_degraded 1", text)

    def test_generation_retention_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = journal.Journal(Path(td))
            ledger.initialize()
            for index in range(25):
                ledger.mark_failure(f"failure {index}", source="poll", when=NOW)
            generations = list((Path(td) / "generations").iterdir())
            self.assertLessEqual(len(generations), 2)


if __name__ == "__main__":
    unittest.main()
