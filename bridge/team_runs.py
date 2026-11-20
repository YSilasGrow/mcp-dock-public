"""Durable local scheduling for simulation and the optional DeepSeek assistant."""

import fcntl
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .team_assistant import consult_assistant, inspect_question
from .harness import HarnessCancelled, HarnessError
from .team_template import bind_inputs, default_template, evaluate, validate_template


SCENARIOS = {"success", "timeout", "missing", "limit", "choice", "review", "test_failure"}
MAX_REWORK = 2
# DeepSeek calls wait without a time limit once received. The owner can stop waiting; the
# stop signal reaches the worker thread of this process through the run's event.
_WAITING = {}
_WAITING_LOCK = threading.Lock()
SUMMARY = ("id", "task_id", "root_id", "parent_id", "round", "status", "lead", "scenario",
           "created_at", "updated_at", "cloud_calls", "assistant")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _event(run, event, message, node=None):
    run["updated_at"] = _now()
    item = {"at": run["updated_at"], "event": event, "message": message}
    if node is not None:
        item["node"] = node
    run["history"].append(item)


def _wait(run, node, kind, message, actions):
    run["status"] = "waiting"
    run["help"] = {"id": uuid.uuid4().hex, "node": node, "kind": kind,
                   "message": message, "actions": actions}
    _event(run, "waiting", message, node)


def _new_run(task_id, scenario, brief, lead, template, parent=None, *, assistant="off", assistant_question=""):
    run_id = uuid.uuid4().hex
    revision = {}
    inherited = None
    if parent:
        outputs = {a["node"]: a["output"] for a in parent["attempts"]}
        assistant = parent.get("assistant", "off")
        assistant_question = parent.get("assistant_question", "")
        if assistant == "deepseek" and "opinion" in outputs:
            inherited = dict(outputs["opinion"])
            inherited.setdefault("from_run_id", parent["id"])
        revision = {"parent_id": parent["id"], "patch": outputs.get("lead", {}).get("patch"),
                    "review": outputs.get("review", {}).get("review"),
                    "checks": outputs.get("verify", {}).get("checks")}
    run = {"id": run_id, "root_id": parent["root_id"] if parent else run_id,
           "parent_id": parent["id"] if parent else None, "round": parent["round"] + 1 if parent else 1,
           "task_id": task_id, "template": template, "brief": brief, "lead": lead,
           "assistant": assistant, "assistant_question": assistant_question,
           "initial_assistant_question": assistant_question, "inherited_opinion": inherited,
           "scenario": scenario, "status": "running", "help": None, "history": [],
           "decisions": [], "attempts": [], "choices": {}, "cloud_calls": 0, "created_at": _now(),
           "source": {"brief": "" if scenario == "missing" and not parent else brief, "revision": revision}}
    _event(run, "started", "已保存模板与本轮材料。" + ("已选择 DeepSeek 助手。" if assistant == "deepseek" else "开始本地模拟。"))
    try:
        validate_template(template)
    except ValueError as exc:
        _wait(run, "template", "missing", str(exc), ["stop"])
    if run["status"] == "running" and not run["source"]["brief"].strip():
        _wait(run, "lead", "missing", "主做的 lead.brief 缺少任务材料。请补充后继续。", ["supply", "stop"])
    return run


def _bounded_text(value, name, max_length):
    if (not isinstance(value, str) or len(value) > max_length or "\x00" in value
            or any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)):
        raise ValueError(f"{name}须为最多 {max_length} 字的有效文本")
    return value


