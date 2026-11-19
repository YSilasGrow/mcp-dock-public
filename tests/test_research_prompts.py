import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge import events, research, research_prompts, server


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        env = patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(Path(self.root.name) / "events.jsonl")})
        env.start()
        self.addCleanup(env.stop)
        self.rid = research.start("公开的 Python 问题", "仅官方来源", 300)["research_id"]

    def test_independent_uses_only_public_question_and_constraints(self):
        research.record(self.rid, [{"claim_id": "C1", "text": "私人结论不能先泄露", "author": "codex"}], [])
        version, task = research_prompts.build_task(self.rid, "independent", events.read_all_events())
        self.assertIn("公开的 Python 问题", task)
        self.assertIn("仅官方来源", task)
        self.assertNotIn("私人结论不能先泄露", task)
        self.assertTrue(version)

    def test_challenge_uses_active_sorted_claims_and_stored_disagreement(self):
        research.record(self.rid, [{"claim_id": "C2", "text": "旧结论", "author": "codex"},
                                   {"claim_id": "C1", "text": "第一条", "author": "codex"},
                                   {"claim_id": "C3", "text": "替代结论", "author": "codex", "supersedes": "C2"}], [])
        research.record_phase(self.rid, "challenge", "started", disagreements=[{"claim_id": "C1", "note": "核查时间"}])
        _, task = research_prompts.build_task(self.rid, "challenge", events.read_all_events())
        self.assertIn("第一条", task)
        self.assertIn("替代结论", task)
        self.assertNotIn("旧结论", task)
        self.assertIn("核查时间", task)
        self.assertLess(task.index("C1"), task.index("C3"))

    def test_full_task_is_logged_exactly_before_harness_call(self):
        research.record(self.rid, [{"claim_id": "C1", "text": "自己的结论", "author": "codex"}], [])
        research.record_phase(self.rid, "independent", "finished")
        observed = []

        def fake(task, on_event=None):
            logged = [row for row in events.read_all_events() if row["kind"] == "question"]
            observed.append((task, logged[-1]["text"], logged[-1]["template_version"]))
            return {"answer": "D1 来源 NOT_TRIED", "usage": {"total_tokens": 1}}

        with patch.object(server, "consult_prepared", side_effect=fake):
            result = server.research_ask_deepseek(self.rid, "independent")
        self.assertEqual(observed[0][0], observed[0][1])
        self.assertTrue(observed[0][2])
        self.assertEqual(result["answer"], "D1 来源 NOT_TRIED")
        phases = [row["state"] for row in events.read_all_events() if row["kind"] == "research_phase" and row["actor"] == "deepseek"]
        self.assertEqual(phases, ["started", "finished"])

    def test_timeout_records_partial_phase_without_answer(self):
        research.record(self.rid, [{"claim_id": "C1", "text": "自己的结论", "author": "codex"}], [])
        research.record_phase(self.rid, "independent", "finished")
        with patch.object(server, "consult_prepared", side_effect=RuntimeError("timed out after 180 seconds")):
            with self.assertRaises(RuntimeError):
                server.research_ask_deepseek(self.rid, "independent")
        stored = events.read_all_events()
        self.assertIn("timeout", [row.get("state") for row in stored if row.get("kind") == "research_phase"])
        self.assertNotIn("answer", [row["kind"] for row in stored])

    def test_concurrent_same_phase_starts_only_one_harness_call(self):
        research.record(self.rid, [{"claim_id": "C1", "text": "自己的结论", "author": "codex"}], [])
        research.record_phase(self.rid, "independent", "finished")
        barrier = threading.Barrier(3)
        outcomes = []
        calls = []

        def fake(task, on_event=None):
            calls.append(task)
            time.sleep(0.1)
            return {"answer": "D1 NOT_TRIED", "usage": {}}

        def worker():
            barrier.wait()
            try:
                outcomes.append(server.research_ask_deepseek(self.rid, "independent"))
            except ValueError as exc:
                outcomes.append(str(exc))

        with patch.object(server, "consult_prepared", side_effect=fake):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len([x for x in outcomes if isinstance(x, dict)]), 1)
        self.assertEqual(len([x for x in events.read_all_events() if x["kind"] == "question"]), 1)

    def test_oversized_challenge_rejected_before_phase_or_harness(self):
        claims = [{"claim_id": f"C{i:02d}", "text": "结论" + "x" * 250, "author": "codex"} for i in range(10)]
        evidence = [{"claim_id": f"C{i:02d}", "evidence_id": f"E{i:02d}",
                     "url": "https://example.org/" + "x" * 1800} for i in range(10)]
        research.record(self.rid, claims, evidence)
        with patch.object(server, "consult_prepared") as harness:
            with self.assertRaisesRegex(ValueError, "20000"):
                server.research_record_phase(self.rid, "challenge", "started", disagreements=[])
            harness.assert_not_called()
        self.assertFalse(any(row["kind"] == "research_phase" and row.get("phase") == "challenge" for row in events.read_all_events()))
