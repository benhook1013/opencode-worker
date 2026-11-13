# api-worker

Linux/WSL x86_64 CLI for delegated tasks using native OpenCode sessions. Requires Python 3.10+, Linux pidfd support, Node.js 22+ and npm. This repository contains the integration; OpenCode is a separately licensed pinned dependency, not custom agent code.

## Install

```sh
npm ci --prefix runtime
python3 install.py --prefix "$HOME/.local"
"$HOME/.local/bin/api-worker" doctor --offline
```

The installer copies this checkout and its prepared runtime into `PREFIX/share/api-worker` and puts the launcher in `PREFIX/bin/api-worker`. Install only under a trusted, user-controlled prefix. Shared prefixes writable by untrusted users are unsupported. The prefix and its `share` and `bin` directories must resolve outside the source `runtime` directory. Choose a fresh prefix for testing or side-by-side installation. The installer refuses existing destinations. `bin/api-worker` also runs directly from the prepared checkout. The Python code uses only the standard library.

Config defaults to `${XDG_CONFIG_HOME:-$HOME/.config}/api-worker`; state defaults to `${XDG_STATE_HOME:-$HOME/.local/state}/api-worker`. Explicit absolute `API_WORKER_CONFIG_DIR` and `API_WORKER_STATE_DIR` override those directories. The runtime is resolved relative to the installed source, not the current directory. A different prefix alone does **not** isolate state; set separate state/config directories when testing or running independent installations. Existing records can remain in place; no automatic migration or replay occurs. Do not upgrade source beneath running owners.

Before dispatch, create an owned config directory with mode 0700 and place an OpenAI API key in its `openai-api-key` regular file with mode 0600. Use a private editor or secret manager; do not put the key in command arguments. After independently verifying available API credits, initialize the local guard with `api-worker credit-reset --confirm-credit-available`. `doctor --offline` checks installation presence only, not credentials, account access or billing. Task files and worktrees must be explicit; there is no default model.

## Usage

A thin CLI over pinned **OpenCode 1.19.5**. Native OpenCode owns model execution, tools, sessions, questions, events and compaction. No custom model loop, provider proxy, billing poller or default paid model is used. The provider remains direct OpenAI Responses; Codex subscription/OAuth is not used.

```sh
api-worker doctor --offline
api-worker start --task-file /absolute/task.txt --worktree /absolute/worktree --model openai/gpt-6-luna --reasoning high
api-worker start --task-file /absolute/coding-task.txt --worktree /absolute/worktree --model openai/gpt-6.1-sol --reasoning medium --allow-write --allow-commands
api-worker send WORKER_ID --task-file /absolute/followup.txt --submission-id assignment-followup-1
api-worker wait WORKER_ID
api-worker status WORKER_ID
api-worker result WORKER_ID
api-worker reply WORKER_ID --question-id NATIVE_QUESTION_ID --answers-file /absolute/answers.json
api-worker cancel WORKER_ID
api-worker resume WORKER_ID --task-file /absolute/next-task.txt
api-worker shutdown WORKER_ID
```

`start` promptly returns JSON with stable worker, native session and native request IDs. `run --live` is `start --wait`: it prints the initial receipt before waiting. Plain `run` retains batch behavior; `run --allow-commands` selects the managed backend. Existing batch session records continue through their original implementation, without replay or conversion. `resume`, `status` and `result` route by the stored backend.

`send` accepts a distinct follow-up only after the current request is terminal and the native runner is idle. Active or uncertain prior requests block new submissions rather than silently overlapping work. Immediate redirection requires explicit `cancel`, inspection of partial effects, then a new task. `wait` blocks in ordinary HTTP/SSE code, not a model or sentinel. It handles late attachment and reconnects using retained native state and the submitted native message ID. Request tracking follows verified native automatic-compaction/continuation ancestry, pages retained history when needed, excludes summary-only answers and stops at an unrelated explicit follow-up; initial idle and an old terminal response are not completion of a newer request. If another submission supersedes a waiting cursor, the receipt identifies the next request instead of claiming completion.

A native stop message with visible text is provisional while the native runner is busy: automatic compaction can continue the same request after that message. Completion requires native idle as well as a valid terminal answer.

A confirmed dispatch that is idle without assistant execution becomes `incomplete` with `incomplete_reason: dispatched_without_execution` when a session error explicitly identifies that request's message ID, or after a 10-second grace period on the next reconciliation. Session-only errors remain diagnostics and do not establish request ownership or shorten the grace period; older cached startup errors without explicit attribution are exposed as `unattributed_session_error` diagnostics. This makes `wait` return and allows idle cleanup; it never resubmits the task or limits ongoing execution.

