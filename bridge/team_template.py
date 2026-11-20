"""Fixed development/review template and deterministic, in-memory phase-one fixtures."""

from copy import deepcopy
from hashlib import sha256


INPUT_PORTS = {
    "lead": {"brief": "text", "revision": "handoff"},
    "opinion": {"context": "text"},
    "review": {"context": "text", "patch": "patch", "opinion": "opinions"},
    "verify": {"patch": "patch", "review": "review"},
}
OUTPUT_PORTS = {
    "source": {"brief": "text", "revision": "handoff"},
    "lead": {"context": "text", "patch": "patch"},
    "opinion": {"opinion": "opinions"},
    "review": {"review": "review"},
    "verify": {"checks": "checks"},
}
VALUE_TYPES = {"text": str, "handoff": dict, "patch": dict, "opinions": list,
               "review": dict, "checks": dict}


def default_template() -> dict:
    return {
        "version": "development-review-sim-v1",
        "nodes": ["lead", "opinion", "review", "verify"],
        "roles": {"lead": "主做", "opinion": "大肥鱼（可选）", "review": "独立复核", "verify": "本地验证"},
        "bindings": {
            "lead": {"brief": "source.brief", "revision": "source.revision"},
            "opinion": {"context": "lead.context"},
            "review": {"context": "lead.context", "patch": "lead.patch", "opinion": "opinion.opinion"},
            "verify": {"patch": "lead.patch", "review": "review.review"},
        },
    }


def validate_template(template: dict) -> None:
    if not isinstance(template, dict) or not isinstance(template.get("version"), (str, int)) or not template["version"]:
        raise ValueError("template.version 缺少模板版本")
    nodes = template.get("nodes")
    if not isinstance(nodes, list) or any(not isinstance(node, str) for node in nodes):
        raise ValueError("template.nodes 需要节点名称列表")
    if len(nodes) != len(INPUT_PORTS) or set(nodes) != set(INPUT_PORTS):
        raise ValueError("template.nodes 必须各含一次 lead、opinion、review 和 verify")
    bindings = template.get("bindings")
    if not isinstance(bindings, dict):
        raise ValueError("template.bindings 缺少连线")
    for node in bindings:
        if node not in INPUT_PORTS:
            raise ValueError(f"{node}.bindings 未知节点")
    ready = {"source"}
    for node in nodes:
        edges = bindings.get(node, {})
        if not isinstance(edges, dict):
            raise ValueError(f"{node}.bindings 需要端口连线表")
        for port, kind in INPUT_PORTS[node].items():
            reference = edges.get(port)
            destination = f"{node}.{port}"
            if not isinstance(reference, str) or reference.count(".") != 1:
                raise ValueError(f"{destination} 缺少合法的来源端口")
            upstream, field = reference.split(".")
            source_kind = OUTPUT_PORTS.get(upstream, {}).get(field)
            if source_kind != kind:
                raise ValueError(f"{destination} 需要 {kind} 类型，来源 {reference} 无效")
            if upstream not in ready:
                raise ValueError(f"{destination} 的来源 {reference} 必须先执行，请检查环和节点顺序")
        for port in edges:
            if port not in INPUT_PORTS[node]:
                raise ValueError(f"{node}.{port} 未知输入端口")
        ready.add(node)


def bind_inputs(template: dict, node: str, source: dict, outputs: dict) -> dict:
    validate_template(template)
    if node not in INPUT_PORTS:
        raise ValueError(f"{node}.inputs 未知节点")
    inputs = {}
    for port, reference in template["bindings"][node].items():
        upstream, field = reference.split(".")
        record = source if upstream == "source" else outputs.get(upstream)
        kind = INPUT_PORTS[node][port]
        if not isinstance(record, dict) or not isinstance(record.get(field), VALUE_TYPES[kind]):
            raise ValueError(f"{node}.{port} 缺少来自 {reference} 的 {kind} 产物")
        inputs[port] = deepcopy(record[field])
    return inputs


def _result(status, output, help_card=None):
    return {"status": status, "output": output, "help": help_card}


def _pause(kind, message, actions, output=None):
    return _result("failed", output or {}, {"kind": kind, "message": message, "actions": actions})


