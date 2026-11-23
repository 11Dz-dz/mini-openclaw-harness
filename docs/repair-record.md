# Harness 逐项修复记录

本记录对应 2026-10-26 的历史修复。问题清单见[初始审计](archive/baseline-audit.md)，当前启动方式见[仓库首页](../README.md)。

原审计修复队列 R01–R28 均已实施；R29–R32 是复核和低优先级补充。每包有服务器备份、SHA256 清单和独立补丁。

当前完整回归：642 passed，1 skipped（Docker 集成测试因本机无 Docker 跳过）；Ruff、离线 Evals 和 fake 全流程通过。最终服务器验收结果另见交付包 verification。

| 编号 | 优先级 | 已实施修改 | 包验证 |
| --- | --- | --- | --- |
| R01 | P1 | 无隔离后端时拒绝子进程执行；Shell/Git 在模型工具目录中隐藏，旧调用也拒绝。 | 526 passed, 1 skipped in 6.48s |
| R02 | P1 | Git 禁用执行扩展、过滤器、签名验签、隐式远程获取与索引写回。 | 543 passed, 1 skipped in 6.65s |
| R03 | P1 | MCP 启动前需要一次性信任审批，绑定配置摘要和来源；未批准不构造进程。 | 545 passed, 1 skipped in 7.44s |
| R04 | P1 | MCP 仅使用 Docker stdio，挂载副本、关闭网络；本机缺少 Docker 时不可用。 | 546 passed, 1 skipped in 7.11s |
| R05 | P1 | 审批展示具体操作并遮盖凭据，参数指纹及深拷贝保证执行的是已审阅的快照。 | 549 passed, 1 skipped in 7.23s |
| R06 | P1 | 从初始副本基线计算变更；目标或副本并发变化会拒绝应用，成功后更新基线。 | 553 passed, 1 skipped in 7.28s |
| R07 | P1 | 计入工具参数、结果、schema 与请求字段；预留输出量传给模型。 | 555 passed, 1 skipped in 7.65s |
| R08 | P1 | 系统指令和最新请求不能静默裁掉，超预算时明确失败。 | 557 passed, 1 skipped in 8.90s |
| R09 | P1 | 历史按用户轮次原子裁剪；恢复补齐未知结果且不自动重放，每项结果执行后立即持久化。 | 559 passed, 1 skipped in 7.69s |
| R10 | P1 | 调度任务绑定源项目，其他项目不能读取、领取、取消或恢复；无归属旧任务不自动执行。 | 561 passed, 1 skipped in 7.66s |
| R11 | P1 | 任务租约、心跳、执行者与轮次校验；活任务不误恢复，重试预算在恢复时也生效。 | 563 passed, 1 skipped in 7.51s |
| R12 | P1 | 审计流省略正文和参数片段；检查点遮盖已知凭据，数据文件权限为 0600，日志避免原始异常正文。 | 565 passed, 1 skipped in 7.90s |
| R13 | P1 | 实际连接固定到已校验 IP，保留 Host/TLS 域名，关闭环境代理，每次重定向重新校验。 | 570 passed, 1 skipped in 7.78s |
| R14 | P1 | 先校验能力值，再判断风险和副作用；高风险或非幂等状态操作不会误判为纯计算。 | 576 passed, 1 skipped in 7.71s |
| R15 | P1 | 未知工具形成可恢复工具错误；模型/上下文异常形成终态、事件和状态记录，REPL 可继续。 | 579 passed, 1 skipped in 8.27s |
| R16 | P2 | 拒绝运行数据与工作目录重叠；复制有文件数/大小/深度上限，并排除依赖和缓存目录。 | 583 passed, 1 skipped in 8.44s |
| R17 | P2 | 网页流、子进程输出和文件读取受限；超限停止，超时清理进程/容器；哈希采用流式读取。 | 587 passed, 1 skipped in 8.49s |
| R18 | P2 | 九类断言读取真实离线观察；事件序列按顺序校验，故障必须触发，不支持的类型提前拒绝。 | 595 passed, 1 skipped in 8.91s |
| R19 | P2 | 只恢复最后未结束的 JSON 片段，先隔离保存，再截断；完整记录损坏不跳过，追加与恢复加锁。 | 599 passed, 1 skipped in 9.56s |
| R20 | P2 | 按 UTC 时间戳判断到期，拒绝无时区任务；运行中取消、终态保护与循环父关系处理。 | 603 passed, 1 skipped in 9.22s |
| R21 | P2 | 递归校验对象/数组/附加属性和支持的组合约束；未知 schema 在注册前拒绝。 | 610 passed, 1 skipped in 9.31s |
| R22 | P2 | MCP 错误传播到 ToolResult.is_error，失败结果不伪装为成功；日志仅记错误类型与标识。 | 610 passed, 1 skipped in 9.15s |
| R23 | P2 | 技能逐文件容错并可查询诊断；显式技能选择进入模型，支持斜杠消息转义。 | 612 passed, 1 skipped in 9.46s |
| R24 | P2 | Todo 改为计划内复合主键并迁移旧表；依赖检查和状态变更在同一数据库事务内。 | 615 passed, 1 skipped in 9.62s |
| R25 | P2 | 支持新增/修改/删除及权限；全部预检、暂存、备份、逐项写入，后续失败时回滚并保留日志。 | 619 passed, 1 skipped in 9.98s |
| R26 | P2 | 记忆工具只读取真实请求或文件来源并保存哈希；拒绝凭据、伪造来源与失效来源，排除被替代记录。 | 623 passed, 1 skipped in 10.06s |
| R27 | P2 | 文字完成声明使用 reported_done；done 需本任务当前运行中真实成功工具的 execution: 记录。 | 625 passed, 1 skipped in 10.38s |
| R28 | P2/P3 | 技能/记忆分条预算，注入事件在选取后记录；完成/失败/取消/耗尽有终态，采集真实流式 usage。 | 628 passed, 1 skipped in 10.73s |