Wait exit codes: complete **0**, failed **1**, incomplete **2**, needs input **3**, superseded cursor **4**, interrupted **124**. Questions contain native IDs/options; answer JSON is an array of selected-label arrays, for example `[["Proceed"]]`. Answers are guarded API dispatches too. Result/status observation and cancellation remain available after credit exhaustion. Truncation, aborted/unfinished terminal tools and missing terminal text remain incomplete; earlier recovered tool errors do not invalidate a later successful final answer. No automatic replay or budget-related resume occurs. Native side effects already performed persist after cancellation.

Identical initial assignments deterministically identify the same worker; their identity includes resolved worktree, task, model, permissions, cost policy and controls. An optional `--worker-id` supplies an explicit stable ID. Repeated identical task text within a worker returns its existing submission receipt by default; `--submission-id` provides an owner-chosen idempotency key and permits intentionally distinct repeated text. Native message and text-part IDs are obtained from native staging receipts and reused exactly once. A lost staging response leaves a durable pending intent and fails closed: inspect retained native state before an explicit new submission. Staging uncertainty and dispatch uncertainty are separate: a durable dispatch-pending receipt precedes the HTTP dispatch, and confirmation requires its acknowledgement or retained assistant execution tied to that exact request/verified native continuation. If the dispatch acknowledgement is lost, `start` or `send` exits with an error. Without such execution evidence, a subsequent `status` or `wait` reports `incomplete` with `failure_reason: dispatch_unconfirmed`; repeated start/send and later submissions fail closed, and wait returns rather than hanging on never-dispatched work. Observation can recover confirmed native execution without resubmitting. Cancellation and inspection remain available, but cancellation alone does not prove whether a generation previously ran; unresolved dispatch requires owner inspection and remains blocked. There is no redispatch/reset command for an uncertain submission. If the backend is stopped and no request-tied execution receipt was cached, status cannot inspect its database through the server API; that outcome remains unknown rather than restarting or resubmitting automatically. A duplicate invocation never silently appends or replays that uncertain task. A matching start with no accepted request retries the existing guarded submission path; uncertain staging remains `incomplete` and inspectable. If idle shutdown wins a concurrent send, the send refuses staging and requires an explicit retry after shutdown completes.

Only read/glob/grep are available by default. `--allow-write` enables native edits; separate `--allow-commands` enables normal native Bash for the task's closed command/validation allowlist. The task must specify permitted commands, scope, prohibitions and completion evidence. A task prompt saying read-only or Validation NONE does not revoke `--allow-commands`: Bash can write files or run tests even when `--allow-write` is absent and native edit tools are denied. For read-only/no-validation work, omit `--allow-commands` and use file-reading tools. A task command allowlist is an instruction to the agent, not an enforced arbitrary-shell classifier. There is no automatic GitHub/controller/review/CI/commit/push/publication authority. Applicable ancestor/worktree AGENTS instructions are supplied explicitly, and nested instructions must be read before edits. External web tools, MCP, repository configuration and arbitrary plugins remain disabled.

**These permissions are not an OS sandbox or physical per-file isolation.** Worktrees equal to or containing the configured credential directory or state directory are rejected, including resolved symlink aliases. Bash executes with host filesystem/network authority, constrained by native permissions and the delegated task. A writable worktree and command permission are not review/merge authorization. Coordinate overlapping assignments and use worktrees without secrets. Commands receive nonsecret PATH and, when present, JAVA_HOME, GRADLE_USER_HOME, DOCKER_HOST and DOCKER_CONTEXT; the Gradle cache defaults to the user's `.gradle`. A single controlled local `shell.env` hook removes server authentication and OpenCode configuration from native command environments. Other/default plugins stay off. OpenAI credentials are not exported to shell environments.

The sole provider credential is `openai-api-key` in the configured config directory; the launcher checks owned 0700 parent/regular non-symlink 0600 nonempty file metadata, while native OpenCode resolves the file reference at runtime. The Python launcher never reads credential contents. It is never copied into CLI arguments, generated configuration or receipts. Per-worker server authentication is separately generated in a private 0600 state file, never printed or placed in arguments. Native servers bind authenticated **127.0.0.1 only**. Same-user processes and OpenCode remain trusted. HOME/XDG paths are private; inherited secrets/proxies are discarded. Repository configs, external skills, Claude config, sharing, updates, model fetching, LSP, formatters and snapshots remain off; Git hooks/fsmonitor/global configuration are overridden.

| Control | Current behavior |
| --- | --- |
| Whole-task time | Unlimited by default. Optional positive `--timeout` requests cancellation after the elapsed deadline; zero clears a saved timeout. No one-hour ceiling. |
| Advisory steps | `--max-steps` passes a positive target to native `agent.steps`; zero clears the saved target. This is not an enforced step or tool-call cap. |
| Per-response output | Native OpenCode default, currently up to 32,000 reasoning-plus-output tokens; not a whole-task cap. Optional `--max-output-tokens` up to model maximum; zero restores native behavior. |
| Model metadata | Verified gpt-6-luna/gpt-6.1-sol: 1,050,000 context and 128,000 maximum output. Other IDs require documented `--context-window` and `--model-output-limit`. No model substitution. |
| Cost-aware compaction | Native compaction near 250,000 total tokens by default, using truthful model input upper bound and an 800,000-token reserve. `--long-context` opts into full-window policy; `--no-long-context` restores early compaction. Compaction uses the same selected model/provider. |
| Request timing | No launcher 120-second request override. Native OpenCode still has 300-second response-header/stream-idle defaults. |
| Resume controls | Known explicit overrides persist. Legacy implicit 8-step/4096/32768 output restrictions are dropped. New provenance is stored. Changing controls requires an idle backend; live follow-up without overrides does not stop it. |

