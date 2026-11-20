import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge import harness


class TeamAssistantTests(unittest.TestCase):
    def adapter(self):
        from bridge import team_assistant
        return team_assistant

    def test_send_check_identifies_lines_without_echoing_private_material(self):
        adapter = self.adapter()
        examples = ("token=synth-secret", "-----BEGIN OPENSSH PRIVATE KEY-----",
                    "我的密码：合成示例", "sk-syntheticvalueforatestonly", "https://user:secret@example.test/",
                    "/Users/example/check.txt", r"C:\Users\example\check.txt", "~/project/private.md",
                    "198.51.100.7", "[2001:db8::8]:443", "::1", "fe80::1%en0",
                    "我用的 Clash 配置", "我的 VPS 出口", "DNS 诊断", "ip route show", "proxy-server=foo")
        for value in examples:
            with self.subTest(value=value):
                findings = adapter.inspect_question("公开问题\n" + value)
                self.assertTrue(findings)
                self.assertTrue(all("2" in f and value not in f for f in findings))
        self.assertEqual(adapter.inspect_question("审阅 shipping pipeline 的合成边界条件。"), [])

    def test_consult_checks_again_and_keeps_rejected_question_out_of_errors(self):
        adapter = self.adapter()
        for value in ("  ", None, "长" * 5001, "假字段\x00", "登录密码：fixture-private"):
            with self.subTest(variant=type(value).__name__):
                with patch.object(harness, "_run", side_effect=AssertionError("不得派发")):
                    with self.assertRaises(ValueError) as error:
                        adapter.consult_assistant(value)
                    self.assertNotIn("fixture-private", str(error.exception))

    def test_new_cli_session_has_one_exact_question_two_patches_and_isolated_home(self):
        adapter = self.adapter()
        question = "仅就这个合成例子提出边界测试。"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "synthetic-dsh"
            fake.write_text(
                "#!" + sys.executable + "\nimport json, os, sys\nfrom pathlib import Path\n"
                "payload = dict(args=sys.argv[1:], cwd=str(Path.cwd()), files=os.listdir('.'), "
                "mode=os.environ['DSH_PERMISSION_MODE'], home=os.environ['HOME'], "
                "profile=os.environ['DSH_HOME'], credential=os.environ['MCP_DOCK_CREDENTIAL_FILE'])\n"
                "print(json.dumps(dict(type='final', text=json.dumps(payload))))\n")
            fake.chmod(0o700)
            with patch.object(harness, "DSH", fake), patch.dict(os.environ, {"DSH_HOME": str(root)}):
                result = adapter.consult_assistant(question)
            captured = json.loads(result["answer"])
            self.assertEqual(captured["args"], [
                "--profile", "headless", "--patch", str(harness.PATCH),
                "--patch", str(root_project() / "team-assistant.patch.yml"),
                "--json", adapter.ASSISTANT_INSTRUCTIONS + "\n\n" + question,
            ])
            self.assertEqual(captured["mode"], "read-only")
            self.assertEqual(captured["files"], [])
            self.assertEqual(captured["home"], captured["profile"])
            self.assertNotEqual(captured["cwd"], captured["home"])
            self.assertNotEqual(captured["profile"], str(root))
            self.assertEqual(captured["credential"], str(root / ".credentials.yaml"))
            self.assertFalse(Path(captured["cwd"]).exists())
            self.assertFalse(Path(captured["profile"]).exists())


def root_project():
    return Path(__file__).resolve().parents[1]
