"""Characterize human waits on the pinned, unmodified native lifecycle.

Real temporary DB, claims, block/unblock, dependencies and gateway watcher;
only auxiliary model output and OS worker spawning are fixtures. Every watcher
probe runs in a new process. A passing limitation test proves the hazard exists,
NOT that repeated human waits are protected. No production cards or model calls.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from test_native_intake import NativeFixture


def watcher_probe(ticks):
    import logging

    logging.basicConfig(level=logging.WARNING)
    sys.path.insert(0, os.environ["HERMES_TEST_SOURCE"])
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from hermes_cli import kanban_db_dispatch as dispatch, kanban_decompose as decompose

    calls = {"auxiliary_calls": 0, "spawned": []}

    def model(*args, **kwargs):
        calls["auxiliary_calls"] += 1
        return json.dumps(
            {
                "fanout": False,
                "title": "Same bounded fixture",
                "body": "Continue the bounded fixture assignment",
                "assignee": "default",
            }
        ), ""

    def spawn(task, workspace, board=None):
        calls["spawned"].append(task.id)
        return None

    class Gateway(GatewayKanbanWatchersMixin):
        def __init__(self):
            self._running = True
            self.seen = 0

        async def _sleep_between_ticks(self, interval):
            self.seen += 1
            if self.seen >= ticks:
                self._running = False

    gateway = Gateway()
    with (
        patch.object(decompose, "_call_aux", side_effect=model),
        patch.object(dispatch, "_default_spawn", side_effect=spawn),
    ):
        asyncio.run(gateway._kanban_dispatcher_watcher())
    return {**calls, "ticks": gateway.seen}


class NativeHumanWaitTests(NativeFixture):
    def setUp(self):
        super().setUp()
        from hermes_cli import kanban_db

        self.db = kanban_db
        (self.home / "config.yaml").write_text(
            "kanban:\n  dispatch_in_gateway: true\n  auto_decompose: true\n"
            "  auto_subscribe_on_create: false\n  max_in_progress: 2\n"
        )

    def tick_after_restart(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--watcher-probe"],
            env={
                **{
                    key: os.environ[key]
                    for key in (
                        "PATH",
                        "PYTHONPATH",
                        "HERMES_TEST_SOURCE",
                        "HERMES_HOME",
                        "HERMES_KANBAN_BOARD",
                        "HERMES_KANBAN_DB",
                        "HERMES_REPOSITORY_INSTANCE",
                    )
                },
                "HOME": str(self.home),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("ERROR:", result.stderr, result.stdout + result.stderr)
        results = [
            line.split("=", 1)[1]
            for line in result.stdout.splitlines()
            if line.startswith("HUMAN_WAIT_TICK=")
        ]
        self.assertEqual(len(results), 1, result.stdout + result.stderr)
        self.probe_diagnostics = result.stdout + result.stderr
        return json.loads(results[0])

    def assert_held_after_restart(self):
        for _ in range(2):
            self.assertEqual(
                self.tick_after_restart(),
                {
                    "auxiliary_calls": 0,
                    "spawned": [],
                    "ticks": 3,
                },
            )

    def test_real_prerequisite_then_implementation_each_hold_first_question(self):
        with self.kbc.connect_closing(board=self.board) as conn:
            plan = self.db.create_task(conn, title="Freeze shared contract", assignee="default")
            work = self.db.create_task(
                conn,
                title="Implement accepted contract",
                assignee="default",
                parents=[plan],
            )
            self.db.recompute_ready(conn)
            self.assertIsNotNone(self.db.claim_task(conn, plan))
            self.assertTrue(
                self.db.block_task(conn, plan, kind="needs_input", reason="Choose interface")
            )
            self.assertEqual(self.db.get_task(conn, plan).status, "blocked")
            self.assertEqual(self.db.get_task(conn, work).status, "todo")
            self.assertIsNone(self.db.claim_task(conn, work))
        self.assert_held_after_restart()
        with self.kbc.connect_closing(board=self.board) as conn:
            # Explicit fixture answer; completing real design work is not a reset.
            self.assertTrue(self.db.unblock_task(conn, plan))
            self.assertIsNotNone(self.db.claim_task(conn, plan))
            self.assertTrue(
                self.db.complete_task(
                    conn,
                    plan,
                    summary="Interface chosen and contract frozen",
                    fire_lifecycle_hook=False,
                )
            )
            self.db.recompute_ready(conn)
            self.assertIsNotNone(self.db.claim_task(conn, work))
            self.assertTrue(
                self.db.block_task(conn, work, kind="needs_input", reason="Supply app secret")
            )
            self.assertEqual(self.db.get_task(conn, work).status, "blocked")
        self.assert_held_after_restart()

    def test_documented_limit_second_different_question_can_restart_without_answer(self):
        with self.kbc.connect_closing(board=self.board) as conn:
            card = self.db.create_task(conn, title="One execution phase", assignee="default")
            self.db.recompute_ready(conn)
            self.assertIsNotNone(self.db.claim_task(conn, card))
            self.assertTrue(
                self.db.block_task(conn, card, kind="needs_input", reason="Authorize profile")
            )
            self.assertEqual(self.db.get_task(conn, card).status, "blocked")
        self.assert_held_after_restart()
        with self.kbc.connect_closing(board=self.board) as conn:
            self.assertTrue(self.db.unblock_task(conn, card))
            self.assertEqual(self.db.get_task(conn, card).block_recurrences, 1)
            self.assertIsNotNone(self.db.claim_task(conn, card))
            self.assertTrue(
                self.db.block_task(
                    conn, card, kind="needs_input", reason="Choose activation contract"
                )
            )
            state = self.db.get_task(conn, card)
            self.assertEqual((state.status, state.block_recurrences), ("triage", 2))
        # No second answer/unblock: native auto-decomposition can release this card.
        self.assertEqual(
            self.tick_after_restart(),
            {
                "auxiliary_calls": 1,
                "spawned": [card],
                "ticks": 3,
            },
            self.probe_diagnostics,
        )
        with self.kbc.connect_closing(board=self.board) as conn:
            self.assertEqual(self.db.get_task(conn, card).status, "running")
            self.assertEqual(len(self.db.list_tasks(conn)), 1)


if __name__ == "__main__" and sys.argv[1:] == ["--watcher-probe"]:
    print("HUMAN_WAIT_TICK=" + json.dumps(watcher_probe(3)))
