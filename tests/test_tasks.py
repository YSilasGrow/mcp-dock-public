import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from bridge import events


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "DEEPSEEK_BRIDGE_EVENTS": str(Path(root) / "private" / "events.jsonl"),
        }))

    def test_conversations_and_cursor_stay_in_their_task(self):
        from bridge import tasks

        legacy = events.append_event("codex", "question", "旧记录")
        a = tasks.create_task("任务 A", "目标 A", actor="codex")
        b = tasks.create_task("任务 B", actor="user")
        local = tasks.post_message(a["thread_id"], "仅 A 可见")
        shared = tasks.post_message(a["thread_id"], "可以发", share=True)
        other = tasks.post_message(b["thread_id"], "B 的消息", share=True)

        self.assertFalse(local["share"])
        self.assertTrue(shared["share"])
        page = tasks.read_thread(a["thread_id"], limit=1)
        self.assertEqual([row["id"] for row in page["messages"]], [local["id"]])
        self.assertTrue(page["has_more"])
        self.assertEqual(
            [row["id"] for row in tasks.read_thread(a["thread_id"], after_id=page["next_after_id"])["messages"]],
            [shared["id"]],
        )
        with self.assertRaises(ValueError):
            tasks.read_thread(a["thread_id"], after_id=other["id"])
        self.assertEqual(tasks.list_tasks(status="pending")[0]["task_id"], b["task_id"])
        self.assertEqual(tasks.list_tasks(status="active")[0]["message_count"], 2)
        self.assertEqual(events.read_all_events()[0], legacy)

    def test_claims_are_serialized_and_archived_conversations_are_read_only(self):
        from bridge import tasks

        task = tasks.create_task("网页新任务", actor="user")

        def claim():
            try:
                return tasks.update_task_status(task["task_id"], "active")["to"]
            except ValueError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(claim)
            second = pool.submit(claim)
            self.assertCountEqual([first.result(), second.result()], ["active", "rejected"])

        tasks.update_task_status(task["task_id"], "archived")
        with self.assertRaises(ValueError):
            tasks.update_task_status(task["task_id"], "active")
        with self.assertRaises(ValueError):
            tasks.post_message(task["thread_id"], "归档后追加")
        self.assertEqual([row["to"] for row in tasks.read_thread(task["thread_id"])["messages"]], ["active", "archived"])

    def test_invalid_title_or_sharing_flag_writes_nothing(self):
        from bridge import tasks

        with self.assertRaises(ValueError):
            tasks.create_task("标" * 201)
        task = tasks.create_task("有效任务")
        with self.assertRaises(ValueError):
            tasks.post_message(task["thread_id"], "内容", share="false")
        self.assertEqual([row["kind"] for row in events.read_all_events()], ["task_create"])
