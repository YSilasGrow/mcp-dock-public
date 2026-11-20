"""Invoke the user's signed-in DeepSeek Harness from the local MCP server."""

import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .workspace import MAX_FILE_BYTES, Workspace, WorkspaceError


DSH = Path("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh")
PATCH = Path(__file__).resolve().parent.parent / "dsh-auth.patch.yml"
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_CHANGES = 20
# A call only has to be received: Harness must report that it started working on the task.
# After that it may search and think for as long as it needs; the caller can stop waiting.
HARNESS_RECEIPT_TIMEOUT_SECONDS = 120
# Upper bound for an optional, caller-chosen total limit. No limit applies by default.
HARNESS_TIMEOUT_SECONDS = 600
MAX_ERROR_CHARS = 500
STDERR_TAIL_BYTES = 8192
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PRIVATE_KEY_BEGIN = r"-----BEGIN [^\r\n]*PRIVATE KEY-----"
_PRIVATE_KEY_END = r"-----END [^\r\n]*PRIVATE KEY-----"
_PRIVATE_KEY_BLOCK = re.compile(rf"{_PRIVATE_KEY_BEGIN}[\s\S]*?(?:{_PRIVATE_KEY_END}|\Z)")
_CREDENTIAL_FIELD = re.compile(
    r"(?i)(?<![a-z0-9])(?:authorization|(?:set[-_ ]?)?cookie|"
    r"(?:api|access|private|secret)[-_ ]?key|secret|credentials?|"
    r"password|passwd|passphrase|(?:oauth|access|refresh|session)[-_ ]?token|token)"
    r"""(?:\\?["'])?\s*[:=]""",
)
_CREDENTIAL_VALUE = re.compile(
    r"(?i)\b(?:bearer|basic)\s+\S+|--(?:api[-_]?key|token|password)\s+\S+"
    r"|\b[a-z][a-z0-9+.-]*://[^\s]*(?:@|\?|#)[^\s]*"
    r"|\b(?:sk-(?:proj-)?|gh[pousr]_)[a-z0-9_-]{8,}|\beyJ[\w-]+\.[\w-]+\.[\w-]+",
)


class HarnessError(RuntimeError):
    pass


class HarnessCancelled(HarnessError):
    """The owner stopped waiting. The task was received, so usage may already exist."""


def validate_timeout_seconds(value):
    """An explicit total limit is optional: None means wait until DeepSeek answers."""
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= HARNESS_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds 可省略（不限时），或为 1 到 {HARNESS_TIMEOUT_SECONDS} 的整数")
    return value


def _is_progress(event):
    # dsh-headless --json emits committed model text and tool/step boundaries.
    kind = event.get("type")
    if kind == "status":
        return event.get("phase") in ("turn_start", "step_start", "step_end", "turn_end")
    if kind in ("thinking", "text", "final"):
        return isinstance(event.get("text"), str) and bool(event["text"].strip())
    if kind == "tool_call":
        return bool(event.get("callId") and event.get("tool"))
    if kind == "tool_result":
        return bool(event.get("callId") and event.get("status") in ("completed", "error"))
    return False


def _environment(mode, overrides=None):
    keep = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL", "DSH_HOME")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update(overrides or {})
    env["DSH_PERMISSION_MODE"] = mode
    return env


def _safe_error_text(text):
    text = _ANSI_ESCAPE.sub("", text)
    text = _PRIVATE_KEY_BLOCK.sub("[凭据已隐藏]", text)
    # A bounded tail can start inside a key block. Discard through its end.
    text = re.split(_PRIVATE_KEY_END, text)[-1]
    lines = []
    hide_next_line = hide_indented = False
    for raw_line in text.splitlines():
        line = "".join(char for char in raw_line if char.isprintable() or char == "\t").strip()
        if not line:
            continue
        if hide_next_line or (hide_indented and raw_line[:1].isspace()):
            hide_next_line = False
            continue
        field = _CREDENTIAL_FIELD.search(line)
        hide_next_line = field is not None and not line[field.end():].strip()
        hide_indented = field is not None or _CREDENTIAL_VALUE.search(line) is not None
        lines.append("[凭据已隐藏]" if hide_indented else line)
    return "\n".join(lines)


def _failure_message(summary, stderr, detail=""):
    detail = _safe_error_text(detail)[:150]
    if detail:
        summary = f"{summary}: {detail}"
    size = stderr.seek(0, os.SEEK_END)
    start = max(0, size - STDERR_TAIL_BYTES)
    stderr.seek(start)
    raw_tail = stderr.read(size - start)
    if start:
        raw_tail = raw_tail.partition(b"\n")[2]  # The first line may be partial.
    safe_tail = _safe_error_text(raw_tail.decode("utf-8", errors="replace"))
    tail = "\n".join(safe_tail.splitlines()[-4:])
    if not tail:
        return summary
    label = "\nstderr:\n"
    room = MAX_ERROR_CHARS - len(summary) - len(label)
    if len(tail) > room:
        tail = "…" + tail[-(room - 1):]
    return summary + label + tail