class RunStore:
    def __init__(self, dbpath):
        self.path = Path(dbpath)

    @contextmanager
    def _execution(self, *, recover=False):
        # Readers of this lock may run concurrently. Only startup recovery takes
        # it exclusively, so it can never interrupt a live dispatch in another server.
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path.parent / "execution.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if recover else fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _connection(self, *, write=False):
        if write:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=15)
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("""CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, request_id TEXT,
                    parent_id TEXT UNIQUE, body TEXT NOT NULL, UNIQUE(task_id, request_id))""")
            yield conn
            if write:
                conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _read(conn, run_id):
        row = conn.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("找不到这次模拟运行")
        return json.loads(row[0])

    @staticmethod
    def _save(conn, run):
        conn.execute("UPDATE runs SET body=? WHERE id=?",
                     (json.dumps(run, ensure_ascii=False), run["id"]))

    @staticmethod
    def _insert(conn, run, request_id=None):
        conn.execute("INSERT INTO runs VALUES (?,?,?,?,?)",
                     (run["id"], run["task_id"], request_id, run["parent_id"],
                      json.dumps(run, ensure_ascii=False)))

    @staticmethod
    def _initialized(conn):
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone() is not None

    def get(self, run_id):
        if not self.path.exists():
            raise ValueError("找不到这次模拟运行")
        with self._connection() as conn:
            if not self._initialized(conn):
                raise ValueError("找不到这次模拟运行")
            return self._read(conn, run_id)

    def list_runs(self, task_id):
        if not self.path.exists():
            return []
        with self._connection() as conn:
            if not self._initialized(conn):
                return []
            # Select summary fields only; a task card never loads the full attempt history.
            fields = ", ".join(f"json_extract(body, '$.{key}')" for key in SUMMARY)
            rows = conn.execute(f"SELECT {fields} FROM runs WHERE task_id=? ORDER BY rowid DESC", (task_id,))
            return [dict(zip(SUMMARY, row)) for row in rows]

    def start(self, task_id, request_id, scenario, brief, lead, *, assistant="off", assistant_question=""):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
            raise ValueError("启动请求编号无效")
        if not isinstance(scenario, str) or scenario not in SCENARIOS or lead not in ("codex", "claude"):
            raise ValueError("未知的模拟场景或主做角色")
        brief = _bounded_text(brief, "任务材料", 5000)
        assistant_question = _bounded_text(assistant_question, "给助手的问题", 5000)
        if assistant not in ("off", "deepseek") or (assistant == "off" and assistant_question):
            raise ValueError("助手和发送的问题不匹配")
        if assistant == "deepseek" and not assistant_question.strip():
            raise ValueError("请填写单独发给 DeepSeek 的问题")
        with self._execution():
            with self._connection(write=True) as conn:
                row = conn.execute("SELECT body FROM runs WHERE task_id=? AND request_id=?",
                                   (task_id, request_id)).fetchone()
                if row:
                    run = json.loads(row[0])
                    if ((run["scenario"], run["brief"], run["lead"], run.get("assistant", "off"),
                         run.get("initial_assistant_question", "")) !=
                            (scenario, brief, lead, assistant, assistant_question)):
                        raise ValueError("同一启动编号已用于不同的请求")
                    return run
                else:
                    run = _new_run(task_id, scenario, brief, lead, default_template(),
                                   assistant=assistant, assistant_question=assistant_question)
                    self._insert(conn, run, request_id)
            return self._advance(run)

    def _advance(self, run):
        if run["status"] != "running":
            return run
        if run.get("assistant") != "deepseek":
            return self._drive(run["id"])
        # The workbench owns the request. A slow cloud reply or browser reload
        # must not require replaying the HTTP POST.
        def work():
            with self._execution():
                self._drive(run["id"])
        threading.Thread(target=work, name="team-run-" + run["id"][:8], daemon=True).start()
        return run

    def _claim(self, conn, run):
        if run["status"] != "running" or any(a["status"] == "running" for a in run["attempts"]):
            return None
        outputs = {a["node"]: a["output"] for a in run["attempts"] if a["status"] in ("success", "skipped")}
        for node in run["template"]["nodes"]:
            if node not in outputs:
                break
        else:
            run["status"] = "completed"
            _event(run, "completed", "模拟复核和模拟验证通过，本轮模拟已完成。")
            self._save(conn, run)
            return None
        if run["scenario"] == "limit" and node == "review" and "extend" not in run["choices"]:
            _wait(run, node, "limit", "已用完本轮的 1 次模拟主力调用。可增加 1 次供复核使用。", ["extend", "stop"])
            self._save(conn, run)
            return None
        try:
            inputs = bind_inputs(run["template"], node, run["source"], outputs)
        except ValueError as exc:
            _wait(run, node, "missing", str(exc), ["stop"])
            self._save(conn, run)
            return None
        real_assistant = node == "opinion" and run.get("assistant") == "deepseek"
        sending = real_assistant and not run.get("inherited_opinion") and "skip_assistant" not in run["choices"]
        if real_assistant:
            inputs = {"question": run["assistant_question"]}
        if sending:
            hits = inspect_question(run["assistant_question"])
            if hits:
                _wait(run, node, "choice", "发送给 DeepSeek 前被拦截，尚未发送。" + "、".join(hits)
                      + "。请替换问题后复检，或收工。", ["replace_question", "stop"])
                self._save(conn, run)
                return None
            # Persist the submission claim before invoking the external process.
            run["cloud_calls"] += 1
        attempt = {"node": node, "number": 1 + sum(a["node"] == node for a in run["attempts"]),
                   "status": "running", "inputs": inputs, "output": {}, "started_at": _now()}
        if real_assistant:
            attempt["external_dispatch"] = bool(sending)
        run["attempts"].append(attempt)
        _event(run, "dispatched", "已登记发往 DeepSeek 的调用。" if sending else "已领取本地步骤。", node)
        self._save(conn, run)
        return attempt

    def _drive(self, run_id):
        while True:
            with self._connection(write=True) as conn:
                run = self._read(conn, run_id)
                attempt = self._claim(conn, run)
            if attempt is None:
                return run
            node = attempt["node"]
            if node == "opinion" and run.get("assistant") == "deepseek":
                result = self._assistant_result(run, attempt)
            else:
                try:
                    result = evaluate(node, attempt["inputs"], scenario=run["scenario"], round_number=run["round"],
                                      attempt=attempt["number"], choices=run["choices"], lead=run["lead"])
                except Exception as exc:
                    result = {"status": "failed", "output": {}, "help": {"kind": "error",
                              "message": f"模拟步骤异常（{type(exc).__name__}），已暂停。", "actions": ["retry", "stop"]}}
            with self._connection(write=True) as conn:
                run = self._read(conn, run_id)
                record = run["attempts"][-1]
                record.update(status=result["status"], output=result["output"], ended_at=_now())
                _event(run, result["status"],
                       {"success": "该步骤完成。", "skipped": "可选步骤已跳过。", "failed": "模拟步骤需要处理。"}[result["status"]], node)
                problem = result.get("help")
                if problem:
                    if "rework" in problem["actions"] and run["round"] >= 1 + MAX_REWORK:
                        _wait(run, node, "limit", "已追加返工 2 轮，达到上限。"
                              + problem["message"] + " 可保留本轮记录并收工。", ["stop"])
                    else:
                        _wait(run, node, problem["kind"], problem["message"], problem["actions"])
                self._save(conn, run)

    def _assistant_result(self, run, attempt):
        if run.get("inherited_opinion"):
            output = run["inherited_opinion"]
            return {"status": "success" if output.get("opinion") else "skipped", "output": output}
        if "skip_assistant" in run["choices"]:
            return {"status": "skipped", "output": {"opinion": [], "reason": "主人选择跳过助手。"}}

        def progress(event):
            phase = event.get("phase")
            if event.get("type") != "status" or phase not in ("step_start", "step_end"):
                return
            with self._connection(write=True) as conn:
                current = self._read(conn, run["id"])
                current["attempts"][-1]["progress"] = {"phase": phase, "at": _now()}
                self._save(conn, current)
        cancel = threading.Event()
        with _WAITING_LOCK:
            _WAITING[run["id"]] = cancel
        try:
            result = consult_assistant(attempt["inputs"]["question"], on_event=progress, cancel=cancel)
            answer = result["answer"]
            if not isinstance(answer, str) or not answer.strip():
                raise HarnessError("DeepSeek 未返回可用意见")
            return {"status": "success", "output": {"opinion": [answer], "usage": result.get("usage", {}),
                                                     "simulated": False, "provider": "deepseek"}}
        except HarnessCancelled as exc:
            return {"status": "failed", "output": {}, "help": {"kind": "choice",
                    "message": f"{exc} 本次不会重发，可跳过助手继续或收工。", "actions": ["skip_assistant", "stop"]}}
        except Exception as exc:
            reason = str(exc) if isinstance(exc, HarnessError) else type(exc).__name__
            return {"status": "failed", "output": {}, "help": {"kind": "error",
                    "message": f"DeepSeek 助手未完成（{reason}）。本次不会重发，可跳过助手继续或收工。",
                    "actions": ["skip_assistant", "stop"]}}
        finally:
            with _WAITING_LOCK:
                if _WAITING.get(run["id"]) is cancel:
                    del _WAITING[run["id"]]

    def stop_waiting(self, run_id):
        """Stop waiting for this run's DeepSeek reply. The run then asks the owner what next."""
        run = self.get(run_id)
        attempt = run["attempts"][-1] if run["attempts"] else None
        if run["status"] != "running" or not attempt or attempt["status"] != "running" \
                or not attempt.get("external_dispatch"):
            raise ValueError("这次运行没有在等待 DeepSeek 回复")
        with _WAITING_LOCK:
            cancel = _WAITING.get(run_id)
        if cancel is None:
            raise ValueError("这次调用不在当前工作台进程中，请刷新查看最新状态")
        cancel.set()
        return run

    def answer(self, run_id, help_id, action, value=""):
        if not self.path.exists():
            raise ValueError("找不到这次模拟运行")
        value = _bounded_text(value, "回答", 5000)
        with self._execution():
            with self._connection(write=True) as conn:
                run = self._read(conn, run_id)
                old = next((d for d in run["decisions"] if d["help_id"] == help_id), None)
                if old:
                    if (old["action"], old["value"]) != (action, value):
                        raise ValueError("该求助已有不同回答，请刷新运行详情")
                    return self._read(conn, old.get("child_id", run_id))
                else:
                    target_id = self._apply_answer(conn, run, help_id, action, value)
            return self._advance(self.get(target_id))

    def _apply_answer(self, conn, run, help_id, action, value):
        card = run["help"]
        if run["status"] != "waiting" or not card or card["id"] != help_id or action not in card["actions"]:
            raise ValueError("求助已失效或该动作不可用")
        if action in ("supply", "replace_question") and not value.strip():
            raise ValueError("请填写要补充的材料")
        if (action == "choose" and value != "continue") or (action == "extend" and value != "1"):
            raise ValueError("选择或增加的额度无效")
        decision = {"help_id": help_id, "action": action, "value": value, "at": _now(),
                    "kind": card["kind"], "node": card["node"], "question": card["message"]}
        run["decisions"].append(decision)
        _event(run, "answered", "已保存主人的回答。", card["node"])
        run["help"] = None
        target_id = run["id"]
        if action == "stop":
            run["status"] = "stopped"
            _event(run, "stopped", "已收工，保留已有模拟产物。")
        elif action == "rework":
            child = _new_run(run["task_id"], run["scenario"], run["source"]["brief"], run["lead"],
                             run["template"], parent=run)
            self._insert(conn, child)
            target_id = decision["child_id"] = child["id"]
            run["status"] = "revised"
            _event(run, "revised", f"已批准第 {child['round']} 轮，新运行已建立。")
        else:
            if action == "supply":
                run["source"]["brief"] = value
            if action == "replace_question":
                run["assistant_question"] = value
            run["choices"][action] = value
            run["status"] = "running"
        self._save(conn, run)
        return target_id

    def recover(self):
        if not self.path.exists():
            return
        with self._execution(recover=True):
            with self._connection(write=True) as conn:
                pending = []
                rows = conn.execute("SELECT body FROM runs WHERE json_extract(body,'$.status')='running'").fetchall()
                for (raw,) in rows:
                    run = json.loads(raw)
                    if run["attempts"] and run["attempts"][-1]["status"] == "running":
                        attempt = run["attempts"][-1]
                        attempt.update(status="interrupted", ended_at=_now())
                        if attempt.get("external_dispatch"):
                            _wait(run, "opinion", "error", "DeepSeek 派发后中断，送达情况未知。可跳过助手或收工。",
                                  ["skip_assistant", "stop"])
                        else:
                            _wait(run, attempt["node"], "error", "上次派发后中断，是否送达未知；请决定是否重试。", ["retry", "stop"])
                        self._save(conn, run)
                    else:
                        pending.append(run["id"])
            for run_id in pending:
                self._advance(self.get(run_id))
