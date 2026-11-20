// Real browser + HTTP server with a private temporary task journal. No external services.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const { tmpdir } = require("node:os");
const path = require("node:path");
const readline = require("node:readline");
const { chromium } = require("playwright");

const project = path.resolve(__dirname, "..");
const bootstrap = `
import os, sys
from pathlib import Path
from bridge.workbench import make_server
def audit(event, args):
    if event in ("subprocess.Popen", "os.posix_spawn", "socket.connect", "socket.getaddrinfo"):
        Path(os.environ["SIM_AUDIT"]).write_text(event)
        raise RuntimeError(f"Unexpected outbound operation during local simulation: {event}")
sys.addaudithook(audit)
with make_server(port=int(sys.argv[1])) as server:
    print(server.server_port, flush=True)
    server.serve_forever()
`;

async function launch(dataDir, port = 0) {
  const child = spawn(path.join(project, ".venv/bin/python"), ["-c", bootstrap, String(port)], {
    cwd: project, stdio: ["ignore", "pipe", "inherit"],
    env: {...process.env, DEEPSEEK_BRIDGE_EVENTS: path.join(dataDir, "events.jsonl"),
      SIM_AUDIT: path.join(dataDir, "unexpected-outbound")},
  });
  const lines = readline.createInterface({input: child.stdout});
  const exited = once(child, "exit");
  const ready = await Promise.race([once(lines, "line").then(([line]) => Number(line)),
    exited.then(() => { throw new Error("Test workbench exited before listening"); })]);
  return {port: ready, close: async () => { child.kill(); await exited; }};
}

test("a real task card can simulate, survive a restart and stop after two approved revisions", {timeout: 90000}, async () => {
  const dir = await fs.mkdtemp(path.join(tmpdir(), "dock-browser-stage-one-"));
  let server, browser;
  try {
    server = await launch(dir);
    const origin = "http://127.0.0.1:" + server.port;
    browser = await chromium.launch({channel: "chrome", headless: true, args: ["--disable-background-networking"]});
    const context = await browser.newContext({locale: "zh-CN"});
    const outside = [], errors = [];
    await context.route("**/*", route => {
      const url = new URL(route.request().url());
      if (url.origin === origin) return route.continue();
      outside.push(url.origin);
      return route.abort();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(6000);
    page.on("pageerror", e => errors.push(e.message));
    await page.goto(origin);
    await page.locator(".create-task > summary").click();
    await page.locator("#new-task-form [name=title]").fill("合成验收卡");
    await page.locator("#new-task-form [name=goal]").fill("检查标签样例");
    await page.locator("#new-task-form button").click();
    await page.locator(".task-card > summary").click();
    await page.locator(".task-card a[href*='/team?']").first().click();
    const launchForm = page.locator("#launch");
    const taskId = new URL(page.url()).searchParams.get("task_id");
    async function start(scenario) {
      await launchForm.waitFor();
      if (!await launchForm.evaluate(e => e.open)) await launchForm.locator(":scope > summary").click();
      await page.getByLabel("模拟场景").selectOption(scenario);
      await page.getByRole("button", {name: "启动模拟运行"}).click();
      await page.waitForFunction(() => !document.querySelector("#launch").open);
    }
    async function state(expected) {
      await page.locator('#detail > .row .state[data-state="' + expected + '"]').waitFor();
    }
    for (const [scenario, label] of [["timeout","重试这一步"],["missing","补齐并继续"],
                                     ["limit","增加 1 次模拟调用"],["choice","选择继续"]]) {
      await start(scenario);
      await state("waiting");
      if (scenario === "timeout") {
        const port = server.port;
        await server.close();
        server = await launch(dir, port);
        await page.reload();
        await state("waiting");
      }
      if (scenario === "missing") await page.getByLabel("补充材料").fill("新的合成输入");
      await page.getByRole("button", {name: label, exact: true}).click();
      await state("completed");
    }
    await start("review");
    for (let i = 0; i < 2; i++) {
      const previous = page.url();
      await page.getByRole("button", {name: "批准返工一轮"}).click();
      await page.waitForURL(url => url.href !== previous);
    }
    await page.locator("#detail .help").getByText(/已追加返工 2 轮/).waitFor();
    assert.equal(await page.getByRole("button", {name: "批准返工一轮"}).count(), 0);
    await page.getByRole("button", {name: "收工", exact: true}).click();
    await state("stopped");
    await page.reload();
    await state("stopped");
    const {runs} = await (await context.request.get(origin + "/api/team/runs?task_id=" + taskId)).json();
    assert.equal(runs.length, 7);
    assert.ok(runs.every(r => r.cloud_calls === 0));
    const revisions = runs.filter(r => r.scenario === "review");
    assert.deepEqual(revisions.map(r => r.round), [3, 2, 1]);
    assert.equal(revisions[0].parent_id, revisions[1].id);
    assert.equal(revisions[1].parent_id, revisions[2].id);
    assert.deepEqual(outside, []);
    assert.deepEqual(errors, []);
    assert.ok(!(await fs.readdir(dir)).includes("unexpected-outbound"));
  } finally {
    if (browser) await browser.close();
    if (server) await server.close();
    await fs.rm(dir, {recursive: true, force: true});
  }
});
