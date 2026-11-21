# DeepSeek

The model client uses the Chat Completions API and supports streamed
`reasoning_content` alongside visible text and tool calls. Protocol reasoning
is retained on assistant messages, counted in context budgets, and saved in
owner-only checkpoints for subsequent requests and session recovery. It is not
printed as the answer or emitted as reasoning events to the audit/trace streams.
Existing checkpoints without this optional field remain readable.

## Configuration

The default configuration for a root login is `/root/.miniclaw/config.toml`.
Create/edit it through the CLI to retain owner-only file permissions:

```sh
conda activate harness
miniclaw config set base_url https://api.deepseek.com
miniclaw config set model deepseek-flash
miniclaw config set workspace /root/rivermind-data/mini-openclaw-harness
miniclaw config set api_key
```

The last command asks for the API key without echoing it. Manual TOML string
values, including the API key, must be quoted; the file must have mode 600.
Environment values such as `OPENAI_API_KEY`, `OPENAI_MODEL`, and
`OPENAI_BASE_URL` override the corresponding saved settings.

```sh
cd /root/rivermind-data/mini-openclaw-harness
miniclaw --execution-mode trusted
```

The explicit flag enables the existing non-root host execution backend and
does not change the saved execution mode. Shell/Git calls still require approval.

The provider's default thinking behavior is retained; no thinking toggle or
reasoning-effort setting is introduced by this change. The configured output
reserve includes both reasoning and answer tokens. Increase it together with
the context budget for tasks requiring longer outputs, keeping the reserve
smaller than the total budget. A saved checkpoint retains credential redaction;
it is not a byte-for-byte archive of raw provider responses.

Official protocol: https://api-docs.deepseek.com/guides/thinking_mode/
