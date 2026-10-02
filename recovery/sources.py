#!/usr/bin/env python3
"""Fetch exact locked sources before flake evaluation. No branch-head fallback."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import tempfile
import subprocess
import sys

TIMEOUT = 45
# Cold Git trees (notably Hermes) can take longer than metadata probes.
FETCH_TIMEOUT = 180
# Only availability failures qualify. Trust, authorization, identity and absent
# revisions are terminal, including when a mirror is reachable but stale.
DENIED = re.compile(
    r"permission denied|publickey|host key|host-key|authentication|unauthorized|forbidden|hash mismatch|NAR hash mismatch|integrity|couldn't find remote ref|not our ref|repository not found",
    re.I,
)
UNAVAILABLE = re.compile(
    r"timed? out|timeout|connection refused|connection reset|could not resolve|temporary failure in name resolution|network is unreachable|no route to host|HTTP (?:error )?(?:502|503|504)|unable to connect",
    re.I,
)


class FetchError(RuntimeError):
    def __init__(self, message, availability=False):
        super().__init__(message)
        self.availability = availability


def run(command, timeout=TIMEOUT):
    env = dict(os.environ, LC_ALL="C", GIT_TERMINAL_PROMPT="0")
    env.setdefault(
        "GIT_SSH_COMMAND",
        "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o ConnectionAttempts=1",
    )
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    ) as process:
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise FetchError("source transport exceeded bounded wait", availability=True)
        if process.returncode:
            raise FetchError(
                err.strip(), availability=not DENIED.search(err) and bool(UNAVAILABLE.search(err))
            )
        return out


def nix_string(value):
    return json.dumps(value).replace("${", "\\${")


def fetch(locked, offline=False):
    if offline:
        if not locked.get("narHash"):
            raise FetchError("locked source has no content hash")
        path = run(
            [
                "nix-store",
                "--print-fixed-path",
                "--recursive",
                "sha256",
                locked["narHash"],
                "source",
            ]
        ).strip()
        try:
            run(["nix-store", "--check-validity", path])
        except FetchError:
            raise FetchError("source is absent from the local store")
        return {**locked, "path": path}
    expression = (
        "let source = builtins.fetchTree (builtins.fromJSON "
        + nix_string(json.dumps(locked))
        + '); in (builtins.removeAttrs source [ "outPath" ]) // { path = source.outPath; }'
    )
    args = [
        "nix",
        "--extra-experimental-features",
        "nix-command flakes",
        "eval",
        "--impure",
        "--json",
        "--expr",
        expression,
    ]
    result = json.loads(run(args, timeout=FETCH_TIMEOUT))
    for key in ("narHash", "rev", "lastModified", "revCount"):
        if key in locked and result.get(key) != locked[key]:
            raise FetchError(f"source identity mismatch for {key}")
    return result


def transport(locked, url):
    # Preserve source-content and commit metadata; flags which change source
    # content must survive both transports. A GitHub archive and Git tree must
    # match the locked NAR, otherwise recovery fails.
    result = {k: v for k, v in locked.items() if k not in ("type", "owner", "repo", "url", "ref")}
    result.update(type="git", url=url)
    # With shallow fetches, allRefs fetches branch tips instead of the exact
    # revision, leaving older locked commits absent. Fetch the SHA directly.
    if result.get("shallow"):
        result.pop("allRefs", None)
    else:
        result["allRefs"] = True
    return result


def source_pair(locked, policy):
    key = locked.get("url")
    if locked.get("type") == "github":
        key = "github:" + locked["owner"] + "/" + locked["repo"]
    pair = policy["sources"].get(key)
    if pair is None:
        return None
    return transport(locked, pair["primary"]), (
        locked if pair.get("recovery") == "locked" else transport(locked, pair["recovery"])
    )


def required_nodes(lock, policy, target):
    """Resolve an operation's declared roots, including transitive follows edges."""
    scopes = policy.get("operation_inputs", {})
    matches = [key for key in scopes if target == key or target.startswith(key + ".")]
    if not matches:
        raise FetchError(f"no source scope declared for operation: {target}")
    selected = set(scopes[max(matches, key=len)])
    projects = set(policy["required_repositories"]) - {"deployment"}
    roots = lock["nodes"][lock["root"]].get("inputs", {})
    missing = selected - set(roots)
    if missing:
        raise FetchError("operation requires missing locked inputs: " + ", ".join(sorted(missing)))
    # Shared upstream input roots remain in the lock graph. Private project roots
    # enter recovery only when this operation selects them.
    roots_to_walk = (set(roots) - projects) | selected
    visited = set()

    def resolve(edge, resolving=()):
        if isinstance(edge, str):
            return edge
        path = tuple(edge)
        if path in resolving:
            raise FetchError("cyclic follows path in source lock")
        node = lock["root"]
        for part in path:
            node = resolve(lock["nodes"][node]["inputs"][part], (*resolving, path))
        return node

    def walk(node):
        if node in visited:
            return
        visited.add(node)
        for edge in lock["nodes"][node].get("inputs", {}).values():
            walk(resolve(edge))

    for name in sorted(roots_to_walk):
        walk(resolve(roots[name]))
    return visited


