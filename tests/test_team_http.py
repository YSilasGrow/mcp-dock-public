import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from bridge import tasks, workbench


class TeamHttpTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = root / "team-runs" / "runs.sqlite3"
        self.enterContext(patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(root / "events.jsonl")}))
        self.task = tasks.create_task("合成任务", "验证本机模拟", actor="user")
        self.task_id = self.task["task_id"]
        self.server = workbench.make_server(port=0)
        self.addCleanup(self.server.server_close)
        worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02})
        worker.start()
        self.addCleanup(worker.join, 3)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, **headers):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            connection.close()

    def post(self, path, body, **headers):
        headers = {"Origin": f"http://127.0.0.1:{self.server.server_port}",
                   "Content-Type": "application/json", **headers}
        status, _, data = self.request("POST", "/api/team/" + path, json.dumps(body).encode(), **headers)
        return status, json.loads(data)

    def get_json(self, path):
        status, _, data = self.request("GET", "/api/team/" + path)
        self.assertEqual(status, 200, data)
        return json.loads(data)

    def start_body(self, **fields):
        return {"task_id": self.task_id, "request_id": "request-1", "scenario": "timeout",
                "brief": "合成样例", "lead": "codex", **fields}

    def start(self):
        status, run = self.post("start", self.start_body())
        self.assertIn(status, {200, 201}, run)
        return run

    def test_stop_waiting_only_applies_to_a_deepseek_call_in_progress(self):
        run = self.start()
        status, error = self.post("stop-waiting", {"run_id": run["id"]})
        self.assertEqual(status, 400)
        self.assertIn("没有在等待 DeepSeek", error["error"])
        status, _ = self.post("stop-waiting", {"run_id": run["id"], "extra": "x"})
        self.assertEqual(status, 400)

    def test_existing_card_lists_empty_history_without_creating_a_database(self):
        card = self.get_json("runs?task_id=" + self.task_id)
        self.assertEqual(card["task"]["title"], "合成任务")
        self.assertEqual(card["task"]["task_id"], self.task_id)
        self.assertEqual(card["task"]["status"], "pending")
        self.assertEqual(card["runs"], [])
        self.assertFalse(self.db.parent.exists())

    def test_local_routes_serve_team_page_and_script(self):
        for path, mime in [(f"/team?task_id={self.task_id}", "text/html"),
                           ("/assets/team-run.js", "text/javascript")]:
            with self.subTest(path=path):
                status, content_type, data = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(content_type.split(";")[0], mime)
                self.assertTrue(data)

    def test_start_answer_and_reload_use_one_run_and_isolate_other_tasks(self):
        run = self.start()
        self.assertEqual(run["status"], "waiting")
        self.assertEqual(run["help"]["kind"], "error")
        self.assertEqual(self.start()["id"], run["id"])
        self.assertEqual(self.get_json("run?id=" + run["id"]), run)
        other = tasks.create_task("另一张卡")
        self.assertEqual(self.get_json("runs?task_id=" + other["task_id"])["runs"], [])
        answer = {"run_id": run["id"], "help_id": run["help"]["id"], "action": "stop"}
        status, stopped = self.post("answer", answer)
        self.assertEqual(status, 200, stopped)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(self.post("answer", answer), (200, stopped))
        self.assertEqual(self.get_json("run?id=" + run["id"]), stopped)
        history = self.get_json("runs?task_id=" + self.task_id)["runs"]
        self.assertEqual([(r["id"], r["status"]) for r in history], [(run["id"], "stopped")])
        self.assertEqual(stopped["cloud_calls"], 0)

    def test_archiving_card_makes_start_and_answer_read_only(self):
        run = self.start()
        tasks.update_task_status(self.task_id, "archived")
        answer = {"run_id": run["id"], "help_id": run["help"]["id"], "action": "stop"}
        for path, body in [("start", self.start_body(request_id="new")), ("answer", answer)]:
            with self.subTest(path=path):
                status, error = self.post(path, body)
                self.assertIn(status, {400, 409})
                self.assertIn("归档", error["error"])
        self.assertEqual(self.get_json("run?id=" + run["id"]), run)
        card = self.get_json("runs?task_id=" + self.task_id)
        self.assertEqual(card["task"]["status"], "archived")
        self.assertEqual(len(card["runs"]), 1)

    def test_unknown_or_malformed_ids_do_not_create_store(self):
        unknown = "f" * 32
        for route, status in [(f"runs?task_id={unknown}", 404), (f"run?id={unknown}", 404),
                              ("run?id=../invalid", 400), ("runs?", 400),
                              (f"runs?task_id={self.task_id}&task_id={unknown}", 400)]:
            with self.subTest(route=route):
                self.assertEqual(self.request("GET", "/api/team/" + route)[0], status)
        self.assertEqual(self.post("start", self.start_body(task_id=unknown))[0], 404)
        self.assertFalse(self.db.parent.exists())

    def test_team_posts_require_origin_json_and_bounded_body(self):
        for path in ("start", "answer"):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, {}, Origin="http://external.invalid")[0], 403)
                self.assertEqual(self.post(path, {}, **{"Content-Type": "text/plain"})[0], 415)
                self.assertEqual(self.post(path, {"brief": "x" * 65537})[0], 413)
        self.assertEqual(self.request("GET", "/api/team/runs", Host="external.invalid")[0], 403)
        self.assertFalse(self.db.parent.exists())

    def test_rejects_invalid_fields_and_types_before_a_run_is_created(self):
        for changes in [{"template": {}}, {"request_id": ["request"]}, {"lead": {}},
                        {"scenario": "real"}, {"brief": False}, {"task_id": "../invalid"},
                        {"brief": "\udfff"}, {"brief": "line\x00break"}]:
            with self.subTest(changes=changes):
                status, error = self.post("start", self.start_body(**changes))
                self.assertEqual(status, 400, error)
        for body in [[], {}, {"task_id": self.task_id}]:
            self.assertEqual(self.post("start", body)[0], 400)
        bad_value = {"run_id": "a" * 32, "help_id": "b" * 32, "action": "supply", "value": {}}
        self.assertEqual(self.post("answer", bad_value)[0], 400)
        self.assertEqual(self.get_json("runs?task_id=" + self.task_id)["runs"], [])
