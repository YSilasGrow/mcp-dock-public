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
    const history = add(details, "section");
    history.setAttribute("aria-label", event.title + "的对话");
    const card = {
      stateLabel, summary, preview, history, status: event.status, previewText: "", count: 0,
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
    card.calls.set(event.call_id, {
      state, expires: Date.parse(event.at) + event.timeout_seconds * 1000,
      result: null, reply: false,
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
        let label = "进行中";
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

  rememberOpen(document.getElementById("uncategorized"), "uncategorized");
  return {applyEvents, refreshCallStates};
})();
