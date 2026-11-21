# Trusted host execution

`trusted` enables Shell and Git on a POSIX machine without Docker. It is an
explicit trust decision: it does not provide filesystem, network, CPU, memory,
or process-count isolation. Commands can access the host resources available to
their execution user. Their working directory is a copy, not a security boundary.
Writes outside that copy are not covered by `/changes` or `/apply`.

## Select a mode

- `auto` (default): select an available isolated backend, otherwise keep file
  tools in restricted mode. Never fall back automatically to trusted execution.
- `restricted`: explicitly disable Shell/Git even if Docker is installed.
- `trusted`: enable non-root host execution. An unavailable identity fails startup.

Enable for one invocation:

```sh
conda activate harness
cd /root/rivermind-data/mini-openclaw-harness
miniclaw --execution-mode trusted
```

The normal model/API configuration is still required. Startup and `/sandbox`
display the selected mode, actual execution identity, and lack of isolation.
The model also receives this boundary in required context. `run_command` accepts
an argv array; use `["sh", "-c", "..."]` when shell syntax is necessary.

Equivalent environment configuration is `MINICLAW_EXECUTION_MODE=trusted`.
To persist the selection, use `miniclaw config set execution_mode trusted`.
Return to the default with `miniclaw config set execution_mode auto` or override
with `--execution-mode auto`. No deployment automatically enables trusted mode.
`--require-strong-sandbox` is incompatible with `trusted` and `restricted`.

## Execution identity and files

When the Harness runs as root, `trusted_user` defaults to `miniclaw-runner`.
An administrator must provision this dedicated non-root user with a non-root
primary group. It should have no sudo privileges, login, or sensitive group
memberships. Account creation is an administration step; application startup
does not create users or change system directory permissions. Set a different
account with `--trusted-user NAME` or `MINICLAW_TRUSTED_USER`.

When the Harness already runs as a non-root user, commands run as that user.
`trusted_user` does not switch identities in this case. Host records and secrets
readable by the same user remain readable by commands.

System directories and executables must be traversable/readable/executable by
the chosen account. A root directory with mode `700`, for example, prevents
ordinary user execution. An administrator must review any necessary permission
changes; the Harness fails startup rather than modifying them or executing as root.

For a root Harness, fresh copies are created under unique
`/var/tmp/miniclaw-trusted-*` directories so that `/root` can stay `700`.
Only the copy and a private command home/temp directory are owned by the runner.
The session parent allows traversal; artifact journals remain root-only. Copies
are retained for review/recovery and are not automatically deleted on exit.
Internal file writes transfer ownership of the touched paths to the runner so
that commands and file tools can operate on the same copy. Deployments sharing
one runner account do not isolate concurrent trusted sessions from each other.

Only policy-allowlisted environment values are passed to commands; Shell defaults
to `PATH`, plus a private `HOME` and `TMPDIR`. Model API keys, proxy settings,
`PYTHONPATH`, and Git configuration variables are not inherited implicitly.
Dependencies should be installed in a venv in the copy or under the command
user's home; root-owned Conda installation directories are not writable.

## Approval and limits

Every Shell/Git tool call requires approval. Existing Git helper/filter/signature
restrictions are retained. Approval previews show the argv and argument digest,
with credential masking. `/changes` reports changes in the copy and `/apply`
asks separately before writing them to the source project. Initial source
permissions determine which other source paths the runner can access.

Commands have a timeout and bounded combined stdout/stderr. The usual POSIX
process group is killed on completion, timeout, excess output, or cancellation.
A deliberately detached process can escape that group: use an isolated backend
for untrusted workloads. There are no OS-enforced memory, CPU, or PID limits.

Scheduled tasks deny commands by default because no human is present. Explicit
host-side preauthorization remains available through the existing API.
Inspection subagents do not receive usable trusted subprocess tools.

MCP retains its existing isolated Docker requirement and is disabled in trusted
mode. This change does not add unisolated MCP server startup or remote transports.
