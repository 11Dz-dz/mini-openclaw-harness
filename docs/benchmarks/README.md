# 评测

[返回首页](../../README.md)

## Held-out A/B

结果来自固定题单中的 57 道已完成配对评分的题。两版使用阿里云百炼的
`deepseek-v4.1-flash`，每道题在每个版本计一次评分，每题时限 360 秒。

| 题集 | 配对题数 | 历史版通过 | `budget-verification` 通过 | 变化 |
| --- | ---: | ---: | ---: | ---: |
| Held-out 合计 | 57 | 20 | 33 | +22.81 个百分点 |
| SWE-bench Lite | 32 | 11 | 23 | +37.50 个百分点 |
| Terminal-Bench 2.1 | 25 | 9 | 10 | +4.00 个百分点 |
| 新增题子集 | 24 | 7 | 14 | +29.17 个百分点 |

| 预算 | 历史版 | `budget-verification` |
| --- | ---: | ---: |
| 上下文窗口 | 36,000 | 64,000 |
| 输出 Token 预留 | 8,000 | 8,000 |
| 模型轮次 | 24 | 32 |
| 工具调用次数 | 48 | 64 |

历史版来自历史归档还原的源码快照。报告保留两版源码树指纹、样本来源、配置和逐题结果。
由于两版同时调整了代码和预算，提升应解读为整套配置的差异。结果仅覆盖本仓库抽取的样本。

固定题单含 18 道 Dev 和 60 道 Held-out。3 道 Requests 题未完成评测环境校验，未计入通过率。
另外 8 道以前已评分的题单独复跑，历史版原先通过 3 道，新版原先通过 2 道，这次两版均为
0/8。16 次复跑中有 14 次触及时间上限。复跑不会重复计入 57 道独立样本。

- [扩样后的固定题单](eval_manifest_v2.json)
- [扩样后的配对结果](heldout_ab_results_v2.json)
- [第一轮题单](eval_manifest.json)与[第一轮结果](heldout_ab_results.json)

## 本地离线验收

```bash
uv run python -m miniclaw.evals.cli run tests/fixtures/evals/core.json --output /tmp/miniclaw-eval
```

离线用例使用脚本化模型，经过真实的 Agent 循环、工具注册表、审批、检查点和 trace。
覆盖流式响应、工具结果、审批和故障处理。完整开发检查可用
`bash scripts/smoke_offline.sh` 运行。
