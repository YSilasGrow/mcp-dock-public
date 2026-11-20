# MCP Dock

MCP Dock 是一个带浏览器工作台的本地 MCP 桥接项目。你可以在工作台建任务卡片，在 Codex 对话里分派任务，并回到工作台查看双方的交流与交付记录。

## 当前接入与分工

目前实际接入并使用过的组合只有 **GPT（通过 Codex）与 DeepSeek（通过 DeepSeek Harness）**，由 **GPT 主导**。

- **GPT／Codex（小龙女）**负责领取任务、先做初查、决定何时分派以及共享哪些内容，最后核对来源、整理终稿并标记完成。
- **DeepSeek（大肥鱼）**按分派提供独立意见、补查公开资料、挑错，或在受限范围内协助处理项目文件。

## 已实现功能

- **任务卡片和对话**：在网页创建任务，在 Codex 中领取并更新状态。工作台显示待接手、进行中、等待中、已完成和已归档的状态，以及每张卡片的对话和分派记录。
- **独立与共享分派**：`independent` 发送任务说明和本次问题。`shared` 还可带上近期明确标为可共享的消息。卡片消息默认仅保存在本机。
- **咨询与协作检索**：支持一次性咨询，也支持对公开问题独立检索、互查、记录来源读取状态和分歧，由 Codex 复核并写入最终结论。
- **团队运行**：从任务卡进入团队页，按固定模板在本机模拟主做、独立复核和验证。需要第二意见时，可选 DeepSeek 助手，真实回复会交给模拟复核。缺材料、失败、到限或需要选择时，运行暂停等待确认。每轮返工需单独批准，最多追加两轮。
- **调用进度与排错**：工作台可查看提交给 Harness 的提示词、共享上下文、模型回复和调用结果。Harness 需要在 120 秒内报告本机开始处理，之后默认持续等待回复。卡片和研究调用可通过 `timeout_seconds` 设置 1–600 秒的总时限。工作台会记录超时、失败和可用的脱敏诊断。
- **受限项目委派**：DeepSeek 在临时副本中处理指定项目目录内符合条件的 UTF-8 文本。桥接检查变更后写回，Codex 负责审查和验收。

桥接服务、工作台和事件记录在本机运行。通过 Harness 发送的内容由 DeepSeek 云端模型处理。

## 安装与启动

需要 macOS、Python 3.12、Codex 以及已登录的 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)。项目复用 Harness 的登录。仓库内的 `dsh-auth.patch.yml` 只声明模型和工具设置。请按 `requirements.txt` 安装项目锁定的 MCP SDK 版本。

在仓库根目录执行以下命令。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
codex mcp add deepseek-bridge -- "$PWD/.venv/bin/python" "$PWD/run_server.py"
.venv/bin/python run_workbench.py
```

打开 [本机工作台](http://127.0.0.1:57420/)。也可以运行 `.venv/bin/python launch_workbench.py` 启动并打开页面。新 Codex 会话会加载注册的 MCP 工具。

项目委派默认使用仓库内的 `projects/` 作为允许根目录。启用委派时，先创建该目录并把待处理的项目放入其子目录，或在 MCP 服务环境中设置 `DEEPSEEK_BRIDGE_ROOT`，指向专门用于委派的根目录。向工具提供项目子目录的绝对路径。

## 使用

1. 在网页建立待接手卡片。任务说明填写允许发送给 DeepSeek 的内容。
2. 在 Codex 中用 `list_tasks` 找到卡片，调用 `update_task_status` 领取，再用 `post_message` 记录进展。消息默认仅本地保存。
3. 需要独立意见时，先在 `thread_ask_deepseek` 中用中性问题和 `independent`。需要基于初查挑错时，再把允许共享的摘要标为可共享，使用 `shared`。
4. 用 `read_thread` 回读结果，写入复核和终稿，然后标记为 `done`。

普通问答和专用协作检索轮次显示在「未归类记录」。协作检索的工具链为 `research_start`、`research_record`、`research_record_phase`、`research_ask_deepseek` 和 `research_finish`。小龙女核对来源并写入最终结论。

### 从任务卡使用 DeepSeek 助手

打开任务卡的团队页，展开「发起团队运行」，在「助手」中选择「DeepSeek · 真实调用」。单独填写问题，查看完整提示后提交。助手默认关闭。开启时，仅将问题和固定指令交给 DeepSeek，发送前在本机检查凭据、路径、IP 和个人网络资料，命中会暂停并询问。临时 Harness 配置复用已登录的凭据。

运行期间可以点「停止等待 DeepSeek」，随后决定如何继续。停止操作不会自动重发。页面显示的 Harness 进度来自本机，收到模型回复后会另外显示原文。

## 本地数据与测试

事件、团队运行记录和工作台日志默认保存在 `.local/workbench/`。任务说明、共享消息、分派和模型回复都可能出现在这些本机记录中。如需将事件日志放到其他位置，请为 MCP 服务和工作台设置相同的 `DEEPSEEK_BRIDGE_EVENTS`，指向所需的本地事件文件。团队运行数据库位于事件文件所在目录下的 `team-runs/`。

```sh
.venv/bin/python -m unittest discover -s tests -v
```

测试使用临时日志和模拟 Harness。HTTP 测试需要允许绑定本机回环端口。

如果已安装 Node.js、Playwright 和 Chrome，并保证 Node.js 可以加载 `playwright`，可运行浏览器回归。

```sh
node --test tests/team_run_ui.test.cjs tests/team_run_integration.test.cjs
```

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
