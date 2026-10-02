#!/usr/bin/env python3
"""Prepare a repository-qualified Paperclip review handoff without dispatching it."""

import argparse
import hashlib
import json
from pathlib import Path
import re

from hermes_repository_instance import load_instance
from hermes_workflow_state import ProtocolError


def review_handoff(directory: Path, repository: str, number: int, head: str) -> dict:
    if not directory.is_absolute() or not directory.is_dir():
        raise ProtocolError("repository registry must be an existing absolute directory")
    contracts = [load_instance(path) for path in sorted(directory.glob("*.json"))]
    if not contracts:
        raise ProtocolError("repository registry is empty")
    for field in ("identity", "repository", "namespace"):
        if len({getattr(contract, field) for contract in contracts}) != len(contracts):
            raise ProtocolError("repository registry contains colliding " + field)
    matches = [contract for contract in contracts if contract.repository == repository]
    if len(matches) != 1:
        raise ProtocolError("requested repository has no unique declared authority")
    contract = matches[0]
    if contract.review_executor != "paperclip":
        raise ProtocolError("repository reviews are not assigned to Paperclip")
    if type(number) is not int or number <= 0 or re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise ProtocolError("review requires a positive PR number and exact head commit")
    if not contract.forgejo_origin:
        raise ProtocolError("review requires the authoritative Forgejo origin")
    url = f"{contract.forgejo_origin}/{repository}/pulls/{number}"
    identity = json.dumps([contract.identity, repository, number, head], separators=(",", ":"))
    return {
        "executor": "paperclip",
        "action": "handoff",
        "kind": "review",
        "repository": repository,
        "repository_instance": contract.identity,
        "namespace": contract.namespace,
        "pull_request": number,
        "head_revision": head,
        "url": url,
        "idempotency_key": "repository-review-" + hashlib.sha256(identity.encode()).hexdigest(),
        "title": f"Review {repository}#{number} at {head[:12]}",
        "description": (
            f"Review {url} at exact head {head}. Verify repository and head before submitting "
            "the review; report a changed head to the caller. Use a Paperclip review task agent "
            "with its own repository credentials. Preserve the repository, PR and head in the "
            "result. This handoff does not authorize merge or deployment."
        ),
        "dispatched": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    try:
        result = review_handoff(args.registry, args.repository, args.pr, args.head)
    except (ProtocolError, ValueError, OSError) as error:
        parser.exit(2, f"Cannot route review: {error}\n")
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
