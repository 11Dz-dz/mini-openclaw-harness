# MiniClaw

MiniClaw 是一个用 Python 手写的终端 Coding Agent Harness。它能读取和修改工作副本、运行测试，并让你在写回真实项目之前查看和批准改动。Agent 循环、工具执行、上下文预算和会话持久化都由仓库实现，运行时依赖只有 `httpx`。

## 快速开始

需要 Python 3.12+ 和 `uv`。开发和离线回归覆盖 Linux 和 macOS。

```bash
git clone https://github.com/11Dz-dz/mini-openclaw-harness.git
cd mini-openclaw-harness
uv sync --extra dev --python 3.12
uv run python examples/full_agent_flow.py --fake
```

离线示例使用脚本化模型，无需 API Key。它会加载 Skill、生成计划、委派子 Agent、记录审计事件，并演示怎样批准应用沙箱产物。

## 连接模型，开始改代码

在 MiniClaw 仓库目录执行：

```bash
uv run miniclaw config init
uv run miniclaw --workspace /path/to/your/project
```

配置向导会提示输入支持 Chat Completions 的 API Base URL、模型 ID 和 API Key。密钥输入不回显。已配置的用户可以直接运行第二条命令。使用 DeepSeek 或百炼时，模型 ID 和 Base URL 以服务商及地域为准。详见 [DeepSeek 接入](docs/deepseek.md)。

进入终端后，可以先用 `/sandbox` 确认执行后端，再让 Agent 定位代码、修改副本并运行针对性测试。

```text
you> 找到失败的测试，做最小修复，再运行相关测试。
you> /changes
you> /apply path/to/changed_file.py
```

`/changes` 列出副本的改动。`/apply` 会发起一次单独的审批，通过后才写回真实项目。`/help` 显示所有命令。

## 执行模式

| 模式 | 行为 |
| --- | --- |
| `auto`（默认） | Docker 和配置的镜像可用时启用隔离执行，否则进入 `restricted`。 |
| `restricted` | 保留副本内的文件工具，禁用 Shell、Git 子进程和 MCP。 |
| `trusted` | 显式允许在 POSIX 主机上以普通用户身份运行 Shell 和 Git。没有文件系统或网络隔离，调用仍需审批。 |

需要 Shell 或 Git 时，先确认 Docker daemon 和镜像可用。默认镜像为 `python:3.12-slim`。Git 和 MCP 所需程序必须存在于配置的镜像中。`--require-strong-sandbox` 可在隔离后端不可用时直接拒绝启动。详见[使用指南](docs/usage.md)和[可信本机执行](docs/trusted-execution.md)。

## 核心能力

- **上下文与工具执行**。组装流式工具调用，校验参数，并按能力和风险决定是否请求审批。历史按消息组裁剪，同批工具逐个执行并记录结果。
- **会话恢复与审计**。通过 `/sessions`、`/resume` 继续对话。恢复会标记结果未知的调用，审计流保留事件顺序与执行元数据。
- **扩展与后台任务**。支持 Skills、Docker 内的 stdio MCP、带来源的记忆、计划、定时任务和只读子 Agent。定时任务默认拒绝需交互审批的动作。

## Held-out 评测

| 配对样本 | 历史基线 | `budget-verification` |
| --- | ---: | ---: |
| 全部 57 道 | 20/57（35.1%） | 33/57（57.9%） |
| 本轮新增 24 道 | 7/24（29.2%） | 14/24（58.3%） |

使用同一个 `deepseek-v4.1-flash` 模型，题目来自 SWE-bench Lite 和 Terminal-Bench 2.1。题单、分组结果、超时复跑和实验限制见[评测说明](docs/benchmarks/README.md)。

## 文档与开发

| 文档 | 内容 |
| --- | --- |
| [使用指南](docs/usage.md) | 启动参数、斜杠命令、工具、Skills、MCP 与网页搜索 |
| [架构与行为约定](docs/architecture.md) | Agent 循环、权限、检查点、产物应用与实现边界 |
| [DeepSeek 接入](docs/deepseek.md) | 模型配置和思考模式工具调用 |
| [可信本机执行](docs/trusted-execution.md) | 普通用户执行、审批和安全边界 |
| [评测说明](docs/benchmarks/README.md) | Held-out 题单、A/B 结果和离线验收 |
| [历史审计](docs/archive/baseline-audit.md) | 原始问题清单及修复记录入口 |

```bash
uv run ruff check .
uv run pytest -q
bash scripts/smoke_offline.sh
```

`src/miniclaw/` 是实现，`tests/` 按模块组织，`examples/` 提供可运行的离线示例。GitHub Actions 在 Linux 和 macOS 上跑回归，并检查 wheel 的独立安装。

## 许可证

[MIT](LICENSE)
