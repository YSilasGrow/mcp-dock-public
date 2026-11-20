// Run with Node's test runner and Playwright; all pages and API responses are fixtures.
const { after, before, test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const bridge = path.join(__dirname, "../bridge");
const origin = "http://127.0.0.1:18765";
let browser;

before(async () => {
  browser = await chromium.launch({
    channel: "chrome", headless: true,
    args: ["--disable-background-networking"],
  });
});
after(async () => { await browser?.close(); });

const fixedInstructions = "仅依据本次提供的问题回答。";
const task = {task_id: "sample-card", title: "合成任务", goal: "验证表单", status: "pending"};
function run(id, round, kind, actions) {
  return {
    id, round, task_id: task.task_id, root_id: "root", parent_id: round > 1 ? "root" : null,
    status: kind ? "waiting" : "completed", scenario: "review", template: "development-review",
    brief: "接口校验", lead: "codex", cloud_calls: 0,
    attempts: [{node: "review", number: 1, status: "failed",
      inputs: {patch: "合成输入 <i>示例</i>"}, output: {note: "合成复核证据"}}],
    help: kind ? {id: "help-" + id, kind, node: "review", message: "合成求助", actions} : null,
    decisions: [], history: [{at: "2026-11-18T12:00:00Z", event: "started"}],
  };
}

async function panel(t, initial, post, taskStatus = task.status) {
  const context = await browser.newContext({locale: "zh-CN", viewport: {width: 390, height: 844}});
  t.after(() => context.close());
  const page = await context.newPage();
  page.setDefaultTimeout(3500);
  const runs = initial;
  const requests = [];
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await context.route("**/*", async route => {
    const req = route.request();
    const url = new URL(req.url());
    assert.equal(url.origin, origin);
    const pathname = url.pathname;
    if (pathname === "/team" || pathname === "/assets/team-run.js") {
      const html = pathname === "/team";
      const file = path.join(bridge, html ? "team_run.html" : "team_run.js");
      return route.fulfill({status: fs.existsSync(file) ? 200 : 404,
        contentType: html ? "text/html; charset=utf-8" : "text/javascript; charset=utf-8",
        body: fs.existsSync(file) ? fs.readFileSync(file) : "Missing team panel"});
    }
    if (pathname === "/api/team/runs") return route.fulfill({json: {task: {...task, status: taskStatus}, runs, assistant_instructions: fixedInstructions}});
    if (pathname === "/api/team/run") {
      return route.fulfill({json: runs.find(r => r.id === url.searchParams.get("id"))});
    }
    if (req.method() === "POST") {
      const payload = req.postDataJSON();
      requests.push({pathname, payload});
      const next = await post(pathname, payload);
      if (!next) return route.abort("failed");
      const i = runs.findIndex(r => r.id === next.id);
      if (i >= 0) runs[i] = next;
      else runs.unshift(next);
      return route.fulfill({json: next});
    }
    return route.fulfill({status: 404, body: ""});
  });
  await page.goto(origin + "/team?task_id=" + task.task_id);
  await page.getByRole("heading", {name: task.title}).waitFor({timeout: 1600});
  return {page, requests, errors};
}

test("retains the original launch request across reload and a transport retry", async t => {
  let calls = 0;
  const {page, requests, errors} = await panel(t, [], () => ++calls === 1 ? null : run("root", 1));
  await page.getByLabel("本轮说明").fill("模拟验收");
  await page.getByLabel("模拟场景").selectOption("timeout");
  await page.getByLabel("主做与复核").selectOption("claude");
  await page.getByRole("button", {name: "启动模拟运行"}).click();
  await page.getByText(/可重试同一请求/).waitFor();
  await page.reload();
  await page.getByRole("button", {name: "重试启动"}).click();
  await page.getByText("合成复核证据", {exact: false}).first().waitFor({state: "attached"});
  assert.equal(requests.length, 2);
  assert.equal(requests[0].pathname, "/api/team/start");
  assert.deepEqual(requests[0].payload, requests[1].payload);
  assert.ok(requests[0].payload.request_id);
  assert.deepEqual({...requests[0].payload, request_id: 0}, {
    request_id: 0, task_id: "sample-card", brief: "模拟验收", scenario: "timeout", lead: "claude",
  });
  assert.deepEqual(errors, []);
});

for (const [kind, action, label, value] of [
  ["missing", "supply", "补齐并继续", "合成材料"],
  ["error", "retry", "重试这一步", undefined],
  ["limit", "extend", "增加 1", "1"],
  ["choice", "choose", "选择继续", "continue"],
]) {
  test("renders and answers " + kind + " help with its bound action", async t => {
    const first = run("root", 1, kind, [action, "stop"]);
    const {page, requests, errors} = await panel(t, [first], () => ({...first, status: "completed", help: null}));
    await page.getByText("合成求助").waitFor();
    if (kind === "missing") await page.getByLabel("补充材料").fill(value);
    await page.getByRole("button", {name: label, exact: false}).click();
    await page.getByText("合成求助").waitFor({state: "detached"});
    const payload = requests[0].payload;
    assert.equal(requests[0].pathname, "/api/team/answer");
    assert.equal(payload.run_id, "root");
    assert.equal(payload.help_id, "help-root");
    assert.equal(payload.action, action);
    if (value !== undefined) assert.equal(payload.value, value);
    assert.equal(await page.locator("i").count(), 0, "treat data as text, including markup");
    assert.deepEqual(errors, []);
  });
}

test("opening a rework child keeps its parent round and evidence reachable on mobile", async t => {
  const parent = run("root", 1, "error", ["rework", "stop"]);
  const child = run("child", 2);
  const initial = [parent];
  const {page, requests} = await panel(t, initial, () => {
    parent.status = "revised"; parent.help = null;
    return child;
  });
  await page.getByRole("button", {name: /批准.*返工/}).click();
  await page.waitForURL("**run_id=child*");
  assert.equal(requests[0].payload.action, "rework");
  await page.getByRole("link", {name: /首轮/}).first().click();
  await page.waitForURL("**run_id=root*");
  await page.getByText("合成复核证据", {exact: false}).first().waitFor({state: "attached"});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
});

test("task card exposes its team route and loads local milestones only when expanded", async t => {
  const context = await browser.newContext();
  t.after(() => context.close());
  const page = await context.newPage();
  const endpoints = [];
  const recent = run("recent", 1, "missing", ["supply", "stop"]);
  recent.history = [{at: "2026-11-18T12:00:00Z", event: "started"},
    {at: "2026-11-18T12:02:00Z", event: "waiting"}];
  await context.route("**/*", async route => {
    const url = new URL(route.request().url());
    assert.equal(url.origin, origin);
    if (url.pathname === "/") return route.fulfill({contentType: "text/html; charset=utf-8", body:
      '<details id="uncategorized"></details><form id="new-task-form"></form><span id="new-task-status"></span>' +
      '<div id="task-grid"></div><p id="no-tasks"></p><script src="/assets/task-cards.js"></script>'});
    if (url.pathname === "/assets/task-cards.js") return route.fulfill({contentType: "text/javascript; charset=utf-8",
      body: fs.readFileSync(path.join(bridge, "task_cards.js"))});
    endpoints.push(url.pathname);
    if (url.pathname === "/api/team/runs") return route.fulfill({json: {task, runs: [recent]}});
    if (url.pathname === "/api/team/run") return route.fulfill({json: recent});
    return route.fulfill({status: 404, body: ""});
  });
  await page.goto(origin + "/");
  await page.evaluate(task => taskCards.applyEvents([{kind: "task_create", role: "user",
    at: "2026-11-18T12:00:00Z", ...task}]), task);
  assert.deepEqual(endpoints, []);
  await page.getByText(task.title, {exact: true}).click();
  const latest = page.getByRole("link", {name: /最近.*等主人/});
  assert.equal(await latest.waitFor({timeout: 1200}).then(() => true, () => false), true,
    "expanded task card must show current run status");
  const target = new URL(await latest.getAttribute("href"), origin);
  assert.equal(target.pathname, "/team");
  assert.equal(target.searchParams.get("run_id"), recent.id);
  assert.equal(target.searchParams.get("task_id"), task.task_id);
  await page.getByText(/模拟运行已启动/).waitFor();
  assert.deepEqual(endpoints, ["/api/team/runs", "/api/team/run"]);
});

test("archived card retains its history and displays the pending question read-only", async t => {
  const {page, requests} = await panel(t, [run("root", 1, "error", ["retry", "stop"])],
    () => assert.fail("archived UI submitted an action"), "archived");
  await page.getByText("合成求助").waitFor();
  assert.equal(await page.getByRole("button").count(), 0);
  await page.getByText(/任务已归档/).first().waitFor();
  assert.deepEqual(requests, []);
});

test("patch evidence preserves real diff lines and decisions retain the answered question", async t => {
  const first = run("root", 1);
  const diff = "--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-return 1\n+return 2";
  first.attempts = [{node: "lead", number: 1, status: "success", inputs: {}, output: {patch: {diff}}}];
  first.decisions = [{kind: "error", node: "lead", question: "上次模拟已超时。", action: "retry", at: ""}];
  const {page} = await panel(t, [first], () => {});
  const hasDiff = await page.locator("pre").evaluateAll(nodes => nodes.some(n => n.textContent.includes("\n+return 2")));
  assert.equal(hasDiff, true, "show the line-oriented patch without JSON escapes");
  await page.getByText("上次模拟已超时。").waitFor();
});

test("details show the snapshotted template and supplied brief, and omit empty handoffs", async t => {
  const first = run("root", 1);
  first.template = {version: "development-review-sim-v1"};
  first.source = {brief: "后来补上的输入", revision: {}};
  const {page} = await panel(t, [first], () => {});
  await page.getByText("合成复核证据", {exact: false}).waitFor({state: "attached"});
  assert.equal(await page.getByText("后来补上的输入", {exact: true}).count(), 1);
  assert.equal(await page.getByText(/development-review-sim-v1/).count(), 1);
  assert.equal(await page.getByText("上一轮交接材料", {exact: true}).count(), 0);
});

function assistantRun(status, extra = {}) {
  return {...run("live", 1), status, assistant: "deepseek", assistant_question: "独立问题",
    scenario: "success", cloud_calls: 1, attempts: [{node: "opinion", number: 1, inputs: {},
      status: "running", output: {}, progress: {phase: "step_start", at: "2026-11-20T12:00:00Z"}}], ...extra};
}

test("real assistant is opt-in, previews exact prompt, and shows real output after polling", async t => {
  const question = " 第一行\n<em>原样</em> ";
  const history = [];
  const {page, requests, errors} = await panel(t, history, () => assistantRun("running", {assistant_question: question}));
  const assistant = page.getByLabel("助手", {exact: true});
  assert.equal(await assistant.inputValue(), "off");
  await assistant.selectOption("deepseek");
  const input = page.getByLabel("给 DeepSeek 的问题", {exact: true});
  assert.equal(await input.inputValue(), "");
  const start = page.getByRole("button", {name: "启动并发送给 DeepSeek"});
  await start.click();
  assert.deepEqual(requests, []);
  await input.fill(question);
  assert.equal(await page.locator("#assistant-preview").textContent(), fixedInstructions + "\n\n" + question);
  await start.click();
  await page.getByText(/助手步骤已开始/).waitFor();
  assert.equal(requests[0].payload.assistant_question, question);
  assert.equal(requests[0].payload.assistant, "deepseek");
  assert.equal(requests[0].pathname, "/api/team/start");
  const answer = "合成意见 <em>长句</em>\n第二段";
  history[0] = assistantRun("completed", {attempts: [{node: "opinion", number: 1, status: "success",
    inputs: {}, output: {opinion: [answer], simulated: false}}]});
  await page.getByRole("heading", {name: "DeepSeek 的回复"}).waitFor();
  assert.equal(await page.getByText(answer, {exact: true}).isVisible(), true);
  await page.getByRole("link", {name: /首轮.*已完成.*DeepSeek/}).waitFor();
  assert.equal(await page.locator("em").count(), 0);
  assert.deepEqual(errors, []);
});

test("polling discovers privacy hold, previews replacement, and allows skipping a failed assistant", async t => {
  const history = [assistantRun("running", {cloud_calls: 0})];
  const actions = [];
  const {page, errors} = await panel(t, history, (_, payload) => {
    actions.push(payload);
    if (payload.action === "replace_question") return assistantRun("waiting", {
      help: {id: "second", kind: "error", node: "opinion", message: "助手调用超时，请决定是否继续。",
        actions: ["skip_assistant", "stop"]}});
    return assistantRun("completed");
  });
  history[0] = assistantRun("waiting", {cloud_calls: 0, help: {id: "first", node: "opinion",
    kind: "choice", message: "输入需要调整。", actions: ["replace_question", "stop"]}});
  await page.getByText("输入需要调整。").waitFor();
  await page.getByRole("link", {name: /首轮.*等主人/}).waitFor();
  const value = "新问题 <样例>";
  const replacement = page.getByLabel("替换发给 DeepSeek 的问题", {exact: true});
  await replacement.fill(value);
  assert.equal(await page.locator("#replacement-preview").textContent(), fixedInstructions + "\n\n" + value);
  await page.getByRole("button", {name: "复检并发送给 DeepSeek"}).click();
  await page.getByText("助手调用超时，请决定是否继续。").waitFor();
  assert.equal(await page.getByRole("button", {name: /重试这一步/}).count(), 0);
  await page.getByRole("button", {name: "跳过助手，继续复核"}).click();
  assert.deepEqual(actions, [
    {run_id: "live", help_id: "first", action: "replace_question", value},
    {run_id: "live", help_id: "second", action: "skip_assistant"},
  ]);
  assert.deepEqual(errors, []);
});

test("a received DeepSeek call waits without a limit and can be stopped", async t => {
  const waiting = assistantRun("running", {attempts: [{node: "opinion", number: 1, inputs: {}, status: "running",
    external_dispatch: true, started_at: "2026-11-20T12:00:00Z", output: {},
    progress: {phase: "step_start", at: "2026-11-20T12:00:01Z"}}]});
  const {page, requests, errors} = await panel(t, [waiting], () => waiting);
  await page.getByText(/本机 Harness 已开始处理.*等待 DeepSeek 回复.*不限时/).waitFor();
  await page.getByRole("button", {name: "停止等待 DeepSeek"}).click();
  await page.getByText("已请求停止", {exact: false}).waitFor();
  assert.deepEqual(requests.map(r => [r.pathname, r.payload]), [["/api/team/stop-waiting", {run_id: "live"}]]);
  assert.deepEqual(errors, []);
});

test("turning assistant off omits its draft question from launch", async t => {
  const {page, requests} = await panel(t, [], () => run("root", 1));
  const assistant = page.getByLabel("助手", {exact: true});
  await assistant.selectOption("deepseek");
  await page.getByLabel("给 DeepSeek 的问题", {exact: true}).fill("草稿");
  await assistant.selectOption("off");
  await page.getByRole("button", {name: "启动模拟运行"}).click();
  await page.waitForURL("**run_id=root*");
  assert.equal(Object.hasOwn(requests[0].payload, "assistant"), false);
  assert.equal(Object.hasOwn(requests[0].payload, "assistant_question"), false);
});
