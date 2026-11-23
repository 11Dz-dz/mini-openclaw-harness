# 架构与行为约定

[返回首页](../README.md)

## 从终端到工具结果

```text
AppRepl.process
├─ cli/app_commands.py         处理斜杠命令
└─ MiniClawApp.run_prompt      提交任务、更新会话
   └─ AgentLoop.run            状态转换、预算、结果落盘
      ├─ request_context.py    构建上下文和模型请求
      ├─ stream.py             接收流式响应、收集调用片段
      ├─ tool_batch.py         校验参数、审批、按顺序分派
      └─ sessions/             检查点、事件与恢复
```

| 从哪里读 | 职责 |
| --- | --- |
| [app.py](../src/miniclaw/app.py) | 组装模型、执行后端、存储和工具注册表 |
| [application.py](../src/miniclaw/application.py) | 启停、会话、工作副本应用 |
| [agent/loop.py](../src/miniclaw/agent/loop.py) 与 [agent/state.py](../src/miniclaw/agent/state.py) | Agent 循环与状态机 |
| [tools/](../src/miniclaw/tools) 与 [permissions/](../src/miniclaw/permissions) | Schema 校验、工具执行和审批决策 |
| [sessions/](../src/miniclaw/sessions) 与 [workspace/](../src/miniclaw/workspace) | 恢复、路径校验、差异和批准写回 |
| [config_store.py](../src/miniclaw/config_store.py) 与 [config_toml.py](../src/miniclaw/config_toml.py) | 安全持久化配置与 TOML 编解码 |

## 上下文与执行顺序

Token 预算使用按字符和文字类型估算的启发式算法。当前用户请求与必要系统指令优先保留，
工具调用及对应结果作为一组筛选。裁掉的历史可保留带来源索引的摘录，摘录覆盖范围取决于预算。
收到完整响应后才解析一批工具调用。流式响应中断或发生截断时，未完整提交的调用不会执行。

工具声明文件系统、网络、子进程、凭据和风险能力，策略据此做出允许、审批或拒绝决策。
无效参数在审批前返回字段错误。同批工具按顺序执行，每个结果写入检查点后才分派下一条。
子 Agent 共享 Provider 和工作副本，并受白名单、委派深度和只读能力约束。

## 检查点与审计

Checkpoint 是会话恢复的权威记录。它保存聊天历史和工具调用结果，使用仅所有者可读写的文件
权限并对凭据做脱敏。它是本地文件存档，未加密。SQLite 的 `messages` 表为保留结构。

事件流和 trace 存放结构化元数据，省略提示词、正文和流式参数正文。恢复时结果缺失的调用被
标记为结果未知。未完成的非幂等调用会要求用户批准恢复历史。恢复流程不会自动重放调用。

## 工作副本与产物应用

工作副本与真实项目之间的写回通过 `/apply` 发起。审批展示目标、操作类型和文件状态指纹。
审批后会检查目标和产物状态，发生冲突时拒绝应用。失败时使用备份和回滚日志，回滚不完整时
返回备份路径。多文件写回不提供跨文件系统的瞬时原子性。

Docker 能力不可用时，普通进程不会在自动模式下接管模型的 Shell/Git 执行。MCP 要求隔离后端。
Git 检查关闭 external diff、textconv、fsmonitor、签名验证和隐式远程对象获取，不递归子模块。
检测到 clean/smudge/process 过滤器时，拒绝执行可能触发过滤器的工作区状态和差异读取。

## 记忆、计划和调度

记忆使用用户请求或工作区文件作为 `source_ref`。来源内容的 SHA-256 用来核对数据完整性。
早期无可验证来源的记忆不会注入模型上下文。

计划中的文字完成声明为 `reported_done`。`done` 要求关联任务处于活动状态时的成功工具执行
记录，`verify_task` 用于核对报告。执行记录证明操作发生，目标达成仍取决于验证。

调度器以 SQLite 保存任务和租约，使用 UTC 时间判断到期。取消正在运行的任务采用合作式停止。
跨进程认领依赖同一个 SQLite 文件，部署范围是单机。
