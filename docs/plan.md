# Implementation Plan: LLM Rate-Limit, Fairness and Cost Layer (the PoC slice)

Status: design phase complete (DESIGN.docx, diagrams). Build phase not started.
Companion files: [DESIGN.docx](DESIGN.docx), [system-diagram.mmd](system-diagram.mmd), [limiter-diagram.mmd](limiter-diagram.mmd), [diagram_explaination.md](diagram_explaination.md).

This document is the build contract. It records what we are building, why, every decision with its alternatives, every known
issue with how we handle it, how each claim will be tested, and the order we build in. Anything not in here is out of scope.

---

## Table of contents

1. Purpose and non-goals
2. How we got here (design evolution)
3. Requirements traceability
4. Decision log
5. System context: where the slice sits
6. Slice specification (the limiter)
7. Fake provider and simulator (the test oracle)
8. Live demo wrapper
9. Issue catalogue: what can go wrong and how we handle it
10. Test plan
11. Repository layout and module contracts
12. Build order, time budget and checkpoints
13. Risk register for the build itself
14. Definition of done
15. README and AI log plan
16. Production mapping, 10x path and cut list
17. Assumptions and open questions
18. Appendices

---

## 1. Purpose and non-goals

### 1.1 What the brief asks for
Design an orchestration framework running ~1000 concurrent agents on Kubernetes with safe tool calls, sandboxed execution,
durability and observability. Deliver a design (max 4 pages), a runnable proof of concept for **one vertical slice** with at least
one test of the safety or scale property claimed, and an AI usage log. The justification for the slice is graded as hard as the code.

### 1.2 The slice we picked
**The LLM rate-limit, fairness and cost layer** (the "LLM Gateway" box in the system diagram).

### 1.3 Why this slice (the justification paragraph, expanded)
- **It is the binding constraint.** ~10k tool calls/min at ~1 LLM call each is ~170 LLM requests/s and, at ~3.4k tokens per call,
  ~34M tokens/min through **one API key**. A provider key's RPM/TPM limit is set by contract, not by us, and sits below that.
  Kubernetes, Postgres and gateways all scale by adding replicas. This one cannot.
- **It is the largest cost line**, so it is where cost control (budgets, per-agent attribution) has to live.
- **It is the only place all tenants contend**, so multi-tenant fairness is real here and nowhere else.
- **It is the uncertain part.** Token costs are unknown until after the call, the provider's real window semantics are unknown,
  and a fair scheduler with priorities must still bound a quiet tenant's wait. Those are correctness problems under imperfect
  information, which is the kind of risk a PoC should retire.
- **Why not the sandbox** (the other risky area): gVisor does not run on macOS, so a local demo would be a stand-in. The limiter runs
  faithfully anywhere. This is a choice made for impact, not familiarity, and the sandbox executor is what we would build second.

### 1.4 Non-goals (explicitly out of the PoC)
- No real LLM calls, no real Kubernetes, no real Postgres (SQLite stands in), no gVisor, no real Splunk.
- No tool gateway, scheduler/timers, worker pool or sandbox. They exist in the design only.
- No multi-key routing, no multi-region, no cross-run memory sharing.
- No authentication or TLS on the demo wrapper (the run identity token is modelled as a field, not verified).
- Secret/PII redaction in logs and the ledger (listed in the cut list).

---

## 2. How we got here (design evolution)

The design changed several times. Recording the changes shows the reasoning and gives the interview something concrete to defend.

| # | Stage | Proposal | Problem found | Resulting change |
|---|---|---|---|---|
| 1 | First sketch | Namespace per tenant; one pod per agent running agent + tools; shared memory between pods; warm/cold pods; per-namespace queue + balancer | Agent and tools in one pod means arbitrary code sits next to the agent's credentials; pod memory is lost on restart; idle agents hold pods; per-tenant queues cannot govern one global LLM quota; "shared memory" across pods is an attack surface | Split into worker (no secrets), sandbox (untrusted), gateways (credentials) |
| 2 | Reshape | Agent = YAML definition loaded into a generic pod per request | Definition is not state; a restart still loses the run | Agent = definition version + event log in Postgres; stateless workers pull and hold a lease |
| 3 | Sandbox granularity | Per call vs per agent vs shared pool | Per call: worst cold start and loses state; shared pool: weakest isolation | Per-agent warm sandbox, per tenant, from a pre-warmed pool; per-call forced for low-trust agents |
| 4 | Isolation tech | Plain containers / gVisor / Kata / Firecracker / WASM | Containers share the kernel; WASM cannot run `pandoc`, `gh`, `git`; Kata/Firecracker need KVM nodes | gVisor `RuntimeClass` by default; microVM tier as the upgrade for low-trust agents |
| 5 | Orchestration engine | Temporal | Heavy dependency, awkward to run locally in one command | Postgres + Postgres-backed queue (leases, reaper, timers owned by us); reverse condition recorded |
| 6 | Slice selection | Five suggested slices | Needed the part everything depends on and the one we are least sure of | LLM rate-limit, fairness and cost layer |
| 7 | Limiter placement | Per-namespace queue + balancer | A per-tenant queue cannot see other tenants, so it cannot be fair against a global quota | One global dispatcher with per-tenant queues |
| 8 | Limiter scope | Eight mechanisms in the PoC | Too much for 3 to 4 hours; shallow beats wide | Core vs stretch split (section 6.14) |
| 9 | Priority | Agent or caller sets priority; ~15% bucket reserved for P0 | Gameable; reserve wastes quota when no P0 exists | Class derived by the platform from run state; per-tenant P0 allowance; P0 charged to the tenant's own fairness account |
| 10 | Quota accounting | Token buckets at 90% | Bucket math couples burst capacity to the largest request (section 6.5) | Sliding-window accountant that mirrors the provider's window; headroom 10% |
| 11 | Test strategy | Real-time async clients | Per-minute limits make tests slow and flaky | Deterministic core + discrete-event simulator in virtual time |

Every row is a candidate entry for the AI log where an AI suggestion was overridden or corrected (section 15.2).

---

## 3. Requirements traceability

| Brief requirement | Design element | Demonstrated in the PoC? |
|---|---|---|
| Scale: ~1000 agents, ~10k tool calls/min, plan for 10x | Global dispatcher, per-tenant queues, capacity math, multi-key path | **Yes, for the LLM quota dimension** (simulated load at and above peak); infrastructure scale is paper only |
| Safe tool calls | Tool gateway, run identity token, deny by default | No (design only). Attribution fields on every LLM request are modelled |
| Kubernetes-native | Namespaces, quotas, pools, KEDA; runs are rows | No (design only; stated mapping in section 16) |
| Sandboxed execution | gVisor per-agent sandbox, egress proxy | No (design only) |
| Durability | Event log + leases + reaper | Partly: requests are retryable by contract; failover is a stretch goal |
| Observability | Event log + ledger + Splunk | **Yes**: ledger answers "what has agent X cost so far"; structured events |
| Cost is the largest line (brief context) | Budgets, accounting, cost levers | **Yes**: budgets and ledger are built and tested |
| LLM provider is external and rate-limited | Limiter | **Yes: this is the slice** |

