import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge import team_runs
from bridge.harness import HarnessCancelled, HarnessError


class LiveRunTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "private/runs.sqlite3"
        self.store = team_runs.RunStore(self.path)
        self.calls = []
        self.answer_text = "独立意见：下一轮应仍需批准。"
        def fake(question, on_event=None, **_):
            self.calls.append(question)
            if on_event:
                on_event({"type":"status", "phase":"step_start"})
            return {"answer":self.answer_text, "usage":{"total_tokens":7}}
        self.client = self.enterContext(patch("bridge.team_runs.consult_assistant", side_effect=fake))

    def start(self, scenario="success", key="request", question="复核返工轮数"):
        return self.store.start("task",key,scenario,"任务说明只留本机", "codex",
                                assistant="deepseek",assistant_question=question)

    def settled(self, run):
        end=time.monotonic()+3
        while time.monotonic()<end:
            data=self.store.get(run["id"])
            if data["status"]!="running":
                return data
            time.sleep(0.01)
        self.fail("worker did not settle")

    def answer(self, run, action, value=""):
        return self.settled(self.store.answer(run["id"],run["help"]["id"],action,value))

    def test_live_answer_reaches_actual_review_input_and_rework_does_not_resend(self):
        first=self.settled(self.start("review"))
        self.assertEqual(self.calls,["复核返工轮数"])
        record=next(a for a in first["attempts"] if a["node"]=="opinion")
        self.assertEqual(record["inputs"],{"question":"复核返工轮数"})
        self.assertEqual(record["progress"]["phase"],"step_start")
        self.assertEqual(record["output"]["usage"]["total_tokens"],7)
        review=next(a for a in first["attempts"] if a["node"]=="review")
        self.assertEqual(review["inputs"]["opinion"],[self.answer_text])
        child=self.answer(first,"rework")
        self.assertEqual(child["cloud_calls"],0)
        self.assertEqual(self.calls,["复核返工轮数"])
        inherited=next(a for a in child["attempts"] if a["node"]=="opinion")
        self.assertEqual(inherited["output"]["opinion"],[self.answer_text])
        self.assertEqual(inherited["output"]["from_run_id"],first["id"])

    def test_prompt_is_checked_again_on_replacement_and_before_counting(self):
        first=self.settled(self.start(question="代理地址为 192.0.2.123"))
        self.assertEqual(first["cloud_calls"],0)
        self.assertEqual(first["help"]["node"],"opinion")
        self.assertIn("replace_question",first["help"]["actions"])
        still=self.answer(first,"replace_question","另一处 203.0.113.7")
        self.assertEqual(still["status"],"waiting")
        self.assertEqual(self.calls,[])
        done=self.answer(still,"replace_question","请给出验收建议")
        self.assertEqual(done["status"],"completed")
        self.assertEqual(self.calls,["请给出验收建议"])
        self.assertEqual(self.start(question="代理地址为 192.0.2.123")["id"],first["id"])

    def test_timeout_does_not_replay_on_refresh_or_recovery(self):
        self.client.side_effect=HarnessError("DeepSeek Harness task timed out after 600 seconds")
        run=self.settled(self.start())
        self.assertEqual(run["status"],"waiting")
        self.assertEqual(run["help"]["actions"],["skip_assistant","stop"])
        self.assertEqual(run["cloud_calls"],1)
        self.assertIn("600",run["help"]["message"])
        self.store.recover()
        self.assertEqual(self.start()["id"],run["id"])
        done=self.answer(run,"skip_assistant")
        self.assertEqual(done["status"],"completed")
        self.assertEqual(self.client.call_count,1)
        self.assertEqual(next(a for a in done["attempts"] if a["node"]=="review")["inputs"]["opinion"],[])

    def test_owner_can_stop_waiting_and_the_call_is_not_resent(self):
        started = threading.Event()
        def wait(question, on_event=None, *, cancel=None, **_):
            started.set()
            if on_event:
                on_event({"type":"status", "phase":"step_start"})
            self.assertTrue(cancel.wait(2))
            raise HarnessCancelled("已停止等待 DeepSeek（主人手动停止）。对方已收到，可能已产生用量。")
        self.client.side_effect=wait
        run=self.start()
        self.assertTrue(started.wait(1))
        self.store.stop_waiting(run["id"])
        done=self.settled(run)
        self.assertEqual(done["status"],"waiting")
        self.assertEqual(done["help"]["actions"],["skip_assistant","stop"])
        self.assertIn("已停止等待",done["help"]["message"])
        self.assertEqual(done["cloud_calls"],1)
        self.assertEqual(self.client.call_count,1)
        with self.assertRaises(ValueError):
            self.store.stop_waiting(run["id"])
        finished=self.answer(done,"skip_assistant")
        self.assertEqual(finished["status"],"completed")
        self.assertEqual(self.client.call_count,1)

    def test_slow_worker_does_not_hold_http_request_or_launch_twice(self):
        started, release = threading.Event(),threading.Event()
        def slow(question,on_event=None,**_):
            started.set()
            release.wait(2)
            return {"answer":"收到","usage":{}}
        self.client.side_effect=slow
        try:
            run=self.start()
            self.assertTrue(started.wait(1))
            self.assertEqual(self.store.get(run["id"])["status"],"running")
            self.assertEqual(self.start()["id"],run["id"])
            with self.assertRaises(ValueError):
                self.start(question="不同问题")
        finally:
            release.set()
        self.assertEqual(self.settled(run)["cloud_calls"],1)
        self.assertEqual(self.client.call_count,1)
