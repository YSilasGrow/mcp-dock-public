"""Local task conversations and bridge history for the browser workbench."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import tasks
from .events import read_events
from .research import all_snapshots

TASK_CARDS_SCRIPT = Path(__file__).with_name("task_cards.js")
MAX_TASK_REQUEST_BYTES = 64 * 1024

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>小龙女 × 大肥鱼 · 协作工作台</title>
  <style>
    :root { color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; background: #10131b; color: #f1f4fb; }
    header { position: sticky; top: 0; z-index: 1; padding: 18px 24px; background: #171c28; border-bottom: 1px solid #303849; }
    h1 { margin: 0; font-size: 20px; font-weight: 650; }
    .sub { color: #aeb8ce; font-size: 13px; margin-top: 6px; }
    #state { color: #8ee0b7; }
    main { max-width: 980px; margin: 0 auto; padding: 22px 18px 70px; }
    .empty { color: #aeb8ce; text-align: center; padding: 80px 10px; }
    article { margin: 0 0 14px; padding: 17px 19px; border-radius: 14px; background: #1b2230; border: 1px solid #313b50; }
    article.codex { border-left: 4px solid #93b5ff; }
    article.deepseek { border-left: 4px solid #6bd4b0; }
    article.system { border-left: 4px solid #d8bc7c; }
    .meta { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; color: #afbbcf; font-size: 12px; margin-bottom: 10px; }
    .name { color: #f1f4fb; font-weight: 700; font-size: 14px; }
    .kind { padding: 3px 8px; border-radius: 999px; background: #2b3549; }
    .body { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; font-size: 15px; }
    .notice { margin: 0 0 18px; padding: 12px 15px; border-radius: 10px; background: #242d3e; color: #bdc8dd; font-size: 13px; }
    .research-card { border-left: 4px solid #d6acfa; }
    .research-card h2 { font-size: 17px; margin: 0 0 8px; overflow-wrap: anywhere; }
    .research-card h3 { font-size: 14px; margin: 12px 0 5px; }
    .research-card p, .research-card li { overflow-wrap: anywhere; line-height: 1.5; }
    .research-card details { margin-top: 10px; }
    .research-card summary { cursor: pointer; color: #cbd8f7; }
    .research-card a { color: #a4c8ff; overflow-wrap: anywhere; }
    .research-card .warning { color: #ffd59d; }
    .task-header { display: flex; justify-content: space-between; align-items: baseline; gap: 14px; }
    .task-header h2 { font-size: 18px; }
    .task-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 290px), 1fr)); gap: 12px; align-items: start; }
    .task-card, .old-feed, .create-task { background: #1b2230; border: 1px solid #313b50; border-radius: 12px; padding: 14px 16px; min-width: 0; }
    .task-card[open] { grid-column: 1 / -1; }
    .task-card summary, .old-feed summary, .create-task summary, .task-entry details summary { cursor: pointer; }
    .task-card summary { color: #dce7ff; }
    .task-title { font-size: 16px; font-weight: 700; overflow-wrap: anywhere; }
    .task-state { margin-left: 8px; padding: 2px 7px; border-radius: 8px; font-size: 12px; background: #314263; color: #e3edff; }
    .task-state[data-state="active"] { background: #1f4e43; color: #b6f6dd; }
    .task-state[data-state="done"], .task-state[data-state="archived"] { background: #343b4a; color: #d6deec; }
    .task-summary, .task-preview, .task-hint { margin: 7px 0 0; font-size: 13px; color: #afbbcf; overflow-wrap: anywhere; }
    .task-preview { color: #e2e9f7; }
    .task-goal { border-top: 1px solid #313b50; padding-top: 12px; }
    .task-entry { margin-top: 12px; padding: 12px 14px; border-radius: 8px; background: #242d3e; }
    .task-entry h3 { margin: 0 0 8px; font-size: 14px; }
    .task-entry details { margin-top: 8px; }
    .task-entry .body { margin: 0; }
    .call-state { margin: 8px 0 0; color: #aee5d3; font-size: 13px; overflow-wrap: anywhere; }
    .call-state[data-state="failed"], .call-state[data-state="timeout"], .call-state[data-state="incomplete"] { color: #ffd59d; }
    .create-task { margin-bottom: 16px; }
    .create-task summary { width: max-content; color: #a4c8ff; font-weight: 700; }
    .create-task form { display: grid; gap: 8px; margin: 12px 0 0; max-width: 650px; }
    .create-task input, .create-task textarea { box-sizing: border-box; width: 100%; padding: 8px 10px; border: 1px solid #44516a; border-radius: 6px; background: #121925; color: inherit; font: inherit; }
    .create-task button { width: max-content; border-radius: 6px; border: 0; padding: 8px 12px; background: #a4c8ff; color: #131a28; cursor: pointer; font: inherit; font-weight: 700; }
    .old-feed { margin-top: 26px; }
    .old-feed[open] summary { margin-bottom: 14px; }
    .task-empty { padding: 28px 8px; }
    @media (max-width: 600px) { header { padding: 14px; } main { padding: 14px; } }
  </style>
</head>
<body>
  <header>
    <h1>小龙女 × 大肥鱼 · 协作工作台</h1>
    <div class="sub">本机协作 · <span id="state">连接中</span></div>
  </header>
  <main>
    <div class="task-header"><h2>任务</h2></div>
    <details class="create-task">
      <summary>新任务</summary>
      <form id="new-task-form">
        <label for="task-title">标题</label>
        <input id="task-title" name="title" maxlength="200" required>
        <label for="task-goal">任务说明（可选）</label>
        <textarea id="task-goal" name="goal" maxlength="5000" rows="3" aria-describedby="goal-sharing"></textarea>
        <small id="goal-sharing" class="task-hint">调用大肥鱼时，两种模式都会发送这段说明。</small>
        <button type="submit">创建任务</button>
        <span id="new-task-status" class="task-hint" role="status"></span>
      </form>
    </details>
    <section id="task-grid" class="task-grid" aria-label="任务卡片"></section>
    <p id="no-tasks" class="empty task-empty">还没有任务。可以在这里新建，或在 Codex 中请小龙女创建。</p>
    <details id="uncategorized" class="old-feed">
      <summary>未归类记录</summary>
      <section id="research" aria-label="协作检索轮次"></section>
      <div id="empty" class="empty">还没有未归类记录。</div>
      <div id="feed"></div>
    </details>
  </main>
  <script src="/assets/task-cards.js"></script>
  <script>
    let cursor = 0;
    const feed = document.getElementById("feed");
    const empty = document.getElementById("empty");
    const state = document.getElementById("state");
    const research = document.getElementById("research");
    let refreshResearch = true;
    const roles = {codex: "小龙女", deepseek: "大肥鱼", system: "桥接状态"};
    const kinds = {question: "提问", answer: "回复", feedback: "复核", status: "运行中", error: "错误"};
    const states = {completed: "完成", partial: "部分完成", aborted: "已中止"};
    const retrievals = {opened: "已打开", search_only: "仅搜索结果", open_failed: "抓取失败", no_source: "无来源"};
    function addEvent(item) {
      empty.hidden = true;
      const card = document.createElement("article");
      card.className = ["codex", "deepseek", "system"].includes(item.role) ? item.role : "system";
      const meta = document.createElement("div");
      meta.className = "meta";
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = roles[item.role] || item.role;
      const kind = document.createElement("span");
      kind.className = "kind";
      kind.textContent = kinds[item.kind] || item.kind;
      const time = document.createElement("time");
      time.textContent = new Date(item.at).toLocaleString();
      const call = document.createElement("span");
      call.textContent = item.call_id ? "轮次 " + item.call_id.slice(0, 8) : "";
      meta.append(name, kind, time, call);
      const body = document.createElement("div");
      body.className = "body";
      body.textContent = item.text;
      card.append(meta, body);
      feed.append(card);
    }
    function line(parent, tag, value, className) {
      const node = document.createElement(tag);
      node.textContent = value;
      if (className) node.className = className;
      parent.append(node);
      return node;
    }
    function safeLink(parent, value) {
      try {
        const url = new URL(value);
        if (!(url.protocol === "http:" || url.protocol === "https:")) return line(parent, "span", value);
        const link = document.createElement("a");
        link.href = url.href;
        link.textContent = value;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        parent.append(link);
      } catch (_) { line(parent, "span", value); }
    }
    function renderResearch(rounds) {
      research.replaceChildren();
      for (const round of rounds) {
        const card = document.createElement("article");
        card.className = "research-card";
        line(card, "h2", round.question_public);
        line(card, "p", "研究编号 " + round.research_id + " · 流程 " + (states[round.state] || round.state) + " · 核验记录覆盖 " + round.coverage);
        if (round.warning) line(card, "p", round.warning, "warning");
        const details = document.createElement("details");
        details.open = !(round.state || "").startsWith("未收尾");
        line(details, "summary", "结论、来源与实际提示词");
        for (const claim of round.claims) {
          line(details, "h3", claim.claim_id + " · " + claim.text);
          line(details, "p", claim.labels.join(" · "));
          for (const item of claim.evidence) {
            const row = document.createElement("p");
            const retrieval = item.retrieval_state === "opened"
              ? (roles[item.opened_by] || item.opened_by) + "记录已打开"
              : (retrievals[item.retrieval_state] || item.retrieval_state);
            line(row, "span", (roles[item.reported_by] || item.reported_by) + " 提供 · " + retrieval + " · " + item.support + " · ");
            if (item.url) safeLink(row, item.url);
            if (item.quote) line(row, "span", " · 摘录：" + item.quote);
            details.append(row);
          }
        }
        for (const item of round.exchanges || []) {
          if (item.kind === "question") {
            line(details, "h3", "实际发给大肥鱼的提示词 · " + (item.template_version || ""));
            line(details, "p", item.text, "body");
          }
        }
        card.append(details);
        research.append(card);
      }
    }
    async function poll() {
      try {
        const response = await fetch("/api/events?after=" + cursor, {cache: "no-store"});
        if (!response.ok) throw new Error("HTTP " + response.status);
        const data = await response.json();
        taskCards.applyEvents(data.events.filter(item => item.task_id));
        const legacy = data.events.filter(item => !item.task_id);
        legacy.filter(item => !item.kind.startsWith("research_")).forEach(addEvent);
        cursor = data.next;
        refreshResearch ||= legacy.some(item => item.research_id);
        if (refreshResearch) {
          const researchResponse = await fetch("/api/research", {cache: "no-store"});
          if (!researchResponse.ok) throw new Error("HTTP " + researchResponse.status);
          renderResearch(await researchResponse.json());
          refreshResearch = false;
        }
        state.textContent = "已连接";
      } catch (error) {
        state.textContent = "连接中断，正在重试";
      } finally {
        taskCards.refreshCallStates();
        setTimeout(poll, 900);
      }
    }
    poll();
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _allowed_host(self):
        allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        hosts = self.headers.get_all("Host", [])
        return hosts[0] if len(hosts) == 1 and hosts[0] in allowed_hosts else None

    def do_GET(self):
        if self._allowed_host() is None:
            self.send_error(403, "local workbench only")
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/assets/task-cards.js":
            self._send(TASK_CARDS_SCRIPT.read_bytes(), "text/javascript; charset=utf-8")
            return
        if parsed.path == "/api/events":
            raw = parse_qs(parsed.query).get("after", ["0"])[0]
            try:
                payload = read_events(after=int(raw))
            except ValueError:
                self.send_error(400, "invalid cursor")
                return
            self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/research":
            self._send(json.dumps(all_snapshots(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        self.send_error(404)

    def do_POST(self):
        host = self._allowed_host()
        if host is None or self.headers.get_all("Origin", []) != [f"http://{host}"]:
            self._send_json({"error": "只允许从同源工作台创建任务"}, 403)
            return
        if self.path != "/api/tasks":
            self._send_json({"error": "接口不存在"}, 404)
            return
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].partition(";")[0].strip().lower() != "application/json":
            self._send_json({"error": "请求必须为 application/json"}, 415)
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                or self.headers.get("Transfer-Encoding") is not None):
            self._send_json({"error": "请求体长度无效"}, 400)
            return
        digits = lengths[0].lstrip("0") or "0"
        if len(digits) > len(str(MAX_TASK_REQUEST_BYTES)) or int(digits) > MAX_TASK_REQUEST_BYTES:
            self._send_json({"error": "请求体超过 64 KiB"}, 413)
            return
        length = int(digits)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("请求体不完整")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) - {"title", "goal"}:
                raise ValueError("只接受标题和任务说明")
            task = tasks.create_task(payload.get("title"), payload.get("goal", ""), actor="user")
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            self._send_json({"error": "请求体不是有效的 JSON"}, 400)
            return
        except ValueError as error:
            self._send_json({"error": str(error)}, 400)
            return
        self._send_json(task, 201)

    def _send_json(self, payload, status):
        self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def _send(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


def make_server(host="127.0.0.1", port=57420):
    if host != "127.0.0.1":
        raise ValueError("workbench must bind to 127.0.0.1")
    return ThreadingHTTPServer((host, port), Handler)


def serve(host="127.0.0.1", port=57420):
    with make_server(host, port) as server:
        server.serve_forever()
