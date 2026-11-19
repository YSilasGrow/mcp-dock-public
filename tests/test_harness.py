import fcntl
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge import harness
from bridge.workspace import Workspace, WorkspaceError


class HarnessTests(unittest.TestCase):
    def _fake_harness(self, root, body):
        executable = Path(root) / "fake-dsh"
        executable.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        executable.chmod(0o700)
        patch_file = Path(root) / "patch.yml"
        patch_file.write_text("[]", encoding="utf-8")
        return executable, patch_file

    def test_run_reports_status_before_final_reply(self):
        with tempfile.TemporaryDirectory() as root:
            executable, patch_file = self._fake_harness(
                root,
                'import json, time\n'
                'print(json.dumps({"type":"status","phase":"step_start"}), flush=True)\n'
                'time.sleep(0.5)\n'
                'print(json.dumps({"type":"final","text":"真实答复"}), flush=True)\n',
            )
            received = threading.Event()
            result = {}

            def invoke():
                result.update(harness._run("test", root, mode="read-only", on_event=lambda event: received.set()))

            with patch.object(harness, "DSH", executable), patch.object(harness, "PATCH", patch_file):
                worker = threading.Thread(target=invoke)
                worker.start()
                self.assertTrue(received.wait(timeout=2))
                self.assertTrue(worker.is_alive(), "status must arrive before final reply")
                worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result["answer"], "真实答复")

    def test_run_times_out_even_when_harness_emits_no_lines(self):
        with tempfile.TemporaryDirectory() as root:
            executable, patch_file = self._fake_harness(root, "import time\ntime.sleep(10)\n")
            started = time.monotonic()
            with patch.object(harness, "DSH", executable), patch.object(harness, "PATCH", patch_file), patch.object(harness, "HARNESS_TIMEOUT_SECONDS", 0.2):
                with self.assertRaisesRegex(harness.HarnessError, "timed out"):
                    harness._run("test", root, mode="read-only")
            self.assertLess(time.monotonic() - started, 2)

    def test_run_timeout_releases_descendant_lock_before_returning(self):
        with tempfile.TemporaryDirectory() as root:
            lock = Path(root) / "profile.lock"
            ready = Path(root) / "lock-acquired"
            descendant = (
                "import fcntl, time\n"
                "from pathlib import Path\n"
                f"with open({str(lock)!r}, 'w') as handle:\n"
                "    fcntl.flock(handle, fcntl.LOCK_EX)\n"
                f"    Path({str(ready)!r}).touch()\n"
                "    time.sleep(3)\n"
            )
            executable, patch_file = self._fake_harness(
                root,
                'import json, subprocess, sys\n'
                f'subprocess.Popen([sys.executable, "-c", {descendant!r}])\n'
                'print(json.dumps({"type":"status","phase":"step_start"}), flush=True)\n',
            )
            started = time.monotonic()
            with patch.object(harness, "DSH", executable), patch.object(harness, "PATCH", patch_file), patch.object(harness, "HARNESS_TIMEOUT_SECONDS", 0.5):
                with self.assertRaisesRegex(harness.HarnessError, "timed out"):
                    harness._run("test", root, mode="read-only")
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(ready.is_file(), "descendant must have acquired the lock")
            with lock.open("r") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_run_ignores_malformed_stdout_and_keeps_final(self):
        with tempfile.TemporaryDirectory() as root:
            executable, patch_file = self._fake_harness(
                root,
                'print("not json", flush=True)\nprint("{\\\"type\\\":\\\"final\\\",\\\"text\\\":\\\"ok\\\"}", flush=True)\n',
            )
            with patch.object(harness, "DSH", executable), patch.object(harness, "PATCH", patch_file):
                self.assertEqual(harness._run("test", root, mode="read-only")["answer"], "ok")

    def test_run_ignores_non_utf8_line_and_keeps_final(self):
        with tempfile.TemporaryDirectory() as root:
            executable, patch_file = self._fake_harness(
                root,
                'import os\n'
                'os.write(1, b"\\xff\\n")\n'
                'os.write(1, b"{\\\"type\\\":\\\"final\\\",\\\"text\\\":\\\"ok\\\"}\\n")\n',
            )
            with patch.object(harness, "DSH", executable), patch.object(harness, "PATCH", patch_file):
                self.assertEqual(harness._run("test", root, mode="read-only")["answer"], "ok")

    def test_consult_uses_empty_temporary_directory(self):
        def fake_run(task, cwd, *, mode, on_event=None):
            self.assertEqual(mode, "read-only")
            self.assertEqual(list(Path(cwd).iterdir()), [])
            self.assertIn("问题", task)
            return {"answer": "答复", "usage": {}}

        with patch.object(harness, "_run", side_effect=fake_run):
            self.assertEqual(harness.consult("问题")["answer"], "答复")

    def test_delegate_copies_back_only_changed_files(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "project"
            project.mkdir()
            (project / "note.txt").write_text("old", encoding="utf-8")
            (project / "AGENTS.md").write_text("rules", encoding="utf-8")
            workspace = Workspace(project, allowed_root=root)

            def fake_run(task, cwd, *, mode, on_event=None):
                self.assertEqual(mode, "workspace-write")
                self.assertEqual((Path(cwd) / "note.txt").read_text(), "old")
                (Path(cwd) / "note.txt").write_text("new", encoding="utf-8")
                return {"answer": "done", "usage": {}}

            with patch.object(harness, "_run", side_effect=fake_run):
                result = harness.delegate("edit", workspace)
            self.assertEqual(result["status"], "completed")
            self.assertEqual((project / "note.txt").read_text(), "new")
            self.assertEqual((project / "AGENTS.md").read_text(), "rules")
            self.assertEqual(len(result["changes"]), 1)

    def test_instruction_edit_is_rejected_before_any_copy_back(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "project"
            project.mkdir()
            (project / "note.txt").write_text("old")
            (project / "AGENTS.md").write_text("rules")
            workspace = Workspace(project, allowed_root=root)

            def fake_run(task, cwd, *, mode, on_event=None):
                (Path(cwd) / "note.txt").write_text("new")
                (Path(cwd) / "AGENTS.md").write_text("changed")
                return {"answer": "done", "usage": {}}

            with patch.object(harness, "_run", side_effect=fake_run):
                with self.assertRaises(WorkspaceError):
                    harness.delegate("edit", workspace)
            self.assertEqual((project / "note.txt").read_text(), "old")

    def test_binary_source_and_generated_binary_are_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "project"
            project.mkdir()
            (project / "note.txt").write_text("old")
            (project / "image.bin").write_bytes(b"\xff\xfe")
            workspace = Workspace(project, allowed_root=root)

            def fake_run(task, cwd, *, mode, on_event=None):
                self.assertFalse((Path(cwd) / "image.bin").exists())
                (Path(cwd) / "note.txt").write_text("new")
                (Path(cwd) / "cache.bin").write_bytes(b"\xff\xfe")
                return {"answer": "done", "usage": {}}

            with patch.object(harness, "_run", side_effect=fake_run):
                result = harness.delegate("edit", workspace)
            self.assertEqual((project / "note.txt").read_text(), "new")
            self.assertFalse((project / "cache.bin").exists())
            self.assertEqual(len(result["changes"]), 1)

    def test_environment_does_not_forward_api_secrets(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret", "HOME": "/tmp/home"}):
            env = harness._environment("read-only")
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertEqual(env["HOME"], "/tmp/home")


if __name__ == "__main__":
    unittest.main()