The PoC proves one row completely rather than several rows partially, which is the brief's stated preference.

---

## 4. Decision log

Each decision states the choice, the rejected alternative, why, and the condition under which we would reverse it.

| ID | Decision | Rejected alternative | Why | Reverse if |
|---|---|---|---|---|
| D1 | Agent = durable record (definition version + event log), not a pod | Pod/process per agent | Idle agents cost nothing; drains and deploys are routine; no per-agent pressure on etcd at 10x | Replay on resume costs more than a few seconds, or agents need hot in-process state or sticky routing |
| D2 | Postgres + Postgres-backed queue | Temporal | One datastore, one-command local demo | We re-implement more than ~3 workflow-engine features, or queue claims exceed ~2k/s |
| D3 | Single logical dispatcher (leader + warm standby) | Distributed counters (e.g. sharded Redis buckets) | Global fairness needs a global view; ~170 decisions/s is trivial for one process; the quota, not CPU, is the limit | Decision rate nears ~10k/s, or multiple keys/regions exist: shard per key, keep fairness global |
| D4 | Fairness unit = **tokens**, measured per rolling window | Requests or wall-clock time | The provider limits tokens and requests; counting requests lets large prompts take more than a fair share | Provider bills by something else (e.g. compute seconds) |
| D5 | Deficit round robin (DRR) across tenants | Strict priority; weighted fair queuing with virtual finish times | O(1), simple, bounded wait, work conserving, easy to explain and test | We need per-request latency guarantees tighter than one DRR round |
| D6 | **Head waits, never skipped** | Skip to a smaller request that fits | Skipping starves large requests indefinitely; cost is some idle quota (bounded by one request) | Measured idle quota exceeds ~5% in realistic workloads: allow bounded skipping with an age override |
| D7 | Priority class derived by the platform from run state | Caller-set priority | A prompt-injected or greedy agent cannot raise its own priority | Platform cannot know urgency (no run-state signal available) |
| D8 | P0 gets its own per-tenant allowance **and is charged to the tenant's fairness deficit** | Fixed global P0 reserve | A reserve wastes quota when idle; charging P0 to the tenant's own account means flooding P0 gains no extra share | Human-blocking traffic is systematically larger than any single tenant's share |
| D9 | Sliding-window **accountant** mirroring the provider (limit headroom 90%) | Classic token bucket | Bucket safety needs `capacity + rate*window <= limit`, forcing a small burst capacity that must also exceed the largest request (section 6.5); a window ledger has no such coupling | Provider documents a true bucket semantic |
| D10 | Reserve `ceil(est_input * safety) + max_output`, reconcile to actual | Reserve estimate only | `max_output` is a hard ceiling so output cannot exceed the reservation; input is only estimated, hence the safety multiplier | Provider offers exact free token counting at low latency: use it and drop the multiplier |
| D11 | Reactive cooldown on 429 in the core; AIMD in stretch | AIMD only | Cooldown is five lines and makes the system safe against a wrong limit; AIMD is tuning | 429s persist after cooldown (limit is lower than configured): AIMD becomes core |
| D12 | Deterministic core + discrete-event simulator in virtual time | Real-time asyncio load test | Per-minute limits make real-time tests slow and flaky; virtual time makes claims exact and reproducible | Reviewers need evidence under real concurrency: add a live soak test via the HTTP wrapper |
| D13 | Fake provider is an **independent oracle** (never imports the scheduler) | Assert on the gateway's own counters | A test that checks the gateway against its own beliefs proves nothing | n/a |
| D14 | Python, standard library only at runtime; `pytest` as the only dev dependency; SQLite ledger | FastAPI/aiohttp, Redis, Postgres container | One-command run with no install surprises; the design does not depend on those libraries | Wrapper needs real async concurrency at scale |
| D15 | Queues in memory; losing them is safe | Durable LLM-request queue in Postgres | Runs are durable and retry on `retry_after` or connection loss; durable per-request queue adds a write per LLM call for little gain at this scale | Duplicate paid calls after failover become a material cost |

---

## 5. System context: where the slice sits

```
 Worker (per run)                     LLM Gateway  (THIS SLICE)                          External
 ---------------                      -------------------------                         --------
 builds prompt            request     1 Admission --> 2 Per-tenant queues --> 3 DRR      LLM provider
 est tokens, run state  ---------->   (reject: retry_after)                    dispatcher  (1 key,
 holds no API key        <---------   4 Reconcile  <------------------------- sends ---->  RPM/TPM)
                          result or        |
                          retry_after      v
                                       Ledger (SQLite / Postgres) + structured events (Splunk)
```

### 5.1 Interfaces the slice depends on (all faked in the PoC)

| Dependency | Real system | PoC stand-in |
|---|---|---|
| Caller | Worker running an agent run | Simulated agent arrival processes |
| Run state (is a human waiting?) | Event log / run record in Postgres | Field on the request, set by the simulator through `derive_class()` |
| Run identity token | Issued by the API, verified by gateways | Plain fields `tenant`, `run`, `agent` (no verification) |
| Provider | External LLM API | `FakeProvider` (section 7.1) |
| Tenant weights, budgets, allowances | Platform schema in Postgres | `config.py` dataclasses |
| Ledger | Postgres platform schema | SQLite file (`:memory:` in tests) |

### 5.2 Contract with the worker (important for correctness)
- `submit` returns **accepted** (the worker waits for the result) or **rejected with `retry_after`** (the worker releases its lease, records
  `llm_backoff`, and the run is requeued with a not-before time; waiting for quota holds no worker and no sandbox).
- The worker must add **jitter** to `retry_after` so rejected callers do not return in lockstep.
- A lost connection means the outcome is unknown. The worker retries; the gateway deduplicates by request id while the original is in flight.
  An already-sent provider call whose response was lost is paid for twice (rare, bounded, visible in the ledger).

---

## 6. Slice specification (the limiter)

### 6.1 Terminology
- **Cost** of a request: `reserved_tokens = ceil(est_input * input_safety) + max_output`, plus 1 request.
- **Window**: the provider's rate-limit period, 60 s in production, configurable (scaled for the live demo).
- **Usable capacity**: `headroom * limit` with `headroom = 0.9`.
- **Class**: P0 (a human is blocked on this run), P1 (interactive/default), P2 (batch/background).
- **Active tenant**: has at least one queued P1/P2 request.

### 6.2 Data types (`types.py`)

```python
class Klass(IntEnum): P0 = 0; P1 = 1; P2 = 2

@dataclass(frozen=True)
class RunState:                      # what the platform knows; the caller cannot choose the class
    human_waiting: bool
    interactive: bool

def derive_class(rs: RunState) -> Klass:
    return Klass.P0 if rs.human_waiting else (Klass.P1 if rs.interactive else Klass.P2)

@dataclass(frozen=True)
class LLMRequest:
    id: str                          # idempotency key
    tenant: str; run: str; agent: str; model: str
    klass: Klass
    est_input_tokens: int
    max_output_tokens: int
    deadline: float                  # absolute time after which the answer is useless
    submitted_at: float

@dataclass(frozen=True) class Accepted:  id: str; est_wait: float
@dataclass(frozen=True) class Rejected:  id: str; reason: str; retryable: bool; retry_after: float | None
@dataclass(frozen=True) class Dispatch:  id: str; tenant: str; reserved_tokens: int; attempt: int
@dataclass(frozen=True) class Usage:     input_tokens: int; output_tokens: int; cached_tokens: int = 0
```

