"""Issue #220 regressions: source semantics, not parent timestamp churn."""

import datetime as dt
import unittest

import forgejo_event_journal as journal


class Source:
    origin = "https://git.example.test"

    def __init__(self, issue):
        self.issue = issue

    def pages(self, path, query=None):
        return [self.issue] if path.endswith("/issues") else []


class IssueIntakeTests(unittest.TestCase):
    def test_authenticated_handled_receipt_is_evidence_not_new_work(self):
        receipt = (
            "Existing work retained.\n\n"
            "<!-- hermes:handled-comment repo=nixos/nix-config "
            "source=comment:3307 card=t_3e3def5c -->"
        )
        self.assertEqual(journal.comment_disposition(receipt, "system-admin-agent"), "evidence")
        self.assertEqual(journal.comment_disposition(receipt, "human"), "actionable")
        self.assertEqual(
            journal.comment_disposition("New regression needs investigation", "system-admin-agent"),
            "actionable",
        )
        for malformed in (
            receipt.replace("nixos/nix-config", "other/repo"),
            receipt.replace("comment:3307", "comment:wrong"),
            receipt.replace("t_3e3def5c", "bad-card"),
        ):
            self.assertEqual(
                journal.comment_disposition(malformed, "system-admin-agent"), "actionable"
            )

    def test_new_eligible_issue_is_actionable_without_comment(self):
        source = Source(
            {
                "number": 12,
                "title": "Repair service",
                "body": "Observed failure",
                "state": "open",
                "labels": [],
                "updated_at": "2026-09-15T10:00:00Z",
            }
        )
        events = journal.poll_events(source, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["disposition"], "actionable")

    def test_human_only_issue_never_requests_planning(self):
        source = Source(
            {
                "number": 12,
                "title": "Manual operation",
                "body": "Human only",
                "state": "open",
                "labels": [{"name": "human-only"}],
                "updated_at": "2026-09-15T10:00:00Z",
            }
        )
        events = journal.poll_events(source, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(events[0]["disposition"], "evidence")

    def test_body_edit_changes_semantics_but_comment_count_does_not(self):
        issue = {"number": 12, "title": "Repair", "body": "Original", "state": "open", "labels": []}
        original = journal.issue_semantic(issue)
        self.assertEqual(original, journal.issue_semantic({**issue, "comments": 12}))
        self.assertNotEqual(
            original, journal.issue_semantic({**issue, "body": "Edited requirements"})
        )

    def test_invalid_source_revision_is_error_not_empty_success(self):
        source = Source({"number": 12, "state": "open", "updated_at": "broken"})
        with self.assertRaises(journal.InputError):
            journal.poll_events(source, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))

    def test_inline_finding_on_old_review_is_discovered(self):
        class Reviews:
            origin = "https://git.example.test"

            def pages(self, path, query=None):
                if path.endswith("/pulls"):
                    return [{"number": 9, "state": "open", "updated_at": "2026-08-01T00:00:00Z"}]
                if path.endswith("/reviews"):
                    return [{"id": 4, "state": "COMMENT", "submitted_at": "2026-08-01T00:00:00Z"}]
                if path.endswith("/reviews/4/comments"):
                    return [
                        {
                            "id": 31,
                            "body": "This drops data",
                            "updated_at": "2026-09-15T00:00:00Z",
                            "user": {"login": "human"},
                        }
                    ]
                return []

        rows = journal.poll_events(Reviews(), dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["disposition"], "actionable")
        self.assertEqual(rows[0]["source_key"], "review-comment:31")


if __name__ == "__main__":
    unittest.main()
