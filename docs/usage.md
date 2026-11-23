# 使用指南

[返回首页](../README.md)

## 配置与启动

在 MiniClaw 的仓库目录执行 `uv run miniclaw config init` 完成首次配置。已有配置时，用
`uv run miniclaw config show` 查看脱敏后的设置，用 `uv run miniclaw config path` 查看文件路径。
配置默认保存在当前用户的 `~/.miniclaw/config.toml`。用下面的命令更新 API Key，输入不会回显。

```bash
uv run miniclaw config set api_key
uv run miniclaw --workspace /path/to/your/project
```

CLI 参数优先于环境变量，环境变量优先于配置文件。常用模型环境变量是
`OPENAI_BASE_URL`、`OPENAI_MODEL` 和 `OPENAI_API_KEY`。可参考 [环境变量示例](../.env.example)。
MiniClaw 不会自动加载 `.env` 文件。

`data_dir` 与工作区必须互不包含。复制工作区时跳过 `.venv`、`node_modules` 和常见缓存目录。
副本有 20,000 文件、512 MiB 总量、64 MiB 单文件和 64 层目录限制。`read_file` 只读取不超过
1 MiB 的文本文件，可按行分页。

## 执行与审批

```bash
docker version
docker image inspect python:3.12-slim
uv run miniclaw --workspace /path/to/your/project --require-strong-sandbox
```

`--docker-image` 可指定已在本机准备好的镜像，若使用 Git 或 MCP，所需程序应在该镜像中可执行。
Shell 在工作副本中运行，Docker 根文件系统只读，`/tmp` 可写，容器网络关闭。可用
`/sandbox` 查看后端、禁用的工具和原因。可信模式的行为见[可信本机执行](trusted-execution.md)。

| 命令 | 用途 |
| --- | --- |
| `/help`、`/exit` | 查看命令、退出 |
| `/changes`、`/apply <path>`、`/apply --all` | 查看副本改动并请求应用 |
| `/sessions`、`/resume <id>`、`/clear` | 查询、恢复或切换会话 |
| `/tasks`、`/jobs`、`/cancel <job-id>` | 查看任务和定时任务，取消定时任务 |
| `/memory <query>`、`/agents` | 搜索已保存记忆，查看子 Agent 状态 |
| `/skills`、`/skills errors`、`/skill <name> [任务]` | 查看、排查或显式选用 Skill |
| `/sandbox`、`/mcp`、`/metrics`、`/trace show` | 检查沙箱、MCP、指标和审计日志 |

在输入前加 `//` 可把斜杠开头的内容作为普通文本发送。Trace 始终开启。

| 工具类别 | 常用工具 |
| --- | --- |
| 文件 | `read_file`、`search_text`、`write_file`、`replace_text` |
| 执行与检查 | `run_command`、`git_status`、`git_diff`、`git_log`、`git_show` |
| 网页 | `fetch_url`、配置搜索端点后可用的 `web_search` |
| 任务与知识 | `create_plan`、`list_tasks`、`verify_task`、`remember`、`search_memory` |
| 扩展 | `delegate_task`、`schedule_task`、MCP 提供的工具 |

`search_text` 默认做字面量、区分大小写搜索，传入 `ignore_case=true` 可忽略大小写。
Git 工具只做检查。在可信模式下，Git 也需要审批。

## Skills

在项目的 `.miniclaw/skills/<name>/SKILL.md` 中定义 Skill。触发词匹配成功时，Skill
正文会进入模型上下文，审计流记录 `ContextInjected`。下面是发布说明的示例。

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

## MCP 与网页搜索

扩展配置可放在项目的 `.miniclaw/` 或用户的 `~/.miniclaw/`。项目配置优先。MCP
使用 `mcp.json`，支持 `servers` 和 `mcpServers` 两种顶层键。

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

示例中的脚本需由使用者放进项目。MCP 在 Docker 内以 `/workspace` 为工作目录，关闭网络，
只挂载工作副本。启动需要信任审批，工具命名为 `mcp_<服务器>_<工具>`。单个服务启动失败可在
`/mcp` 查看。当前支持 stdio，工具之外的 MCP resources、prompts 以及 HTTP/SSE 传输未实现。

网页搜索配置在 `search.json`，下面的字段值应按搜索服务商的响应格式替换。

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

搜索密钥从 `MINICLAW_SEARCH_API_KEY` 读取。未配置搜索端点时，模型不会看到 `web_search`。
`fetch_url` 可以获取静态网页正文，不执行 JavaScript。

## 后台任务

定时任务默认会拒绝需要人工审批的动作。嵌入 MiniClaw 的调用方可用
`create_app(config, unattended_approved_tools=(...))` 显式授权所需工具。新任务的时间必须带时区，
任务绑定创建它的源项目。`/jobs` 会显示因缺少项目归属而保留的早期任务数量。