### 6.3 Request lifecycle

```
submitted --admission--> rejected (terminal for this attempt; caller retries if retryable)
    |
    v
 queued --(deadline passes)--> expired
    |  \--(cancel)----------> cancelled
    v
dispatched (reservation held, in flight)
    |--> completed (usage reconciled, ledger row)
    |--> provider_429 --> requeued at head, global cooldown, reservation released
    |--> provider_error/timeout --> unknown outcome: reservation KEPT until window expiry, one retry at head
```

### 6.4 Admission (ordered checks, first failure wins)

| Order | Check | Result | Retryable |
|---|---|---|---|
| 1 | Duplicate `id` already queued or in flight | return the existing result (idempotent) | n/a |
| 2 | Unknown tenant | `unknown_tenant` | no |
| 3 | `reserved_tokens > usable_TPM` (can never fit) | `too_large` | no |
| 4 | Run token cap exhausted | `run_budget_exhausted` | no |
| 5 | Tenant budget (tokens or cost) exhausted | `tenant_budget_exhausted` | no (until reset) |
| 6 | Tenant queue bound exceeded (length or queued tokens) | `queue_full` | yes, `retry_after` = est. drain time of one request |
| 7 | Estimated wait greater than time to deadline | `wait_exceeds_deadline` | yes, `retry_after` = est_wait minus remaining deadline slack |

Estimated wait for tenant `i` (cheap, monotone, deliberately conservative):

```
rate_i   = usable_TPM/window * w_i / sum(w_j over active tenants, including i)
est_wait = (queued_tokens_ahead_in_tenant_i + own_cost) / rate_i
```

**Why this order:** cheap and permanent failures first so a doomed request never occupies queue space; budget before queue so the
caller learns it should stop rather than retry.

### 6.5 Quota accounting: why a sliding-window accountant and not a token bucket

A token bucket with capacity `C` and refill rate `r` can release at most `C + r*W` tokens in any window of length `W`.
For the provider to never see more than `L` in a window we need `C + r*W <= L`. With `r = 0.9*L/W` that forces **`C <= 0.1*L`**.
Two consequences:
1. The bucket's burst capacity is only 10% of the minute limit, and every request must satisfy `cost <= C`, so large requests are
   rejected or never dispatch.
2. Safety depends on tuning `C` and `r` together, which is easy to get wrong.

A window accountant keeps the reservations made in the last `W` seconds and admits a request only if
`sum(reservations in window) + cost <= 0.9*L`. This mirrors what the provider enforces, has no burst/size coupling, and is easy to prove:
if no rolling window exceeds `0.9*L`, then no fixed (aligned) window can either.

```python
class WindowAccountant:
    # entries: deque of (t, id, tokens); running sums of tokens and requests
    def fits(self, cost_tokens, now) -> bool
    def reserve(self, id, cost_tokens, now) -> None
    def reconcile(self, id, actual_tokens) -> None       # replace entry with actual; frees or consumes headroom
    def release(self, id) -> None                         # provider 429: remove reservation
    def next_free_time(self, cost_tokens, now) -> float   # earliest time the request could fit (drives wakeups)
```

Two instances run side by side: tokens (TPM) and requests (RPM).