补充包：

- R29：共享工作区的子 Agent 禁用写文件及 MCP，默认只允许检查。
- R30：schema 数值/枚举边界复核；无效 MCP schema 直接拒绝，审批补充 Cookie 遮盖。
- R31：剩余预算内提供旧轮次原文摘录，带 message 索引和工具结果哈希，保留完整检查点，不编造摘要。
- R32：中日韩连续文本的技能触发和双字词项匹配；增加六个正反标签用例，未宣称达到语义检索准确率。

## 运行与兼容变化

- 本服务器继续使用 restricted 模式，文件等内置能力可用；Shell、Git 子进程和 MCP 不启动。Docker 路径有单元校验，本机不能验证真实容器运行。
- `data_dir` 必须位于项目外；复制和读取的上限详见 README。
- `remember.source_ref` 使用 `request` 或 `file:<相对路径>`。旧未验证记忆不自动注入，原记录保留。可信程序确认导入需显式 confirmed=True。
- 无项目归属的旧定时任务隔离保留，`/jobs` 显示数量；新任务必须含时区。
- 旧的 DONE 计划状态首次迁移为 reported_done，必须提供真实执行证据才重新标记完成。执行证据不替代对结果正确性的人工/测试判断。
- 审计日志不保存完整提示词、回答或参数；完整会话从受限权限的 checkpoint 恢复。没有自动清理历史日志，也没有加密旧数据。
- 工具执行维持顺序，以保持依赖和持久化次序；未引入无条件并发。默认不递归委派是保守权限设计。
- `messages` 数据表保留兼容，Checkpoint 是历史权威，不制造双写一致性问题。

## 验证范围与剩余限制

- 覆盖了批准后参数/文件变化、并发修改冲突、真实 TCP 的 DNS 固定、调度租约与取消、日志断尾及并发追加、实际离线工具故障、证据伪造和消息配对。
- 所有联网服务和真实模型 API 均未调用，未消耗模型 API 费用；真实提供方差异仍需用目标 API 做后续联调。
- 多文件 apply 在运行中失败时可回滚并留存 journal；系统突然掉电时可凭 journal/原件检查恢复，不提供跨多个文件的瞬间原子提交。
- 静态符号链接越界会拒绝。本轮未声称已证明或完全消除恶意本地并发改目录的 TOCTOU 竞态；本地可信进程仍共享操作系统身份。
- Token 计数仍为启发式，不是目标模型 tokenizer；自动技能匹配仍为词项策略，原文摘录也不保证无损覆盖所有历史信息。
- 工具输出失败语义以其适配器实现为准；已修复 MCP structured error。执行证据证明某个成功返回的适配器调用发生，不能证明任意外部服务或项目目标已达成。

