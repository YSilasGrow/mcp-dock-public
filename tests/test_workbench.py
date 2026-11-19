import json
import os
import stat
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from urllib.request import urlopen
from unittest.mock import patch

from bridge import events, workbench


class EventStoreTests(unittest.TestCase):
    def test_append_is_private_and_incremental_read_returns_only_new_events(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "private" / "events.jsonl"
            with patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(path)}):
                first = events.append_event("codex", "question", "第一个问题", call_id="one")
                initial = events.read_events()
                second = events.append_event("deepseek", "answer", "第二个回答", call_id="one")
                later = events.read_events(after=initial["next"])
            self.assertEqual([entry["text"] for entry in initial["events"]], ["第一个问题"])
            self.assertEqual([entry["text"] for entry in later["events"]], ["第二个回答"])
            self.assertEqual(first["call_id"], second["call_id"])
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_invalid_cursor_does_not_read_secret_log_from_another_path(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            with patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(path)}):
                events.append_event("codex", "question", "hello")
                with self.assertRaises(ValueError):
                    events.read_events(after=-1)


class WorkbenchHttpTests(unittest.TestCase):
    def test_http_returns_json_and_page_without_injecting_message_into_html(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            with patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(path)}):
                events.append_event("codex", "question", "<script>alert(1)</script>")
                server = workbench.make_server(host="127.0.0.1", port=0)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    base = f"http://127.0.0.1:{server.server_port}"
                    with urlopen(base + "/api/events?after=0") as response:
                        payload = json.load(response)
                        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
                    with urlopen(base + "/") as response:
                        page = response.read().decode("utf-8")
                        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(timeout=2)
            self.assertEqual(payload["events"][0]["text"], "<script>alert(1)</script>")
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertIn("textContent", page)

    def test_rejects_foreign_host_header(self):
        server = workbench.make_server(host="127.0.0.1", port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/api/events", headers={"Host": "untrusted.example"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
