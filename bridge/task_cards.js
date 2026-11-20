"use strict";

const taskCards = (() => {
  const cards = new Map();
  const actors = {user: "你", codex: "小龙女", deepseek: "大肥鱼"};
  const states = {pending: "待接手", active: "进行中", waiting: "等待中", done: "已完成", archived: "已归档"};
  const grid = document.getElementById("task-grid");
  const empty = document.getElementById("no-tasks");
  const form = document.getElementById("new-task-form");
  const formStatus = document.getElementById("new-task-status");

  function add(parent, tag, text = "", className = "") {
    const element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    parent.append(element);
    return element;
  }

  function rememberOpen(element, id) {
    const key = "workbench:expanded:" + id;
    try { element.open = localStorage.getItem(key) === "yes"; } catch (_) {}
    element.addEventListener("toggle", () => {
      try { localStorage.setItem(key, element.open ? "yes" : "no"); } catch (_) {}
    });
  }

  function updateSummary(card, at) {
    card.stateLabel.textContent = states[card.status] || card.status;
    card.stateLabel.dataset.state = card.status;
    const participants = [...card.participants].map(role => actors[role]).filter(Boolean).join("、");
    card.summary.textContent = participants + " · " + card.count + " 条消息 · " + new Date(at).toLocaleString();
    card.preview.textContent = card.previewText || "暂无消息";
  }

  function addTeamRun(details, taskId) {
    const query = new URLSearchParams({task_id: taskId});
    const section = add(details, "section", "", "task-goal");
    const link = add(section, "a", "团队模拟运行");
    link.style.color = "#a4c8ff";
    link.href = "/team?" + query;
    const latest = add(section, "p", "", "task-hint");
    const milestones = {started: "模拟运行已启动", waiting: "等主人", revised: "已开返工轮",
      stopped: "已收工", completed: "模拟完成"};
    const statuses = {running: "运行中", waiting: "等主人", revised: "已开返工轮", stopped: "已收工", completed: "模拟完成"};
    let generation = 0;
    async function refresh() {
      if (!details.open) return;
      const version = ++generation;
      try {
        const response = await fetch("/api/team/runs?" + query, {cache: "no-store"});
        if (!response.ok) throw new Error();
        const {runs} = await response.json();
        if (version !== generation) return;
        latest.replaceChildren();
        if (!runs.length) return;
        const run = runs[0];
        const round = run.round === 1 ? "首轮" : "返工第 " + (run.round - 1) + " 轮";
        const recent = add(latest, "a", "最近运行 · " + round + " · " + (statuses[run.status] || run.status));
        recent.href = "/team?" + new URLSearchParams({task_id: taskId, run_id: run.id});
        recent.style.color = "#a4c8ff";
        const result = await fetch("/api/team/run?" + new URLSearchParams({id: run.id}), {cache: "no-store"});
        if (!result.ok) return;
        const record = await result.json();
        if (version !== generation) return;
        for (const row of record.history.filter(row => milestones[row.event])) {
          add(latest, "span", " · " + milestones[row.event]);
        }
      } catch (_) {
        if (version === generation && !latest.children.length) latest.textContent = "团队运行状态暂时无法读取。";
      }
    }
    details.addEventListener("toggle", refresh);
    return refresh;
  }

  function createCard(event) {
    const details = document.createElement("details");
    details.className = "task-card";
    details.dataset.taskId = event.task_id;
    grid.prepend(details);
    rememberOpen(details, event.task_id);
    const heading = add(details, "summary");
    add(heading, "span", event.title, "task-title");
    const stateLabel = add(heading, "span", "", "task-state");
    const summary = add(heading, "div", "", "task-summary");
    const preview = add(heading, "div", "", "task-preview");
    if (event.goal) {
      add(details, "p", "任务说明（调用大肥鱼时会发送）", "task-hint task-goal");
      add(details, "p", event.goal, "body");
    }
    const refreshTeam = addTeamRun(details, event.task_id);
    const history = add(details, "section");
    history.setAttribute("aria-label", event.title + "的对话");
    const card = {
      stateLabel, summary, preview, history, refreshTeam, status: event.status, previewText: "", count: 0,
      participants: new Set([event.role]), calls: new Map(),
    };
    cards.set(event.task_id, card);
    updateSummary(card, event.at);
    empty.hidden = true;
  }

  function entry(card, title, text) {
    const group = add(card.history, "article", "", "task-entry");
    add(group, "h3", title);
    if (text) add(group, "p", text, "body");
    return group;
  }

  function showDispatch(card, event) {
    const shared = event.mode === "shared";
    const group = entry(card, "小龙女分派 · " + (shared ? "共享上下文" : "独立回答"), event.question);
    const state = add(group, "p", "进行中", "call-state");
    state.setAttribute("role", "status");
    if (shared && !event.context_ids.length) add(group, "p", "本次没有带任何对话内容", "task-hint");
    const details = add(group, "details");
    add(details, "summary", "提交给 Harness 的提示词");
    add(details, "p", "上下文消息 ID：" + (event.context_ids.join("、") || "无"), "task-hint");
    if (event.dropped_ids.length) {
      add(details, "p", "因上限跳过的消息 ID：" + event.dropped_ids.join("、"), "task-hint");
    }
    add(details, "pre", event.text, "body");
    rememberOpen(details, "prompt:" + event.call_id);
    // A call without timeout_seconds waits until DeepSeek answers; it never expires here.
    const limited = Number.isInteger(event.timeout_seconds);
    card.calls.set(event.call_id, {
      state, sent: Date.parse(event.at),
      expires: limited ? Date.parse(event.at) + event.timeout_seconds * 1000 : Infinity,
      result: null, reply: false, received: null,
    });
  }

  function applyEvents(events) {
    for (const event of events) {
      if (event.kind === "task_create") {
        createCard(event);
        continue;
      }
      const card = cards.get(event.task_id);
      if (!card) continue;
      if (actors[event.role]) card.participants.add(event.role);
      if (event.kind === "task_status") {
        card.status = event.to;
        entry(card, "任务状态 · " + states[event.to], event.note);
      } else if (event.kind === "thread_dispatch") {
        showDispatch(card, event);
        card.previewText = event.question.slice(0, 160);
        card.count++;
      } else if (event.kind === "thread_result") {
        const call = card.calls.get(event.call_id);
        if (call) call.result = event;
      } else if (event.kind === "thread_received") {
        const call = card.calls.get(event.call_id);
        if (call) call.received = Date.parse(event.at);
      } else if (event.kind === "thread_reply" || event.kind === "thread_message") {
        entry(card, (actors[event.role] || event.role) + " · " + (event.share ? "可共享" : "仅本地"), event.text);
        if (event.kind === "thread_reply") {
          const call = card.calls.get(event.call_id);
          if (call) call.reply = true;
        }
        card.previewText = event.text.slice(0, 160);
        card.count++;
      }
      updateSummary(card, event.at);
    }
  }

  function refreshCallStates() {
    for (const card of cards.values()) {
      for (const call of card.calls.values()) {
        const result = call.result;
        let state = "running";
        const minutes = Math.floor((Date.now() - call.sent) / 60000);
        let label = call.received ? "本机 Harness 已开始处理，等待大肥鱼回复" + (minutes > 0 ? " · 已等 " + minutes + " 分钟" : "")
          : "已提交，等待本机 Harness 报告开始处理";
        if (result?.state === "finished") {
          state = call.reply ? "finished" : "incomplete";
          label = call.reply ? "调用完成" : "调用成功，回复未保存";
        } else if (result) {
          state = result.state;
          label = (state === "timeout" ? "已超时" : "调用失败") + (result.error ? " · " + result.error : "");
        } else if (Date.now() >= call.expires) {
          state = "incomplete";
          label = "无结果（可能中断）";
        }
        if (call.state.textContent !== label) call.state.textContent = label;
        if (call.state.dataset.state !== state) call.state.dataset.state = state;
      }
    }
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();
    const button = form.querySelector("button");
    if (button.disabled) return;
    const values = new FormData(form);
    button.disabled = true;
    formStatus.textContent = "正在创建…";
    try {
      const response = await fetch("/api/tasks", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({title: values.get("title"), goal: values.get("goal")}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "请求未成功");
      form.reset();
      formStatus.textContent = "已创建「" + data.title + "」，等待小龙女接手。";
    } catch (error) {
      formStatus.textContent = "创建失败：" + error.message;
    } finally {
      button.disabled = false;
    }
  });

  window.addEventListener("pageshow", event => {
    if (event.persisted) for (const card of cards.values()) card.refreshTeam();
  });
  rememberOpen(document.getElementById("uncategorized"), "uncategorized");
  return {applyEvents, refreshCallStates};
})();
