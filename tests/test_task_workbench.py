import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from bridge import events, tasks, workbench


class TaskWorkbenchHttpTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "DEEPSEEK_BRIDGE_EVENTS": str(Path(root) / "private" / "events.jsonl"),
        }))
        self.server = workbench.make_server(port=0)
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def post(self, payload, **headers):
        return self.request("POST", "/api/tasks", json.dumps(payload).encode("utf-8"), headers)

    def test_same_origin_create_is_visible_to_mcp_and_incremental_feed(self):
        legacy = events.append_event("codex", "question", "旧工具消息")
        earlier = events.read_events()
        for hostname in ("127.0.0.1", "localhost"):
            host = f"{hostname}:{self.port}"
            status, data = self.post(
                {"title": hostname, "goal": "调用大肥鱼时使用"},
                Origin=f"http://{host}", Host=host, **{"Content-Type": "application/json"},
            )
            self.assertEqual(status, 201, data)
            created = json.loads(data)
            self.assertEqual(created["status"], "pending")
            self.assertEqual(created["role"], "user")
            tasks.update_task_status(created["task_id"], "active")
        self.assertEqual(len(tasks.list_tasks("active")), 2)
        status, data = self.request("GET", f"/api/events?after={earlier['next']}")
        self.assertEqual(status, 200)
        self.assertEqual([row["kind"] for row in json.loads(data)["events"]],
                         ["task_create", "task_status", "task_create", "task_status"])
        self.assertEqual(events.read_all_events()[0], legacy)

    def test_refuses_untrusted_or_mismatched_origin_without_appending(self):
        for origin in ("https://external.example", "null", None, f"http://localhost:{self.port}"):
            headers = {"Content-Type": "application/json"}
            if origin is not None:
                headers["Origin"] = origin
            status, _ = self.post({"title": "不得建卡"}, **headers)
            self.assertEqual(status, 403)
        self.assertEqual(events.read_all_events(), [])

    def test_rejects_invalid_or_oversize_json_without_appending(self):
        base = {"Origin": self.origin, "Content-Type": "application/json"}
        status, _ = self.post({"title": "标" * 201}, **base)
        self.assertEqual(status, 400)
        status, _ = self.post({"title": "标", "goal": "内" * 5001}, **base)
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/api/tasks", b" " * 65537, base)
        self.assertEqual(status, 413)
        status, _ = self.request("POST", "/api/tasks", b"{", base)
        self.assertEqual(status, 400)
        status, _ = self.post({"title": "标"}, Origin=self.origin, **{"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _ = self.post({"title": "标", "role": "codex"}, **base)
        self.assertEqual(status, 400)
        self.assertEqual(events.read_all_events(), [])

    def test_card_script_and_form_are_served_by_the_workbench(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("未归类记录", body.decode())
        self.assertIn(b'id="new-task-form"', body)
        status, asset = self.request("GET", "/assets/task-cards.js")
        self.assertEqual(status, 200)
        self.assertIn("调用成功，回复未保存", asset.decode())
