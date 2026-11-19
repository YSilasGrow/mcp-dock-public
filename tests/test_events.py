import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge import events


class EventReliabilityTests(unittest.TestCase):
    def test_read_cursor_stops_before_unterminated_line(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            with patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(path)}):
                events.append_event("codex", "question", "complete")
                end = path.stat().st_size
                with path.open("ab") as stream:
                    stream.write(b'{"kind":"research_claim"')
                result = events.read_events()
                self.assertEqual([row["text"] for row in result["events"]], ["complete"])
                self.assertEqual(result["next"], end)

    def test_recover_partial_tail_to_private_file_and_append_marker(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            with patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(path)}):
                events.append_event("codex", "question", "before")
                cursor = path.stat().st_size
                with path.open("ab") as stream:
                    stream.write(b'{"private":"half"')
                events.append_event("deepseek", "answer", "after")
                result = events.read_events(after=cursor)
                self.assertEqual([row["kind"] for row in result["events"]], ["recovery", "answer"])
                self.assertEqual(result["events"][-1]["text"], "after")
                self.assertEqual(events.read_events(after=result["next"])["events"], [])
                quarantines = list(path.parent.glob("events.jsonl.partial-*.bin"))
                self.assertEqual(len(quarantines), 1)
                self.assertEqual(quarantines[0].read_bytes(), b'{"private":"half"')
                self.assertEqual(stat.S_IMODE(quarantines[0].stat().st_mode), 0o600)

    def test_legacy_and_research_writers_share_lock_and_callback_sees_prior_event(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            with patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(path)}):
                legacy = events.append_event("codex", "question", "old")
                candidate = {"role": "codex", "kind": "research_start", "text": "new", "research_id": "r1", "v": 1}
                seen = []

                def check(rows):
                    seen.extend(rows)
                    return next((row for row in rows if row.get("research_id") == "r1"), None)

                first = events.append_research_event(candidate, check)
                second = events.append_research_event(candidate, check)
                self.assertEqual(first, second)
                self.assertEqual(len(events.read_all_events()), 2)
                self.assertEqual(seen[0]["id"], legacy["id"])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_short_write_leaves_recoverable_partial_line(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            with patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(path)}):
                events.append_event("codex", "question", "before")
                real_write = os.write

                def short_write(fd, data):
                    return real_write(fd, data[:len(data) // 2])

                with patch.object(events.os, "write", side_effect=short_write):
                    with self.assertRaises(OSError):
                        events.append_event("codex", "question", "interrupted")
                events.append_event("codex", "question", "after")
                stored = events.read_all_events()
                self.assertEqual([row["kind"] for row in stored], ["question", "recovery", "question"])
                self.assertEqual(stored[-1]["text"], "after")

    def test_parallel_readers_do_not_duplicate_incremental_cache_rows(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            with patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(path)}):
                events.append_event("codex", "question", "first")
                external = {"id": "external", "at": "2026-11-16T00:00:00+00:00", "role": "system",
                            "kind": "status", "text": "second", "call_id": ""}
                with path.open("ab") as stream:
                    stream.write((json.dumps(external) + "\n").encode())
                barrier = threading.Barrier(2)
                original = events._read_rows

                def paused_read(fd, start=0, limit=None):
                    if start and limit is None:
                        barrier.wait(timeout=3)
                    return original(fd, start, limit)

                with patch.object(events, "_read_rows", side_effect=paused_read):
                    workers = [threading.Thread(target=events.read_all_events) for _ in range(2)]
                    for worker in workers:
                        worker.start()
                    for worker in workers:
                        worker.join(timeout=5)
                self.assertEqual([row["text"] for row in events.read_all_events()], ["first", "second"])
