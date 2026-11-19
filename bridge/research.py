"""Server-owned research state, validation, and evidence labels."""

import json
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from .events import append_research_event, read_all_events


KINDS = {"research_start", "research_phase", "research_claim", "research_evidence", "research_finish"}
PHASES = {"independent", "codex_verify", "challenge", "synthesis"}
PHASE_STATES = {"started", "finished", "failed", "timeout", "skipped"}
SIDES = {"ok", "failed", "timeout", "no_search"}


def _text(value, field, maximum, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > maximum:
        raise ValueError(f"{field} must be {'0' if empty else '1'} to {maximum} characters")
    return value


def _choice(value, field, allowed):
    if value not in allowed:
        raise ValueError(f"invalid {field}: {value}")
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _payload(event):
    return {key: value for key, value in event.items() if key not in {"id", "at", "role", "call_id"}}


def _round(rows, rid):
    current = [row for row in rows if row.get("research_id") == rid and row.get("kind") in KINDS]
    if not current or current[0]["kind"] != "research_start":
        raise ValueError("unknown research_id")
    return current


def _append(rid, kind, data, identity=None, *, allow_finished=False, with_reused=False):
    role = "deepseek" if kind == "research_phase" and data.get("actor") == "deepseek" else "system" if kind in {"research_start", "research_finish"} else "codex"
    candidate = {"v": 1, "research_id": rid, "role": role,
                 "kind": kind, "text": kind, **data}

    def validate(rows):
        if kind == "research_start":
            if any(row.get("research_id") == rid for row in rows):
                raise ValueError("research_id collision")
            return None
        current = _round(rows, rid)
        if kind != "research_finish" and any(row["kind"] == "research_finish" for row in current):
            raise ValueError("research round already finished")
        matches = [row for row in current if row["kind"] == kind and identity is not None and identity(row)]
        if matches:
            if _canonical(_payload(matches[0])) == _canonical(_payload(candidate)):
                return matches[0]
            raise ValueError(f"conflicting {kind} identifier")
        if kind == "research_finish" and any(row["kind"] == "research_finish" for row in current) and not allow_finished:
            raise ValueError("research round already finished")
        if kind == "research_phase" and data.get("actor") == "deepseek" and data.get("state") == "started":
            if any(row["kind"] == "research_phase" and row.get("actor") == "deepseek" and row.get("phase") == data["phase"] and row.get("state") == "started" for row in current):
                raise ValueError(f"DeepSeek {data['phase']} already started")
        if len(current) >= 200:
            raise ValueError("research event limit (200) reached")
        if kind == "research_claim":
            if len([row for row in current if row["kind"] == kind]) >= 30:
                raise ValueError("claim limit (30) reached")
            if data.get("supersedes") and data["supersedes"] not in {row["claim_id"] for row in current if row["kind"] == kind}:
                raise ValueError("supersedes references unknown claim")
        if kind == "research_evidence":
            if len([row for row in current if row["kind"] == kind]) >= 80:
                raise ValueError("evidence limit (80) reached")
            if data["claim_id"] not in {row["claim_id"] for row in current if row["kind"] == "research_claim"}:
                raise ValueError("evidence references unknown claim")
        if kind == "research_finish":
            known = {row["claim_id"] for row in current if row["kind"] == "research_claim"}
            if set(data["final_claims"]) - known:
                raise ValueError("final_claims references unknown claim")
            if data["state"] == "completed":
                if not any(row["kind"] == "research_phase" and row.get("actor") == "codex" and row.get("phase") == "independent" and row.get("state") == "finished" for row in current):
                    raise ValueError("completed requires Codex independent search")
                for phase in ("independent", "challenge"):
                    if not any(row["kind"] == "research_phase" and row.get("actor") == "deepseek" and row.get("phase") == phase and row.get("state") == "finished" for row in current):
                        raise ValueError(f"completed requires DeepSeek {phase} finish")
        return None

    return append_research_event(candidate, validate, with_reused=with_reused)


def start(question_public: str, constraints: str, budget_seconds: int) -> dict:
    question_public = _text(question_public, "question_public", 500)
    constraints = _text(constraints, "constraints", 1000, empty=True)
    if type(budget_seconds) is not int or not 60 <= budget_seconds <= 900:
        raise ValueError("budget_seconds must be 60 to 900")
    rid = uuid.uuid4().hex
    _append(rid, "research_start", {"question_public": question_public, "constraints": constraints,
                                    "budget_seconds": budget_seconds})
    return {"research_id": rid, "question_public": question_public, "constraints": constraints,
            "budget_seconds": budget_seconds,
            "protocol": "先独立搜索并 research_record；调用 research_record_phase(independent, finished)，再 research_ask_deepseek(independent)。核查来源、记录分歧并 research_ask_deepseek(challenge)，最后 research_finish。"}


def _public_url(raw: str, retrieval_state: str) -> tuple[str, bool]:
    _text(raw, "url", 2048, empty=True)
    if not raw:
        if retrieval_state != "no_source":
            raise ValueError("url required unless no_source")
        return "", False
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must be HTTP(S)")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host += f":{parsed.port}"
    # Opaque query parameters can be credentials even without familiar names.
    redacted = bool(parsed.username or parsed.password or parsed.query)
    return urlunsplit((parsed.scheme.lower(), host, parsed.path, "", "")), redacted


def _claim(raw):
    if not isinstance(raw, dict):
        raise ValueError("claim must be an object")
    value = {"claim_id": _text(raw.get("claim_id"), "claim_id", 40),
             "text": _text(raw.get("text"), "claim text", 300),
             "author": _choice(raw.get("author"), "author", {"codex", "deepseek", "both_mentioned"})}
    if raw.get("supersedes") is not None:
        value["supersedes"] = _text(raw["supersedes"], "supersedes", 40)
    return value


def _evidence(raw):
    if not isinstance(raw, dict):
        raise ValueError("evidence must be an object")
    value = {"claim_id": _text(raw.get("claim_id"), "claim_id", 40),
             "evidence_id": _text(raw.get("evidence_id"), "evidence_id", 40)}
    for name, allowed, default in (
        ("source_type", {"primary", "secondary", "forum", "unknown"}, "unknown"),
        ("retrieval_state", {"search_only", "opened", "open_failed", "no_source"}, "search_only"),
        ("support", {"supports", "contradicts", "unclear", "not_assessed"}, "not_assessed"),
        ("reported_by", {"codex", "deepseek"}, "codex"),
        ("opened_by", {"codex", "deepseek", "none"}, "none"),
        ("assessed_by", {"codex", "deepseek", "none"}, "none"),
        ("quote_check", {"matched", "mismatch", "uncheckable", "not_checked"}, "not_checked"),
    ):
        value[name] = _choice(raw.get(name, default), name, allowed)
    value["url"], url_redacted = _public_url(raw.get("url", ""), value["retrieval_state"])
    for name, limit in (("source_date", 60), ("quote", 200), ("quote_location", 200), ("evidence_note", 500)):
        value[name] = _text(raw.get(name, ""), name, limit, empty=True)
    if url_redacted:
        value["retrieval_state"] = "search_only"
        value["support"] = "not_assessed"
        value["opened_by"] = "none"
        value["assessed_by"] = "none"
        value["quote"] = ""
        value["quote_location"] = ""
        value["evidence_note"] = (value["evidence_note"][:400] + " [原链接含私人信息或未知参数，已脱敏；公开地址未核验]").strip()
    if value["retrieval_state"] == "no_source" and value["url"]:
        raise ValueError("no_source must have empty url")
    if value["retrieval_state"] != "opened" and (value["support"] in {"supports", "contradicts"} or value["quote"]):
        raise ValueError("unopened source cannot support, contradict, or quote")
    if value["quote"] and not value["quote_location"]:
        raise ValueError("quote_location required with quote")
    if value["opened_by"] != "none" and value["retrieval_state"] != "opened":
        raise ValueError("opened_by requires opened retrieval state")
    return value


def record(research_id: str, claims: list[dict], evidence: list[dict]) -> dict:
    if not isinstance(claims, list) or not isinstance(evidence, list):
        raise ValueError("claims and evidence must be lists")
    prepared_claims = [_claim(row) for row in claims]
    prepared_evidence = [_evidence(row) for row in evidence]
    if len({row["claim_id"] for row in prepared_claims}) != len(prepared_claims):
        raise ValueError("duplicate claim_id in batch")
    if len({row["evidence_id"] for row in prepared_evidence}) != len(prepared_evidence):
        raise ValueError("duplicate evidence_id in batch")
    written = {"written_claim_ids": [], "written_evidence_ids": []}
    try:
        for row in prepared_claims:
            _append(research_id, "research_claim", row, lambda old, key=row["claim_id"]: old.get("claim_id") == key)
            written["written_claim_ids"].append(row["claim_id"])
        for row in prepared_evidence:
            _append(research_id, "research_evidence", row, lambda old, key=row["evidence_id"]: old.get("evidence_id") == key)
            written["written_evidence_ids"].append(row["evidence_id"])
    except (ValueError, OSError) as exc:
        if written["written_claim_ids"] or written["written_evidence_ids"]:
            raise type(exc)(f"{exc}; already written: {written}") from exc
        raise
    return written


def record_phase(research_id: str, phase: str, state: str, *, disagreements=None, selected_claim_ids=None, error=None) -> dict:
    _choice(phase, "phase", PHASES)
    _choice(state, "state", PHASE_STATES)
    data = {"phase": phase, "state": state, "actor": "codex"}
    if error is not None:
        data["error"] = _text(error, "error", 500)
    if phase == "challenge" and state == "started":
        disagreements = [] if disagreements is None else disagreements
        if not isinstance(disagreements, list) or len(disagreements) > 20:
            raise ValueError("disagreements limit is 20")
        known = {row["claim_id"] for row in _round(read_all_events(), research_id) if row["kind"] == "research_claim"}
        data["disagreements"] = []
        for item in disagreements:
            if not isinstance(item, dict) or item.get("claim_id") not in known:
                raise ValueError("disagreement references unknown claim")
            data["disagreements"].append({"claim_id": item["claim_id"], "note": _text(item.get("note", ""), "note", 300, empty=True)})
        if selected_claim_ids is not None:
            if not isinstance(selected_claim_ids, list) or set(selected_claim_ids) - known:
                raise ValueError("selected_claim_ids references unknown claim")
            data["selected_claim_ids"] = selected_claim_ids
    return _append(research_id, "research_phase", data,
                   lambda old: old.get("actor") == "codex" and old.get("phase") == phase and old.get("state") == state)


def record_deepseek_phase(research_id: str, phase: str, state: str, call_id: str, error=None) -> dict:
    _choice(phase, "phase", {"independent", "challenge"})
    _choice(state, "state", {"started", "finished", "failed", "timeout"})
    data = {"phase": phase, "state": state, "actor": "deepseek", "call_id": _text(call_id, "call_id", 40)}
    if error is not None:
        data["error"] = _text(error, "error", 500)
    return _append(research_id, "research_phase", data,
                   lambda old: old.get("actor") == "deepseek" and old.get("phase") == phase and old.get("state") == state and old.get("call_id") == call_id)


def validate_deepseek_call(research_id: str, phase: str):
    _choice(phase, "phase", {"independent", "challenge"})
    current = _round(read_all_events(), research_id)
    if any(row["kind"] == "research_finish" for row in current):
        raise ValueError("research round already finished")
    if phase == "independent":
        if not any(row["kind"] == "research_phase" and row.get("actor") == "codex" and row.get("phase") == "independent" and row.get("state") in {"finished", "skipped"} for row in current):
            raise ValueError("缺少 independent 结束事件")
        if any(row["kind"] == "research_phase" and row.get("actor") == "codex" and row.get("phase") == "independent" and row.get("state") == "finished" for row in current) and not active_codex_claims(current):
            raise ValueError("independent finished requires a Codex claim")
    else:
        if not any(row["kind"] == "research_phase" and row.get("actor") == "deepseek" and row.get("phase") == "independent" and row.get("state") in {"finished", "failed", "timeout"} for row in current):
            raise ValueError("DeepSeek independent 尚未结束")
        if not any(row["kind"] == "research_phase" and row.get("actor") == "codex" and row.get("phase") == "challenge" and row.get("state") == "started" for row in current):
            raise ValueError("缺少 challenge 分歧清单事件")
        if not active_codex_claims(current):
            raise ValueError("challenge requires active Codex claims")


def active_codex_claims(current):
    superseded = {row.get("supersedes") for row in current if row["kind"] == "research_claim"}
    return sorted((row for row in current if row["kind"] == "research_claim" and row["author"] in {"codex", "both_mentioned"} and row["claim_id"] not in superseded), key=lambda row: row["claim_id"])


def _labels(claim_id, current):
    evidence = [row for row in current if row["kind"] == "research_evidence" and row["claim_id"] == claim_id]
    verified = any(row["opened_by"] == "codex" and row["retrieval_state"] == "opened" and row["assessed_by"] == "codex" and row["support"] == "supports" and row["quote"] and row["quote_location"] for row in evidence)
    labels = ["小龙女记录已核验" if verified else "大肥鱼自报读取" if any(row["opened_by"] == "deepseek" for row in evidence) else "未核验"]
    if any(row["support"] == "contradicts" for row in evidence):
        labels.append("有反证")
    return labels


def snapshot(research_id: str, rows=None) -> dict:
    rows = read_all_events() if rows is None else rows
    current = _round(rows, research_id)
    start_event = current[0]
    finish_event = next((row for row in current if row["kind"] == "research_finish"), None)
    claims = [{**row, "labels": _labels(row["claim_id"], current),
               "evidence": [x for x in current if x["kind"] == "research_evidence" and x["claim_id"] == row["claim_id"]]}
              for row in current if row["kind"] == "research_claim"]
    final_ids = finish_event["final_claims"] if finish_event else []
    x = sum("小龙女记录已核验" in _labels(cid, current) for cid in final_ids)
    y = len(final_ids)
    deep_evidence = [row for row in current if row["kind"] == "research_evidence" and row["reported_by"] == "deepseek"]
    if not any(row["url"] for row in deep_evidence):
        warning = "本轮大肥鱼未提供来源"
    elif not any(row["retrieval_state"] == "opened" for row in deep_evidence):
        warning = "本轮大肥鱼未读取任何原文"
    elif not any(row["opened_by"] == "codex" for row in current if row["kind"] == "research_evidence"):
        warning = "抓取由大肥鱼自报，待小龙女核对"
    else:
        warning = ""
    last = datetime.fromisoformat(current[-1]["at"])
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if finish_event:
        state = finish_event["state"]
    elif age > max(900, 2 * start_event["budget_seconds"]):
        state = "未收尾／可能中断"
    else:
        state = "进行中"
    running = [row for row in current if row["kind"] == "research_phase" and row.get("actor") == "deepseek" and row.get("state") == "started" and not any(x["kind"] == "research_phase" and x.get("actor") == "deepseek" and x.get("call_id") == row.get("call_id") and x.get("state") in {"finished", "failed", "timeout"} for x in current)]
    if running and (datetime.now(timezone.utc) - datetime.fromisoformat(running[-1]["at"])).total_seconds() > 210:
        warning = "调用疑似卡住"
    return {"research_id": research_id, "question_public": start_event["question_public"],
            "constraints": start_event["constraints"], "budget_seconds": start_event["budget_seconds"],
            "state": state, "last_activity": current[-1]["at"], "claims": claims,
            "coverage": f"{x}/{y}" if y else "无最终结论", "warning": warning,
            "events": current, "exchanges": [row for row in rows if row.get("research_id") == research_id and row.get("kind") in {"question", "answer", "error"}],
            "finish": finish_event}


def all_snapshots():
    rows = read_all_events()
    ids = [row["research_id"] for row in rows if row.get("kind") == "research_start" and row.get("v") == 1]
    return sorted((snapshot(rid, rows) for rid in ids), key=lambda row: row["last_activity"], reverse=True)


def finish(research_id: str, state: str, codex_side: str, deepseek_side: str, final_claims: list[str], unresolved: list[str]) -> dict:
    _choice(state, "state", {"completed", "partial", "aborted"})
    _choice(codex_side, "codex_side", SIDES)
    _choice(deepseek_side, "deepseek_side", SIDES)
    if state == "completed" and (codex_side != "ok" or deepseek_side != "ok"):
        raise ValueError("completed requires both sides ok")
    if not isinstance(final_claims, list) or not all(isinstance(x, str) for x in final_claims):
        raise ValueError("final_claims must be IDs")
    if len(final_claims) != len(set(final_claims)):
        raise ValueError("duplicate final claim ID")
    if not isinstance(unresolved, list) or not all(isinstance(x, str) and len(x) <= 500 for x in unresolved):
        raise ValueError("unresolved must be short strings")
    row, already_finished = _append(research_id, "research_finish", {"state": state, "codex_side": codex_side,
                  "deepseek_side": deepseek_side, "final_claims": final_claims, "unresolved": unresolved},
                  lambda old: True, allow_finished=True, with_reused=True)
    snap = snapshot(research_id)
    return {"research_id": research_id, "state": state, "coverage": snap["coverage"],
            "labels": {claim["claim_id"]: claim["labels"] for claim in snap["claims"] if claim["claim_id"] in final_claims},
            "already_finished": already_finished}
