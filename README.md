# MiniClaw

一个手写的 Agent Harness —— 在终端里跑的通用任务 Agent，形态类似 Claude Code。

不依赖任何 Agent 框架或厂商 SDK：Agent Loop、Tool Runtime、Context 预算、长期记忆、
Planning、Permission、Sandbox、Skills、Subagent、Scheduler、MCP 客户端、Tracing 和
Evals 全部由仓库代码实现。运行时依赖只有 `httpx` 一个。

要求 Python 3.12+。测试、Evals 和示例完全离线，不需要 API Key。

**命令执行需要可用的隔离后端。** 普通进程后端不提供文件系统和网络隔离，现已拒绝执行。
Docker 后端不可用时，应用以 `restricted` 模式启动，保留副本内文件工具，但不向模型提供
`run_command` 和 Git 子进程工具；旧会话中的此类调用也会被拒绝，审批不能解除限制。
`/sandbox` 显示实际选中的后端、禁用的工具及原因。
设置 `--require-strong-sandbox` 时，没有隔离后端会直接拒绝启动。
个人可信项目可显式使用 `--execution-mode trusted`，以普通用户在本机执行 Shell/Git。
该模式没有文件系统或网络沙箱，每次命令都需要审批；默认不会启用，也不会自动回退到它。
root 运行时需预先配置专用 `miniclaw-runner` 用户；详细边界及用法见 [可信本机执行](docs/trusted-execution.md)。
DeepSeek 的配置及思考模式工具调用说明见 [DeepSeek 接入](docs/deepseek.md)。
MCP 同样要求隔离后端，且启动前需要一次性信任审批；没有 Docker 时不启动 MCP。

Git 只读检查使用原始差异，关闭 external diff、textconv、fsmonitor、签名验签程序和
隐式远程对象获取，不递归检查子模块，也不刷新磁盘索引。
若仓库配置了 clean/smudge/process 内容过滤器，工作区状态与差异检查会明确拒绝执行，
以免 Git 在读取工作区文件时启动过滤程序。该限制独立于隔离后端；没有隔离后端时 Git 工具仍保持禁用。

```
you> 看一下这个项目的测试覆盖，把没测到的模块列出来
you> 给 utils.py 补类型注解，然后跑测试确认没坏
you> 查一下 httpx 0.28 的 timeout 参数怎么改了，对照我们的代码
you> 记住：这个项目的 sqlite 连接不能跨线程复用
```

## 设计要点

**改动不会直接落到你的项目里。** 启动时把工作区复制到沙箱，Agent 只动副本；
`/changes` 查看差异，`/apply` 显式批准后才写回真实目录。

**权限由能力声明决定，不是由工具名。** 每个工具声明自己的
`filesystem / network / subprocess / secrets / risk_level`，策略据此给出
ALLOW / ASK / DENY。只读检查类工具自动放行，写入和网络访问必须审批。

**后台运行会自动收紧权限。** 定时任务没有人在场，所以策略判为 ASK 的动作一律拒绝，
除非事先显式预授权某个工具。

**全程可审计。** 每个事件（模型输出、工具调用、审批结果、上下文注入）都进结构化
事件流并写入 trace。审计日志保存事件类型、顺序和执行元数据，省略提示词、回答正文及流式参数正文。
恢复检查点保存会话内容，对已知凭据和带凭据标识的文本脱敏，并使用仅所有者可读写的文件权限。

**状态可恢复。** Session、Checkpoint、Plan、Memory 全部持久化；进程重启后
`/resume` 能接着上次继续。未完成的非幂等调用需要确认是否恢复历史；缺失结果会标记为
“执行结果未知”，不会自动重放。恢复后应先检查实际状态，避免重复副作用。

## 架构

```text
terminal input
  └─ AppRepl.process
      ├─ CommandRouter ──────────────── 斜杠命令（本地处理，不发给模型）
      └─ MiniClawApp.run_prompt
          └─ AgentLoop.run              显式状态机，非法状态转移直接报错
              ├─ ContextBuilder         确定性预算裁剪 + Skill/Memory 注入
              ├─ ModelProvider.stream   手写 SSE 解码
              ├─ PermissionPolicy       能力声明 → ALLOW/ASK/DENY
              ├─ ToolExecutor
              │   ├─ 文件 / Shell / Git / 网页 / MCP
              │   ├─ Memory / Plan / Scheduler
              │   └─ Subagent ────────── 独立 Child AgentLoop，工具白名单取交集
              ├─ SandboxRouter          Process 或 Docker 后端
              └─ Session / Checkpoint / Event Store / Trace
```

## 安装

```bash
git clone <repo-url> miniclaw
cd miniclaw
uv python install 3.12
uv sync --extra dev --python 3.12
```

## 先离线试一下（零成本，不需要 API Key）

