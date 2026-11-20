"""Local MCP entry point for two-model Codex and DeepSeek collaboration."""

import os
import uuid
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .harness import (HARNESS_TIMEOUT_SECONDS, MAX_ERROR_CHARS, HarnessError, consult, consult_prepared, delegate,
                      validate_timeout_seconds)
from .events import append_event, append_research_event, read_all_events
from . import research, tasks
from .research_prompts import build_task
from .workspace import DEFAULT_ROOT, Workspace


mcp = FastMCP("deepseek-bridge")
MAX_THREAD_TIMEOUT_SECONDS = HARNESS_TIMEOUT_SECONDS
# Omitted means no total limit: the call only has to be received.
OptionalLimit = Annotated[int | None, Field(strict=True, ge=1, le=HARNESS_TIMEOUT_SECONDS)]


@mcp.tool()
def ask_deepseek(question: str) -> dict:
    """Ask DeepSeek for an ordinary second opinion. For collaborative research, call research_start first."""
    call_id = uuid.uuid4().hex
    append_event("codex", "question", question, call_id=call_id)
    append_event("system", "status", "已发送给大肥鱼，等待回复", call_id=call_id)
    try:
        result = consult(question, on_event=lambda event: _record_status(event, call_id))
    except Exception as exc:
        append_event("system", "error", str(exc), call_id=call_id)
        raise
    append_event("deepseek", "answer", result["answer"], call_id=call_id)
    return {**result, "call_id": call_id}


@mcp.tool()
def delegate_to_deepseek(task: str, project_dir: str) -> dict:
    """Delegate a scoped project task through DeepSeek Harness in a temporary copy. Review returned changes and run checks independently."""
    call_id = uuid.uuid4().hex
    append_event("codex", "question", f"项目：{project_dir}\n任务：{task}", call_id=call_id)
    append_event("system", "status", "已委派给大肥鱼，正在处理临时副本", call_id=call_id)
    try:
        root = os.environ.get("DEEPSEEK_BRIDGE_ROOT", str(DEFAULT_ROOT))
        workspace = Workspace(project_dir, allowed_root=root)
        result = delegate(task, workspace, on_event=lambda event: _record_status(event, call_id))
    except Exception as exc:
        append_event("system", "error", str(exc), call_id=call_id)
        raise
    append_event("deepseek", "answer", result["summary"], call_id=call_id)
    return {**result, "call_id": call_id}


def _record_status(event: dict, call_id: str):
    if event.get("type") == "status":
        phase = event.get("phase")
        if phase == "step_start":
            append_event("system", "status", "大肥鱼正在生成回复", call_id=call_id)
        elif phase == "step_end":
            append_event("system", "status", "大肥鱼完成一个处理步骤", call_id=call_id)


@mcp.tool()
def record_codex_feedback(message: str, call_id: str = "") -> dict:
    """Show 小龙女's explicit review, correction, or decision in the local workbench. Does not send it to DeepSeek."""
    if not isinstance(message, str) or not message.strip() or len(message) > 20000:
        raise ValueError("message must be 1 to 20000 characters")
    return append_event("codex", "feedback", message, call_id=call_id)


@mcp.tool()
def research_start(question_public: str, constraints: str = "", budget_seconds: int = 600) -> dict:
    """Create a research round after reviewing the exact public question to send."""
    return research.start(question_public, constraints, budget_seconds)


@mcp.tool()
def research_record(research_id: str, claims: list[dict], evidence: list[dict]) -> dict:
    """Append numbered claims and evidence; each ID is retry-safe and never upgrades its own label."""
    return research.record(research_id, claims, evidence)


@mcp.tool()
def research_record_phase(research_id: str, phase: str, state: str, disagreements: list[dict] | None = None,
                          selected_claim_ids: list[str] | None = None, error: str | None = None) -> dict:
    """Record Codex's own research phase; finish independent before asking DeepSeek."""
    if phase == "challenge" and state == "started":
        preview = {"research_id": research_id, "kind": "research_phase", "actor": "codex", "phase": phase,
                   "state": state, "disagreements": disagreements or []}
        if selected_claim_ids is not None:
            preview["selected_claim_ids"] = selected_claim_ids
        build_task(research_id, "challenge", [*read_all_events(), preview])
    return research.record_phase(research_id, phase, state, disagreements=disagreements,
                                 selected_claim_ids=selected_claim_ids, error=error)