def _proposal(inputs, scenario, round_number, lead):
    # All scenarios use a small synthetic baseline. The two rework fixtures have
    # distinct, visible defects in the proposed text matching their evidence.
    replacement = {
        "review": 'value.strip(" ")',
        "test_failure": "value.strip()",
    }.get(scenario, '(value or "").strip()')
    path = "labels.py"
    diff = (f"--- a/{path}\n+++ b/{path}\n@@ -1,2 +1,2 @@\n def normalize_label(value):\n"
            f"-    return value\n+    return {replacement}\n")
    digest = sha256(diff.encode("utf-8")).hexdigest()[:12]
    patch = {"id": f"r{round_number}-{digest}", "path": path, "diff": diff,
             "author": lead, "round": round_number, "simulated": True}
    revision = inputs["revision"]
    previous_patch = revision.get("patch") or {}
    findings = (revision.get("review") or {}).get("findings", [])
    cases = (revision.get("checks") or {}).get("cases", [])
    follow_up = [f'{item["location"]} · {item["message"]}' for item in findings]
    follow_up.extend(f'{case["name"]} · {case["detail"]}' for case in cases if not case["passed"])
    plan = "在合成文件 labels.py 调整 normalize_label，演示去除两端空白和处理空值。"
    if follow_up:
        plan += "\n沿用上轮记录核对以下项目。\n" + "\n".join(follow_up)
    return {"context": f'第 {round_number} 轮主做说明 · {inputs["brief"]}', "patch": patch, "plan": plan,
            "previous_patch_id": previous_patch.get("id"), "simulated": True}


def _review(inputs, scenario, reviewer):
    patch = inputs["patch"]
    findings = []
    if scenario == "review":
        findings.append({"location": f'{patch["path"]}:2',
                         "message": "只处理了半角空格。验收样例还包含制表符。"})
    review = {"patch_id": patch["id"], "reviewer": reviewer, "context": inputs["context"],
              "opinion": inputs["opinion"], "approved": not findings, "findings": findings,
              "summary": "模拟复核要求修改。" if findings else "模拟复核通过。",
              "simulated": True}
    if findings:
        return _pause("error", "模拟复核要求修改。请查看 labels.py 第 2 行的定位意见。",
                      ["rework", "stop"], {"review": review})
    return _result("success", {"review": review})


def _verify(inputs, scenario):
    patch, review = inputs["patch"], inputs["review"]
    if review.get("patch_id") != patch.get("id") or not review.get("approved"):
        return _pause("error", "此候选补丁缺少对应的复核通过记录。", ["rework", "stop"])
    passed = scenario != "test_failure"
    cases = [
        {"name": "test_strip_whitespace", "passed": True, "detail": "两端空白已去除。"},
        {"name": "test_none_label", "passed": passed,
         "detail": "空值转换为空字符串。" if passed else "预期空字符串，模拟结果为 AttributeError。"},
    ]
    checks = {"patch_id": patch["id"], "passed": passed, "cases": cases, "simulated": True,
              "summary": "模拟检查通过。" if passed else "模拟检查有 1 项失败。"}
    if not passed:
        return _pause("error", "模拟检查 test_none_label 失败，返工时可沿用失败摘要。",
                      ["rework", "stop"], {"checks": checks})
    return _result("success", {"checks": checks})


def evaluate(node: str, inputs: dict, *, scenario: str, round_number: int,
             attempt: int, choices: dict, lead: str) -> dict:
    lead = lead.lower()
    if lead not in ("codex", "claude"):
        raise ValueError("lead.role 需要 codex 或 claude")
    if node == "lead":
        if scenario == "timeout" and round_number == 1 and attempt == 1:
            return _pause("error", "模拟调用超时，送达情况未知。可核对后重试或收工。", ["retry", "stop"])
        if scenario == "choice" and choices.get("choose") != "continue":
            return _pause("choice", "是否继续使用合成标签样例演练？", ["choose", "stop"])
        return _result("success", _proposal(inputs, scenario, round_number, lead))
    if node == "opinion":
        return _result("skipped", {"opinion": [], "reason": "本轮关闭大肥鱼第二意见。", "simulated": True})
    if node == "review":
        return _review(inputs, scenario, reviewer="claude" if lead == "codex" else "codex")
    if node == "verify":
        return _verify(inputs, scenario)
    raise ValueError(f"{node}.inputs 未知节点")