In pinned OpenCode 1.19.5, reaching `agent.steps` adds `MAX_STEPS_PROMPT`, asking the model to finish, but leaves tools enabled. A model can continue making tool calls beyond that target. Do not use `--max-steps` as a hard execution, cost, or safety boundary. Use permissions to restrict capabilities; `--timeout` requests cancellation but is not a spend guarantee. This applies to both batch and live workers. [Pinned native implementation](https://github.com/anomalyco/opencode/blob/v1.19.5/packages/opencode/src/session/prompt.ts#L1178-L1285).

For the two documented models above, API prompts over 272,000 input tokens have higher full-request pricing (2× input/cache rates and 1.5× output rates). Early compaction is **best effort**: input/tool jumps and the compaction request can still cross that boundary. Recorded crossings produce a structured pricing warning. Neither context metadata nor compaction is a hard billing guard; no balance/account API is used.

Managed servers remain alive while working or awaiting input. After terminal work, roughly 30 seconds of idle time reaps the native server, freeing its RAM; native heartbeat timing can add a little delay. The native session database persists. A later send/resume restarts the same saved session; status/result after reaping use retained terminal receipts. Explicit shutdown calls native abort first, then stops only a process whose identity/start time/group is verified through pidfd. Pending questions are not idle-reaped. A permanently killed owner can leave its native child until explicit cleanup; process identities prevent signalling unrelated reused PIDs.

RAM is recorded after a managed native server exits as `native_server_lifetime_peak_rss_kib`, the Linux child-process peak for that server lifetime. While it remains alive, that value is unknown; it is not attributed to the short-lived CLI client or summed across workers. Legacy batch runs retain per-invocation `native_child_peak_rss_kib`. Neither field is total host RAM.

Private state is under the configured state directory (0700). It retains native database/HOME, private native event/log files, exact per-turn task, native submission receipt, status and partial result. Generated files use 0600; output sizes are not capped. Transport failures retain degraded/unknown state and reconnect; they do not justify killing healthy active work. Sessions/workers are not a worktree-level concurrency lock.

Exact `credit_balance_exhausted`/`insufficient_quota` native errors latch globally within the configured state directory; ordinary rate limits do not. Creation, follow-up and question answers preflight the shared latch. `credit-status` is local; `credit-reset --confirm-credit-available` initializes/resets only after independently verifying credits. Missing/malformed guard state fails closed. There are no balance checks, renewal schedules, automatic resets or inference retries added by the launcher. Requests already in flight can continue, and native internal retries can precede the final exhaustion error; no hard spend guarantee is claimed.

## Development and proof

```sh
npm ci --prefix runtime
python3 -m compileall -q worker.py live_worker.py native_transport.py cli_router.py install.py tests
bash -n bin/api-worker
python3 -m unittest discover -s tests -v
```

The suite uses temporary homes, dummy credentials and a loopback fake Responses provider with the real pinned native server. It covers identity/text uniqueness, follow-up, questions, cancellation, late wait, observer recovery, idempotency, idle reaping/restart, command environment, compaction ancestry and provisional completion. It never needs a real key or account request. Install tests use disposable prefixes and refuse existing destinations. Offline proof does not verify live model availability, billing, the account's enabled capabilities or compatibility with a future OpenCode release. Upgrading the pinned native dependency requires repeating protocol/lifecycle proof. The wrapper owns HTTP/SSE interpretation, server lifecycle, receipts, permissions/configuration and the shared credit guard; it does not implement the model/tool loop.

References: [native server/SDK](https://opencode.ai/docs/server/), [native permissions](https://opencode.ai/docs/permissions/), [pinned overflow/compaction policy](https://github.com/anomalyco/opencode/blob/v1.19.5/packages/opencode/src/session/overflow.ts), [Luna model](https://developers.openai.com/api/docs/models/gpt-6-luna), [Sol model](https://developers.openai.com/api/docs/models/gpt-6.1-sol). Command-enabled workers load a local shell-environment hook. Its matching plugin SDK is pinned alongside OpenCode and installed by the same `npm ci --prefix runtime` step. Each private worker config gets the locked manifest and a link to those prepared dependencies, so native startup does not need a registry connection or inherited proxy settings. Missing prepared SDK dependencies fail before launching the server. The pinned npm lock remains in `runtime/package-lock.json`; no inspection archive is retained.