@mcp.tool()
def research_ask_deepseek(
    research_id: str,
    phase: str,
    timeout_seconds: OptionalLimit = None,
) -> dict:
    """Run one DeepSeek research phase using only this round's stored public facts.

    The call only has to be received: Harness must start the task within 120 seconds. After
    that it waits until DeepSeek answers. timeout_seconds (1-600) optionally caps the total.
    """
    validate_timeout_seconds(timeout_seconds)
    research.validate_deepseek_call(research_id, phase)
    version, task = build_task(research_id, phase, read_all_events())
    call_id = uuid.uuid4().hex

    def validate_log(rows):
        if any(row.get("research_id") == research_id and row.get("kind") == "research_finish" for row in rows):
            raise ValueError("research round already finished")
        return None

    research.record_deepseek_phase(research_id, phase, "started", call_id, timeout_seconds=timeout_seconds)
    try:
        append_research_event({"v": 1, "research_id": research_id, "role": "codex", "kind": "question",
                               "text": task, "call_id": call_id, "template_version": version, "phase": phase}, validate_log)
    except Exception as exc:
        research.record_deepseek_phase(research_id, phase, "failed", call_id, error=f"prompt log failed: {exc}"[:500])
        raise
    try:
        result = consult_prepared(task, on_event=lambda event: _record_status(event, call_id),
                                  timeout_seconds=timeout_seconds)
    except Exception as exc:
        state = "timeout" if "timed out" in str(exc).lower() else "failed"
        research.record_deepseek_phase(research_id, phase, state, call_id, error=str(exc)[:500])
        append_event("system", "error", str(exc), call_id=call_id)
        raise
    research.record_deepseek_phase(research_id, phase, "finished", call_id)
    append_research_event({"v": 1, "research_id": research_id, "role": "deepseek", "kind": "answer",
                           "text": result["answer"], "call_id": call_id, "template_version": version, "phase": phase}, validate_log)
    return {**result, "call_id": call_id, "research_id": research_id, "phase": phase,
            "template_version": version}


@mcp.tool()
def research_finish(research_id: str, state: str, codex_side: str, deepseek_side: str,
                    final_claims: list[str], unresolved: list[str]) -> dict:
    """Close a research round and return server-derived evidence labels and coverage."""
    return research.finish(research_id, state, codex_side, deepseek_side, final_claims, unresolved)


@mcp.tool()
def create_task(title: str, goal: str = "") -> dict[str, Any]:
    """Open a local task conversation. The optional goal is sent on any later DeepSeek thread call."""
    return tasks.create_task(title, goal)


@mcp.tool()
def list_tasks(status: str | None = None) -> list[dict[str, Any]]:
    """List workbench tasks. Filter by pending to find cards requested from the web page."""
    return tasks.list_tasks(status)


@mcp.tool()
def update_task_status(task_id: str, status: str, note: str = "") -> dict[str, Any]:
    """Change a task status. Set a pending task to active to claim it."""
    return tasks.update_task_status(task_id, status, note)


@mcp.tool()
def post_message(thread_id: str, content: str, share: bool = False) -> dict[str, Any]:
    """Record a message in one task. Mark share=true only for content authorized for DeepSeek."""
    return tasks.post_message(thread_id, content, share)


@mcp.tool()
def read_thread(thread_id: str, limit: int = 100, after_id: str | None = None) -> dict[str, Any]:
    """Read one task conversation in journal order. Includes local-only notes and prior model replies."""
    return tasks.read_thread(thread_id, limit, after_id)


@mcp.tool()
def thread_ask_deepseek(
    thread_id: str,
    question: str,
    mode: str,
    timeout_seconds: OptionalLimit = None,
) -> dict[str, Any]:
    """Ask DeepSeek in one task. Shared also sends marked messages.

    The call only has to be received: Harness must start the task within 120 seconds, and the
    thread then shows it as delivered. After that it waits until DeepSeek answers, and the reply
    is saved to the thread even if this tool call is abandoned. timeout_seconds (1-600)
    optionally caps the total.
    """
    validate_timeout_seconds(timeout_seconds)
    dispatch = tasks.start_thread_dispatch(thread_id, question, mode, timeout_seconds=timeout_seconds)
    try:
        result = consult_prepared(dispatch["text"], timeout_seconds=timeout_seconds,
                                  on_receipt=lambda: tasks.mark_thread_received(dispatch))
        answer = result["answer"]
        if not isinstance(answer, str):
            raise HarnessError("DeepSeek Harness returned an invalid answer")
    except Exception as exc:
        timed_out = isinstance(exc, TimeoutError) or (
            isinstance(exc, HarnessError) and str(exc).startswith("DeepSeek Harness task timed out")
        )
        tasks.finish_thread_dispatch(dispatch, "timeout" if timed_out else "failed", error=str(exc)[:MAX_ERROR_CHARS])
        raise
    tasks.finish_thread_dispatch(dispatch, "finished", answer=answer)
    context_count = len(dispatch["context_ids"])
    return {
        **result, "task_id": dispatch["task_id"], "thread_id": thread_id,
        "call_id": dispatch["call_id"], "mode": mode, "context_count": context_count,
        **({"notice": "本次没有带任何对话内容"} if mode == "shared" and not context_count else {}),
    }


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
