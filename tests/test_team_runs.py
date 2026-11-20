import copy
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from bridge import team_runs


class RunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "private" / "runs.sqlite3"
        self.store = team_runs.RunStore(self.db)

    def start(self, scenario="success", key="first", brief="合成示例"):
        return self.store.start("card", key, scenario, brief, "codex")

    def answer(self, run, action, value=""):
        return self.store.answer(run["id"], run["help"]["id"], action, value)

    def test_empty_history_is_read_only_and_existing_run_replays_once(self):
        self.store.recover()
        self.assertEqual(self.store.list_runs("card"), [])
        self.assertFalse(self.db.parent.exists())
        run = self.start()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["cloud_calls"], 0)
        self.assertEqual([(a["node"], a["number"]) for a in run["attempts"]],
                         [("lead", 1), ("opinion", 1), ("review", 1), ("verify", 1)])
        self.assertEqual(self.start()["id"], run["id"])
        with self.assertRaises(ValueError):
            self.start(brief="不同请求")
        self.assertEqual(team_runs.RunStore(self.db).get(run["id"]), run)

    def test_four_pauses_persist_and_only_matching_answers_continue(self):
        for scenario, kind, action, value in [
            ("timeout", "error", "retry", ""), ("missing", "missing", "supply", "已补材料"),
            ("limit", "limit", "extend", "1"), ("choice", "choice", "choose", "continue")
        ]:
            with self.subTest(scenario=scenario):
                run = self.start(scenario, key=scenario)
                self.assertEqual(run["status"], "waiting")
                self.assertEqual(run["help"]["kind"], kind)
                if scenario == "missing":
                    self.assertEqual(run["attempts"], [])
                    self.assertIn("lead.brief", run["help"]["message"])
                self.store.recover()
                self.assertEqual(self.store.get(run["id"]), run)
                with self.assertRaises(ValueError):
                    self.answer(run, "rework")
                done = self.answer(run, action, value)
                self.assertEqual(done["status"], "completed")
                self.assertEqual(self.answer(run, action, value), done)
                self.assertEqual(done["decisions"][-1]["value"], value)
                if scenario == "timeout":
                    self.assertEqual([(a["number"], a["status"]) for a in done["attempts"]
                                      if a["node"] == "lead"], [(1, "failed"), (2, "success")])

    def test_parallel_approval_opens_one_child_and_hard_limit(self):
        first = self.start("review")
        with ThreadPoolExecutor(4) as pool:
            children = list(pool.map(lambda _: self.answer(first, "rework"), range(4)))
        self.assertEqual(len({r["id"] for r in children}), 1)
        second = self.store.get(children[0]["id"])
        self.assertEqual(second["parent_id"], first["id"])
        self.assertEqual(second["round"], 2)
        self.assertTrue(second["source"]["revision"]["patch"])
        self.assertFalse(second["source"]["revision"]["review"]["approved"])
        self.assertEqual(second["attempts"][0]["inputs"]["revision"], second["source"]["revision"])
        third = self.answer(second, "rework")
        self.assertEqual(third["round"], 3)
        self.assertEqual(third["root_id"], first["id"])
        self.assertEqual(third["help"]["kind"], "limit")
        self.assertEqual(third["help"]["actions"], ["stop"])
        with self.assertRaises(ValueError):
            self.answer(third, "rework")
        self.assertEqual(self.answer(third, "stop")["status"], "stopped")
        self.assertEqual(len(self.store.list_runs("card")), 3)

    def test_failed_check_keeps_evidence_for_next_round(self):
        first = self.start("test_failure")
        second = self.answer(first, "rework")
        self.assertFalse(second["source"]["revision"]["checks"]["passed"])
        self.assertTrue(second["source"]["revision"]["review"]["approved"])

    def test_existing_empty_database_during_initialization_is_an_empty_listing(self):
        self.db.parent.mkdir()
        self.db.touch()
        self.assertEqual(self.store.list_runs("card"), [])
        with self.assertRaises(ValueError):
            self.store.get("missing")

    def test_recovery_between_steps_only_continues_unclaimed_nodes(self):
        original = self.store._claim
        def stop_before_second(conn, run):
            if run["attempts"] and run["attempts"][-1]["node"] == "lead":
                raise SystemExit
            return original(conn, run)
        with patch.object(self.store, "_claim", side_effect=stop_before_second):
            with self.assertRaises(SystemExit):
                self.start()
        self.store.recover()
        summary, = self.store.list_runs("card")
        run = self.store.get(summary["id"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual([a["number"] for a in run["attempts"]], [1, 1, 1, 1])

    def test_concurrent_starts_submit_attempt_only_once(self):
        barrier = threading.Barrier(4)
        def launch(_):
            barrier.wait()
            return self.start()
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(launch, range(4)))
        ids = {r["id"] for r in results}
        self.assertEqual(len(ids), 1)
        run = self.store.get(ids.pop())
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(run["attempts"]), 4)

    def test_crash_after_claim_becomes_interrupted_without_resubmit(self):
        with patch.object(team_runs, "evaluate", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.start()
        summary, = self.store.list_runs("card")
        other = team_runs.RunStore(self.db)
        other.recover()
        run = other.get(summary["id"])
        self.assertEqual(len(run["attempts"]), 1)
        self.assertEqual(run["attempts"][0]["status"], "interrupted")
        self.assertEqual(run["help"]["kind"], "error")
        other.recover()
        self.assertEqual(other.get(run["id"]), run)
        self.assertEqual(self.answer(run, "retry")["status"], "completed")

    def test_changed_wire_changes_actual_review_input_and_missing_wire_blocks_start(self):
        template = team_runs.default_template()
        alternate = copy.deepcopy(template)
        alternate["bindings"]["review"]["context"] = "source.brief"
        normal = self.start()
        with patch.object(team_runs, "default_template", return_value=alternate):
            changed = self.start(key="alternate")
        def review_context(run):
            return next(a["inputs"]["context"] for a in run["attempts"] if a["node"] == "review")
        self.assertEqual(review_context(changed), "合成示例")
        self.assertNotEqual(review_context(normal), review_context(changed))
        del alternate["bindings"]["review"]["patch"]
        with patch.object(team_runs, "default_template", return_value=alternate):
            broken = self.start(key="broken")
        self.assertEqual(broken["attempts"], [])
        self.assertIn("review.patch", broken["help"]["message"])
        self.assertEqual(self.store.get(normal["id"]), normal)