```bash
uv run python examples/full_agent_flow.py --fake
```

用脚本化的假模型跑完整链路：加载 Skill → 生成计划 → 委派子 Agent → 写文件 →
高危命令被权限拒绝 → 存记忆 → 调度任务 → 写 trace → 沙箱产物显式应用。

## 接真实模型

```bash
uv run miniclaw config set base_url https://api.openai.com/v1
uv run miniclaw config set model gpt-4o-mini
uv run miniclaw config set api_key        # 交互式输入，不进 shell 历史
```

任何 OpenAI 兼容端点都可以（DeepSeek、Kimi、本地 vLLM / Ollama 等）。也支持环境变量
`OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_API_KEY`（优先级更高）。

然后在你的项目目录下运行：

```bash
cd /path/to/your/project
miniclaw
```

## 命令

```
/changes            查看 Agent 在沙箱里改了什么
/apply <path>|--all 显式应用改动到真实项目
/resume <id>        从 Checkpoint 恢复会话（/sessions 查 id）
/clear              清空对话并开启新会话
/memory <query>     搜索长期记忆
/tasks  /jobs       计划任务 / 定时任务
/skills /agents     已加载的 Skill / 子 Agent 状态
/mcp    /sandbox    MCP 服务器 / 沙箱后端状态
/metrics /trace     token 消耗 / 审计日志
/help
```

## 工具

| 类别 | 工具 |
| --- | --- |
| 文件 | `read_file` `search_text` `write_file` `replace_text` |
| 执行 | `run_command`（沙箱内，需审批） |
| 版本控制 | `git_status` `git_log` `git_diff` `git_show`（只读，自动放行） |
| 网络 | `fetch_url`（含 SSRF 防护） `web_search`（需配置端点） |
| 记忆 | `remember` `search_memory` |
| 计划 | `create_plan` `list_tasks` `start_task` `complete_task` `verify_task` `fail_task` |
| 并发 | `delegate_task` |
| 调度 | `schedule_task` `list_scheduled_tasks` `cancel_scheduled_task` |
| 扩展 | 任意 MCP 服务器提供的工具 |

## 可选配置

配置文件放在 `<workspace>/.miniclaw/`（项目级）或 `~/.miniclaw/`（用户级），项目级优先。

**MCP 服务器** —— `mcp.json`（`servers` 与 `mcpServers` 两种键名都认）：

```json
{
  "servers": {
    "files": {
      "command": "python",
      "args": [".miniclaw/servers/files.py"]
    }
  }
}
```

MCP 工具统一命名为 `mcp_<服务器>_<工具>`，不会覆盖内置工具；服务器启动失败只影响该
服务器，不影响 Agent 启动。用 `/mcp` 查看状态。

MCP 在配置的 Docker 镜像内启动，工作目录为 `/workspace`，只挂载工作副本，网络关闭，
根文件系统只读。可执行程序必须已在镜像内，或脚本位于副本内；不会联网安装依赖。
配置中的绝对服务器路径必须是容器内路径。审批绑定启动配置指纹，每次启动单独审批；
环境变量值及敏感参数在预览中脱敏。无隔离后端时直接显示 unavailable，不请求无效审批。

**网页搜索** —— `search.json`，厂商无关，字段映射写在配置里：

```json
{
  "url": "https://api.example.com/search",
  "query_parameter": "q",
  "results_path": "web.results",
  "title_field": "title",
  "url_field": "url",
  "snippet_field": "description",
  "api_key_header": "X-Api-Key"
}
```

密钥只从环境变量 `MINICLAW_SEARCH_API_KEY` 读取，写进配置文件会被拒绝。未配置端点时
不注册 `web_search` —— 不给模型一个注定失败的能力。

**后台任务预授权** —— 定时任务默认无法执行需要审批的动作，要放行必须显式声明：

```python
app = await create_app(config, unattended_approved_tools=("fetch_url",))
```

## Skills

在 `<workspace>/.miniclaw/skills/<name>/SKILL.md` 放一个带 frontmatter 的 Markdown：

```markdown
---
name: release-notes-writer
description: Write concise release notes
triggers:
  - release
scope: project
---
Inspect the git log before writing release notes.
```

命中触发词时，Skill 正文会作为 system message 注入，并留下 `ContextInjected` 审计事件。

## 开发

```bash
uv run ruff check .                                          # lint
uv run pytest -q                                             # 完整回归测试
bash scripts/smoke_offline.sh                                # 完整离线验收
```

CI 在 Linux 和 macOS 上跑 lint、测试、离线验收，并从构建出的 wheel 做一次干净安装
验证——全程不需要 API Key，不产生模型费用。

## Held-out 评测

