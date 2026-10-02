from __future__ import annotations

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import forgejo_event_journal as journal
from hermes_repository_instance import (
    instance,
    instance_argument,
    activate_instance,
    RepositoryStore,
)
from hermes_workflow_state import ProtocolError, canonical_json_bytes

SCHEMA_VERSION = 1
JOURNAL_SCHEMA_VERSION = journal.SCHEMA_VERSION
PROTOCOL = "forgejo-kanban-workflow"
DEFAULT_STATE_ROOT = Path("/var/lib/hermes-forgejo-kanban-workflow")
DEFAULT_JOURNAL_ROOT = Path("/var/lib/hermes-forgejo-event-journal")
EVENT_ID_RE = re.compile(r"^fj-[0-9a-f]{64}$")
TASK_ID_RE = re.compile(r"^t_[0-9a-f]{8,64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_API_BYTES = 8 * 1024 * 1024
MAX_PAGES = 100
MAX_MAPPINGS = 5000
MAX_EVENTS_PER_RUN = 500
COMMENT_AUTHOR = "forgejo-kanban-reconciler"


class InputError(ValueError):
    pass


def strict_json(raw: bytes | str, label: str = "JSON") -> Any:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            if key in result:
                raise InputError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise InputError(f"{label} contains non-finite number {value}")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except InputError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{label} is malformed") from exc


def exact_dict(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise InputError(f"{label} must contain exactly {sorted(keys)}")
    return value


def bounded_text(value: Any, limit: int = 1000) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())[:limit]


def canonical_origin(endpoint: str) -> str:
    if (
        not endpoint
        or endpoint != endpoint.strip()
        or any(ord(c) < 0x21 or ord(c) == 0x7F for c in endpoint)
    ):
        raise InputError("Forgejo endpoint contains whitespace/control characters or is empty")
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise InputError("Forgejo endpoint must be an exact HTTPS origin")
    if parsed.path not in {"", "/", "/api/v1"} or parsed.query or parsed.fragment:
        raise InputError("Forgejo endpoint has an unsupported suffix")
    netloc = parsed.hostname + (f":{parsed.port}" if parsed.port is not None else "")
    origin = f"https://{netloc}"
    if endpoint not in {origin, origin + "/", origin + "/api/v1"}:
        raise InputError("Forgejo endpoint is not canonical")
    return origin


def read_secret(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise InputError("credential file is unavailable") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise InputError("credential file is unsafe")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise InputError("credential file is empty")
    return value


def logical_key(kind: str, number: int) -> str:
    if kind not in {"issue", "pull_request"} or type(number) is not int or number <= 0:
        raise InputError("logical target identity is invalid")
    return f"{instance().workflow_key_prefix}:{instance().repository}:{kind}:{number}"


def execution_key(logical: str, event_id: str, continuation: bool) -> str:
    if not EVENT_ID_RE.fullmatch(event_id):
        raise InputError("execution epoch is invalid")
    return f"{logical}:epoch:{event_id}" if continuation else logical


def marker(event_id: str) -> str:
    value = f"{instance().namespace}-forgejo-event:{event_id}"
    if not re.fullmatch(re.escape(instance().namespace) + r"-forgejo-event:fj-[0-9a-f]{64}", value):
        raise InputError("event marker is invalid")
    return value


def validate_event(value: Any) -> dict[str, Any]:
    try:
        event = dict(journal.validate_event(value))
    except journal.InputError as exc:
        raise InputError(str(exc)) from exc
    target = event["target"]
    event["logical_key"] = logical_key(target["kind"], target["number"])
    return event


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class ForgejoClient:
    def __init__(
        self, endpoint: str, credential_file: Path, opener: Callable[..., Any] | None = None
    ) -> None:
        self.origin = canonical_origin(endpoint)
        instance().check_origin(self.origin, "forgejo")
        self.token = read_secret(credential_file)
        # Never forward the repository credential across a redirect. Forgejo API
        # calls are exact-origin requests and redirects are protocol failures.
        self._opener = opener or urllib.request.build_opener(NoRedirect()).open

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        if (
            path != "/user"
            and not path.startswith(f"/repos/{instance().repository}/")
            and path != f"/repos/{instance().repository}"
        ):
            raise InputError("Forgejo API path escapes the repository allowlist")
        if path == "/user" and method != "GET":
            raise InputError("Forgejo current-user endpoint is read-only")
        url = self.origin + "/api/v1" + path
        body = canonical_json_bytes(payload) if payload is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": "token " + self.token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener(request, timeout=30) as response:
                raw = response.read(MAX_API_BYTES + 1)
                if len(raw) > MAX_API_BYTES:
                    raise RuntimeError("Forgejo response exceeds the bounded body limit")
                return strict_json(raw, "Forgejo response") if raw else None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Forgejo {method} failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Forgejo {method} failed: {exc.reason}") from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        return self._request("POST", path, payload)

    def pages(self, path: str, *, limit: int = 50) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            separator = "&" if "?" in path else "?"
            rows = self.get(f"{path}{separator}page={page}&limit={limit}")
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise RuntimeError("paginated Forgejo response is not a list of objects")
            result.extend(rows)
            if len(rows) < limit:
                return result
        raise RuntimeError("Forgejo pagination exceeded its hard request bound")

    def snapshot(self, event: Mapping[str, Any]) -> dict[str, Any]:
        target = event["target"]
        number = target["number"]
        if target["kind"] == "issue":
            issue = self.get(f"/repos/{instance().repository}/issues/{number}")
            if (
                not isinstance(issue, dict)
                or issue.get("number") != number
                or issue.get("pull_request")
            ):
                raise InputError("Forgejo issue readback does not match the event target")
            comments = self.pages(f"/repos/{instance().repository}/issues/{number}/comments")
            return {
                "target": "issue",
                "number": number,
                "url": f"{self.origin}/{instance().repository}/issues/{number}",
                "issue": issue,
                "comments": comments,
            }
        pull = self.get(f"/repos/{instance().repository}/pulls/{number}")
        if not isinstance(pull, dict) or pull.get("number") != number:
            raise InputError("Forgejo PR readback does not match the event target")
        head = str((pull.get("head") or {}).get("sha") or "")
        if not SHA_RE.fullmatch(head):
            raise InputError("Forgejo PR lacks an exact full head SHA")
        reviews = self.pages(f"/repos/{instance().repository}/pulls/{number}/reviews")
        review_rows: list[dict[str, Any]] = []
        for review in reviews:
            review_id = review.get("id")
            if type(review_id) is not int or review_id <= 0:
                raise InputError("Forgejo review lacks a stable id")
            comments = self.pages(
                f"/repos/{instance().repository}/pulls/{number}/reviews/{review_id}/comments"
            )
            review_rows.append({"review": review, "comments": comments})
        statuses = self.pages(f"/repos/{instance().repository}/commits/{head}/statuses")
        # Bind the snapshot to one exact head; any concurrent push invalidates it.
        confirm = self.get(f"/repos/{instance().repository}/pulls/{number}")
        confirm_head = str(((confirm or {}).get("head") or {}).get("sha") or "")
        if confirm_head != head:
            raise RuntimeError("PR head changed during exact-head snapshot")
        if any(confirm.get(k) != pull.get(k) for k in ("state", "merged", "merge_commit_sha")):
            raise RuntimeError("PR outcome changed during exact-head snapshot")
        return {
            "target": "pull_request",
            "number": number,
            "url": f"{self.origin}/{instance().repository}/pulls/{number}",
            "head": head,
            "pull": pull,
            "reviews": review_rows,
            "statuses": statuses,
        }

    def readiness_snapshot(self, number: int) -> dict[str, Any]:
        """Read the effective base-branch policy, conversation and exact-head evidence.

        The branch endpoint resolves wildcard protection rules on the server.
        A failed/unauthorized policy read is an error, never an empty success.
        """
        if type(number) is not int or number <= 0:
            raise InputError("PR number is invalid")
        event = {"target": {"kind": "pull_request", "number": number}}
        snapshot = self.snapshot(event)
        base = (snapshot["pull"].get("base") or {}).get("ref")
        if not isinstance(base, str) or not base:
            raise InputError("PR lacks its base branch identity")
        branch = self.get(
            f"/repos/{instance().repository}/branches/{urllib.parse.quote(base, safe='')}"
        )
        if not isinstance(branch, dict) or branch.get("name") != base:
            raise InputError("effective branch policy returned a different branch")
        snapshot["branch"] = branch
        snapshot["comments"] = self.pages(
            f"/repos/{instance().repository}/issues/{number}/comments"
        )
        confirm = self.get(f"/repos/{instance().repository}/pulls/{number}")
        if (
            not isinstance(confirm, dict)
            or (confirm.get("head") or {}).get("sha") != snapshot["head"]
            or (confirm.get("base") or {}).get("ref") != base
            or confirm.get("merged") != snapshot["pull"].get("merged")
            or confirm.get("state") != snapshot["pull"].get("state")
        ):
            raise RuntimeError("PR changed during readiness snapshot")
        return snapshot

    def guarded_reply(self, kind: str, number: int, body: str, event_id: str) -> dict[str, Any]:
        if kind not in {"issue", "pull_request"} or type(number) is not int or number <= 0:
            raise InputError("reply target is invalid")
        expected_marker = marker(event_id)
        if expected_marker not in body or len(body) > 10000:
            raise InputError("reply body must contain the exact source marker and remain bounded")
        target_path = (
            f"/repos/{instance().repository}/{'issues' if kind == 'issue' else 'pulls'}/{number}"
        )
        target = self.get(target_path)
        if not isinstance(target, dict) or target.get("number") != number:
            raise InputError("reply preflight target mismatch")
        if kind == "issue" and target.get("pull_request"):
            raise InputError("reply preflight expected an issue but resolved a PR")
        actor = self.get("/user")
        if not isinstance(actor, dict):
            raise InputError("Forgejo current user response is invalid")
        login = bounded_text(actor.get("login"), 100)
        if not login:
            raise InputError("Forgejo current user login is empty")

        def authored_matches() -> list[dict[str, Any]]:
            return [
                row
                for row in self.pages(f"/repos/{instance().repository}/issues/{number}/comments")
                if row.get("body") == body
                and isinstance(row.get("user"), Mapping)
                and row["user"].get("login") == login
            ]

        def proof(row: Mapping[str, Any]) -> dict[str, Any]:
            comment_id = row.get("id")
            if type(comment_id) is not int:
                raise RuntimeError("Forgejo reply readback omitted its comment identity")
            html_url = str(row.get("html_url") or "")
            expected_path = (
                f"/{instance().repository}/{'issues' if kind == 'issue' else 'pulls'}/{number}"
            )
            canonical = f"{self.origin}{expected_path}#issuecomment-{comment_id}"
            parsed = urllib.parse.urlsplit(html_url)
            if parsed.username or parsed.password or parsed.query or html_url != canonical:
                raise RuntimeError("Forgejo reply URL is not exact and canonical")
            return {"target_verified": True, "comment_id": comment_id, "url": canonical}

        existing = authored_matches()
        if len(existing) > 1:
            raise RuntimeError("Forgejo reply has duplicate authenticated readbacks")
        if existing:
            return proof(existing[0])
        try:
            result = self.post(
                f"/repos/{instance().repository}/issues/{number}/comments", {"body": body}
            )
        except (InputError, RuntimeError):
            existing = authored_matches()
            if len(existing) == 1:
                return proof(existing[0])
            raise
        if not isinstance(result, dict) or type(result.get("id")) is not int:
            raise RuntimeError("Forgejo reply did not return a comment identity")
        matches = [row for row in authored_matches() if row.get("id") == result["id"]]
        if len(matches) != 1:
            raise RuntimeError("Forgejo reply readback failed")
        return proof(matches[0])

    def deliver_issue_intent(
        self,
        intent: Mapping[str, Any],
        *,
        allow_write: bool,
        before_write: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        """Reconcile one bounded monthly issue; never create implementation work here."""
        expected = hashlib.sha256(
            canonical_json_bytes({k: v for k, v in intent.items() if k != "intent_sha256"})
        ).hexdigest()
        period = str(intent.get("period", ""))
        if (
            intent.get("schema_version") not in {1, 2}
            or intent.get("kind") != "container-monthly-update-issue"
            or intent.get("repository") != instance().repository
            or not SHA_RE.fullmatch(str(intent.get("source_commit", "")))
            or not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", period)
            or intent.get("logical_key") != f"{instance().namespace}-container-update:{period}"
            or intent.get("intent_sha256") != expected
        ):
            raise InputError("issue intent identity or digest is invalid")
        decisions = intent.get("decisions")
        if not isinstance(decisions, list) or not decisions or len(decisions) > 256:
            raise InputError("issue intent decisions are invalid")
        for decision in decisions:
            if (
                not isinstance(decision, dict)
                or decision.get("decision") not in {"include", "needs-human"}
                or not isinstance(decision.get("candidate"), dict)
            ):
                raise InputError(
                    "issue intent lacks exact candidate details; retain and reconcile legacy state"
                )
        logical = f"<!-- {intent['logical_key']} -->"
        title = f"Container image update audit {period}"
        report = (
            {"decisions": decisions, "inputs": intent["inputs"]}
            if intent["schema_version"] == 2
            else decisions
        )
        body = (
            f"{logical}\n<!-- intent-sha256:{expected} -->\n\n"
            f"Source: {instance().repository} at {intent['source_commit']}.\n\n"
            "Planning input only: verify release notes, compatibility, backups and runbooks before implementation. "
            "Use the normal issue-to-Kanban planning workflow. No merge, deployment or closure is authorized.\n\n"
            + (
                "Protocol: load nix-maintenance-protocol and repository-knowledge before processing this report.\n\n"
                if intent["schema_version"] == 2
                else ""
            )
            + "```json\n"
            + json.dumps(report, sort_keys=True, indent=2)
            + "\n```\n"
        )
        if len(body.encode()) > 65536:
            raise InputError("container issue exceeds bounded publication size")
        user = self.get("/user")
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(login, str) or not login:
            raise InputError("authenticated issue author is unavailable")
        path = f"/repos/{instance().repository}/issues"

        def same_month(row: Mapping[str, Any]) -> bool:
            text = str(row.get("body") or "")
            return (
                logical in text
                or (
                    instance().legacy_container_marker is not None
                    and f"<!-- {instance().legacy_container_marker}:{period} -->" in text
                )
                or row.get("title") == f"Monthly container image update report — {period}"
            )

        def lookup() -> dict[str, Any] | None:
            matches = [
                row for row in self.pages(path + "?state=all&type=issues") if same_month(row)
            ]
            if len(matches) > 1:
                raise RuntimeError("duplicate monthly issue identity requires reconciliation")
            if not matches:
                return None
            row = self.get(path + "/" + str(matches[0]["number"]))
            if (
                row.get("pull_request")
                or row.get("state") != "open"
                or (row.get("user") or {}).get("login") != login
                or not same_month(row)
            ):
                raise RuntimeError("monthly issue ownership/state changed; no write authorized")
            return row

        def report_readback(row: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
            if row is None:
                return None
            # Never replace human edits to the issue body. Later reports are
            # immutable authored comments on the same monthly issue.
            if str(row.get("body") or "").startswith(body):
                return row
            matches = [
                comment
                for comment in self.pages(path + "/" + str(row["number"]) + "/comments")
                if comment.get("body") == body and (comment.get("user") or {}).get("login") == login
            ]
            if len(matches) > 1:
                raise RuntimeError("duplicate authored issue reports require reconciliation")
            return matches[0] if matches else None

        observed = lookup()
        report = report_readback(observed)
        if report is None:
            if not allow_write:
                raise RuntimeError("ambiguous issue write has no exact readback; refusing replay")
            if before_write is not None:
                before_write()
            try:
                if observed is None:
                    self.post(path, {"title": title, "body": body})
                else:
                    self.post(path + "/" + str(observed["number"]) + "/comments", {"body": body})
            except (RuntimeError, OSError):
                # A lost response is never permission to repeat the write.
                observed = lookup()
                if report_readback(observed) is None:
                    raise
            observed = lookup()
            report = report_readback(observed)
        if report is None or observed is None:
            raise RuntimeError("container issue write did not read back exactly")
        number = observed.get("number")
        url = f"{self.origin}/{instance().repository}/issues/{number}"
        if type(number) is not int or number < 1 or observed.get("html_url") != url:
            raise RuntimeError("container issue readback has noncanonical identity")
        return {
            "repository": instance().repository,
            "source_commit": intent["source_commit"],
            "intent_sha256": expected,
            "target_kind": "issue",
            "target_number": number,
            "target_url": url,
            "readback_sha256": hashlib.sha256(
                canonical_json_bytes({"issue": observed, "report": report})
            ).hexdigest(),
        }

    def submit_pending_review(
        self, number: int, review_id: int, state: str, body: str | None
    ) -> dict[str, Any]:
        state = state.upper()
        if state not in {"APPROVED", "REQUEST_CHANGES", "COMMENT"}:
            raise InputError("review submission state is not allowlisted")
        before = self.snapshot({"target": {"kind": "pull_request", "number": number}})
        review = next(
            (row["review"] for row in before["reviews"] if row["review"].get("id") == review_id),
            None,
        )
        if not review or str(review.get("state") or "").upper() != "PENDING":
            raise InputError("review is not pending on the exact current PR")
        if review.get("commit_id") != before["head"] or review.get("stale") is True:
            raise InputError("pending review is not bound to the exact current head")
        payload: dict[str, Any] = {"state": state}
        expected_body = review.get("body")
        if body is not None:
            payload["body"] = body
            expected_body = body
        self.post(f"/repos/{instance().repository}/pulls/{number}/reviews/{review_id}", payload)
        after = self.snapshot({"target": {"kind": "pull_request", "number": number}})
        if after["head"] != before["head"]:
            raise RuntimeError("PR head changed while publishing review")
        stored = next(
            (row for row in after["reviews"] if row["review"].get("id") == review_id), None
        )
        if not stored or str(stored["review"].get("state") or "").upper() != state:
            raise RuntimeError("published review did not read back in the requested state")
        published = stored["review"]
        if published.get("commit_id") != after["head"] or published.get("stale") is not False:
            raise RuntimeError("published review is not bound to the current exact head")
        if published.get("official") is not True:
            raise RuntimeError("published review is not an official Forgejo review")
        if published.get("body") != expected_body:
            raise RuntimeError("published review body differs from the submitted body")
        return {"head": after["head"], "review": published, "comments": stored["comments"]}


def pr_actionable_digest(snapshot: Mapping[str, Any]) -> str:
    """Reuse intake classification; timestamp/status churn cannot invalidate a handoff."""
    comments = snapshot.get("comments")
    if not isinstance(comments, list):
        raise InputError("PR handoff lacks complete conversation evidence")
    semantic = []
    for comment in comments:
        author = (comment.get("user") or {}).get("login")
        if type(comment.get("id")) is not int or not isinstance(author, str):
            raise InputError("PR conversation identity is malformed")
        if journal.comment_disposition(str(comment.get("body") or ""), author) == "actionable":
            semantic.append({**journal.comment_semantic(comment), "author": author})
    review_bodies = [
        {
            "review": journal.review_semantic(entry["review"]),
            "comments": sorted(
                [journal.comment_semantic(c) for c in entry["comments"]], key=lambda c: c["id"]
            ),
        }
        for entry in snapshot["reviews"]
    ]
    return evidence_digest(
        {
            "head": snapshot["head"],
            "comments": sorted(semantic, key=lambda c: c["id"]),
            "reviews": sorted(review_bodies, key=lambda r: r["review"]["id"]),
        }
    )


def executed_ci_success(status: Mapping[str, Any]) -> bool:
    """Forgejo maps skipped Actions jobs to success; those are not execution evidence.

    Commit-status publishers otherwise own their success assertion. For Forgejo
    Actions links require its positive execution description; unknown/missing
    Actions descriptions fail closed. Never apply this requirement to optional
    skipped contexts merely because they appear in the status list.
    """
    if status.get("status") != "success":
        return False
    description = str(status.get("description") or "").strip().casefold()
    if re.search(r"\b(skip(?:ped)?|cancel(?:led|ed)?|not run|not executed)\b", description):
        return False
    target = urllib.parse.urlsplit(str(status.get("target_url") or ""))
    if "/actions/" in target.path or description.startswith("has "):
        # Supported commit-status serialization: legacy success text and the
        # duration-bearing success rows captured from Forgejo in review 48009557.
        # This is a full grammar, not a substring/prose success heuristic.
        return (
            description == "has succeeded"
            or re.fullmatch(
                r"successful in (?:[0-9]+h)?(?:[0-9]+m)?[0-9]+(?:\.[0-9]+)?s", description
            )
            is not None
        )
    return True


def pr_outcome_identity(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    pull = snapshot.get("pull") or {}
    if pull.get("state") != "closed":
        return None
    head, merged = snapshot.get("head"), pull.get("merged")
    merge = pull.get("merge_commit_sha") if merged is True else None
    if (
        not isinstance(head, str)
        or not SHA_RE.fullmatch(head)
        or type(merged) is not bool
        or (merged and (not isinstance(merge, str) or not SHA_RE.fullmatch(merge)))
    ):
        raise InputError("closed PR lacks an exact outcome identity")
    return {
        "url": snapshot["url"],
        "head": head,
        "outcome": "merged" if merged else "closed",
        "merge_commit": merge,
    }


def pr_readiness(snapshot: Mapping[str, Any], *, reviewer: str) -> dict[str, Any]:
    """Technical readiness only; it grants no human, merge or deploy authority."""
    head = snapshot.get("head")
    if not isinstance(head, str) or not SHA_RE.fullmatch(head):
        raise InputError("readiness requires an exact PR head")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise InputError("readiness requires the technical reviewer identity")
    pull, branch = snapshot.get("pull"), snapshot.get("branch")
    statuses, reviews = snapshot.get("statuses"), snapshot.get("reviews")
    if not isinstance(pull, dict) or not isinstance(branch, dict):
        raise InputError("readiness lacks PR or effective branch policy")
    if not isinstance(statuses, list) or not isinstance(reviews, list):
        raise InputError("readiness lacks complete status or review evidence")
    reasons: list[str] = []
    if pull.get("state") != "open" or pull.get("merged") is not False:
        reasons.append("pr-not-open")
    if pull.get("draft") is not False or pull.get("mergeable") is not True:
        reasons.append("pr-draft-or-not-mergeable")
    required = branch.get("status_check_contexts")
    if (
        branch.get("enable_status_check") is not True
        or not isinstance(required, list)
        or not required
    ):
        reasons.append("required-ci-policy-missing")
        required = []
    if any(not isinstance(context, str) or not context.strip() for context in required):
        raise InputError("required CI contexts are malformed")
    latest: dict[str, dict[str, Any]] = {}
    seen: set[int] = set()
    for status in statuses:
        if (
            not isinstance(status, dict)
            or type(status.get("id")) is not int
            or status["id"] <= 0
            or status["id"] in seen
            or not isinstance(status.get("context"), str)
            or not status["context"]
            or status.get("status") not in {"success", "pending", "failure", "error", "warning"}
        ):
            raise InputError("commit status evidence is malformed or duplicated")
        seen.add(status["id"])
        context = status["context"]
        if context not in latest or status["id"] > latest[context]["id"]:
            latest[context] = status
    for pattern in required:
        # Forgejo's common context globs use '*' and '?'. Do not approximate
        # extended glob syntax with a more permissive interpretation.
        if any(char in pattern for char in "[]{}\\"):
            reasons.append("unsupported-ci-context-pattern")
            continue
        matches = [row for context, row in latest.items() if fnmatch.fnmatchcase(context, pattern)]
        if not matches:
            reasons.append("missing-ci:" + pattern)
        elif any(row["status"] != "success" for row in matches):
            reasons.append("unsuccessful-ci:" + pattern)
        elif any(not executed_ci_success(row) for row in matches):
            reasons.append("unexecuted-ci:" + pattern)
    if any(row["status"] != "success" for row in latest.values()):
        reasons.append("ci-not-successful")
    latest_reviews: dict[str, dict[str, Any]] = {}
    seen_reviews: set[int] = set()
    for entry in reviews:
        if not isinstance(entry, dict) or not isinstance(entry.get("review"), dict):
            raise InputError("review evidence is malformed")
        review, comments = entry["review"], entry.get("comments")
        login = (review.get("user") or {}).get("login")
        if (
            type(review.get("id")) is not int
            or review["id"] <= 0
            or review["id"] in seen_reviews
            or not isinstance(login, str)
            or not login
            or not isinstance(comments, list)
        ):
            raise InputError("review identity or inline-comment evidence is malformed")
        seen_reviews.add(review["id"])
        # Read every inline surface, including older-head reviews. A stale
        # review does not resolve its discussion; Forgejo exposes 'resolver'.
        for comment in comments:
            if not isinstance(comment, dict) or type(comment.get("id")) is not int:
                raise InputError("inline-comment evidence is malformed")
            resolver = comment.get("resolver")
            if not isinstance(resolver, dict) or not resolver.get("login"):
                reasons.append("unresolved-inline:" + str(comment["id"]))
        if review.get("dismissed") is True or review.get("state") in {"PENDING", "COMMENT"}:
            continue
        if login not in latest_reviews or review["id"] > latest_reviews[login]["id"]:
            latest_reviews[login] = review
    for review in latest_reviews.values():
        if review.get("state") == "REQUEST_CHANGES":
            reasons.append("requested-changes:" + str(review["id"]))
    minimum = branch.get("required_approvals", 0)
    if type(minimum) is not int or minimum < 0:
        raise InputError("required approval policy is malformed")
    official_approvals = [
        r
        for r in latest_reviews.values()
        if r.get("state") == "APPROVED"
        and r.get("official") is True
        and r.get("commit_id") == head
        and r.get("stale") is False
        and r.get("dismissed") is False
    ]
    if len(official_approvals) < minimum:
        reasons.append("required-official-approvals-missing")
    approval = latest_reviews.get(reviewer, {})
    if (
        approval.get("state") != "APPROVED"
        or approval.get("commit_id") != head
        or approval.get("stale") is not False
        or approval.get("dismissed") is not False
    ):
        reasons.append("exact-head-technical-review-missing")
    return {
        "head": head,
        "ready": not reasons,
        "reasons": sorted(set(reasons)),
        "review_id": approval.get("id") if not reasons else None,
    }


class NativeKanban:
    def __init__(
        self,
        hermes: Path,
        board: str,
        hermes_home: Path,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        if (
            not hermes.is_absolute()
            or not SAFE_BOARD_RE.fullmatch(board)
            or board != instance().board
        ):
            raise InputError("Hermes path or board is invalid")
        self.hermes = hermes
        self.board = board
        self.hermes_home = hermes_home
        self._runner = runner

    def _run(self, args: Sequence[str], *, expect_json: bool = False) -> Any:
        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.hermes_home)
        env["HERMES_KANBAN_BOARD"] = self.board
        command = [str(self.hermes), "kanban", "--board", self.board, *args]
        result = self._runner(
            command, env=env, text=True, capture_output=True, timeout=60, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"native Kanban command failed: {bounded_text(result.stderr, 500)}")
        return (
            strict_json(result.stdout, "native Kanban response") if expect_json else result.stdout
        )

    def show(self, task_id: str) -> dict[str, Any]:
        if not TASK_ID_RE.fullmatch(task_id):
            raise InputError("Kanban task id is invalid")
        result = self._run(["show", task_id, "--json"], expect_json=True)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("task"), dict)
            or result["task"].get("id") != task_id
        ):
            raise RuntimeError("native Kanban show returned the wrong response envelope")
        if not isinstance(result.get("comments"), list):
            raise TypeError("native Kanban show omitted comments")
        return result

    def ensure_task(
        self, event: Mapping[str, Any], snapshot: Mapping[str, Any], continuation: bool
    ) -> dict[str, Any]:
        # Recover pre-existing/legacy work through native readback. The mapping
        # ledger is not the only place where authorized work may already exist.
        rows = self._run(["list", "--json"], expect_json=True)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("native Kanban list returned an invalid envelope")
        urls = [snapshot["url"], *[pull["html_url"] for pull in snapshot.get("linked_pulls", [])]]
        matches = [
            row
            for row in rows
            if row.get("status") not in {"done", "archived"}
            and "PR workflow: native-v1" not in str(row.get("body") or "").splitlines()
            and any(
                re.search(re.escape(url) + r"(?![A-Za-z0-9_/?#-])", str(row.get("body") or ""))
                for url in urls
            )
        ]
        if len(matches) > 1:
            raise RuntimeError("ambiguous live Forgejo lineage; refusing a competing worker")
        if matches:
            return self.show(matches[0]["id"])
        target = event["target"]
        logical = logical_key(target["kind"], target["number"])
        key = execution_key(logical, event["event_id"], continuation)
        label = "Issue" if target["kind"] == "issue" else "PR"
        title = bounded_text(
            (snapshot.get("issue") or snapshot.get("pull") or {}).get("title"), 160
        )
        body = "\n".join(
            [
                f"Forgejo authority: {snapshot['url']}",
                f"Logical workflow: {logical}",
                f"Execution epoch: {event['event_id']}",
                "Planning first: read the complete issue and comments, reconcile existing cards and PRs, and publish questions/plan before creating implementation work. Continue an existing implementation only within its already granted authority; never create a competing PR.",
                "Use Forgejo for technical state and this Kanban card for agent execution. Do not infer merge, deploy, rebuild, verification, or closure authority.",
            ]
        )
        args = [
            "create",
            f"{label} #{target['number']} — {title}",
            "--body",
            body,
            "--assignee",
            instance().implementer,
            "--tenant",
            instance().tenant,
            "--workspace",
            "scratch",
            "--idempotency-key",
            key,
            "--created-by",
            "forgejo-kanban-reconciler",
            "--skill",
            "forgejo-pr-lifecycle",
            "--skill",
            "forgejo-api-workflow",
            "--json",
        ]
        result = self._run(args, expect_json=True)
        task_id = str((result or {}).get("id") or "")
        if not TASK_ID_RE.fullmatch(task_id):
            raise RuntimeError("native Kanban create did not return a task id")
        return self.show(task_id)

    def ensure_outcome(self, snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
        """F11 owns the transition, native identity/dependencies own retry and dispatch.

        Only an active, explicitly opted-in anchor authorizes this continuation.
        Include archived executions in recovery so timestamp churn cannot revive
        completed work. The read-only projector never creates a worker.
        """
        identity = pr_outcome_identity(snapshot)
        if identity is None:
            return None
        rows = self._run(["list", "--archived", "--json"], expect_json=True)
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise RuntimeError("native outcome list returned an invalid envelope")
        authority = "Forgejo authority: " + snapshot["url"]
        anchors = [
            r
            for r in rows
            if authority in str(r.get("body") or "").splitlines()
            and "PR workflow: native-v1" in str(r.get("body") or "").splitlines()
            and r.get("status") not in {"done", "archived"}
        ]
        if not anchors:
            return None
        if len(anchors) != 1 or anchors[0].get("assignee") != instance().implementer:
            raise InputError("ambiguous or wrongly owned native PR anchor")
        anchor = anchors[0]
        if self.show(anchor["id"])["task"] != anchor:
            raise RuntimeError("native PR anchor changed during outcome readback")
        anchor_ref = f"{self.board}/{anchor['id']}"
        key = (
            instance().namespace
            + "-pr-outcome:"
            + evidence_digest({"anchor": anchor_ref, **identity})
        )
        fields = [
            f"PR projection anchor: {anchor_ref}",
            "PR workflow phase: outcome",
            f"PR head: {identity['head']}",
            authority,
            f"PR outcome: {identity['outcome']}",
            f"PR outcome identity: {key}",
        ]
        if identity["merge_commit"]:
            fields.append("Merge commit: " + identity["merge_commit"])

        def verify(task: dict[str, Any]) -> dict[str, Any]:
            row = task["task"]
            lines = str(row.get("body") or "").splitlines()
            if row.get("assignee") != instance().implementer or any(
                lines.count(f) != 1 for f in fields
            ):
                raise RuntimeError("native outcome readback does not match its exact identity")
            if anchor["id"] in task.get("parents", []):
                raise RuntimeError("outcome must not depend on its blocked projection anchor")
            return task

        existing = [
            r
            for r in rows
            if f"PR outcome identity: {key}" in str(r.get("body") or "").splitlines()
        ]
        if len(existing) > 1:
            raise InputError("ambiguous native PR outcome identity")
        if existing:
            return verify(self.show(existing[0]["id"]))
        # Wait for active execution instead of launching a competing worker or
        # releasing unrelated human gates. Completed predecessors are evidence,
        # not a requirement for recognizing a close without implementation.
        parents = sorted(
            r["id"]
            for r in rows
            if r["id"] != anchor["id"]
            and authority in str(r.get("body") or "").splitlines()
            and r.get("status") not in {"done", "archived"}
        )
        body = "\n".join(
            fields
            + [
                "Read the authoritative PR again before recording its outcome. Reconcile the anchor and prior phase evidence.",
                "Determine required rebuild and verification from the reviewed scope. Request any missing human authorization through the native Human-Wait gate.",
                "A merge is not deployment, rebuild, rollback, verification or issue-closure authorization. Publish an outcome receipt and create the required native followups before completing.",
            ]
        )
        args = [
            "create",
            f"PR #{snapshot['number']} — reconcile {identity['outcome']}",
            "--body",
            body,
            "--assignee",
            instance().implementer,
            "--tenant",
            instance().tenant,
            "--workspace",
            "scratch",
            "--idempotency-key",
            key,
            "--created-by",
            "forgejo-kanban-reconciler",
            "--skill",
            "forgejo-pr-lifecycle",
            "--skill",
            "forgejo-api-workflow",
            "--json",
        ]
        for parent in parents:
            args.extend(["--parent", parent])
        created = self._run(args, expect_json=True)
        return verify(self.show(str((created or {}).get("id") or "")))

    def comment_once(self, task_id: str, body: str, event_id: str) -> None:
        marker(event_id)  # Validate that the body is bound to an exact F10 identity.

        def present(task: Mapping[str, Any]) -> bool:
            return any(
                row.get("body") == body and row.get("author") == COMMENT_AUTHOR
                for row in task["comments"]
            )

        current = self.show(task_id)
        if present(current):
            return
        try:
            self._run(["comment", task_id, body, "--author", COMMENT_AUTHOR])
        except RuntimeError:
            # The write may have committed before the CLI response was lost.
            if present(self.show(task_id)):
                return
            raise
        after = self.show(task_id)
        matches = [
            row
            for row in after["comments"]
            if row.get("body") == body and row.get("author") == COMMENT_AUTHOR
        ]
        if len(matches) != 1:
            raise RuntimeError("native Kanban comment mutation did not read back exactly once")

    def wake_actionable(self, task: Mapping[str, Any], event: Mapping[str, Any]) -> None:
        # A source comment is new evidence, not proof of deployment approval,
        # recovered credentials, or expiry of a scheduled gate. Persist it for
        # the native live-comment bridge/next authorized run, but do not erase
        # an unrelated blocker or scheduled start. Ready/review/running work is
        # already owned by the native dispatcher.
        return


def empty_mapping() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "mappings": []}


def validate_mapping(documents: Mapping[str, Any]) -> dict[str, Any]:
    if set(documents) != {"mapping.json"}:
        raise InputError("workflow mapping generation has the wrong document set")
    root = exact_dict(documents["mapping.json"], {"schema_version", "mappings"}, "workflow mapping")
    if (
        root["schema_version"] != SCHEMA_VERSION
        or not isinstance(root["mappings"], list)
        or len(root["mappings"]) > MAX_MAPPINGS
    ):
        raise InputError("workflow mapping is invalid or unbounded")
    logicals: list[str] = []
    for row in root["mappings"]:
        row = exact_dict(
            row,
            {
                "logical_key",
                "target_kind",
                "target_number",
                "current_task_id",
                "execution_epoch",
                "task_ids",
                "last_event_id",
            },
            "mapping row",
        )
        if row["logical_key"] != logical_key(row["target_kind"], row["target_number"]):
            raise InputError("mapping logical identity is invalid")
        if not TASK_ID_RE.fullmatch(str(row["current_task_id"])) or not EVENT_ID_RE.fullmatch(
            str(row["execution_epoch"])
        ):
            raise InputError("mapping task or execution identity is invalid")
        if (
            not isinstance(row["task_ids"], list)
            or not row["task_ids"]
            or row["task_ids"] != list(dict.fromkeys(row["task_ids"]))
        ):
            raise InputError("mapping task history is invalid")
        if any(not TASK_ID_RE.fullmatch(str(task_id)) for task_id in row["task_ids"]):
            raise InputError("mapping task history contains an invalid id")
        if row["current_task_id"] != row["task_ids"][-1] or not EVENT_ID_RE.fullmatch(
            str(row["last_event_id"])
        ):
            raise InputError("mapping current identity is inconsistent")
        logicals.append(row["logical_key"])
    if logicals != sorted(set(logicals)):
        raise InputError("workflow mappings must be sorted and unique")
    return {"mapping.json": root}


def validate_legacy_mapping(documents):
    validated = validate_mapping(documents)
    if not validated["mapping.json"]["mappings"]:
        raise InputError("legacy workflow has no repository identity evidence")
    return validated


class MappingStore:
    def __init__(self, root: Path) -> None:
        self.store = RepositoryStore(
            root,
            protocol=PROTOCOL,
            schema_version=SCHEMA_VERSION,
            legacy_validator=validate_legacy_mapping,
        )
        # Publish a complete empty generation under the protocol lock before any
        # reader can observe a newly-created but selector-less root.
        self.store.update(
            lambda selected: (
                validate_mapping(selected.documents)
                if selected
                else {"mapping.json": empty_mapping()}
            )
        )
        self.store.cleanup(keep_previous=1)

    def read(self) -> dict[str, dict[str, Any]]:
        documents = validate_mapping(self.store.read().documents)
        return {row["logical_key"]: row for row in documents["mapping.json"]["mappings"]}

    @contextmanager
    def reconcile_lock(self) -> Any:
        lock_path = self.store.root / ".reconcile.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise ProtocolError("workflow reconciliation lock is unsafe")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def put(self, event: Mapping[str, Any], task_id: str, *, continuation: bool) -> None:
        logical = event["logical_key"]

        def transform(selected: Any) -> Mapping[str, Any]:
            documents = (
                validate_mapping(selected.documents)
                if selected
                else {"mapping.json": empty_mapping()}
            )
            rows = {row["logical_key"]: row for row in documents["mapping.json"]["mappings"]}
            current = rows.get(logical)
            task_ids = list(current["task_ids"]) if current else []
            if task_id not in task_ids:
                task_ids.append(task_id)
            rows[logical] = {
                "logical_key": logical,
                "target_kind": event["target"]["kind"],
                "target_number": event["target"]["number"],
                "current_task_id": task_id,
                "execution_epoch": event["event_id"]
                if continuation or current is None
                else current["execution_epoch"],
                "task_ids": task_ids,
                "last_event_id": event["event_id"],
            }
            return validate_mapping(
                {
                    "mapping.json": {
                        "schema_version": SCHEMA_VERSION,
                        "mappings": [rows[key] for key in sorted(rows)],
                    }
                }
            )

        try:
            self.store.update(transform)
        finally:
            self.store.cleanup(keep_previous=1)


class JournalCli:
    def __init__(
        self, executable: Path, state_root: Path, runner: Callable[..., Any] = subprocess.run
    ) -> None:
        self.executable = executable
        self.state_root = state_root
        self._runner = runner

    def _run(self, args: Sequence[str]) -> Any:
        result = self._runner(
            [str(self.executable), *args, "--state-root", str(self.state_root)],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"event journal command failed: {bounded_text(result.stderr, 500)}")
        return strict_json(result.stdout, "event journal response")

    def pending(self) -> list[dict[str, Any]]:
        result = self._run(["pending"])
        if (
            not isinstance(result, dict)
            or result.get("schema_version") != JOURNAL_SCHEMA_VERSION
            or not isinstance(result.get("events"), list)
        ):
            raise RuntimeError("event journal pending response is invalid")
        # The journal retains and exports the complete backlog for metrics and
        # later runs. Each reconciler invocation processes only the stable
        # leading slice so a valid burst cannot permanently starve the queue.
        return [validate_event(row) for row in result["events"][:MAX_EVENTS_PER_RUN]]

    def ack(self, event_id: str, proof_id: str) -> None:
        result = self._run(["ack", "--event-id", event_id, "--proof-id", proof_id])
        if not isinstance(result, dict) or result.get("event_id") != event_id:
            raise RuntimeError("event journal acknowledgement readback is invalid")


def evidence_digest(snapshot: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()


def event_comment(event: Mapping[str, Any], snapshot: Mapping[str, Any]) -> str:
    lines = [
        marker(event["event_id"]),
        f"Source key: {event['source_key']}",
        f"Disposition: {event['disposition']}",
        f"Forgejo revision: {event['revision']}",
        f"Exact evidence digest: {evidence_digest(snapshot)}",
    ]
    if snapshot.get("head"):
        lines.append(f"Exact PR head: {snapshot['head']}")
    if snapshot.get("reviews") is not None:
        inline_count = sum(len(row["comments"]) for row in snapshot["reviews"])
        lines.append(
            f"Formal reviews inspected: {len(snapshot['reviews'])}; inline comments inspected: {inline_count}"
        )
    lines.append(
        "This is source evidence, not merge, deploy, rebuild, verification, closure, or human authorization."
    )
    return "\n".join(lines)


def _reconcile_locked(
    journal: JournalCli, forgejo: ForgejoClient, kanban: NativeKanban, mappings: MappingStore
) -> dict[str, int]:
    counters = {
        "events": 0,
        "actionable": 0,
        "evidence": 0,
        "created": 0,
        "continued": 0,
        "noop": 0,
    }
    for event in journal.pending():
        counters["events"] += 1
        snapshot = forgejo.snapshot(event)
        logical = event["logical_key"]
        current = mappings.read().get(logical)
        target = snapshot.get("issue") or snapshot.get("pull") or {}
        if "human-only" in {label["name"] for label in target.get("labels", [])}:
            journal.ack(
                event["event_id"],
                f"excluded-human-only:{event['event_id']}:{evidence_digest(snapshot)}",
            )
            counters["noop"] += 1
            continue
        if event["kind"] == "pull_request" and pr_outcome_identity(snapshot) is not None:
            outcome = kanban.ensure_outcome(snapshot)
            if outcome is not None:
                task_id = outcome["task"]["id"]
                # Do not ACK an outdated outcome if the source changed during
                # native creation. Its worker must also re-read before acting.
                if pr_outcome_identity(forgejo.snapshot(event)) != pr_outcome_identity(snapshot):
                    raise RuntimeError("PR outcome changed during native handoff")
                # Later comments may already have advanced execution to a
                # rebuild/verification card. Replaying the terminal snapshot
                # must not move the lineage back to its older outcome task.
                if current is None or task_id not in current["task_ids"]:
                    mappings.put(event, task_id, continuation=True)
                journal.ack(
                    event["event_id"],
                    f"kanban-outcome:{kanban.board}:{task_id}:{event['event_id']}",
                )
                counters["continued"] += 1
                counters["actionable"] += 1
                continue
        if event["disposition"] == "evidence":
            # Status-only changes and confirmed automation echoes belong in
            # the journal, not the live worker comment bridge.
            proof = f"evidence-no-route:{event['event_id']}:{evidence_digest(snapshot)}"
            journal.ack(event["event_id"], proof)
            counters["evidence"] += 1
            counters["noop"] += 1
            continue
        continuation = False
        task: dict[str, Any]
        if current is not None:
            task = kanban.show(current["current_task_id"])
            if (
                task["task"].get("status") in {"done", "archived"}
                or "PR workflow: native-v1" in str(task["task"].get("body") or "").splitlines()
            ) and event["disposition"] == "actionable":
                continuation = True
                task = kanban.ensure_task(event, snapshot, continuation=True)
                counters["continued"] += 1
        else:
            task = kanban.ensure_task(event, snapshot, continuation=False)
            counters["created"] += 1
        task_id = str(task["task"]["id"])
        mappings.put(event, task_id, continuation=continuation)
        comment = event_comment(event, snapshot)
        kanban.comment_once(task_id, comment, event["event_id"])
        if event["disposition"] == "actionable":
            kanban.wake_actionable(kanban.show(task_id), event)
            counters["actionable"] += 1
        else:
            counters["evidence"] += 1
        proof = f"kanban:{kanban.board}:{task_id}:{event['event_id']}:{evidence_digest(snapshot)}"
        journal.ack(event["event_id"], proof)
    return counters


def reconcile(
    journal: JournalCli, forgejo: ForgejoClient, kanban: NativeKanban, mappings: MappingStore
) -> dict[str, int]:
    # Hermes' idempotency lookup and insert are not one native transaction. F11
    # is the sole creator for this keyspace, so serialize read/create/map/ack as
    # one crash-releasing process lock. Response-loss retries then converge via
    # the native idempotency lookup without permitting two simultaneous creates.
    with mappings.reconcile_lock():
        return _reconcile_locked(journal, forgejo, kanban, mappings)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="forgejo-kanban-workflow")
    instance_argument(root)
    root.add_argument("--endpoint", required=True)
    root.add_argument("--credential-file", type=Path, required=True)
    sub = root.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect-pr")
    inspect.add_argument("--number", type=int, required=True)
    inspect.add_argument("--reviewer", required=True)
    run = sub.add_parser("reconcile")
    run.add_argument("--hermes", type=Path, required=True)
    run.add_argument("--hermes-home", type=Path, required=True)
    run.add_argument("--board", required=True)
    run.add_argument("--journal", type=Path, required=True)
    run.add_argument("--journal-state-root", type=Path, default=DEFAULT_JOURNAL_ROOT)
    run.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    reply = sub.add_parser("reply")
    reply.add_argument("--target-kind", choices=["issue", "pull_request"], required=True)
    reply.add_argument("--number", type=int, required=True)
    reply.add_argument("--event-id", required=True)
    reply.add_argument("--body-file", type=Path, required=True)
    review = sub.add_parser("submit-review")
    review.add_argument("--number", type=int, required=True)
    review.add_argument("--review-id", type=int, required=True)
    review.add_argument(
        "--state", choices=["APPROVED", "REQUEST_CHANGES", "COMMENT"], required=True
    )
    review.add_argument("--body-file", type=Path)
    return root


def main() -> int:
    args = parser().parse_args()
    activate_instance(args.instance_config, review_executor="hermes")
    instance().check_origin(canonical_origin(args.endpoint), "forgejo")
    forgejo = ForgejoClient(args.endpoint, args.credential_file)
    if args.command == "inspect-pr":
        snapshot = forgejo.readiness_snapshot(args.number)
        print(
            json.dumps(
                {
                    "snapshot": snapshot,
                    "actionable_digest": pr_actionable_digest(snapshot),
                    "readiness": pr_readiness(snapshot, reviewer=args.reviewer),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "reply":
        body = args.body_file.read_text(encoding="utf-8")
        print(
            json.dumps(
                forgejo.guarded_reply(args.target_kind, args.number, body, args.event_id),
                sort_keys=True,
            )
        )
        return 0
    if args.command == "submit-review":
        body = args.body_file.read_text(encoding="utf-8") if args.body_file else None
        print(
            json.dumps(
                forgejo.submit_pending_review(args.number, args.review_id, args.state, body),
                sort_keys=True,
            )
        )
        return 0
    for path in (
        args.hermes,
        args.hermes_home,
        args.journal,
        args.journal_state_root,
        args.state_root,
    ):
        if not path.is_absolute():
            raise SystemExit("all runtime paths must be absolute")
    result = reconcile(
        JournalCli(args.journal, args.journal_state_root),
        forgejo,
        NativeKanban(args.hermes, args.board, args.hermes_home),
        MappingStore(args.state_root),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InputError, ProtocolError, RuntimeError, TypeError, OSError) as exc:
        print(f"forgejo-kanban-workflow: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
