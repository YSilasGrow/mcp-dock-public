import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bridge import server
from bridge.events import read_events


class ServerTests(unittest.TestCase):
    def test_consult_uses_harness(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(Path(root) / "events.jsonl")}):
                with patch.object(server, "consult", return_value={"answer": "ok"}) as call:
                    result = server.ask_deepseek("hello")
                self.assertEqual([event["kind"] for event in read_events()["events"]], ["question", "status", "answer"])
                self.assertEqual(result["answer"], "ok")
                self.assertEqual(read_events()["events"][0]["call_id"], result["call_id"])
        call.assert_called_once()

    def test_consult_error_is_visible_without_false_answer(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(Path(root) / "events.jsonl")}):
                with patch.object(server, "consult", side_effect=RuntimeError("login expired")):
                    with self.assertRaisesRegex(RuntimeError, "login expired"):
                        server.ask_deepseek("hello")
                self.assertEqual([event["kind"] for event in read_events()["events"]], ["question", "status", "error"])

    def test_codex_feedback_appears_in_same_call(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(Path(root) / "events.jsonl")}):
                event = server.record_codex_feedback("我核对了计划原文", call_id="round-one")
                stored = read_events()["events"][0]
            self.assertEqual(event["kind"], "feedback")
            self.assertEqual(stored["call_id"], "round-one")
            self.assertEqual(stored["text"], "我核对了计划原文")

    def test_delegate_rejects_workspace_root_as_target(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"DEEPSEEK_BRIDGE_ROOT": root, "DEEPSEEK_BRIDGE_EVENTS": str(Path(root) / "events.jsonl")}):
                with self.assertRaises(ValueError):
                    server.delegate_to_deepseek("do work", root)


class StdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_tools_create_claim_and_read_an_isolated_thread(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as root:
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "bridge.server"], cwd=str(project),
                env={**os.environ, "DEEPSEEK_BRIDGE_EVENTS": str(Path(root) / "private" / "events.jsonl")},
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    expected = {"create_task", "list_tasks", "update_task_status", "post_message", "read_thread",
                                "thread_ask_deepseek"}
                    self.assertTrue(expected <= tools.keys(), expected - tools.keys())
                    for name in ("thread_ask_deepseek", "research_ask_deepseek"):
                        dispatch_schema = tools[name].inputSchema
                        self.assertNotIn("timeout_seconds", dispatch_schema["required"])
                        timeout = dispatch_schema["properties"].get("timeout_seconds", {})
                        # Optional: omitted means no total limit, otherwise 1-600 seconds.
                        self.assertIsNone(timeout.get("default"))
                        integer = next(item for item in timeout["anyOf"] if item.get("type") == "integer")
                        self.assertEqual((integer.get("minimum"), integer.get("maximum")), (1, 600))
                    created = await session.call_tool("create_task", {"title": "MCP 新任务"})
                    self.assertFalse(created.isError, created.content)
                    task = created.structuredContent
                    updated = await session.call_tool("update_task_status", {
                        "task_id": task["task_id"], "status": "waiting",
                    })
                    self.assertFalse(updated.isError, updated.content)
                    listed = await session.call_tool("list_tasks", {"status": "waiting"})
                    self.assertFalse(listed.isError, listed.content)
                    self.assertEqual(listed.structuredContent["result"][0]["task_id"], task["task_id"])
                    posted = await session.call_tool("post_message", {
                        "thread_id": task["thread_id"], "content": "工作台本地备注",
                    })
                    self.assertFalse(posted.isError, posted.content)
                    self.assertFalse(posted.structuredContent["share"])
                    thread = await session.call_tool("read_thread", {"thread_id": task["thread_id"]})
                    self.assertFalse(thread.isError, thread.content)
                    self.assertEqual(thread.structuredContent["messages"][-1]["text"], "工作台本地备注")

    async def test_mcp_stdio_lists_legacy_and_research_tools(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as root:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "bridge.server"],
                cwd=str(project),
                env={**os.environ, "DEEPSEEK_BRIDGE_EVENTS": str(Path(root) / "events.jsonl")},
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    rejected = await session.call_tool(
                        "delegate_to_deepseek",
                        {"task": "test", "project_dir": str(project)},
                    )
        self.assertIn("ask_deepseek", names)
        self.assertIn("delegate_to_deepseek", names)
        self.assertIn("record_codex_feedback", names)
        self.assertTrue({"research_start", "research_record", "research_record_phase", "research_ask_deepseek", "research_finish"} <= names)
        self.assertTrue(rejected.isError)


if __name__ == "__main__":
    unittest.main()