def recover_lock(
    lock, policy, fetcher=fetch, log=lambda text: print(text, file=sys.stderr), *, nodes
):
    result = copy.deepcopy(lock)
    for name, node in result["nodes"].items():
        if name not in nodes:
            continue
        locked = node.get("locked")
        if locked is None:
            continue
        pair = source_pair(locked, policy)
        if pair is None:
            continue
        if not locked.get("rev") or not locked.get("narHash"):
            raise FetchError(f"{name}: recovery requires a pinned revision and NAR hash")
        try:
            fetcher(locked, offline=True)
            continue
        except FetchError as error:
            if DENIED.search(str(error)) or "identity mismatch" in str(error):
                raise
        primary, recovery = pair
        try:
            fetcher(primary)
            node["locked"] = primary
        except FetchError as error:
            if not error.availability:
                raise
            log(f"RECOVERY {name}: primary unavailable; requesting exact {locked['rev']}")
            fetcher(recovery)
            node["locked"] = recovery
    return result


def updates_available(policy, runner=run):
    # Updates always consult authority, even with a valid cached pinned source.
    for url in sorted({v["primary"] for v in policy["sources"].values()}):
        runner(["git", "ls-remote", "--exit-code", url, "HEAD"])


def tracked_identity(root):
    names = sorted(
        set(filter(None, run(["git", "-C", str(root), "ls-files", "--cached", "-z"]).split("\0")))
    )
    digest = hashlib.sha256()
    for name in names:
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise FetchError("source entry escapes checkout")
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise FetchError("source entry traverses a symlink")
        digest.update(name.encode() + b"\0")
        digest.update(str(path.lstat().st_mode & 0o777).encode() + b"\0")
        digest.update(
            ("link:" + os.readlink(path)).encode() if path.is_symlink() else path.read_bytes()
        )
        digest.update(b"\0")
    return names, digest.hexdigest()


def copy_snapshot(root, target):
    root = root.resolve()
    names, before = tracked_identity(root)
    for name in names:
        src, dst = root / name, target / name
        if not src.exists() and not src.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            resolved, link = src.resolve(), os.readlink(src)
            if not resolved.is_relative_to(root) and not resolved.is_relative_to(
                Path("/nix/store")
            ):
                raise FetchError("source symlink points outside the checkout and Nix store")
            if resolved.is_relative_to(root) and Path(link).is_absolute():
                link = os.path.relpath(target / resolved.relative_to(root), dst.parent)
            dst.symlink_to(link)
        else:
            shutil.copy2(src, dst)
    if tracked_identity(root) != (names, before):
        raise FetchError("source changed while preparing snapshot; retry")
    return before


