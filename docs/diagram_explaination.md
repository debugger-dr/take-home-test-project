# Diagram explanation: the entire flow

Example run: an agent converts a document with `pandoc`, asks a human to approve, then opens a GitHub issue with `gh`.
Node names below match diagram.

## End-to-end flow

1. **Start.** `USER` calls `API`. The API authenticates them (tenant + triggering user), loads the agent YAML at a pinned version, copies the resolved definition into the run's first event, and enqueues the run in that tenant's schema in `PG`. It also mints the **run identity token** (binds run, tenant, agent, user).
2. **Pickup.** A `WORKER` in the tenant namespace pulls the run from `PG` with `SKIP LOCKED` and takes a **lease**. It renews the lease with heartbeats and rebuilds context by replaying the event log.
3. **LLM call.** `WORKER` builds the prompt from the definition and history and sends it to `LLMGW`. The gateway applies fairness and the rate limit, calls the provider (`LLM`) with the single shared key, records usage in the platform ledger, and returns the response. The worker never holds the API key.
4. **Tool call.** The model emits `pandoc ...`. `WORKER` sends the call plus its token to `TOOLGW`. The gateway authorizes (deny by default), writes an audit "started" record, then dispatches (see diagram 2).
5. **Execution.** For an execution tool, `TOOLGW` dispatches to the run's `SANDBOX` (gVisor, from a warm pool). The sandbox has a scratch directory and no network. It runs `pandoc`; the result returns via the gateway, which redacts it, audits "finished" and hands it back to the worker.
6. **`gh` without a readable token.** The sandbox's only route out is `PROXY`. `gh` makes its request and the proxy adds the credential on the way out to `THIRD`. The token never exists inside the sandbox.
7. **Human wait.** The worker appends a "waiting" event and releases its lease. The sandbox working directory is snapshotted and the sandbox destroyed. Two days of waiting costs one database row. When the human replies, `API` appends the event and requeues the run; any worker resumes it.
8. **Finish.** The worker appends the result event, the sandbox is cleaned up, and the user is notified.
9. **Failure.** If a worker or node dies, its lease expires and `SCHED` (the reaper) requeues the run. The next worker replays the log and continues. A call with "started" but no "finished" audit record is the ambiguous case; the idempotency key handles it.
10. **Observability.** Every component emits to Splunk. The event log and usage ledger give exact per-agent status and cost.
