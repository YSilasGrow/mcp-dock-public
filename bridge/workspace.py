"""Narrow file and check access for a delegated DeepSeek task."""

import hashlib
import os
import tempfile
from pathlib import Path


DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "projects"
MAX_FILE_BYTES = 128 * 1024
BLOCKED_NAMES = {".env", "id_rsa", "id_ed25519", "credentials", "secrets"}
BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
READ_ONLY_INSTRUCTIONS = {"agents.md", "claude.md", "gemini.md", "skill.md"}


class WorkspaceError(ValueError):
    pass


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Workspace:
    def __init__(self, project_dir, *, allowed_root=DEFAULT_ROOT):
        root_input = Path(allowed_root).expanduser().absolute()
        root = root_input.resolve(strict=True)
        supplied = Path(project_dir).expanduser()
        if not supplied.is_absolute():
            raise WorkspaceError("project_dir must be absolute")
        try:
            lexical = supplied.relative_to(root_input)
        except ValueError as exc:
            raise WorkspaceError("project_dir is outside the allowed root") from exc
        if ".." in lexical.parts:
            raise WorkspaceError("project_dir must not contain '..'")
        cursor = root_input
        for part in lexical.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise WorkspaceError("symlinked project directories are not allowed")
        project = supplied.resolve(strict=True)
        try:
            relative = project.relative_to(root)
        except ValueError as exc:
            raise WorkspaceError("project_dir is outside the allowed root") from exc
        if not relative.parts or not project.is_dir():
            raise WorkspaceError("project_dir must be an existing subdirectory")
        self.project = project
        self.changes = []
        self.backup_dir = None

    def _path(self, name):
        if not isinstance(name, str) or not name or "\x00" in name:
            raise WorkspaceError("invalid path")
        relative = Path(name)
        if relative.is_absolute() or any(part in (".", "..") for part in relative.parts):
            raise WorkspaceError("path must stay inside the project")
        if any(part.startswith(".") or part.lower() in BLOCKED_NAMES for part in relative.parts):
            raise WorkspaceError("hidden or credential paths are unavailable")
        if relative.suffix.lower() in BLOCKED_SUFFIXES:
            raise WorkspaceError("credential files are unavailable")
        cursor = self.project
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise WorkspaceError("symlinks are unavailable")
        try:
            cursor.resolve(strict=False).relative_to(self.project)
        except ValueError as exc:
            raise WorkspaceError("path must stay inside the project") from exc
        return cursor

    def list_files(self):
        result = []
        for base, dirs, files in os.walk(self.project, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs if not d.startswith(".") and not (Path(base) / d).is_symlink()
            )
            for filename in sorted(files):
                path = Path(base) / filename
                relative = path.relative_to(self.project).as_posix()
                try:
                    self._path(relative)
                except WorkspaceError:
                    continue
                if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
                    result.append(relative)
                    if len(result) >= 200:
                        return result
        return result

    def read_file(self, name):
        path = self._path(name)
        if not path.is_file():
            raise WorkspaceError("file does not exist")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise WorkspaceError("file exceeds 128 KiB")
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise WorkspaceError("file exceeds 128 KiB")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError("only UTF-8 text files are available") from exc
        return {"path": name, "content": content, "sha256": _digest(data)}

    def write_file(self, name, content, expected_sha256):
        path = self._path(name)
        if path.name.lower() in READ_ONLY_INSTRUCTIONS:
            raise WorkspaceError("agent instruction files are read-only")
        if not isinstance(content, str):
            raise WorkspaceError("content must be text")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise WorkspaceError("file exceeds 128 KiB")
        if not path.parent.is_dir():
            raise WorkspaceError("parent directory does not exist")
        if path.exists():
            if not path.is_file():
                raise WorkspaceError("target is not a regular file")
            old = path.read_bytes()
            before = _digest(old)
            if expected_sha256 != before:
                raise WorkspaceError("file changed; read it again before writing")
            mode = path.stat().st_mode & 0o777
        else:
            if expected_sha256 != "new":
                raise WorkspaceError("new files require expected_sha256='new'")
            before = None
            mode = 0o600
        backup_path = None
        if before is not None:
            if self.backup_dir is None:
                self.backup_dir = Path(tempfile.mkdtemp(prefix="deepseek-bridge-"))
            with tempfile.NamedTemporaryFile(dir=self.backup_dir, prefix="before-", delete=False) as backup:
                backup.write(old)
                backup_path = backup.name
            os.chmod(backup_path, 0o600)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".ds-write-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            os.chmod(temporary, mode)
            if before is not None and _digest(path.read_bytes()) != before:
                raise WorkspaceError("file changed during write")
            if before is None and path.exists():
                raise WorkspaceError("new file appeared during write")
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        change = {"path": name, "before_sha256": before, "after_sha256": _digest(data), "backup_path": backup_path}
        self.changes.append(change)
        return change

