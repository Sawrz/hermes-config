#!/usr/bin/env python3
"""Read-only repository documentation inventory and mechanical-signal helper.

The helper intentionally does not talk to Forgejo, does not create cards, and does
not write into the repository. It inventories repo documentation, classifies the
Forgejo Wiki source namespace (`docs/**/*.md`), scans relevant inline comments,
and emits deterministic JSON plus a short human-readable report.

It does not decide semantic documentation correctness, completeness, staleness,
wrong ports, wrong routes, wrong services, missing sections, or whether docs
match code intent. Linus/the monthly docs-review agent performs that audit using
this inventory plus repo inspection.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

INLINE_SUFFIXES = {".nix", ".sh", ".bash", ".py", ".service", ".timer"}
SKIP_DIRS = {
    ".git",
    ".worktrees",
    ".direnv",
    ".devenv",
    "result",
    "result-1",
    "node_modules",
    "__pycache__",
}
PLACEHOLDER_RE = re.compile(
    r"\b(TODO|FIXME|XXX|replace this|template text|placeholder|TBD)\b", re.IGNORECASE
)
STALE_COMMENT_RE = re.compile(
    r"\b(TODO|FIXME|XXX|stale|old|legacy|deprecated|temporary|remove after)\b", re.IGNORECASE
)
SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key)\b\s*[:=]\s*([^\s`'\"]+|['\"][^'\"]+['\"])",
)
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HOST_RE = re.compile(r"hosts/(nixos|darwin|generic-linux)/([A-Za-z0-9_-]+)")
PORT_RE = re.compile(
    r"(?<![\w.])(?::|port\s*=\s*|port\s+)([1-9][0-9]{1,4})(?![\w.])", re.IGNORECASE
)
OPTION_RE = re.compile(r"`(custom\.[A-Za-z0-9_.-]+)`")
BACKTICK_RE = re.compile(r"`([^`]+)`")
REPO_PATH_HINT_RE = re.compile(
    r"^(README\.md|docs/|hosts/|modules/|lib/|flake-parts/|pkgs/|home/|scripts/|references/).+"
)


@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    severity: str
    message: str
    line: int | None = None
    target: str | None = None
    wiki_source: bool = False

    def as_dict(self) -> dict:
        result = {
            "kind": self.kind,
            "path": self.path,
            "severity": self.severity,
            "message": self.message,
            "wiki_source": self.wiki_source,
        }
        if self.line is not None:
            result["line"] = self.line
        if self.target is not None:
            result["target"] = self.target
        return result


def relpath(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            yield path


def discover_markdown(root: Path) -> list[Path]:
    """Discover all repository Markdown for inventory/mechanical review signals.

    Only ``docs/**/*.md`` is Forgejo Wiki source. Other Markdown files are still
    documentation and must be included as audit-only inventory unless excluded by
    ``SKIP_DIRS``.
    """
    return sorted(
        (p for p in iter_files(root) if p.suffix.lower() == ".md"), key=lambda p: relpath(p, root)
    )


def is_wiki_source(relative: str) -> bool:
    return relative.startswith("docs/") and relative.endswith(".md")


def wiki_page(relative: str) -> str | None:
    if not is_wiki_source(relative):
        return None
    return relative.removeprefix("docs/").removesuffix(".md")


def doc_scope(relative: str) -> str:
    if relative == "README.md":
        return "root-readme"
    if is_wiki_source(relative):
        return "wiki-source"
    if relative.startswith("modules/services/nixos/containerization/stacks/") and relative.endswith(
        "/README.md"
    ):
        return "stack-readme"
    if relative.startswith("modules/") and relative.endswith("/README.md"):
        return "module-readme"
    if relative.startswith("hosts/") and relative.endswith("/README.md"):
        return "host-readme"
    return "audit-only-doc"


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def anchor_exists(text: str, anchor: str) -> bool:
    wanted = anchor.lower().strip()
    if not wanted:
        return True
    headings = []
    for line in text.splitlines():
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            slug = re.sub(r"[^a-z0-9\s-]", "", title)
            slug = re.sub(r"\s+", "-", slug).strip("-")
            headings.append(slug)
    return wanted in headings


def check_links(root: Path, path: Path, text: str, relative: str, wiki: bool) -> list[Finding]:
    findings: list[Finding] = []
    for match in MARKDOWN_LINK_RE.finditer(text):
        raw = match.group(1).strip()
        if not raw or raw.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target_part, _, anchor = raw.partition("#")
        if not target_part:
            continue
        target = (path.parent / target_part).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            findings.append(
                Finding(
                    kind="broken_link",
                    path=relative,
                    severity="error",
                    line=line_number(text, match.start()),
                    target=raw,
                    message="Markdown link points outside the repository",
                    wiki_source=wiki,
                )
            )
            continue
        if not target.exists():
            findings.append(
                Finding(
                    kind="broken_link",
                    path=relative,
                    severity="error",
                    line=line_number(text, match.start()),
                    target=raw,
                    message="Markdown link target does not exist",
                    wiki_source=wiki,
                )
            )
            continue
        if anchor and target.is_file() and target.suffix.lower() == ".md":
            target_text = read_text(target)
            if not anchor_exists(target_text, anchor):
                findings.append(
                    Finding(
                        kind="broken_anchor",
                        path=relative,
                        severity="warning",
                        line=line_number(text, match.start()),
                        target=raw,
                        message="Markdown link anchor was not found in target file",
                        wiki_source=wiki,
                    )
                )
    return findings


def check_markdown(
    root: Path, path: Path, known_options: set[str], known_hosts: set[str]
) -> tuple[dict, list[Finding]]:
    relative = relpath(path, root)
    text = read_text(path)
    wiki = is_wiki_source(relative)
    lines = text.splitlines()
    title = next((line.strip("# ").strip() for line in lines if line.startswith("#")), None)
    doc = {
        "path": relative,
        "scope": doc_scope(relative),
        "wiki_source": wiki,
        "wiki_page": wiki_page(relative),
        "title": title,
        "lines": len(lines),
    }
    findings = check_links(root, path, text, relative, wiki)
    if not title:
        findings.append(
            Finding(
                "missing_title",
                relative,
                "warning",
                "Markdown file has no top-level heading",
                wiki_source=wiki,
            )
        )
    for match in PLACEHOLDER_RE.finditer(text):
        findings.append(
            Finding(
                "placeholder_text",
                relative,
                "warning",
                "Placeholder/template wording should be replaced or removed",
                line=line_number(text, match.start()),
                wiki_source=wiki,
            )
        )
        break
    for match in SECRET_RE.finditer(text):
        key_name = match.group(1).lower()
        findings.append(
            Finding(
                "secret_assignment_review_signal",
                relative,
                "warning",
                f"Secret-like assignment pattern in documentation ({key_name}); value redacted for human review",
                line=line_number(text, match.start()),
                wiki_source=wiki,
            )
        )
        break
    for match in BACKTICK_RE.finditer(text):
        ref = match.group(1).strip()
        if "\n" in ref or len(ref) > 240:
            continue
        if REPO_PATH_HINT_RE.match(ref):
            target = root / ref
            try:
                exists = target.exists()
            except OSError:
                exists = False
            if not exists:
                findings.append(
                    Finding(
                        "unresolved_path_reference",
                        relative,
                        "warning",
                        "Backtick path-like reference does not resolve in the repository",
                        line=line_number(text, match.start()),
                        target=ref,
                        wiki_source=wiki,
                    )
                )
                host_match = HOST_RE.search(ref)
                if host_match and host_match.group(2) not in known_hosts:
                    findings.append(
                        Finding(
                            "unresolved_host_path_reference",
                            relative,
                            "warning",
                            "Host path-like reference names a host not present in repo paths",
                            line=line_number(text, match.start()),
                            target=host_match.group(2),
                            wiki_source=wiki,
                        )
                    )
        option_match = OPTION_RE.fullmatch(f"`{ref}`")
        if option_match and option_match.group(1) not in known_options:
            findings.append(
                Finding(
                    "unresolved_option_reference",
                    relative,
                    "warning",
                    "Backtick custom option reference was not found in Nix source",
                    line=line_number(text, match.start()),
                    target=option_match.group(1),
                    wiki_source=wiki,
                )
            )
    return doc, findings


def scan_inline_comments(root: Path) -> tuple[list[dict], list[Finding]]:
    comments: list[dict] = []
    findings: list[Finding] = []
    for path in iter_files(root):
        if path.suffix not in INLINE_SUFFIXES:
            continue
        relative = relpath(path, root)
        text = read_text(path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            is_comment = (
                stripped.startswith("#")
                or stripped.startswith("//")
                or stripped.startswith("/*")
                or " #" in line
            )
            if not is_comment:
                continue
            if any(
                marker in stripped.lower()
                for marker in (
                    "todo",
                    "fixme",
                    "stale",
                    "legacy",
                    "deprecated",
                    "temporary",
                    "remove after",
                )
            ):
                comments.append({"path": relative, "line": lineno, "class": "review-marker"})
                findings.append(
                    Finding(
                        "inline_comment_review_candidate",
                        relative,
                        "warning",
                        "Inline comment contains TODO/FIXME/legacy/temporary marker for human docs review",
                        line=lineno,
                    )
                )
            elif relative.endswith(".nix") and (
                "port" in stripped.lower()
                or "route" in stripped.lower()
                or "host" in stripped.lower()
            ):
                comments.append({"path": relative, "line": lineno, "class": "ops-context"})
    return comments, findings


def discover_repo_facts(root: Path) -> dict:
    hosts = sorted(
        {m.group(2) for p in iter_files(root) for m in HOST_RE.finditer(relpath(p, root))}
    )
    markdown_ports: set[int] = set()
    option_refs: set[str] = set()
    known_options = discover_known_options(root)
    for doc in discover_markdown(root):
        text = read_text(doc)
        markdown_ports.update(
            int(m.group(1)) for m in PORT_RE.finditer(text) if int(m.group(1)) <= 65535
        )
        option_refs.update(m.group(1) for m in OPTION_RE.finditer(text))
    return {
        "hosts_referenced_by_path": hosts,
        "ports_referenced_in_docs": sorted(markdown_ports),
        "custom_options_referenced_in_docs": sorted(option_refs),
        "custom_options_seen_in_source_count": len(known_options),
    }


def discover_known_hosts(root: Path) -> set[str]:
    hosts: set[str] = set()
    for family in ("nixos", "darwin", "generic-linux"):
        base = root / "hosts" / family
        if base.exists():
            hosts.update(path.name for path in base.iterdir() if path.is_dir())
    return hosts


def discover_known_options(root: Path) -> set[str]:
    options: set[str] = set()
    for path in iter_files(root):
        if path.suffix != ".nix":
            continue
        text = read_text(path)
        options.update(re.findall(r"\bcustom\.[A-Za-z0-9_.-]+", text))
    return options


def build_report(data: dict) -> str:
    lines = ["# Repository docs inventory", ""]
    summary = data["summary"]
    lines.append(
        f"Documents: {summary['documents']} ({summary['wiki_source_documents']} wiki-source, {summary['audit_only_documents']} audit-only); "
        f"inline comments flagged: {summary['inline_comments_flagged']}; findings: {summary['findings']}"
    )
    lines.append("")
    by_kind: dict[str, int] = {}
    for finding in data["findings"]:
        by_kind[finding["kind"]] = by_kind.get(finding["kind"], 0) + 1
    if by_kind:
        lines.append("Findings by kind:")
        for kind in sorted(by_kind):
            lines.append(f"- {kind}: {by_kind[kind]}")
        lines.append("")
        lines.append("Top findings:")
        for finding in data["findings"][:25]:
            location = finding["path"]
            if "line" in finding:
                location += f":{finding['line']}"
            wiki = " wiki" if finding.get("wiki_source") else " audit"
            lines.append(
                f"- [{finding['severity']}] {finding['kind']} ({wiki}) {location}: {finding['message']}"
            )
    else:
        lines.append("No findings.")
    return "\n".join(lines) + "\n"


def inventory(repo: Path) -> dict:
    root = repo.resolve()
    documents: list[dict] = []
    findings: list[Finding] = []
    known_hosts = discover_known_hosts(root)
    known_options = discover_known_options(root)
    for path in discover_markdown(root):
        doc, doc_findings = check_markdown(root, path, known_options, known_hosts)
        documents.append(doc)
        findings.extend(doc_findings)
    inline_comments, inline_findings = scan_inline_comments(root)
    findings.extend(inline_findings)
    finding_dicts = [
        f.as_dict()
        for f in sorted(findings, key=lambda f: (f.path, f.line or 0, f.kind, f.message))
    ]
    wiki_count = sum(1 for doc in documents if doc["wiki_source"])
    data = {
        "schema_version": 1,
        "repo": str(root),
        "mode": "dry-run-read-only",
        "documents": documents,
        "inline_comments": inline_comments,
        "repo_facts": discover_repo_facts(root),
        "findings": finding_dicts,
        "summary": {
            "documents": len(documents),
            "wiki_source_documents": wiki_count,
            "audit_only_documents": len(documents) - wiki_count,
            "inline_comments_flagged": len(inline_comments),
            "findings": len(finding_dicts),
        },
    }
    data["report"] = build_report(data)
    return data


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only repository documentation inventory/mechanical-signal helper"
    )
    parser.add_argument(
        "--repo", default=".", help="Repository root to scan (default: current directory)"
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.add_argument("--report", action="store_true", help="Emit readable report text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    repo = Path(args.repo)
    if not repo.exists() or not repo.is_dir():
        print(f"error: repo path does not exist or is not a directory: {repo}", file=sys.stderr)
        return 2
    data = inventory(repo)
    if args.report and not args.json:
        print(data["report"], end="")
    else:
        print(json.dumps(data, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
