import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from bridge.workspace import Workspace, WorkspaceError


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def test_read_and_hash_checked_write(self):
        target = self.project / "note.txt"
        target.write_text("first", encoding="utf-8")
        ws = Workspace(self.project, allowed_root=self.root)
        original = ws.read_file("note.txt")
        self.assertEqual(original["content"], "first")
        self.assertEqual(original["sha256"], hashlib.sha256(b"first").hexdigest())
        changed = ws.write_file("note.txt", "second", original["sha256"])
        self.addCleanup(shutil.rmtree, ws.backup_dir)
        self.assertEqual(changed["before_sha256"], original["sha256"])
        self.assertEqual(Path(changed["backup_path"]).read_text(encoding="utf-8"), "first")
        self.assertEqual(target.read_text(encoding="utf-8"), "second")
        with self.assertRaises(WorkspaceError):
            ws.write_file("note.txt", "stale", original["sha256"])
        self.assertEqual(target.read_text(encoding="utf-8"), "second")

    def test_new_file_requires_new_marker_and_lists_visible_text(self):
        (self.project / ".env").write_text("SECRET=hidden", encoding="utf-8")
        ws = Workspace(self.project, allowed_root=self.root)
        with self.assertRaises(WorkspaceError):
            ws.write_file("new.txt", "hello", "wrong")
        ws.write_file("new.txt", "hello", "new")
        self.assertEqual(ws.list_files(), ["new.txt"])
        self.assertNotIn(".env", ws.list_files())

    def test_project_instructions_can_be_read_but_not_rewritten(self):
        (self.project / "AGENTS.md").write_text("project instructions", encoding="utf-8")
        ws = Workspace(self.project, allowed_root=self.root)
        original = ws.read_file("AGENTS.md")
        self.assertEqual(original["content"], "project instructions")
        with self.assertRaises(WorkspaceError):
            ws.write_file("AGENTS.md", "changed", original["sha256"])

    def test_rejects_outside_and_symlink_paths(self):
        outside = self.root / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        (self.project / "linked.txt").symlink_to(outside)
        ws = Workspace(self.project, allowed_root=self.root)
        for path in ("../outside.txt", "linked.txt", ".env", "id_rsa"):
            with self.subTest(path=path), self.assertRaises(WorkspaceError):
                ws.read_file(path)
        linked_project = self.root / "linked-project"
        linked_project.symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(WorkspaceError):
            Workspace(linked_project, allowed_root=self.root)
        (self.project / "nested").mkdir()
        with self.assertRaises(WorkspaceError):
            Workspace(linked_project / "nested", allowed_root=self.root)
        with self.assertRaises(WorkspaceError):
            Workspace(self.root, allowed_root=self.root)

if __name__ == "__main__":
    unittest.main()