在 57 道完成配对评分的 SWE-bench Lite 与 Terminal-Bench 2.1 题目上，
使用 `deepseek-v4.1-flash`，历史版通过 20 道，`budget-verification` 版通过 33 道，
通过率从 35.1% 升至 57.9%。两版使用各自的上下文、模型轮次与工具调用预算，
每题时限均为 360 秒。另有 8 道旧题复跑，两版均为 0/8，16 次运行中有 14 次超时。
题单还包含 18 道 Dev 回归题和 3 道未完成环境校验的 Held-out 题。
见[固定题单](docs/benchmarks/eval_manifest_v2.json)和[配对结果](docs/benchmarks/heldout_ab_results_v2.json)。

## 当前服务器环境

```bash
source /opt/conda/etc/profile.d/conda.sh
conda activate harness
cd /root/rivermind-data/mini-openclaw-harness
python -m pytest -q
python examples/full_agent_flow.py --fake
```

该服务器本身是容器，没有可用的 Docker CLI/daemon，采用 restricted 模式。
文件、计划、记忆和受控网页抓取可用，Shell/Git/MCP 进程执行不可用。
`data_dir` 与项目目录必须互不包含；不要把运行数据目录设置在项目内部。
工作副本复制跳过 `.venv`、`node_modules` 和缓存目录，上限为 20,000 文件、512 MiB 总量、
64 MiB 单文件、64 层目录。超限会明确拒绝。单次 `read_file` 限制为 1 MiB。

## 持久状态与操作约定

- `/apply` 审批会显示目的目录、操作类型及文件状态指纹。审批期间文件或目标变化会拒绝。
  失败回滚的日志和原文件保存在当前 sandbox 的 `artifacts/apply-*`。回滚不完整时返回备份路径。
- `remember` 的 `source_ref` 使用 `request` 或 `file:<工作区相对路径>`，程序读取并保存真实来源。
  来源最多 64 KiB，内容由 SHA256 关联；凭据会被拒绝。旧数据库中未验证来源的记忆保留但不注入。
  可信程序导入旧记忆可显式使用 `store.put(record, confirmed=True)`；模型工具没有此绕过参数。
  内容哈希证明来源内容没有变化，不证明模型对该来源的解释正确。
- 计划任务的文字完成声明为 `reported_done`。`done` 要求该任务处于活动状态时，本轮工具成功执行
  生成的 `execution:` 记录；`verify_task` 用于核对报告。执行记录证明操作发生，不自动证明目标达成。
- 定时任务绑定源项目的绝对目录。旧任务没有项目归属时隔离保留，`/jobs` 显示数量。
  新任务必须带时区，数据库按 UTC 时间戳判断到期；租约/心跳和执行轮次用于防止误恢复和迟到覆盖。
  `/cancel` 会取消本进程正在运行的任务，其他 worker 最迟在下一次状态检查后停止合作式执行。
- `/skills errors` 查看损坏技能；`/skill <名称> [任务]` 明确选用；`//` 开头可把斜杠消息当普通文本。
  自动匹配仍是可解释的触发词和词项匹配，增加了中日韩连续文本匹配，未宣称语义检索准确率。
- Evals 支持九类断言，工具、审批、checkpoint、trace 来自真实离线运行。
  已实现断连、工具异常、工具超时、工作区越界故障；其他故障类型明确拒绝，配置的故障没有触发也失败。
- 会话恢复以 Checkpoint 为权威；`messages` SQLite 表暂为保留结构，不与 Checkpoint 双写。
  审计日志省略正文不是历史丢失。Checkpoint 是有访问权限限制的本地会话存档，不是加密存储。
- 同轮工具维持顺序执行，且每个结果写入 checkpoint 后再执行下一工具，避免依赖错误和扩大重放窗口。

逐项修复与验证见 [docs/repair-record.md](docs/repair-record.md)。

## 已知限制

- Token 计数是启发式的（按字符分脚本估算），不是模型自己的 tokenizer；预算偏保守
- 最新请求、系统指令和当前工具消息组必须保留；旧轮次按组裁剪，有余量时加入带来源索引的原文摘录。摘录不保证覆盖所有历史事实
- `SandboxRouter` 只在启动时选一次后端；Docker 执行前会再次检查能力，不回退到普通进程
- Trace 始终开启，`/trace on|off` 只返回说明
- `/apply` 绑定新增/修改/删除操作及内容和权限，冲突会拒绝；多文件写入有备份和回滚日志，但不具备跨文件系统的瞬时原子性
- Child Agent 共享 Provider 和副本，只允许检查；写文件和 MCP 工具会禁用，避免子任务修改父任务文件
- MCP 只实现 stdio 传输，没有 HTTP/SSE，也没有 resources/prompts
- Scheduler 的跨进程认领依赖单个 SQLite 文件，不适用于多机部署
- 没有浏览器自动化：`fetch_url` 抓取静态 HTML 并提取正文，不执行 JavaScript

## License

MIT
