import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from bridge import research, workbench


class ResearchWorkbenchTests(unittest.TestCase):
    def test_api_returns_server_labels_and_page_uses_safe_text_nodes(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(Path(root) / "events.jsonl")}):
                rid = research.start("<script>alert(1)</script>", "", 120)["research_id"]
                research.record(rid, [{"claim_id": "C1", "text": "<img src=x onerror=alert(1)>", "author": "codex"}], [])
                research.finish(rid, "partial", "ok", "no_search", ["C1"], [])
                server = workbench.make_server(host="127.0.0.1", port=0)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    base = f"http://127.0.0.1:{server.server_port}"
                    with urlopen(base + "/api/research") as response:
                        data = json.load(response)
                    with urlopen(base + "/") as response:
                        page = response.read().decode("utf-8")
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(timeout=2)
        self.assertEqual(data[0]["research_id"], rid)
        self.assertEqual(data[0]["coverage"], "0/1")
        self.assertEqual(data[0]["claims"][0]["labels"], ["未核验"])
        self.assertNotIn("<img src=x onerror=alert(1)>", page)
        self.assertIn("/api/research", page)
        self.assertIn("textContent", page)
        self.assertIn('url.protocol === "http:" || url.protocol === "https:"', page)
