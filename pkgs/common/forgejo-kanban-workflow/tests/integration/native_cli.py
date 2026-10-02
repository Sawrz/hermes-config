#!/usr/bin/env python3
"""Isolated test launcher for the *unmodified* pinned Hermes CLI.

Only used by integration tests with a temporary HERMES_HOME. No model/provider
configuration is loaded; real CLI parsing and native board operations run.
"""

import argparse
import os
import sys
from pathlib import Path


def main():
    home = Path(os.environ["HERMES_HOME"])
    if not (home / "isolated-test-home").is_file():
        raise SystemExit("refusing non-test Hermes home")
    sys.path.insert(0, os.environ["HERMES_TEST_SOURCE"])
    from hermes_cli.kanban import build_parser, kanban_command

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    return kanban_command(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
