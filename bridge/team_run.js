"use strict";

(() => {
  const get = id => document.getElementById(id);
  const query = new URLSearchParams(location.search);
  const taskId = query.get("task_id");
  const pendingKey = "mcp-dock:pending-start:" + taskId;
  const states = {running: "运行中", waiting: "等主人", completed: "模拟完成", revised: "已开返工轮",
    stopped: "已收工", success: "成功", skipped: "已跳过", failed: "失败", interrupted: "已中断"};
  const nodes = {lead: "主做", opinion: "大肥鱼第二意见", review: "交叉复核", verify: "本地验证", template: "模板检查"};
  const kinds = {error: "出错了", missing: "缺东西", limit: "到预定的量", choice: "要做选择"};
  const actions = {retry: "重试这一步", supply: "补齐并继续", extend: "增加 1 次模拟调用", choose: "选择继续",
    rework: "批准返工一轮", replace_question: "复检并发送给 DeepSeek", skip_assistant: "跳过助手，继续复核", stop: "收工"};
  const events = {started: "启动", dispatched: "派发", success: "成功", skipped: "跳过", failed: "失败",
    waiting: "等主人", answered: "主人已答复", revised: "已开返工轮", completed: "模拟完成", stopped: "已收工",
    interrupted: "中断"};
  const scenarios = Object.fromEntries(Array.from(get("scenario").options, o => [o.value, o.textContent]));
  let pending;
  let refreshTimer;
  let busy = false;
  let archived = false;
  let instructions = "";
  let summaries = [];
  let displayed;

  function element(parent, tag, value = "", css = "") {
    const child = document.createElement(tag);
    if (css) child.className = css;
    if (value !== "") child.textContent = value;
    parent.append(child);
    return child;
  }

  function roundName(number) { return number === 1 ? "首轮" : "返工第 " + (number - 1) + " 轮"; }
  function runUrl(id) {
    return "/team?" + new URLSearchParams({task_id: taskId, run_id: id});
  }
  function roles(lead) {
    return lead === "claude" ? "Claude 主做 · Codex 复核" : "Codex 主做 · Claude 复核";
  }
  function stateName(value, run) {
    return value === "completed" && run.assistant === "deepseek" ? "已完成" : (states[value] || value);
  }
  function state(parent, value, run) {
    element(parent, "span", stateName(value, run), "state").dataset.state = value;
  }
  function preview(question) {
    return instructions ? instructions + "\n\n" + question : "无法读取固定指令，请刷新页面。";
  }
  function dataView(parent, title, value) {
    const details = element(parent, "details");
    element(details, "summary", title);
    let content = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    if (!content || value === null) content = "暂无记录";
    element(details, "pre", content);
    return details;
  }
  function dateText(value) {
    if (!value) return "";
    const at = new Date(value);
    return Number.isNaN(at.getTime()) ? String(value) : at.toLocaleString("zh-CN", {hour12: false});
  }

  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {cache: "no-store"} : {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
    const value = await response.json();
    if (!response.ok) {
      const error = new Error(value.error || "请求失败（" + response.status + "）");
      error.status = response.status;
      throw error;
    }
    return value;
  }

  function showList(runs, activeId) {
    summaries = runs;
    const list = get("run-list");
    list.replaceChildren();
    get("no-runs").hidden = runs.length > 0;
    for (const run of runs) {
      const link = element(element(list, "li"), "a");
      link.href = runUrl(run.id);
      if (run.id === activeId) link.setAttribute("aria-current", "page");
      element(link, "strong", roundName(run.round) + " · " + stateName(run.status, run));
      const mode = run.assistant === "deepseek" ? "DeepSeek 真实助手" : "本机模拟";
      element(link, "small", mode + " · " + (scenarios[run.scenario] || run.scenario) + " · " + run.id.slice(0, 8));
    }
  }

  function showHelp(container, run) {
    const help = run.help;
    const box = element(container, "section", "", "help");
    element(box, "h3", "等主人 · " + (kinds[help.kind] || help.kind));
    element(box, "p", "暂停节点 · " + (nodes[help.node] || help.node), "hint");
    element(box, "p", help.message);
    if (archived) return;
    const form = element(box, "form");
    let input;
    const replacing = help.actions.includes("replace_question");
    if (replacing || help.actions.includes("supply")) {
      element(form, "label", replacing ? "替换发给 DeepSeek 的问题" : "补充材料").htmlFor = "help-value";
      input = element(form, "textarea");
      input.id = "help-value";
      input.rows = 2;
      input.required = true;
      input.maxLength = 5000;
      if (replacing) {
        input.value = run.assistant_question || "";
        const details = element(form, "details");
        details.open = true;
        element(details, "summary", "替换后发给 DeepSeek 的完整提示");
        const prompt = element(details, "pre", preview(input.value), "preview");
        prompt.id = "replacement-preview";
        input.addEventListener("input", () => { prompt.textContent = preview(input.value); });
      }
    }
    const buttons = element(form, "div", "", "buttons");
    for (const action of help.actions) {
      const button = element(buttons, "button", actions[action] || action, action === "stop" ? "" : "primary");
      button.type = "submit";
      button.value = action;
      button.formNoValidate = !["supply", "replace_question"].includes(action);
      if (action === "replace_question" && !instructions) button.disabled = true;
    }
    const notice = element(form, "p", "", "notice");
    notice.setAttribute("role", "status");
    form.addEventListener("submit", async e => {
      e.preventDefault();
      const action = e.submitter?.value;
      if (busy || !help.actions.includes(action)) return;
      const payload = {run_id: run.id, help_id: help.id, action};
      if (action === "supply" || action === "replace_question") {
        if (action === "replace_question" && !instructions) return;
        payload.value = action === "replace_question" ? input.value : input.value.trim();
        if (!payload.value.trim()) { notice.textContent = replacing ? "请填写新问题。" : "请填写补充材料。"; input.focus(); return; }
      } else if (action === "extend") payload.value = "1";
      else if (action === "choose") payload.value = "continue";
      busy = true;
      form.querySelectorAll("button").forEach(button => { button.disabled = true; });
      notice.textContent = "正在记录答复…";
      try {
        const next = await api("/api/team/answer", payload);
        await acceptRun(next);
      } catch (error) {
        notice.textContent = "答复未确认，可刷新查看此轮状态。 " + error.message;
      } finally {
        form.querySelectorAll("button").forEach(button => { button.disabled = false; });
        busy = false;
      }
    });
  }

  function showAttempts(container, run) {
    const section = element(container, "section");
    element(section, "h3", "节点与尝试");
    if (!run.attempts.length) element(section, "p", "尚未派发节点。", "empty");
    const list = element(section, "ol", "", "attempts");
    for (const step of run.attempts) {
      const item = element(list, "li");
      const row = element(item, "div", "", "row");
      element(row, "h4", (nodes[step.node] || step.node) + " · 第 " + step.number + " 次尝试");
      state(row, step.status, run);
      if (step.node === "opinion" && step.progress) {
        const phase = {step_start: "助手步骤已开始", step_end: "助手步骤已结束"}[step.progress.phase] || step.progress.phase;
        element(item, "p", [phase, dateText(step.progress.at)].filter(Boolean).join(" · "), "hint");
      }
      const output = step.output || {};
      const summary = output.review?.summary || output.checks?.summary || output.reason;
      if (summary) element(item, "p", summary, "hint");
      if (output.patch?.diff) dataView(item, "候选补丁 · " + (output.patch.path || "查看差异"), output.patch.diff);
      dataView(item, "实际输入", step.inputs);
      dataView(item, "输出与证据", step.output);
    }
  }

  function showRecords(container, run) {
    const section = element(container, "section");
    element(section, "h3", "主人决定");
    const decisions = element(section, "ul", "", "events");
    if (!run.decisions.length) element(section, "p", "本轮尚无答复。", "hint");
    for (const decision of run.decisions) {
      const item = element(decisions, "li");
      if (decision.question) element(item, "p", decision.question);
      element(item, "strong", (kinds[decision.kind] || decision.kind) + " · " + (actions[decision.action] || decision.action));
      if (decision.value) element(item, "span", " · " + String(decision.value));
      if (decision.child_id) element(item, "a", " 打开返工轮").href = runUrl(decision.child_id);
      element(item, "time", dateText(decision.at), "time");
    }
    const log = element(container, "details");
    element(log, "summary", "运行时间线（" + run.history.length + "）");
    const entries = element(log, "ol", "", "events");
    for (const row of run.history) {
      const item = element(entries, "li");
      const label = row.event === "completed" ? stateName("completed", run) : (events[row.event] || row.event);
      element(item, "strong", [label, nodes[row.node] || row.node].filter(Boolean).join(" · "));
      if (row.message) element(item, "span", "　" + row.message);
      element(item, "time", dateText(row.at), "time");
    }
  }

  function showRun(run) {
    const container = get("detail");
    clearTimeout(refreshTimer);
    displayed = run;
    const current = summaries.find(r => r.id === run.id);
    if (current) Object.assign(current, {status: run.status, assistant: run.assistant});
    else summaries.unshift(run);
    showList(summaries, run.id);
    container.replaceChildren();
    const heading = element(container, "div", "", "row");
    element(heading, "h2", roundName(run.round) + " · " + (scenarios[run.scenario] || run.scenario));
    state(heading, run.status, run);
    element(container, "p", "本机模拟 · " + roles(run.lead) + " · 本地验证", "hint");
    const enabled = run.assistant === "deepseek";
    element(container, "p", enabled ? "真实助手 DeepSeek · 本轮向 Harness 提交 " + run.cloud_calls + " 次" :
      "助手关闭 · 云端调用 " + run.cloud_calls + " 次", "hint");
    const version = typeof run.template === "string" ? run.template : run.template?.version;
    if (version) element(container, "p", "模板版本 · " + version, "hint");
    if (run.help) showHelp(container, run);
    if (enabled) {
      const section = element(container, "section", "", "assistant-answer");
      const result = run.attempts.find(step => step.node === "opinion" && step.output?.opinion?.length);
      element(section, "h3", result ? "DeepSeek 的回复" : "DeepSeek 助手");
      if (result) {
        if (run.parent_id) element(section, "p", "本轮沿用上一轮的助手意见。", "hint");
        for (const answer of result.output.opinion) element(section, "pre", answer);
      }
      const waiting = run.status === "running" && run.attempts.find(step =>
        step.node === "opinion" && step.status === "running" && step.external_dispatch);
      if (waiting) showStopWaiting(section, run, waiting);
      dataView(section, "查看本轮助手问题", run.assistant_question);
    }
    const source = run.source?.brief ?? run.brief;
    const materials = element(container, "section");
    element(materials, "h3", "本轮实际材料");
    element(materials, "p", source || "待补齐", "goal");
    if (run.brief && run.brief !== source) dataView(materials, "启动时的说明", run.brief);
    if (run.parent_id) element(container, "a", "查看上一轮").href = runUrl(run.parent_id);
    if (run.parent_id && run.source?.revision && Object.keys(run.source.revision).length) {
      dataView(container, "上一轮交接材料", run.source.revision);
    }
    showAttempts(container, run);
    showRecords(container, run);
    if (run.status === "running") schedulePoll(run);
  }

  function showStopWaiting(section, run, step) {
    // A local Harness progress event starts the unbounded wait for the model reply.
    const since = Date.parse(step.started_at);
    const minutes = Number.isNaN(since) ? 0 : Math.floor((Date.now() - since) / 60000);
    const message = step.progress ? "本机 Harness 已开始处理，等待 DeepSeek 回复。默认不限时"
      : "已提交，等待本机 Harness 报告开始处理（最多 120 秒）";
    element(section, "p", message + (minutes > 0 ? " · 已等 " + minutes + " 分钟" : "") + "。可随时停止等待。", "hint");
    if (archived) return;
    const button = element(section, "button", "停止等待 DeepSeek");
    button.type = "button";
    const notice = element(section, "p", "", "notice");
    notice.setAttribute("role", "status");
    button.addEventListener("click", async () => {
      if (busy) return;
      busy = true;
      button.disabled = true;
      notice.textContent = "正在停止等待…";
      try {
        await api("/api/team/stop-waiting", {run_id: run.id});
        notice.textContent = "已请求停止，稍后显示下一步选择。";
      } catch (error) {
        notice.textContent = "未能停止。 " + error.message;
        button.disabled = false;
      } finally {
        busy = false;
      }
    });
  }

  function schedulePoll(run) {
    refreshTimer = setTimeout(async () => {
      try {
        const latest = await api("/api/team/run?" + new URLSearchParams({id: run.id}));
        if (displayed !== run) return;
        if (!archived) get("notice").textContent = "";
        if (JSON.stringify(latest) === JSON.stringify(run)) schedulePoll(run);
        else showRun(latest);
      } catch (error) {
        if (displayed !== run) return;
        get("notice").textContent = "正在重新读取本机进度。 " + error.message;
        schedulePoll(run);
      }
    }, 1200);
  }

  async function acceptRun(run) {
    history.replaceState(null, "", runUrl(run.id));
    showRun(run);
    get("launch").open = false;
    get("notice").textContent = "";
    try {
      const {runs} = await api("/api/team/runs?" + new URLSearchParams({task_id: taskId}));
      if (displayed.id === run.id) {
        showList(runs.map(item => item.id === displayed.id ? {...item, status: displayed.status} : item), run.id);
      }
    } catch (error) {
      get("notice").textContent = "本轮已记录。更新运行列表失败，可刷新查看。 " + error.message;
    }
  }

  function assistantOn() { return get("assistant").value === "deepseek"; }

  function updateLaunch() {
    const live = assistantOn();
    get("assistant-input").hidden = !live;
    get("assistant-preview").textContent = preview(get("assistant-question").value);
    get("start").disabled = busy;
    get("start").textContent = pending ? "重试启动" : live ? "启动并发送给 DeepSeek" : "启动模拟运行";
    for (const id of ["scenario", "brief", "lead", "assistant", "assistant-question"]) {
      get(id).disabled = busy || Boolean(pending);
    }
  }

  get("assistant").addEventListener("change", updateLaunch);
  get("assistant-question").addEventListener("input", updateLaunch);

  get("start-form").addEventListener("submit", async e => {
    e.preventDefault();
    if (busy || archived || !taskId) return;
    if (!pending && assistantOn()) {
      // The question is sent as typed; the server checks it again before any dispatch.
      if (!instructions) { get("start-status").textContent = "无法读取固定指令，请刷新页面后再发送。"; return; }
      if (!get("assistant-question").value.trim()) {
        get("start-status").textContent = "请填写单独发给 DeepSeek 的问题。";
        get("assistant-question").focus();
        return;
      }
    }
    busy = true;
    try {
      if (!pending) {
        pending = {task_id: taskId, request_id: crypto.randomUUID(), scenario: get("scenario").value,
          brief: get("brief").value.trim(), lead: get("lead").value};
        if (assistantOn()) {
          pending.assistant = "deepseek";
          pending.assistant_question = get("assistant-question").value;
        }
        sessionStorage.setItem(pendingKey, JSON.stringify(pending));
      }
      updateLaunch();
      get("start-status").textContent = "正在启动…";
      const run = await api("/api/team/start", pending);
      pending = null;
      sessionStorage.removeItem(pendingKey);
      get("start-status").textContent = "";
      await acceptRun(run);
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && error.status !== 408) {
        pending = null;
        sessionStorage.removeItem(pendingKey);
        get("start-status").textContent = "启动失败。 " + error.message;
      } else {
        get("start-status").textContent = "启动结果待确认，可重试同一请求。 " + error.message;
      }
    } finally {
      busy = false;
      updateLaunch();
    }
  });

  async function load() {
    if (!taskId) { get("notice").textContent = "请从任务卡打开团队模拟运行。"; return; }
    const {task, runs, assistant_instructions: fixed} = await api("/api/team/runs?" + new URLSearchParams({task_id: taskId}));
    instructions = typeof fixed === "string" ? fixed : "";
    get("task-title").textContent = task.title;
    get("task-goal").textContent = task.goal || "";
    document.title = task.title + " · 团队模拟运行";
    get("brief").value = task.goal || task.title;
    const saved = sessionStorage.getItem(pendingKey);
    if (saved) {
      pending = JSON.parse(saved);
      for (const key of ["scenario", "lead", "brief"]) get(key).value = pending[key];
      get("assistant").value = pending.assistant === "deepseek" ? "deepseek" : "off";
      get("assistant-question").value = pending.assistant_question || "";
      get("start-status").textContent = "上次启动结果待确认，可重试同一请求。";
    }
    archived = task.status === "archived";
    if (archived) get("notice").textContent = "任务已归档，可回看已有运行及证据。";
    get("launch").hidden = archived;
    get("launch").open = Boolean(pending) || !runs.length;
    updateLaunch();
    get("workspace").hidden = false;
    const activeId = query.get("run_id") || runs[0]?.id;
    showList(runs, activeId);
    if (activeId) {
      if (!runs.some(r => r.id === activeId)) {
        get("notice").textContent = "此运行不在当前任务卡中，请从运行记录选择。";
        return;
      }
      showRun(await api("/api/team/run?" + new URLSearchParams({id: activeId})));
    }
  }

  load().catch(error => { get("notice").textContent = "读取本机运行记录失败，请刷新重试。 " + error.message; });
})();
