# DeepSeek 接入

[返回首页](../README.md)

MiniClaw 使用 OpenAI 兼容的 Chat Completions 接口。Base URL 和模型 ID 需要对应你的 API 平台及服务地域。配置向导会以隐藏输入读取 API Key。

```bash
uv run miniclaw config init
uv run miniclaw --workspace /path/to/your/project
```

已有配置时，可逐项更新。以下命令中的地域和工作空间只是格式示例，请按服务商文档填写实际值。

```bash
uv run miniclaw config set base_url 'https://YOUR-WORKSPACE.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
uv run miniclaw config set model YOUR-MODEL-ID
uv run miniclaw config set api_key
```

- DeepSeek 官方接口的 Base URL 和思考模式见 [DeepSeek API 文档](https://api-docs.deepseek.com/guides/thinking_mode/)。
- 百炼的地域端点、可用模型与密钥要求见 [阿里云百炼 DeepSeek API 文档](https://www.alibabacloud.com/help/en/model-studio/deepseek-api)。

配置默认位于 `~/.miniclaw/config.toml`。`uv run miniclaw config path` 可确认实际路径。环境变量
`OPENAI_BASE_URL`、`OPENAI_MODEL` 和 `OPENAI_API_KEY` 会覆盖对应的 TOML 配置。

## 思考模式与工具调用

MiniClaw 能从流式响应读取 `reasoning_content`，并在同一任务的工具续轮中把它随
assistant 消息发送给提供方。它参与请求预算并保存在脱敏后的检查点中。可见回答与审计
事件流省略推理正文。早期没有该字段的检查点仍可读。

普通请求采用提供方默认的思考设置。针对已识别的百炼 DeepSeek 部署，反复出现输出截断且
未发出工具调用时，可触发临时关闭思考的恢复逻辑。这个行为取决于模型 ID 和端点识别，
第三方兼容接口的行为以实际提供方为准。

输出预算预留包含网关计入输出消耗的推理和回答 Token。在增大输出预留时要留出输入空间，
并保持 `reserve_output_tokens` 小于 `context_tokens`。具体预算见 [评测说明](benchmarks/README.md)。