def write_source_identity(root, target, revision=None):
    root = root.resolve()
    policy = json.loads((root / "recovery/sources.json").read_text())
    source_url = policy["required_repositories"]["deployment"]
    base = ["git", "-C", str(root)]
    if revision is None and run([*base, "status", "--porcelain", "--untracked-files=no"]).strip():
        identity = {
            "type": "git",
            "url": source_url,
            "rev": None,
            "narHash": None,
            "lastModified": None,
            "sourceTreeHash": tracked_identity(root)[1],
        }
    else:
        revision = revision or run([*base, "rev-parse", "HEAD"]).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise FetchError("source identity requires an exact commit")
        fetched = fetch({"type": "git", "url": root.as_uri(), "rev": revision})
        identity = {
            "type": "git",
            "url": source_url,
            "rev": revision,
            "narHash": fetched["narHash"],
            "lastModified": fetched["lastModified"],
        }
    destination = target / "recovery/build-source.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(identity, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=["prepare", "update-check", "run", "fetch-root", "sync"]
    )
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--branch", default="master")
    parser.add_argument("--identity-source", type=Path)
    parser.add_argument("--revision")
    parser.add_argument(
        "--target", help="Flake attribute or declared operation whose sources are needed"
    )
    args, command = parser.parse_known_args()
    args.command = command
    policy_path = args.policy or args.source / "recovery/sources.json"
    policy = json.loads(policy_path.read_text())
    if args.operation == "sync":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", args.branch) or ".." in args.branch:
            raise FetchError("invalid deployment branch")
        base = ["git", "-C", str(args.source)]
        if (
            run([*base, "status", "--porcelain"]).strip()
            or run([*base, "branch", "--show-current"]).strip() != args.branch
        ):
            raise FetchError("scheduled synchronization requires a clean deployment branch")
        try:
            run(
                [
                    *base,
                    "fetch",
                    "origin",
                    f"refs/heads/{args.branch}:refs/remotes/origin/{args.branch}",
                ]
            )
        except FetchError as error:
            if not error.availability:
                raise
            print(
                "Primary unavailable; rebuild uses the existing pinned deployment revision; update deferred",
                file=sys.stderr,
            )
        else:
            run([*base, "merge", "--ff-only", "origin/" + args.branch])
        print(run([*base, "rev-parse", "HEAD"]).strip())
        return
    if args.operation == "fetch-root":
        if args.identity is None:
            raise FetchError("fetch-root requires the independent release manifest")
        identity = json.loads(args.identity.read_text())["root"]
        lock = {"nodes": {"deployment": {"locked": identity}}}
        prepared = recover_lock(lock, policy, nodes={"deployment"})
        locked = prepared["nodes"]["deployment"]["locked"]
        try:
            result = fetch(locked, offline=True)
        except FetchError:
            result = fetch(locked)
        print(result["path"])
        return
    if args.operation == "update-check":
        updates_available(policy)
        return
    if args.target is None:
        raise FetchError("source preparation requires --target to select the operation's inputs")
    if args.operation == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            raise FetchError("run requires a command with {flake} as its source")
        with tempfile.TemporaryDirectory(prefix="pinned-nix-source-") as directory:
            target = Path(directory)
            identity = copy_snapshot(args.source, target)
            write_source_identity(args.source, target)
            if tracked_identity(args.source.resolve())[1] != identity:
                raise FetchError("source changed while recording identity")
            print(f"Build source identity: {identity}", file=sys.stderr)
            path = target / "flake.lock"
            lock = json.loads(path.read_text())
            path.write_text(
                json.dumps(
                    recover_lock(lock, policy, nodes=required_nodes(lock, policy, args.target)),
                    indent=2,
                )
                + "\n"
            )
            # Source preparation is over. Evaluation/build/activation failures are
            # returned directly and can never trigger source fallback.
            result = subprocess.run(
                [part.replace("{flake}", "path:" + str(target)) for part in command]
            )
            sys.exit(result.returncode)
    # Only call on a private immutable build snapshot, never the checkout.
    if (args.source / ".git").exists():
        raise FetchError("prepare requires a build snapshot, not a Git checkout")
    if args.identity_source is not None:
        write_source_identity(args.identity_source, args.source, args.revision)
    path = args.source / "flake.lock"
    lock = json.loads(path.read_text())
    prepared = recover_lock(lock, policy, nodes=required_nodes(lock, policy, args.target))
    if prepared != lock:
        tmp = path.with_suffix(".lock.prepared")
        tmp.write_text(json.dumps(prepared, indent=2) + "\n")
        tmp.replace(path)


if __name__ == "__main__":
    try:
        main()
    except (FetchError, OSError, ValueError) as error:
        print(f"source recovery failed: {error}", file=sys.stderr)
        sys.exit(1)