def _run(task, cwd, *, mode, on_event=None, timeout_seconds=None, extra_patch=None, environment=None,
         on_receipt=None, cancel=None):
    """Run one Harness task.

    The task must be received: Harness has to report that it started working within
    HARNESS_RECEIPT_TIMEOUT_SECONDS. After that the call waits for the answer with no time
    limit, unless the caller passed timeout_seconds or sets the optional `cancel` event.
    """
    if not DSH.is_file():
        raise HarnessError("DeepSeek Harness CLI was not found in Applications")
    if not PATCH.is_file():
        raise HarnessError("DeepSeek Harness account patch is missing")
    hard_limit = validate_timeout_seconds(timeout_seconds)
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    final = None
    last_error = ""
    args = [str(DSH), "--profile", "headless", "--patch", str(PATCH)]
    if extra_patch is not None:
        args.extend(["--patch", str(extra_patch)])
    with tempfile.TemporaryFile() as stderr:
        started = time.monotonic()
        hard_deadline = None if hard_limit is None else started + hard_limit
        receipt_deadline = started + HARNESS_RECEIPT_TIMEOUT_SECONDS
        received = False
        stopped = None  # "receipt", "hard" or "cancelled"
        process = subprocess.Popen(
            [*args, "--json", task],
            cwd=cwd,
            env=_environment(mode, environment),
            stdout=subprocess.PIPE,
            stderr=stderr,
            start_new_session=True,
        )
        lines = queue.Queue(maxsize=32)
        stop_reader = threading.Event()

        def enqueue(line):
            while not stop_reader.is_set():
                try:
                    lines.put(line, timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        def pump_stdout():
            try:
                for line in process.stdout:
                    if not enqueue(line):
                        break
            finally:
                enqueue(None)

        reader = threading.Thread(target=pump_stdout, daemon=True)
        reader.start()
        completed = False
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    stopped = "cancelled"
                    break
                now = time.monotonic()
                if hard_deadline is not None and now >= hard_deadline:
                    stopped = "hard"
                    break
                if not received and now >= receipt_deadline:
                    stopped = "receipt"
                    break
                limits = [d for d in (hard_deadline, None if received else receipt_deadline) if d is not None]
                wait = min(limits) - now if limits else None
                if cancel is not None:
                    wait = 0.25 if wait is None else min(wait, 0.25)
                try:
                    line = lines.get(timeout=wait)
                except queue.Empty:
                    continue
                if line is None:
                    # Output closed: the CLI is exiting. A short wait is enough here.
                    returncode = process.wait(timeout=10)
                    completed = True
                    break
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if not received and _is_progress(event):
                    received = True
                    if on_receipt is not None:
                        on_receipt()
                kind = event.get("type")
                if kind == "final":
                    final = event.get("text")
                elif kind == "error":
                    last_error = event.get("message") or event.get("error") or ""
                elif kind == "status" and event.get("phase") == "step_end":
                    step = event.get("usage")
                    if isinstance(step, dict):
                        for key, target in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"), ("totalTokens", "total_tokens")):
                            if type(step.get(key)) is int:
                                usage[target] += step[key]
                if on_event is not None:
                    on_event(event)
        except subprocess.TimeoutExpired:
            stopped = "exit"
        finally:
            stop_reader.set()
            if not completed:
                # The CLI may have exited while a child still holds stdout open.
                # Kill the whole session so the reader can reach EOF promptly.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            reader.join(timeout=1)
            if not reader.is_alive():
                process.stdout.close()
        if not completed:
            if stopped == "cancelled":
                raise HarnessCancelled("已停止等待 DeepSeek（主人手动停止）。"
                                       + ("对方已收到，可能已产生用量。" if received else "对方尚未确认收到。"))
            if stopped == "hard":
                summary = f"DeepSeek Harness task timed out after {hard_limit:g} seconds (hard deadline)"
            elif stopped == "receipt":
                summary = (f"DeepSeek Harness did not confirm receipt within "
                           f"{HARNESS_RECEIPT_TIMEOUT_SECONDS:g} seconds (not received)")
            else:
                summary = "DeepSeek Harness closed its output but did not exit"
            raise HarnessError(_failure_message(summary, stderr))
        if returncode != 0:
            summary = f"DeepSeek Harness: task failed (exit {returncode})"
            raise HarnessError(_failure_message(summary, stderr, str(last_error)))
        if not isinstance(final, str):
            raise HarnessError(_failure_message("DeepSeek Harness returned no final answer", stderr))
    return {"answer": final, "usage": usage}


def consult(question, on_event=None):
    if not isinstance(question, str) or not question.strip() or len(question) > 20000:
        raise ValueError("question must be 1 to 20000 characters")
    with tempfile.TemporaryDirectory(prefix="deepseek-consult-") as cwd:
        task = "独立回答以下问题。不要调用本地工具或读取文件；说明不确定之处。\n\n" + question
        return _run(task, cwd, mode="read-only", on_event=on_event)


def consult_prepared(task, on_event=None, *, timeout_seconds=None, on_receipt=None):
    """Run the exact already-logged research task without appending a prefix."""
    if not isinstance(task, str) or not task.strip() or len(task) > 20000:
        raise ValueError("task must be 1 to 20000 characters")
    with tempfile.TemporaryDirectory(prefix="deepseek-research-") as cwd:
        return _run(task, cwd, mode="read-only", on_event=on_event, timeout_seconds=timeout_seconds,
                    on_receipt=on_receipt)


def delegate(task, workspace: Workspace, on_event=None):
    if not isinstance(task, str) or not task.strip() or len(task) > 10000:
        raise ValueError("task must be 1 to 10000 characters")
    files = workspace.list_files()
    if len(files) >= 200:
        raise WorkspaceError("project has at least 200 eligible files; choose a smaller project directory")
    with tempfile.TemporaryDirectory(prefix="deepseek-delegate-") as cwd:
        snapshot = Path(cwd)
        before = {}
        total = 0
        for name in files:
            try:
                item = workspace.read_file(name)
            except WorkspaceError as exc:
                if "only UTF-8" in str(exc):
                    continue
                raise
            data = item["content"].encode("utf-8")
            total += len(data)
            if total > MAX_SNAPSHOT_BYTES:
                raise WorkspaceError("project snapshot exceeds 4 MiB; choose a smaller project directory")
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            before[name] = item["sha256"]
        prompt = (
            "你是大肥鱼，负责执行小龙女分配的一项任务。当前目录是该项目的临时副本。"
            "只操作当前目录；不要访问账户、网络、原始项目或当前目录以外的文件。"
            "不要运行本机命令或测试；这些由小龙女负责。"
            "可以修改副本中的文件；修改结果将由桥接程序验证并复制回原项目，再由小龙女复核。"
            "不要删除文件，不要修改 AGENTS.md、CLAUDE.md、GEMINI.md、SKILL.md。"
            "完成后简要说明修改内容和未完成事项。\n\n任务：" + task
        )
        result = _run(prompt, cwd, mode="workspace-write", on_event=on_event)
        candidates = []
        seen = set()
        for base, dirs, names in os.walk(snapshot, followlinks=False):
            dirs[:] = [name for name in dirs if not name.startswith(".") and not (Path(base) / name).is_symlink()]
            for filename in names:
                path = Path(base) / filename
                name = path.relative_to(snapshot).as_posix()
                if name.startswith(".") or path.is_symlink():
                    continue
                workspace._path(name)
                if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                    if name in before:
                        raise WorkspaceError(f"DeepSeek produced an unsupported file: {name}")
                    continue
                data = path.read_bytes()
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    if name in before:
                        raise WorkspaceError(f"DeepSeek produced a non-UTF-8 file: {name}") from exc
                    continue
                seen.add(name)
                old = before.get(name)
                if old is None or hashlib.sha256(data).hexdigest() != old:
                    if path.name.lower() in {"agents.md", "claude.md", "gemini.md", "skill.md"}:
                        raise WorkspaceError("DeepSeek changed an agent instruction file")
                    if not (workspace.project / name).parent.is_dir():
                        raise WorkspaceError(f"new parent directory is unsupported: {name}")
                    candidates.append((name, content, old or "new"))
        if set(before) - seen:
            raise WorkspaceError("DeepSeek deleted a project file; no changes were applied")
        if len(candidates) > MAX_CHANGES:
            raise WorkspaceError("DeepSeek changed more than 20 files; no changes were applied")
        for name, _, expected in candidates:
            current = workspace._path(name)
            if expected == "new" and current.exists():
                raise WorkspaceError(f"new file already exists: {name}")
            if expected != "new" and workspace.read_file(name)["sha256"] != expected:
                raise WorkspaceError(f"project file changed during delegation: {name}")
        for name, content, expected in candidates:
            workspace.write_file(name, content, expected)
        return {"status": "completed", "summary": result["answer"], "changes": workspace.changes, "usage": result["usage"]}