## 逐包备份

| 包 | 服务器备份目录 |
| --- | --- |
| r01 | `/root/rivermind-data/harness-fixes/r01-backup-316i00cr` |
| r02 | `/root/rivermind-data/harness-fixes/r02-backup-qv2tyhzt` |
| r03 | `/root/rivermind-data/harness-fixes/r03-backup-jevmavws` |
| r04 | `/root/rivermind-data/harness-fixes/r04-backup-v671g4ou` |
| r05 | `/root/rivermind-data/harness-fixes/r05-backup-qiwnsvh2` |
| r06 | `/root/rivermind-data/harness-fixes/r06-backup-vzmai4qi` |
| r07 | `/root/rivermind-data/harness-fixes/r07-backup-91xep6l2` |
| r08 | `/root/rivermind-data/harness-fixes/r08-backup-p157s46g` |
| r09 | `/root/rivermind-data/harness-fixes/r09-backup-ibsj7ywf` |
| r10 | `/root/rivermind-data/harness-fixes/r10-backup-b5k92g28` |
| r11 | `/root/rivermind-data/harness-fixes/r11-backup-8cgon0nb` |
| r12 | `/root/rivermind-data/harness-fixes/r12-backup-5uw74k8i` |
| r13 | `/root/rivermind-data/harness-fixes/r13-backup-3o9isd3k` |
| r14 | `/root/rivermind-data/harness-fixes/r14-backup-7t2rh1x3` |
| r15 | `/root/rivermind-data/harness-fixes/r15-backup-_ypl6cgl` |
| r16 | `/root/rivermind-data/harness-fixes/r16-backup-ey_sitzb` |
| r17 | `/root/rivermind-data/harness-fixes/r17-backup-y0eq1y4y` |
| r18 | `/root/rivermind-data/harness-fixes/r18-backup-9kx4p9c2` |
| r19 | `/root/rivermind-data/harness-fixes/r19-backup-y8lec43h` |
| r20 | `/root/rivermind-data/harness-fixes/r20-backup-s2d2adv6` |
| r21 | `/root/rivermind-data/harness-fixes/r21-backup-48pzrdeu` |
| r22 | `/root/rivermind-data/harness-fixes/r22-backup-dldld_ur` |
| r22a | `/root/rivermind-data/harness-fixes/r22a-backup-5y3xzqb1` |
| r23 | `/root/rivermind-data/harness-fixes/r23-backup-o_rgdlvl` |
| r24 | `/root/rivermind-data/harness-fixes/r24-backup-9d_xd5tl` |
| r25 | `/root/rivermind-data/harness-fixes/r25-backup-j1w_exo4` |
| r26 | `/root/rivermind-data/harness-fixes/r26-backup-pqdsoyie` |
| r27 | `/root/rivermind-data/harness-fixes/r27-backup-wjydlgua` |
| r28 | `/root/rivermind-data/harness-fixes/r28-backup-fo7aev94` |
| r29 | `/root/rivermind-data/harness-fixes/r29-backup-2efrpyjr` |
| r30 | `/root/rivermind-data/harness-fixes/r30-backup-hfoka540` |
| r31 | `/root/rivermind-data/harness-fixes/r31-backup-rpvx5ou0` |
| r32 | `/root/rivermind-data/harness-fixes/r32-backup-tuus6u3g` |

补丁、before/after SHA256、测试日志与最终验收文件均包含在交付包中。服务器没有 `.git`，未创建提交或覆盖成新的 Git 仓库。

技术依据：[HTTPCore SNI 扩展](https://www.encode.io/httpcore/extensions/#sni-hostname)、[HTTPX 环境代理配置](https://www.python-httpx.org/environment_variables/)。
