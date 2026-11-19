# MCP Dock

本地 Codex 与 DeepSeek Harness 协作桥接，带浏览器工作台。Codex 负责分派、复核和最终决定。桥接、工作台和事件记录在本机运行。发送给 DeepSeek 的内容由其云端模型处理。

界面中「小龙女」指 Codex，「大肥鱼」指 DeepSeek。

## 功能

- 任务卡片支持网页建卡、Codex 领取、等待、完成和归档。每张卡有自己的对话与分派记录。
- 普通咨询和协作检索支持一次性意见、独立检索、互查以及来源核验记录。
- 卡片分派支持 `independent` 和 `shared`。两种模式均发送任务说明和本次问题。共享模式还发送明确标为可共享的近期消息。
- 卡片分派默认等待 180 秒，可设置 1–600 秒。工作台显示调用状态、实际提示词和可用的脱敏错误诊断。客户端的等待时限也会影响较长的调用。
- 项目委派仅处理指定目录内合格的 UTF-8 文本。DeepSeek 操作临时副本，桥接检查变化后写回。小龙女负责审查和验收。

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

## 本地数据与测试

事件和工作台日志默认保存在 `.local/workbench/`。任务说明、共享消息、分派和模型回复都可能出现在这些本机记录中。 如需将事件日志放到其他位置，请为 MCP 服务和工作台设置相同的 `DEEPSEEK_BRIDGE_EVENTS`，指向所需的本地事件文件。

```sh
.venv/bin/python -m unittest discover -s tests -v
```

测试使用临时日志和模拟 Harness。HTTP 测试需要允许绑定本机回环端口。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
