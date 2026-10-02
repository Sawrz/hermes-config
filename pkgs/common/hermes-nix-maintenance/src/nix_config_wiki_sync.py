#!/usr/bin/env python3
"""Dry-run-by-default Forgejo Wiki sync for reviewed repository documentation.

Repo Markdown is the source of truth. The Forgejo Wiki is workflow-owned.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import fnmatch
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

APPLY_CONFIRMATION = "sync-reviewed-docs-to-wiki"
PROVENANCE_TEMPLATE = (
    "<!-- synced from {repo}@{commit} path {path} by scripts/nix_config_wiki_sync.py -->\n\n"
)


class WikiSyncError(RuntimeError):
    pass


class GuardedApplyRefused(WikiSyncError):
    pass


@dataclasses.dataclass(frozen=True)
class SourcePage:
    path: str
    title: str
    raw_content: str
    published_content: str


@dataclasses.dataclass(frozen=True)
class WikiPage:
    title: str
    content: str | None = None


@dataclasses.dataclass(frozen=True)
class PlannedAction:
    action: str
    title: str
    path: str | None = None
    reason: str | None = None
    request: dict[str, Any] | None = None


class ForgejoWikiClient:
    def __init__(self, base_url: str, token: str | None, repo: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.repo = repo.strip("/")
        self.timeout = timeout

    def _url(self, suffix: str) -> str:
        owner_repo = "/".join(urllib.parse.quote(part, safe="") for part in self.repo.split("/", 1))
        return f"{self.base_url}/api/v1/repos/{owner_repo}/wiki/{suffix.lstrip('/')}"

    def _request(self, method: str, suffix: str, payload: dict[str, Any] | None = None) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        if payload is not None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self._url(suffix), data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ForgejoHTTPError(method, self._url(suffix), exc.code, detail) from exc

    def list_pages(self) -> list[WikiPage]:
        try:
            data = self._request("GET", "pages")
        except ForgejoHTTPError as exc:
            if exc.status == 404:
                return []
            raise
        if not data:
            return []
        pages = []
        for item in data:
            title = item.get("title") or item.get("page_name") or item.get("name")
            if title:
                pages.append(WikiPage(title=str(title)))
        return pages

    def get_page(self, title: str) -> WikiPage | None:
        try:
            data = self._request("GET", f"page/{quote_page_title(title)}")
        except ForgejoHTTPError as exc:
            if exc.status == 404:
                return None
            raise
        if not data:
            return None
        content = decode_wiki_content(data)
        page_title = str(data.get("title") or data.get("page_name") or title)
        return WikiPage(title=page_title, content=content)

    def create_page(self, title: str, content: str, message: str) -> Any:
        return self._request("POST", "new", build_wiki_payload(title, content, message))

    def patch_page(self, title: str, content: str, message: str) -> Any:
        return self._request(
            "PATCH", f"page/{quote_page_title(title)}", build_wiki_payload(title, content, message)
        )

    def delete_page(self, title: str) -> Any:
        return self._request("DELETE", f"page/{quote_page_title(title)}")


class ForgejoHTTPError(WikiSyncError):
    def __init__(self, method: str, url: str, status: int, detail: str):
        super().__init__(f"{method} {url} failed with HTTP {status}: {detail}")
        self.method = method
        self.url = url
        self.status = status
        self.detail = detail


def quote_page_title(title: str) -> str:
    # Forgejo treats pageName as one path parameter; nested wiki page slashes
    # must be encoded instead of being sent as extra route segments.
    return urllib.parse.quote(title, safe="")


def run_git(repo_root: Path, args: list[str]) -> str:
    cmd = ["git", "-C", str(repo_root), *args]
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        raise WikiSyncError(f"git command failed: {' '.join(cmd)}\n{exc.stderr.strip()}") from exc


def path_to_page_title(path: str, docs_prefix: str = "docs/") -> str:
    normalized = path.replace("\\", "/")
    if not normalized.startswith(docs_prefix):
        raise ValueError(f"path is outside {docs_prefix}: {path}")
    if not normalized.endswith(".md"):
        raise ValueError(f"path is not markdown: {path}")
    rel = normalized[len(docs_prefix) : -3]
    if not rel or rel.startswith("/") or "//" in rel:
        raise ValueError(f"invalid docs markdown path: {path}")
    return rel


def source_commit(repo_root: Path, source_ref: str) -> str:
    return run_git(repo_root, ["rev-parse", source_ref]).strip()


def list_source_paths(repo_root: Path, source_ref: str, docs_glob: str) -> list[str]:
    prefix = docs_glob.split("**", 1)[0].rstrip("/") or "docs"
    output = run_git(repo_root, ["ls-tree", "-r", "--name-only", source_ref, prefix])
    paths = [line.strip() for line in output.splitlines() if line.strip()]
    if docs_glob == "docs/**/*.md":
        return sorted(path for path in paths if path.startswith("docs/") and path.endswith(".md"))
    return sorted(
        path for path in paths if fnmatch.fnmatch(path, docs_glob) and path.endswith(".md")
    )


def load_source_pages(
    repo_root: Path, source_ref: str, docs_glob: str, repo_name: str
) -> tuple[str, dict[str, SourcePage]]:
    commit = source_commit(repo_root, source_ref)
    pages: dict[str, SourcePage] = {}
    for path in list_source_paths(repo_root, source_ref, docs_glob):
        title = path_to_page_title(path)
        raw = run_git(repo_root, ["show", f"{source_ref}:{path}"])
        published = PROVENANCE_TEMPLATE.format(repo=repo_name, commit=commit, path=path) + raw
        pages[title] = SourcePage(
            path=path, title=title, raw_content=raw, published_content=published
        )
    return commit, pages


def decode_wiki_content(data: dict[str, Any]) -> str:
    if "content_base64" in data and data["content_base64"] is not None:
        return base64.b64decode(str(data["content_base64"])).decode("utf-8")
    if "content" in data and data["content"] is not None:
        content = data["content"]
        if isinstance(content, str):
            # Forgejo/Gitea wiki APIs commonly return plain content here.
            return content
    return ""


def build_wiki_payload(title: str, content: str, message: str) -> dict[str, str]:
    return {
        "title": title,
        "content_base64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "message": message,
    }


def action_request(
    action: str, page: SourcePage | None, title: str, commit: str, repo: str
) -> dict[str, Any] | None:
    if action == "delete":
        return {"method": "DELETE", "path": f"/api/v1/repos/{repo}/wiki/page/{title}"}
    if page is None:
        return None
    msg_action = "Create" if action == "create" else "Update"
    message = f"{msg_action} {title} from {repo}@{commit[:12]}"
    return build_wiki_payload(title, page.published_content, message)


def plan_sync(
    source_pages: dict[str, SourcePage], wiki_pages: dict[str, WikiPage], commit: str, repo: str
) -> list[PlannedAction]:
    actions: list[PlannedAction] = []
    for title in sorted(source_pages):
        source = source_pages[title]
        wiki = wiki_pages.get(title)
        if wiki is None:
            actions.append(
                PlannedAction(
                    "create",
                    title,
                    source.path,
                    "missing in wiki",
                    action_request("create", source, title, commit, repo),
                )
            )
        elif wiki.content != source.published_content:
            actions.append(
                PlannedAction(
                    "update",
                    title,
                    source.path,
                    "content differs",
                    action_request("update", source, title, commit, repo),
                )
            )
    for title in sorted(set(wiki_pages) - set(source_pages)):
        actions.append(
            PlannedAction(
                "delete",
                title,
                None,
                "absent from docs/**/*.md",
                action_request("delete", None, title, commit, repo),
            )
        )
    return actions


def collect_wiki_pages(client: ForgejoWikiClient) -> tuple[dict[str, WikiPage], dict[str, Any]]:
    listed = client.list_pages()
    state = {"listed_count": len(listed), "list_404_treated_as_empty": len(listed) == 0}
    pages: dict[str, WikiPage] = {}
    for page in listed:
        loaded = client.get_page(page.title)
        if loaded is not None:
            pages[page.title] = loaded
        else:
            pages[page.title] = page
    return pages, state


def validate_apply(args: argparse.Namespace) -> None:
    if args.apply_wiki and args.confirm_apply == APPLY_CONFIRMATION:
        return
    if args.apply_wiki or args.confirm_apply:
        raise GuardedApplyRefused(
            f"wiki writes require both --apply-wiki and --confirm-apply {APPLY_CONFIRMATION}"
        )


def apply_actions(client: ForgejoWikiClient, actions: list[PlannedAction]) -> list[dict[str, str]]:
    """Apply each exact action and require source-bound target readback.

    A successful HTTP mutation is not publication proof. Each write is fetched
    from the exact Wiki page path and compared byte-for-byte with the reviewed
    source content; deletion is accepted only after the exact page is absent.
    """
    proofs: list[dict[str, str]] = []
    for item in actions:
        if item.action in {"create", "update"}:
            if not item.request:
                raise WikiSyncError(f"missing request payload for {item.action} {item.title}")
            content = base64.b64decode(item.request["content_base64"]).decode("utf-8")
            message = item.request["message"]
            if item.action == "create":
                client.create_page(item.title, content, message)
            else:
                client.patch_page(item.title, content, message)
            observed = client.get_page(item.title)
            if observed is None or observed.title != item.title or observed.content != content:
                raise WikiSyncError(f"exact Wiki readback failed after {item.action}: {item.title}")
            proofs.append(
                {"action": item.action, "title": item.title, "result": "exact-content-readback"}
            )
        elif item.action == "delete":
            client.delete_page(item.title)
            if client.get_page(item.title) is not None:
                raise WikiSyncError(f"exact Wiki deletion readback failed: {item.title}")
            proofs.append(
                {"action": "delete", "title": item.title, "result": "exact-absence-readback"}
            )
        else:
            raise WikiSyncError(f"unknown action: {item.action}")
    return proofs


def manifest(
    actions: list[PlannedAction],
    commit: str,
    repo: str,
    source_count: int,
    wiki_count: int,
    mode: str,
    wiki_state: dict[str, Any],
    publication_proof: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "mode": mode,
        "repo": repo,
        "source_commit": commit,
        "source_pages": source_count,
        "wiki_pages": wiki_count,
        "wiki_state": wiki_state,
        "publication_proof": publication_proof,
        "counts": {
            "create": sum(1 for a in actions if a.action == "create"),
            "update": sum(1 for a in actions if a.action == "update"),
            "delete": sum(1 for a in actions if a.action == "delete"),
            "total": len(actions),
        },
        "actions": [dataclasses.asdict(a) for a in actions],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync reviewed repository docs/** Markdown to Forgejo Wiki"
    )
    parser.add_argument(
        "--repo-root", default=".", help="local git checkout containing docs/ (default: cwd)"
    )
    parser.add_argument("--repo", required=True, help="Forgejo owner/repo and provenance name")
    parser.add_argument(
        "--source-ref",
        required=True,
        help="reviewed source ref to publish (explicit reviewed ref)",
    )
    parser.add_argument(
        "--docs-glob", default="docs/**/*.md", help="source docs glob (default: docs/**/*.md)"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("FORGEJO_BASE_URL"),
        required=not os.environ.get("FORGEJO_BASE_URL"),
    )
    parser.add_argument(
        "--token-env", default="FORGEJO_TOKEN", help="environment variable containing Forgejo token"
    )
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument(
        "--manifest-json", action="store_true", help="emit JSON manifest instead of text"
    )
    parser.add_argument(
        "--apply-wiki", action="store_true", help="apply wiki writes; requires confirmation"
    )
    parser.add_argument(
        "--confirm-apply", default="", help="must equal sync-reviewed-docs-to-wiki for writes"
    )
    return parser


def render_text_manifest(data: dict[str, Any]) -> str:
    lines = [
        f"Mode: {data['mode']}",
        f"Repo: {data['repo']}",
        f"Source commit: {data['source_commit']}",
        f"Source pages: {data['source_pages']}",
        f"Wiki pages: {data['wiki_pages']}",
        "Plan: create={create} update={update} delete={delete} total={total}".format(
            **data["counts"]
        ),
    ]
    for action in data["actions"]:
        path = f" <- {action['path']}" if action.get("path") else ""
        lines.append(f"- {action['action']}: {action['title']}{path} ({action['reason']})")
    if not data["actions"]:
        lines.append("- no changes")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_apply(args)
        repo_root = Path(args.repo_root).resolve()
        token = os.environ.get(args.token_env)
        commit, source_pages = load_source_pages(
            repo_root, args.source_ref, args.docs_glob, args.repo
        )
        client = ForgejoWikiClient(args.base_url, token, args.repo, args.timeout)
        wiki_pages, wiki_state = collect_wiki_pages(client)
        actions = plan_sync(source_pages, wiki_pages, commit, args.repo)
        publication_proof: list[dict[str, str]] = []
        if args.apply_wiki:
            publication_proof = apply_actions(client, actions)
        data = manifest(
            actions,
            commit,
            args.repo,
            len(source_pages),
            len(wiki_pages),
            "apply" if args.apply_wiki else "dry-run",
            wiki_state,
            publication_proof,
        )
        print(
            json.dumps(data, indent=2, sort_keys=True)
            if args.manifest_json
            else render_text_manifest(data)
        )
        return 0
    except GuardedApplyRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except WikiSyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
