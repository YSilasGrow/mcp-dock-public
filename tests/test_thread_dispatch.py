import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge import events, harness, server, tasks


class ThreadDispatchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "DEEPSEEK_BRIDGE_EVENTS": str(self.root / "private" / "events.jsonl"),
        }))
        self.a = tasks.create_task("任务 A", "A说明-甲")["thread_id"]
        self.b = tasks.create_task("任务 B")["thread_id"]

    def _fake_harness(self, body):
        executable = self.root / "fake-dsh"
        executable.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        executable.chmod(0o700)
        patch_file = self.root / "patch.yml"
        patch_file.write_text("[]", encoding="utf-8")
        self.enterContext(patch.object(harness, "DSH", executable))
        self.enterContext(patch.object(harness, "PATCH", patch_file))

    def test_shared_uses_only_marked_messages_from_this_thread(self):
        shared = tasks.post_message(self.a, "蓝鲸-7", share=True)
        tasks.post_message(self.a, "白狼-5")
        tasks.post_message(self.b, "赤狐-3", share=True)

        def harness(prompt, *, timeout_seconds, on_receipt=None):
            saved = tasks.read_thread(self.a)["messages"][-1]
            self.assertEqual(saved["kind"], "thread_dispatch")
            self.assertEqual(prompt, saved["text"])
            self.assertEqual(timeout_seconds, saved["timeout_seconds"])
            return {"answer": "先前回答只留本地", "usage": {}}

        with patch.object(server, "consult_prepared", side_effect=harness) as call:
            shared_result = server.thread_ask_deepseek(self.a, "请查 A", "shared")
            independent_result = server.thread_ask_deepseek(self.a, "请查 A", "independent")
            server.thread_ask_deepseek(self.a, "请补查", "shared")

        prompts = [args.args[0] for args in call.call_args_list]
        self.assertIn("A说明-甲", prompts[0])
        self.assertIn("蓝鲸-7", prompts[0])
        self.assertNotIn("白狼-5", prompts[0])
        self.assertNotIn("赤狐-3", prompts[0])
        self.assertIn("A说明-甲", prompts[1])
        self.assertNotIn("蓝鲸-7", prompts[1])
        self.assertNotIn("先前回答只留本地", prompts[2])
        self.assertEqual(shared_result["context_count"], 1)
        self.assertEqual(independent_result["context_count"], 0)
        rows = [row for row in tasks.read_thread(self.a)["messages"] if row["call_id"] == shared_result["call_id"]]
        self.assertEqual([row["kind"] for row in rows], ["thread_dispatch", "thread_result", "thread_reply"])
        self.assertEqual(rows[0]["context_ids"], [shared["id"]])
        self.assertEqual(rows[1]["state"], "finished")
        self.assertFalse(rows[2]["share"])
        self.assertEqual([row["text"] for row in tasks.read_thread(self.b)["messages"]], ["赤狐-3"])

    def test_empty_shared_context_is_explicit(self):
        tasks.post_message(self.a, "只留本地")
        with patch.object(server, "consult_prepared", return_value={"answer": "完成"}):
            result = server.thread_ask_deepseek(self.a, "问题", "shared")
        self.assertEqual(result["notice"], "本次没有带任何对话内容")
        self.assertEqual(result["context_count"], 0)

    def test_history_drops_oldest_rows_to_fit_character_limit(self):
        rows = [tasks.post_message(self.a, f"记录 {i:02d} " + "甲" * 3100, share=True) for i in range(23)]
        with patch.object(server, "consult_prepared", return_value={"answer": "完成"}):
            result = server.thread_ask_deepseek(self.a, "问题", "shared")
        dispatch = next(row for row in tasks.read_thread(self.a)["messages"] if row["call_id"] == result["call_id"])
        self.assertEqual(dispatch["context_ids"], [row["id"] for row in rows[-3:]])
        self.assertEqual(dispatch["dropped_ids"], [row["id"] for row in rows[:-3]])
        self.assertLessEqual(len(dispatch["text"]), 20000)

    def test_message_count_limit_keeps_twenty_most_recent_rows(self):
        rows = [tasks.post_message(self.a, f"短消息 {i}", share=True) for i in range(22)]
        with patch.object(server, "consult_prepared", return_value={"answer": "完成"}):
            result = server.thread_ask_deepseek(self.a, "问题", "shared")
        dispatch = next(row for row in tasks.read_thread(self.a)["messages"] if row["call_id"] == result["call_id"])
        self.assertEqual(dispatch["context_ids"], [row["id"] for row in rows[-20:]])
        self.assertEqual(dispatch["dropped_ids"], [row["id"] for row in rows[:2]])

    def test_rejects_a_new_shared_message_that_cannot_fit_but_allows_local_storage(self):
        overlong = "甲" * 13000
        with self.assertRaisesRegex(ValueError, "摘要"):
            tasks.post_message(self.a, overlong, share=True)
        self.assertEqual(tasks.read_thread(self.a)["messages"], [])
        local = tasks.post_message(self.a, overlong)
        self.assertFalse(local["share"])
        self.assertEqual(len(local["text"]), 13000)

    def test_existing_oversize_shared_row_does_not_hide_earlier_context(self):
        shared = tasks.post_message(self.a, "蓝鲸-7", share=True)
        oversized = events.append_validated_event(lambda _: {
            "kind": "thread_message", "role": "codex", "text": "甲" * 13000,
            "task_id": shared["task_id"], "thread_id": self.a, "share": True,
        })
        with patch.object(server, "consult_prepared", return_value={"answer": "完成"}):
            result = server.thread_ask_deepseek(self.a, "问题", "shared")
        dispatch = next(row for row in tasks.read_thread(self.a)["messages"] if row["call_id"] == result["call_id"])
        self.assertEqual(dispatch["context_ids"], [shared["id"]])
        self.assertEqual(dispatch["dropped_ids"], [oversized["id"]])
        self.assertIn("蓝鲸-7", dispatch["text"])

    def test_long_current_question_skips_history_that_cannot_fit_on_its_own(self):
        short = tasks.post_message(self.a, "蓝鲸-7", share=True)
        long = tasks.post_message(self.a, "甲" * 1000, share=True)
        with patch.object(server, "consult_prepared", return_value={"answer": "完成"}):
            result = server.thread_ask_deepseek(self.a, "问" * 19500, "shared")
        dispatch = next(row for row in tasks.read_thread(self.a)["messages"] if row["call_id"] == result["call_id"])
        self.assertEqual(dispatch["context_ids"], [short["id"]])
        self.assertEqual(dispatch["dropped_ids"], [long["id"]])
        self.assertLessEqual(len(dispatch["text"]), 20000)

    def test_total_budget_drops_oldest_even_if_a_short_older_row_could_fit(self):
        old = tasks.post_message(self.a, "旧消息", share=True)
        middle = tasks.post_message(self.a, "甲" * 7100, share=True)
        new = tasks.post_message(self.a, "乙" * 7100, share=True)
        with patch.object(server, "consult_prepared", return_value={"answer": "完成"}):
            result = server.thread_ask_deepseek(self.a, "问题", "shared")
        dispatch = next(row for row in tasks.read_thread(self.a)["messages"] if row["call_id"] == result["call_id"])
        self.assertEqual(dispatch["context_ids"], [new["id"]])
        self.assertEqual(dispatch["dropped_ids"], [old["id"], middle["id"]])

    def test_failure_and_timeout_are_distinct_and_have_no_reply(self):
        for error in (RuntimeError("启动失败"), TimeoutError("超过时限")):
            with patch.object(server, "consult_prepared", side_effect=error):
                with self.assertRaises(type(error)):
                    server.thread_ask_deepseek(self.a, "问题", "independent")
        rows = tasks.read_thread(self.a)["messages"]
        self.assertEqual([row["kind"] for row in rows], [
            "thread_dispatch", "thread_result", "thread_dispatch", "thread_result",
        ])
        self.assertEqual([row["state"] for row in rows if row["kind"] == "thread_result"], ["failed", "timeout"])
        for dispatch, result in zip(rows[::2], rows[1::2]):
            self.assertEqual(dispatch["call_id"], result["call_id"])

    def test_failure_records_bounded_sanitized_stderr_without_json(self):
        diagnostic = "\n".join(("old startup detail" * 500, "x" * 700, "cannot open profile:"))
        self._fake_harness(
            "import os, sys\n"
            f"os.write(2, {diagnostic.encode()!r} + b' \\xff\\n')\n"
            "sys.exit(7)\n",
        )
        with self.assertRaises(harness.HarnessError) as failure:
            server.thread_ask_deepseek(self.a, "问题", "independent")
        result = tasks.read_thread(self.a)["messages"][-1]
        self.assertEqual(result["state"], "failed")
        self.assertIn("cannot open profile", result["error"])
        self.assertIn("7", result["error"])
        self.assertLessEqual(len(result["error"]), 500)
        self.assertLessEqual(len(str(failure.exception)), 500)
        self.assertNotIn("old startup detail", result["error"])

    def test_failure_redacts_credential_lines_and_continuations(self):
        credentials = (
            "Authorization: Bearer fixture-bearer",
            "DEEPSEEK_API_KEY=fixture-api-key",
            "\x1b[31mCookie:\x1b[0m session=fixture-cookie",
            "upstream https://user:fixture-password@example.test/?access_token=fixture-url",
            "-----BEGIN PRIVATE KEY-----\nfixture-private-key-material\n-----END PRIVATE KEY-----",
            "Access token:\n  fixture-folded",
        )
        for credential in credentials:
            with self.subTest(credential=credential.splitlines()[0]):
                self._fake_harness(
                    "import sys\n"
                    f"print({credential!r}, file=sys.stderr)\n"
                    "print('cannot start runtime', file=sys.stderr)\n"
                    "sys.exit(7)\n",
                )
                with self.assertRaises(harness.HarnessError) as failure:
                    server.thread_ask_deepseek(self.a, "问题", "independent")
                result = tasks.read_thread(self.a)["messages"][-1]
                self.assertIn("[凭据已隐藏]", result["error"])
                self.assertIn("cannot start runtime", result["error"])
                self.assertNotIn("fixture-", result["error"])
                self.assertNotIn("fixture-", str(failure.exception))

    def test_failure_keeps_safe_structured_error_and_redacts_stderr(self):
        self._fake_harness(
            "import json, sys\n"
            "print(json.dumps({'type': 'error', 'message': 'runtime refused launch'}))\n"
            "print('OAuth token: fixture-oauth', file=sys.stderr)\n"
            "print('profile slot is in use', file=sys.stderr)\n"
            "sys.exit(2)\n",
        )
        with self.assertRaises(harness.HarnessError):
            server.thread_ask_deepseek(self.a, "问题", "shared")
        result = tasks.read_thread(self.a)["messages"][-1]
        self.assertIn("runtime refused launch", result["error"])
        self.assertIn("profile slot is in use", result["error"])
        self.assertNotIn("fixture-oauth", result["error"])

    def test_custom_timeout_is_used_by_harness_and_recorded(self):
        self._fake_harness(
            "import sys, time\n"
            "print('waiting for profile initialization', file=sys.stderr, flush=True)\n"
            "time.sleep(4)\n",
        )
        started = time.monotonic()
        with self.assertRaisesRegex(harness.HarnessError, "timed out after 1 seconds"):
            server.thread_ask_deepseek(self.a, "问题", "independent", timeout_seconds=1)
        self.assertLess(time.monotonic() - started, 3)
        dispatch, result = tasks.read_thread(self.a)["messages"]
        self.assertEqual(dispatch["timeout_seconds"], 1)
        self.assertEqual(result["state"], "timeout")
        self.assertIn("waiting for profile initialization", result["error"])

    def test_receipt_is_recorded_and_no_limit_applies_after_it(self):
        self._fake_harness(
            "import json, time\n"
            "print(json.dumps({'type':'status','phase':'turn_start'}), flush=True)\n"
            "time.sleep(1.5)\n"
            "print(json.dumps({'type':'final','text':'慢慢想好了'}), flush=True)\n"
        )
        with patch.object(harness, "HARNESS_RECEIPT_TIMEOUT_SECONDS", 1):
            result = server.thread_ask_deepseek(self.a, "问题", "independent")
        self.assertEqual(result["answer"], "慢慢想好了")
        rows = tasks.read_thread(self.a)["messages"]
        self.assertEqual([row["kind"] for row in rows],
                         ["thread_dispatch", "thread_received", "thread_result", "thread_reply"])
        self.assertIsNone(rows[0]["timeout_seconds"])

    def test_default_and_maximum_timeout_are_recorded(self):
        with patch.object(server, "consult_prepared", return_value={"answer": "完成"}):
            server.thread_ask_deepseek(self.a, "问题", "independent")
            server.thread_ask_deepseek(self.a, "问题", "shared", timeout_seconds=600)
        rows = tasks.read_thread(self.a)["messages"]
        # Omitted means no total limit: the call only has to be received.
        self.assertEqual([row["timeout_seconds"] for row in rows if row["kind"] == "thread_dispatch"], [None, 600])

    def test_invalid_timeouts_do_not_create_dispatch(self):
        for invalid in (0, -1, 601, True, 1.5, "180"):
            with self.subTest(timeout=invalid):
                with self.assertRaises(ValueError):
                    server.thread_ask_deepseek(self.a, "问题", "shared", timeout_seconds=invalid)
        self.assertEqual(tasks.read_thread(self.a)["messages"], [])

    def test_invalid_request_cannot_reach_harness_or_leave_dispatch(self):
        pending = tasks.create_task("还未领取", actor="user")
        for thread, question, mode in ((self.a, "问题", "invalid"), (self.a, "长" * 20000, "shared"),
                                       (pending["thread_id"], "问题", "shared")):
            with patch.object(server, "consult_prepared") as harness:
                with self.assertRaises(ValueError):
                    server.thread_ask_deepseek(thread, question, mode)
                harness.assert_not_called()
        self.assertEqual(tasks.read_thread(self.a)["messages"], [])