**Reconcile edge case:** if `actual > reserved` (input estimate too low) the entry is **raised** and the window may exceed the usable
capacity. We allow it (the provider's truth wins) and rely on the 10% headroom plus the input safety multiplier. Because completed entries are exact after
reconcile, only in-flight requests carry estimate error; see the worked example in 18.3. If it ever pushes the
sum above the provider limit the next provider response will be a 429 and the cooldown path engages (section 6.9).

### 6.6 Fair dispatch: deficit round robin in tokens

State per tenant: `weight`, `deficit` (tokens), per-class FIFO queues, `topped_up` flag. Global: ordered list of active tenants and a cursor.

```python
def next_candidate(self, now):
    # 1. P0 lane: round robin over tenants with a pending P0 whose P0 allowance can afford the head
    for t in self.p0_round_robin():
        if t.p0_allowance.can_afford(t.p0_head.cost, now):
            return Candidate(t, t.p0_head, lane="P0")
    # 2. DRR over P1/P2
    for _ in range(len(self.active) * MAX_ROUNDS):          # terminates: every pass adds quantum > 0
        t = self.active[self.cursor]
        if not t.topped_up:
            t.deficit += BASE_QUANTUM * t.weight
            t.topped_up = True
        head = t.head_p1_then_p2()
        if head.cost <= t.deficit:
            return Candidate(t, head, lane="DRR")
        t.topped_up = False                                  # cannot afford this round: end the turn
        self.cursor = (self.cursor + 1) % len(self.active)
    return None

def poll(self, now):
    self.expire_deadlines(now); self.promote_aged(now)
    out = []
    while now >= self.cooldown_until:
        c = self.next_candidate(now)
        if c is None: break
        if not (self.tokens.fits(c.cost, now) and self.requests.fits(1, now)):
            break                                            # WAIT, never skip (D6)
        self.tokens.reserve(c.req.id, c.cost, now); self.requests.reserve(c.req.id, 1, now)
        c.tenant.pop(c.req); c.tenant.deficit -= c.cost       # P0 also charged here (D8); deficit may go negative
        if c.tenant.empty(): c.tenant.deficit = 0; self.deactivate(c.tenant)
        out.append(Dispatch(...))
    return out
```

Properties and why they hold:
- **Work conserving:** a tenant with no backlog is removed from `active`, so its share flows to the others (its deficit resets to 0 so it cannot hoard credit).
- **Bounded wait for a quiet tenant:** one round is `sum(quantum_j over active) / usable_rate`. A request of cost `c` waits at most
  `(ceil(c / quantum_i) + 1)` rounds regardless of how deep a noisy tenant's queue is.
- **Selection is idempotent while blocked:** if the selected head does not fit the window, `poll` returns without consuming the turn; the next `poll`
  re-selects the same candidate (the `topped_up` flag prevents double top-up).
- **No starvation of large requests:** the head is never bypassed, and deficit carries over across rounds until it can pay.
- **Negative deficit (P0 overspend)** is repaid by later top-ups, so a tenant that used P0 receives proportionally less P1/P2 service.

### 6.7 Priority
- Order inside a tenant: P0 lane, then P1, then P2. FIFO within a class.
- **Aging:** a P2 request waiting longer than `age_p2_to_p1` is promoted to P1 (never to P0, which is semantic). Prevents starvation under constant P1 load.
- **P0 allowance:** per tenant token bucket (`p0_rate_tokens_per_s`, `p0_burst_tokens`). Requests above allowance are not lost: they are
  treated as P1 for this attempt (demoted), and an `llm.p0_demoted` event is logged for audit.
- **Gaming analysis:** a tenant that labels everything P0 (a) is limited to its allowance, (b) pays for P0 from its own fairness deficit,
  (c) is visible in audit. The only way to gain priority is the platform believing a human is waiting.

### 6.8 Backpressure
- Per tenant bounds: `max_queue_len`, `max_queue_tokens`. Global bound: sum of the above.
- `Rejected(retry_after)` is returned immediately; the gateway never holds a connection open for a request it cannot serve soon.
- Deadline expiry while queued: removed, counted, `expired` result returned to the caller.
- `next_wakeup(now)` returns the earliest of: window free time for the blocked head, cooldown end, next aging promotion, earliest deadline,
  P0 allowance refill. The driver sleeps until then instead of polling.

### 6.9 Provider feedback
- **429 with `Retry-After`:** release the token reservation, requeue the request at the head of its queue (refund the deficit charge so the tenant is not
  penalized for the provider's refusal), set `cooldown_until = now + retry_after`, log `llm.provider_429`. This is the core safety net for a wrong limit.
- **5xx / timeout (unknown outcome):** keep the reservation until it ages out of the window (the provider may have counted it), retry once at the head
  with `attempt + 1`, ledger row marked `outcome=unknown`. A second failure returns `provider_error` to the caller.
- **Stretch (AIMD):** on 429, multiply the effective headroom by 0.7 (floor 0.5); on each clean window add 0.02 up to the configured headroom. Because the configured
  limit may be wrong or shared with other workloads on the same key.

### 6.10 Cost accounting and budgets
- Every completion writes a ledger row (section 6.11). Cost = `input*price_in + output*price_out` using an **illustrative** price table in config (not a real price list).
- Budgets: per run token cap, per tenant daily token and cost budgets. At 80% an `llm.budget_alert` event fires once; at 100% admission rejects with a
  non-retryable reason, so the worker fails the run (the poison-agent loop stops here).
- Budget counters use **reconciled actuals**, so over-estimation never burns budget.

### 6.11 Ledger schema (SQLite in the PoC, platform schema in Postgres in the design)

```sql
CREATE TABLE llm_calls (
  id TEXT PRIMARY KEY,            -- request id (idempotent writes)
  ts_submit REAL, ts_dispatch REAL, ts_complete REAL,
  tenant TEXT, run TEXT, agent TEXT, model TEXT, klass INTEGER,
  est_input INTEGER, max_output INTEGER, reserved INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
  cost REAL, queue_wait REAL, attempt INTEGER, outcome TEXT   -- ok | unknown | expired | provider_error
);
CREATE INDEX idx_llm_tenant_ts ON llm_calls(tenant, ts_complete);
CREATE INDEX idx_llm_agent ON llm_calls(agent, ts_complete);
```

"What has agent X cost so far" is `SELECT SUM(cost), SUM(input_tokens+output_tokens) FROM llm_calls WHERE agent = ?`.

### 6.12 Observability: structured events
One JSON line per event, ready for Splunk. Common fields: `ts, tenant, run, agent, request_id, klass`.

| Event | Extra fields |
|---|---|
| `llm.submit` | `est_input, max_output, reserved` |
| `llm.rejected` | `reason, retryable, retry_after` |
| `llm.queued` | `est_wait, queue_len` |
| `llm.dispatched` | `queue_wait, window_tokens_used, window_requests_used, deficit` |
| `llm.completed` | `input_tokens, output_tokens, cost, estimate_error_pct` |
| `llm.provider_429` | `retry_after, cooldown_until` |
| `llm.expired`, `llm.cancelled`, `llm.p0_demoted` | reason fields |
| `llm.budget_alert`, `llm.budget_exhausted` | `scope, used, limit` |

Gauges (JSON at `/metrics` in the wrapper, returned by `snapshot()` in the core): queue length and queued tokens per tenant, window utilisation (tokens and
requests), cooldown state, deficits, counts per rejection reason.

### 6.13 Determinism rules for the core
- No wall clock, no threads, no sleeping, no global randomness inside `llmgw/` core modules. Time is a parameter (`now`).
- Iteration order is explicit (lists, deques, insertion-ordered dicts), never set iteration.
- The simulator owns the seeded RNG. **Same seed, same event trace** is itself a test.

### 6.14 Core versus stretch

| Item | Status | Reason |
|---|---|---|
| Admission, queues, DRR, window accountant, reserve and reconcile | **Core** | The safety and fairness claims |
| P0 lane, allowance, aging | **Core** | Human-blocking priority claim |
| Backpressure, deadline expiry, `retry_after` | **Core** | Bounded memory and waiting claim |
| 429 cooldown, unknown-outcome handling | **Core** | Safe when estimates or limits are wrong |
| Ledger, budgets, structured events | **Core** | Cost claim |
| Live HTTP wrapper and `make demo` | **Core** (thin) | "We will run it" |
| AIMD adaptive headroom | Stretch | Tuning, not safety |
| Leader election and failover (new leader pauses one window) | Stretch | Design only unless time remains |
| Live-mode soak test | Stretch | Evidence under real concurrency |

If a stretch item is not built, README and DESIGN already say so; nothing in the core depends on it.

---

## 7. Fake provider and simulator (the test oracle)

### 7.1 FakeProvider (`sim/provider.py`)
The provider is the **ground truth**. It never imports the scheduler, and all safety assertions read the provider's own observations.

| Aspect | Behaviour |
|---|---|
| Limits | `rpm`, `tpm`, `window` seconds |
| Window mode | `rolling` (default) or `fixed_aligned`; tests run both |
| Counting mode | `reserve_at_start` (counts `input + max_tokens` on arrival, corrects at completion) or `count_at_completion` (counts actual usage when finished) |
| Over limit | Returns 429 with `Retry-After` = time until enough window frees; rejected requests consume no tokens |
| Latency | `base + per_output_token * output_tokens`, plus seeded jitter |
| Usage report | `input_tokens = round(est * actual_ratio)` where the ratio is drawn from a configured distribution (can be biased); `output_tokens <= max_tokens` |
| Fault injection | Optional 5xx and timeout probability (used by the unknown-outcome tests) |
| Observations (oracle) | max tokens and requests in any rolling window, count of 429s, total usage reported, per-request timeline |

**Assumption flag:** real providers differ in exactly when they count tokens and how windows align. Our reservations are conservative under both counting modes,
and the rolling-safe property implies fixed-window safety. This is stated in the README as a modelling assumption, not a fact about any vendor.

### 7.2 Workload (`sim/workload.py`)
Each tenant has a profile: arrival process (Poisson with seeded RNG, optionally bursty on/off), class mix, size distributions
(`est_input`, `max_output`), `human_waiting` probability, deadline distribution, and the estimator error model (bias and spread of the
chars-divided-by-4 style estimate relative to the provider's actual count).

Scenario sizes (defaults used in the tests):

| Parameter | Value |
|---|---|
| Provider limits | RPM 600, TPM 400,000, window 60 s |
| Usable capacity (90%) | 540 req/min, 360,000 tokens/min = 6,000 tokens/s |
| Typical reserved cost | ~3,000 tokens (so TPM binds first, ~120 req/min) |
| Base quantum | 4,000 tokens x weight |
| Tenants in the main scenario | A (noisy), B, C, equal weights, fair share 120,000 tokens/min each |
| Duration | 600 s virtual, first 60 s excluded as warm-up |

A second scenario uses small requests so **RPM** binds, proving both buckets are enforced.

### 7.3 Simulator engine (`sim/engine.py`)
- Event queue (`heapq`) of `(time, seq, kind, payload)`; `seq` breaks ties deterministically.
- Event kinds: `arrival`, `wakeup`, `provider_complete`, `provider_reject`, `retry_arrival`.
- Loop: pop event, set `now`, apply to scheduler (`submit`, `on_complete`, ...), call `poll(now)`, send each `Dispatch` to the provider model, schedule its completion,
  schedule the next `wakeup` from `next_wakeup(now)`.
- Callers obey `retry_after` with seeded jitter, so thundering-herd behaviour is part of what is tested.
- A `Report` collects per-request timelines, per-tenant goodput by window, queue wait percentiles, and the provider's observations.

### 7.4 Negative controls (so the tests can fail)
The engine accepts alternative schedulers behind the same interface:
- `FifoNoFairness`: one global FIFO, quota still enforced.
- `NoQuotaCheck`: fair dispatch but ignores the window accountant.
- `CallerSetsPriority`: honours a request's self-declared class.
Each safety test also runs against the matching broken control and **asserts the property is violated**. A test that cannot fail proves nothing.

---

## 8. Live demo wrapper

Goal: satisfy "must run locally with one command" and show the same core behaving as a service. It is thin by design.

- **Processes** (started by `make demo`, one command):
  1. `provider_server` on `:8101`, the fake provider over HTTP using a real clock.
  2. `gateway` on `:8100`: HTTP front end around the **same core**, a scheduler thread calling `poll` and sleeping until `next_wakeup`.
  3. `loadgen`: three tenants (one noisy) for about 90 s, printing a live table.
- **Endpoints** (stdlib `http.server` with threading): `POST /v1/llm` (blocks until completion or returns 429 + `Retry-After` JSON), `GET /metrics`, `GET /ledger/summary`.
- **Scaled window:** the demo uses `window = 10 s` with proportionally scaled limits so behaviour is visible in under two minutes. The tests use the real 60 s window in virtual time.
- **Output:** a final report: per-tenant offered load, goodput, p50/p99 queue wait, provider-observed max window usage versus its limit, 429 count, total cost per tenant.
- **Honest label:** the wrapper is a smoke test of the live path. The proof is the deterministic suite (section 10).

---

## 9. Issue catalogue: what can go wrong and how we handle it

| # | Issue | Why it matters | How we handle it | Where tested |
|---|---|---|---|---|
| 1 | Output length unknown before the call | Cannot know the true cost up front | Reserve `max_output` (hard ceiling), reconcile to actual and refund | T4 |
| 2 | Input token estimate wrong | Could overshoot the limit | Input safety multiplier (default 1.15), 10% headroom, upward reconcile, cooldown on 429; in production use the provider's exact counting | T4, T4b |
| 3 | Estimate error beyond the margin | The guarantee has a boundary | **Documented boundary:** reconcile makes completed entries exact, so estimate error only affects requests still in flight. Overshoot is about `in_flight_tokens * error_fraction` and is safe while it stays within the 10% headroom. Beyond that (slow provider plus large bias) 429s occur, the cooldown bounds them and the system recovers; the test asserts a bounded rate and recovery | T4b |
| 4 | Provider window semantic unknown (rolling vs fixed, count at start vs completion) | Our accounting could be wrong | Conservative reservations; rolling-safe implies fixed-safe; fake supports both modes and tests run both | T2 |
| 5 | Noisy neighbour | One tenant takes the quota | Per-tenant queues, DRR in tokens, weights | T1 |
| 6 | Quiet tenant latency | Fairness is meaningless without a wait bound | Wait bound derived from DRR round time, asserted on p99 | T1 |
| 7 | Head-of-line blocking by a large request | Blocks everyone, or starves itself if skipped | Wait, never skip (D6); cost bounded by one request; more keys shorten waits at 10x | T6 |
| 8 | Large request that can never fit | Would wait forever | `too_large` at admission, non-retryable | U-admission |
| 9 | Priority gaming | Everyone marks urgent | Class derived from run state, per-tenant P0 allowance, P0 charged to deficit, audit event | T3 |
| 10 | P2 starvation | Batch work never runs | Aging to P1 | T5 |
| 11 | Retry storms | Rejected callers return together | `retry_after` plus caller jitter; thundering herd scenario in the simulator | T7 |
| 12 | Unbounded queues and memory | Burst crashes the gateway | Per-tenant and global bounds, immediate rejection | T7 |
| 13 | Duplicate calls and double payment | Money lost on retries | Idempotent request ids, unknown-outcome keeps the reservation, ledger marks `unknown` | U-dup, T8 |
| 14 | Provider outage or rising 429s | Platform-wide stall | Cooldown, requeue at head, runs wait durably with no worker held; AIMD stretch | T8, S1 |
| 15 | Single dispatcher is a single point of failure | All LLM calls stop | Warm standby with lease; new leader dispatches nothing for one window; paused old leader can overlap, 10% headroom is the buffer (stretch) | S2 (stretch) |
| 16 | In-memory queues lost on crash | Lost requests | Safe by contract: runs are durable and retry (D15); cost is an occasional duplicate call | Documented |
| 17 | Budget exhaustion and poison loops | Runaway cost | Per-run token cap and tenant budgets reject non-retryably; run fails with a reason | T9 |
| 18 | Over-estimation burns budget | Unfair billing | Budgets use reconciled actuals only | T4 |
| 19 | Tenant weight misconfiguration (zero or negative) | Divide by zero, starvation | Config validation at load; weight must be > 0 | U-config |
| 20 | Tenant joins or leaves mid-run | DRR state corruption | `active` list updated on first enqueue and on drain; deficit reset to 0 on drain | U-drr |
| 21 | Non-determinism in tests | Flaky proofs | Core has no clock, threads or global RNG (6.13); same-seed trace equality test | T10 |
| 22 | Tests that cannot fail | Proves nothing | Independent oracle (D13) and negative controls (7.4) | All scenario tests |
| 23 | Arbitrary thresholds | Claims look cooked | Thresholds derived from formulas in section 10; the derivation is in the test docstring; tuned only with the formula changed, not the number | Section 10 |
| 24 | Virtual time hides real concurrency bugs | Core is single-threaded; wrapper adds threads | Wrapper serialises access to the core with one lock; documented as a known gap; stretch soak test | Known gap |
| 25 | Streaming responses | Usage known only at the end | Out of scope; reservation is already conservative; noted in cut list | Cut list |
| 26 | Clock skew between gateway replicas | Window mismatch | Single dispatcher owns time; standby waits one window after takeover | S2 |
| 27 | Provider limits shared with other workloads on the key | Our share is smaller than configured | 429 cooldown plus AIMD stretch; headroom | T4b, S1 |

---

## 10. Test plan

### 10.1 Principles
- Safety assertions read the **provider's** observations, not the gateway's.
- Every scenario test has a negative control that fails the same assertion.
- Thresholds are derived, not guessed. Derivations are in docstrings.
- Seeds are fixed; a failing seed is reported so the failure reproduces.

### 10.2 Derived quantities (used by assertions)

```
usable_rate      = 0.9 * TPM / window                    # 6,000 tokens/s in the main scenario
round_time       = sum(quantum_j for active j) / usable_rate   # 3 * 4,000 / 6,000 = 2.0 s
wait_bound(c, i) = (ceil(c / quantum_i) + 1) * round_time      # typical c ~ 3,000: (1+1)*2 = 4 s
fair_share_i     = weighted max-min allocation of usable capacity given each tenant's offered load (water filling)
```

### 10.3 Scenario tests (the claims)

| ID | Claim | Setup | Assertions (all must hold) | Negative control that must fail |
|---|---|---|---|---|
| **T1** Noisy neighbour | A tenant offering 10x load cannot starve others | A offers 1.2M tokens/min; B and C offer 60k each (below fair share 120k). Equal weights | B, C goodput >= 95% of offered; B, C queue-wait p99 <= `wait_bound` + 0.5 s; total goodput >= 90% of usable capacity (work conserving); A receives the remainder (>= 85% of usable minus B and C) | `FifoNoFairness`: B p99 wait exceeds 10x the bound |
| **T1b** Above-share tenant | A tenant demanding more than its share still gets its share | B offers 150k (above 120k fair share) with A noisy | B goodput >= 0.95 * fair_share_B; B never exceeds its share when others are backlogged by more than one quantum per round | FIFO control |
| **T1c** Weights | Weighted shares are honoured | Weights 1:2:1 with all backlogged | Goodput ratios within 5% of 1:2:1 | Equal-weight scheduler |
| **T2** Quota safety | The provider is never driven over its limit | Main scenario, both window modes, both counting modes | Provider-observed max tokens in any rolling window <= `TPM`; max requests <= `RPM`; provider 429 count <= 1% of calls (in steady state, zero) | `NoQuotaCheck`: provider 429s exceed 10% |
| **T2b** RPM binds | Both limits are enforced | Many small requests (cost ~300) | Provider-observed requests <= `RPM`; tokens well below `TPM` | `NoQuotaCheck` |
| **T3** Priority | Human-blocking calls go first and cannot be gamed | Saturated; 5% of B's requests are P0. Then A floods 100% P0 | P0 p99 wait <= 2 * round_time; P1 p50 > P0 p50; A's P0 goodput <= allowance + one burst; A's total goodput <= its weighted share + tolerance; other tenants' P0 p99 unaffected | `CallerSetsPriority`: A's flood starves B and C's P0 |
| **T4** Estimate error | Accounting matches the provider exactly and stays safe | Input estimate error uniform in -15%..+15%; actual output < max | Sum of ledger tokens == provider-reported total (exact integer equality); sum of ledger cost == recomputed cost; per-agent aggregation matches; provider window safety (T2) still holds | Scheduler that reserves only the estimate: provider 429s |
| **T4b** Beyond the margin | The guarantee has a documented boundary | Slow provider (latency ~30 s, so ~50% of the window is in flight) plus +40% input bias, which pushes `in_flight * error` past the 10% headroom | 429s occur but cooldown bounds them: 429 rate <= 5%; system recovers (goodput in the last quarter >= 90% of baseline); ledger still exact | n/a (this test documents the limit) |
| **T5** Aging | Batch work is not starved | Constant P1 load at capacity, a trickle of P2 | Every P2 request dispatches within `age_p2_to_p1` + `wait_bound` | Scheduler without aging |
| **T6** Large request | Wait, never skip | Quiet tenant sends one 8k-token request while a noisy tenant sends many small ones | Large request dispatches within `wait_bound(8000, i)`; idle quota while waiting <= one request's cost | Skipping scheduler: large request waits > 5 rounds |
| **T7** Backpressure | Bounded memory, nothing lost | Burst of 5x capacity for 30 s with jittered retries | Queue length <= bound at every event; `accepted + rejected == sent` (no silent loss); retried callers succeed within 5 attempts; no synchronized retry spike (max concurrent retries within 2x mean) | Unbounded-queue scheduler exceeds the bound |
| **T8** Provider faults | Safe under 5xx, timeouts and 429 | 2% faults injected; one 20 s provider-wide 429 period | No request lost; unknown-outcome requests keep their reservation and are marked `unknown`; no double dispatch of the same id while in flight; recovery after the period | n/a |
| **T9** Budgets | Poison loops and runaway cost stop | A run in a tight loop; a tenant crossing 80% then 100% | Alert at 80% exactly once; admission rejects non-retryably at 100%; ledger totals never exceed budget by more than one reservation | Scheduler without budgets |
| **T10** Determinism | Reproducible | Run T1 twice with the same seed, then a different seed | Identical trace hash for the same seed; different for another | n/a |

### 10.4 Unit tests (the mechanics)

| Area | Cases |
|---|---|
| Window accountant | Admit up to the limit, reject over; entries age out exactly at `window`; `next_free_time` correct; reconcile down frees headroom; reconcile up consumes; release removes; request and token accountants independent |
| DRR | Equal weights alternate; weights 1:2 give 1:2; deficit carries over for large requests; deficit resets on drain; idle tenant gets no credit; selection idempotent when blocked; negative deficit repaid |
| Priority | P0 before P1 before P2; allowance exhaustion demotes to P1 and logs; aging promotes at the threshold only; FIFO within class |
| Admission | Each reject reason in order; duplicate id idempotent; `too_large` non-retryable |
| Backpressure | Queue bound by length and by tokens; `retry_after` monotone in queue depth; deadline expiry removes and reports |
| Provider feedback | 429 releases reservation, requeues at head, refunds deficit, sets cooldown; 5xx keeps reservation; second failure surfaces `provider_error` |
| Budgets | Alert fires once at 80%; exhausted at 100%; actuals not estimates counted |
| Ledger | Idempotent insert by id; aggregates per agent and tenant |
| Config | Zero or negative weights, impossible limits and bad headroom rejected |
| Wakeup | `next_wakeup` never later than the real next state change (property: advancing to it makes progress) |

### 10.5 Stretch tests
- **S1 AIMD:** a provider whose real limit is 60% of configured: with AIMD, 429 rate converges below 1% within N windows; without it, cooldown alone keeps it below 5%.
- **S2 Failover:** kill the dispatcher mid-load; the new one dispatches nothing for one window; provider-observed window never exceeds its limit across the handover.

### 10.6 How the suite is run
`make test` runs everything (target under 60 s because time is virtual). `make sim` prints the scenario reports for human reading.

---

## 11. Repository layout and module contracts

```
.
├── README.md                    how to run, what is real/faked, known gaps, the justification paragraph
├── AI_LOG.md                    the AI usage log
├── Makefile                     demo, test, sim
├── pyproject.toml               python >=3.13 (uv provisions it), pytest as the only dev dependency
├── docs/
│   ├── DESIGN.docx              architecture proposal
│   ├── plan.md                  this file
│   ├── system-diagram.mmd, limiter-diagram.mmd, diagram_explaination.md
├── llmgw/                       the slice (core is pure: no clock, threads, I/O)
│   ├── types.py                 dataclasses and enums (6.2)
│   ├── config.py                tenant, provider and scheduler config with validation
│   ├── window.py                WindowAccountant (6.5)
│   ├── scheduler.py             admission, queues, DRR, priority, backpressure, provider feedback (6.4 to 6.9)
│   ├── budgets.py               run and tenant budgets, alerts (6.10)
│   ├── ledger.py                SQLite ledger and queries (6.11)
│   ├── events.py                structured event emitter (6.12)
│   ├── sim/
│   │   ├── engine.py            discrete-event engine and Report
│   │   ├── workload.py          arrival processes and profiles
│   │   ├── provider.py          FakeProvider oracle
│   │   └── controls.py          negative-control schedulers (7.4)
│   └── live/
│       ├── gateway.py           HTTP wrapper + scheduler thread
│       ├── provider_server.py   FakeProvider over HTTP
│       └── loadgen.py           demo load and live report
└── tests/
    ├── unit/                    section 10.4
    ├── scenarios/               section 10.3 (T1 to T10), stretch S1 and S2
    └── conftest.py              seeds, scenario builders
```

The generated `main.py` stub is removed; the entry point is `python -m llmgw` (runs the demo).

### 11.1 Scheduler public API (the only surface the drivers use)

```python
class Scheduler:
    def __init__(self, cfg: SchedulerConfig, events: EventSink, ledger: Ledger): ...
    def submit(self, req: LLMRequest, now: float) -> Accepted | Rejected
    def cancel(self, request_id: str, now: float) -> bool
    def poll(self, now: float) -> list[Dispatch]                 # idempotent; call after any state change or wakeup
    def on_complete(self, request_id: str, usage: Usage, now: float) -> None      # idempotent
    def on_provider_rejected(self, request_id: str, retry_after: float, now: float) -> None
    def on_provider_error(self, request_id: str, kind: str, now: float) -> None
    def next_wakeup(self, now: float) -> float | None
    def snapshot(self) -> dict
```

---

## 12. Build order, time budget and checkpoints

Budget: 6 to 8 hours total for the whole take-home. Design phase (DESIGN.docx, diagrams, this plan) is done. The PoC and write-up below fit in 4 to 5 hours.
Hard stop at 8 hours total even if unfinished; the README then states where we stopped.

| Step | Work | Time | Checkpoint (must be true to continue) |
|---|---|---|---|
| 0 | Repo scaffolding: `pyproject.toml`, Makefile, package skeleton, remove stub, `pytest` runs | 0.25 h | `make test` runs (zero tests) green |
| 1 | `types`, `config` (validation), `window.py` + unit tests | 0.5 h | Window unit tests pass, including ageing and reconcile |
| 2 | `FakeProvider` (oracle) + `sim/engine.py` + workload, with a trivial pass-through scheduler | 0.75 h | Pass-through scheduler **fails** T2 against the oracle (proves the oracle bites) |
| 3 | `scheduler.py` core: admission, queues, DRR, reserve and reconcile, wakeups; DRR and admission unit tests | 1.0 h | T1, T1b, T1c, T2, T2b, T4 pass; their negative controls fail as required |
| 4 | Priority (P0 lane, allowance, aging), backpressure, provider feedback (cooldown, unknown outcome), budgets; unit tests | 0.75 h | T3, T5, T6, T7, T8, T9, T10 pass with controls |
| 5 | Ledger, events, `snapshot`, report output; T4 ledger exactness | 0.25 h | Ledger equals provider usage exactly |
| 6 | Live wrapper, `make demo`, final report | 0.5 h | `make demo` runs end to end on a clean checkout |
| 7 | README (run, real vs faked, gaps, justification with "what I learned"), AI log, gaps list, update DESIGN where the PoC changed it | 0.75 h | Reviewer can follow README cold |
| **Total core** | | **~4.75 h** | |
| S | Stretch: AIMD (S1), failover (S2), live soak | only with time left | Do not start before step 7 is done |

**Cut line:** if behind at the end of step 4, skip step 6's HTTP wrapper and make `make demo` print the simulator report (still one command). If behind at step 3, drop T1c and T6 and say so in the README.

### 12.1 Working rules
- Test before feature for each mechanism: write the failing scenario or unit test, then the code.
- Commit at every checkpoint with a message that states the claim now proven.
- Keep a running note of every AI suggestion overridden (feeds `AI_LOG.md`).

---

## 13. Risk register for the build itself

| Risk | Likelihood | Impact | Mitigation | Fallback |
|---|---|---|---|---|
| DRR edge cases (blocked heads, deficit resets) cause subtle bugs | Medium | High | Unit tests for each edge first; selection kept side-effect free until dispatch | Simplify to per-tenant round robin with token deficit and no P0 charging |
| Simulator complexity eats time | Medium | High | Keep the engine to five event kinds; no premature abstractions | Fixed-step simulation (1 ms ticks) instead of event queue |
| Thresholds look arbitrary | Medium | Medium | Derive from formulas (10.2) and document | Report measured values next to bounds |
| Oracle too forgiving or too strict | Low | High | Step 2 checkpoint: pass-through must fail T2 | Tighten provider model |
| Python 3.13 not installed locally | Low | Low | `uv` provisions it; core uses only features present in 3.12 | `requires-python >= 3.12` |
| Wrapper threading bugs | Medium | Low | One lock around the core; wrapper is a smoke test, not the proof | Drop the wrapper, print the simulator report |
| Scope creep into stretch | High | Medium | Stretch only after step 7 | n/a |
| Reviewer cannot run it | Low | High | Clean-checkout run in a temp directory before submitting | README troubleshooting |

---

## 14. Definition of done

- [ ] `make test` passes from a clean checkout in under 60 s; scenario tests T1 to T10 and all unit tests green.
- [ ] Every scenario test has a negative control that fails the same assertion (shown in the test output).
- [ ] `make demo` runs end to end and prints per-tenant offered load, goodput, p50 and p99 queue wait, provider-observed maximum versus limit, 429 count and cost.
- [ ] The provider oracle reports **zero or bounded 429s** exactly as the tests state.
- [ ] README: how to run, what is real, what is faked, known gaps, the one-paragraph justification including **what I learned** and **what I would build second**.
- [ ] `AI_LOG.md` with at least: three overrides, one place AI was clearly better, one thing done deliberately without AI, tools and models used.
- [ ] DESIGN.docx Section 7 matches what was built (stretch items still labelled correctly, thresholds consistent).
- [ ] No proprietary code; no secrets; no real provider keys anywhere.

---

## 15. README and AI log plan

### 15.1 README outline
1. One-paragraph summary and the slice justification (risk retired, what I learned, what I would build second).
2. Run: `make demo`, `make test`, `make sim`; requirements (`uv` or Python 3.12+).
3. What is real versus faked (table from section 5.1 and 7).
4. Why virtual time, and what it hides.
5. Claims and the test that proves each (table from 10.3).
6. Known gaps and what was cut (section 16.3).
7. Where each design decision lives in the code (decision log pointers).

### 15.2 AI log: entries already known (capture while fresh)

| Type | Entry |
|---|---|
| Override | AI recommended plain Docker with seccomp for isolation; rejected (shared kernel); gVisor chosen with its cold-start and I/O trade-offs noted. |
| Override | AI proposed Temporal for durability; rejected for local one-command simplicity and a single datastore; reverse condition recorded. |
| Override | First sketch put agent and tools in one pod with shared memory; AI critique identified credential exposure and durability gaps; redesigned. |
| Correction | AI's "token bucket at 90%" was wrong for provider windows (burst capacity coupled to largest request); replaced with a window accountant after working the math. |
| Correction | AI's initial P0 design (global 15% reserve) was dropped for per-tenant allowance charged to deficit after discussing gaming. |
| Correction | "Estimates would not exist in production": clarified that output length is never known in advance; reserve and reconcile remain in production. |
| AI better | Cataloguing failure modes and negative-control tests quickly; drafting the failure table. |
| Without AI | Choice of the slice and the threshold derivations (done by hand so they can be defended live). |
| Tools | Claude (Sonnet 5.5) in Claude Code for design review, drafting and code; manual review of every decision. |

Each override entry is written as a PR review: what the AI proposed, what was wrong, what was done instead, how it was verified.

---

## 16. Production mapping, 10x path and cut list

### 16.1 PoC component to production

| PoC | Production |
|---|---|
| `Scheduler` core | LLM Gateway service (leader + warm standby, lease in Postgres) |
| SQLite ledger | Postgres platform schema, partitioned by day, batched inserts |
| In-memory queues | Same; durability comes from run records and retries |
| Config dataclasses | Tenant plans and weights in the platform schema |
| Fake provider | The real provider with a real key behind the gateway |
| JSON events | Splunk Logging; gauges into Splunk Observability |
| Request fields `tenant, run, agent` | Verified run identity token |

### 16.2 What breaks first at 10x and the change

| Order | Bottleneck | Change |
|---|---|---|
| 1 | LLM quota (~340M tokens/min on one key) | One scheduler with **one window accountant per key**; routing by headroom; fairness stays global. Prompt caching, smaller models for easy steps |
| 2 | Ledger write rate (~6k/s) | Batched inserts, partitioning, tenant sharding |
| 3 | Dispatcher decision rate | Not before ~10k/s; then shard per key |
| 4 | Head-of-line waits | More keys shorten waits; they do not remove the effect |

Triggers to act: sustained quota use above 80%, P0 p99 wait above its SLO, provider 429 rate above 1%.

### 16.3 Cut list (stated honestly in the README)
- Secret and PII redaction, retention and encryption in the ledger and event log.
- Sandbox image supply chain, patching and cold-start measurement.
- Authentication of human replies; signing and review of agent definitions.
- Multi-region and DR; multi-key routing; streaming responses; real provider integration.
- Cross-run memory sharing between agents.
- Stretch items if unbuilt: AIMD, leader failover, live soak test.
- The "exact request sent to the LLM" inspection record (we logged the reserved cost and classes, not payloads, to avoid storing prompts).

---

## 17. Assumptions and open questions

### 17.1 Assumptions (change here first, then in DESIGN.docx)
1. About one LLM call per tool call; ~3k input and ~400 output tokens per call; peak ~10k LLM requests/min.
2. One provider key; its RPM/TPM are fixed and unknown, assumed below peak demand.
3. Provider windows are rolling 60 s; counting mode is either at start or at completion (both modelled).
4. The platform can tell whether a human is waiting on a run (the run record says so).
5. Prices are illustrative placeholders, not a real price list.
6. A tenant's fairness weight is a configured integer from its plan.

### 17.2 Open questions for the owner
1. Confirm the dropped 15% P0 reserve in favour of per-tenant allowance charged to the tenant's deficit.
2. Confirm "wait, never skip" even though it can leave a little quota idle.
3. Confirm Python with the standard library only, and removing the generated `main.py` stub.
4. Confirm the scope split (core vs stretch) in 6.14.
5. Should the ledger store request payloads (privacy cost) or only metadata? Default: metadata only.
6. Is the doc wording "token buckets" to be changed to "window accountant" in DESIGN.docx Section 7? (Recommended: yes, for consistency with D9.)

---

## 18. Appendices

### 18.1 Default configuration (main scenario)

```python
ProviderLimits(rpm=600, tpm=400_000, window=60.0)
SchedulerConfig(
    headroom=0.9, input_safety=1.15, base_quantum=4_000,
    max_queue_len=200, max_queue_tokens=1_000_000,
    age_p2_to_p1=30.0,
    p0_rate_tokens_per_s=300, p0_burst_tokens=8_000,
    run_token_cap=2_000_000,
)
TenantConfig("A", weight=1, daily_token_budget=200_000_000, daily_cost_budget=...)
TenantConfig("B", weight=1, ...)
TenantConfig("C", weight=1, ...)
PriceTable(input_per_1k=0.003, output_per_1k=0.015)   # illustrative only
```

### 18.2 Worked example of fairness (why DRR bounds the quiet tenant's wait)

Capacity 6,000 tokens/s, three tenants with quantum 4,000. Tenant A has 500 queued 3,000-token requests. Tenant B submits one 3,000-token request.
- Round: A gets 4,000 of deficit (one request, 1,000 carried), B gets 4,000, C idle (removed from `active`).
- B is served within one round: at most `(4,000 + 4,000) / 6,000 = 1.33 s` plus its own transmission, independent of A's 500-deep queue.
- Under plain FIFO B would wait behind 1.5M tokens = 250 s. That gap is what T1 asserts.

### 18.3 Worked example of the quota math (why 90% and the safety multiplier)

TPM 400,000. Usable 360,000 tokens/min, so the headroom is 400,000 - 360,000 = 40,000 tokens.

A request estimated at 2,000 input tokens with `max_output = 1,000` reserves `ceil(2,000 * 1.15) + 1,000 = 3,300`. If the provider counts 2,800 input tokens
(estimate biased low by 29%) and the model emits 600 output tokens, actual cost is 3,400.

- **Once the call completes**, reconcile replaces the 3,300 entry with the actual 3,400, so the window is exact again. Estimate error never accumulates in the ledger.
- **While in flight**, the window under-counts by at most 100 tokens for this request. The aggregate exposure is `in_flight_tokens * error_fraction`.
  At 6,000 tokens/s and ~3 s latency, ~18,000 tokens are in flight, so even a 40% error is ~7,200 tokens, well inside the 40,000 headroom.
- **The boundary** appears when latency is long: at ~30 s latency ~180,000 tokens are in flight, and a 28% under-count is ~50,000 tokens, which exceeds the headroom.
  The provider can then answer 429, and the cooldown path engages. That is the documented boundary (T4b), and it is why the headroom is a configured number tied to expected latency.

### 18.4 Glossary
- **RPM / TPM:** requests per minute / tokens per minute limits on an API key.
- **DRR:** deficit round robin; a fair scheduler that gives each backlogged queue a token quantum per round.
- **Work conserving:** capacity never sits idle while any tenant has work.
- **Reserve and reconcile:** hold an estimated cost at dispatch, replace it with the actual after completion.
- **Oracle:** an independent source of truth used by tests (here, the fake provider's own observations).
- **Negative control:** a deliberately broken variant that must fail the same test, proving the test can detect the failure.
- **Head-of-line blocking:** a large request at the front delays everything behind it.
- **AIMD:** additive increase, multiplicative decrease; the congestion control rule used to adapt to a provider that pushes back.
