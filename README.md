# LLM rate-limit, fairness and cost layer

Proof of concept for the Staff Engineer take-home: **Agentic Orchestration Framework**.

One vertical slice of the design is built and tested: the gateway that sits between ~1000 concurrent agents and a single rate-limited LLM API key. It decides whose request goes next, keeps the provider inside its limits, protects tenants from each other, and accounts for every token and dollar.

## Run it

One command installs everything, runs the tests, and runs the demo:

```
make
```

Run only the demo, or a single act of it:

```
make demo
make demo ARGS=safety        # fairness | safety | outage | accounting
make test
```

Requirements: macOS or Linux, and either [`uv`](https://docs.astral.sh/uv/) (recommended: it fetches Python 3.12 and the one test dependency for you) or Python 3.12+ with `venv`. There are no runtime dependencies (standard library only), nothing is downloaded at run time, and no network access is needed. The whole thing takes well under a minute.

## What was asked for, and where it is

| Asked for | Where |
|---|---|
| Architecture proposal, at most 4 pages plus diagrams | `docs/DESIGN.docx`, diagrams in `docs/system-diagram.mmd` and `docs/limiter-diagram.mmd` |
| A proof of concept that runs locally with one command | `make` (installs, tests, demo). `make demo` for the demo alone |
| One vertical slice, with the justification graded as hard as the code | Slice: the LLM rate-limit, fairness and cost layer. Justification: "Why this slice" below |
| At least one test showing the safety or scale property claimed, not only the happy path | Every scenario test has a negative control that must fail; see "How the claims are tested" |
| A short AI usage log: where I overrode the AI, where it beat me, what I did without it | `AI_LOG.md` |
| Position taken on each trade-off, with the condition that would reverse it | `docs/DESIGN.docx` section 3 and 9; full decision log in `docs/plan.md` |
| Honesty about limits and what was cut | "Known gaps and what I cut" below |
| One repo: design, proof of concept, README, AI log; no proprietary code | This repository |

## What you will see

`make demo` runs four short acts. Each compares the real scheduler with a control that lacks exactly one mechanism, and prints `[ok]` or `[FAIL]` computed from the live numbers. Act 1, as run:

```
tenant  offered tok/min  fair: delivered  requests served  p99 wait s  FIFO: delivered  requests served  p99 wait s
a             1,200,000          233,468              21%       120.0          317,049              27%       120.0
b                60,000           60,687             100%         5.2           21,821              34%       120.0
c                60,000           59,489             100%         6.3           13,713              26%       120.0
```

| Act | Claim | Control |
|---|---|---|
| 1. Fairness | A tenant offering 10x its share cannot starve quiet tenants | One global first-in-first-out line |
| 2. Safety | The provider is never given a call it has to refuse | A scheduler that ignores the rate window |
| 3. Outage | A 20 s provider outage is absorbed and throughput recovers | A scheduler that ignores the retry-after hint |
| 4. Accounting | The ledger's tokens and dollars equal the provider's exactly | |

## Why this slice

*Risk retired.* With ~10k tool calls a minute and about one LLM call per tool call, the platform sends ~170 requests a second (~34M tokens a minute) through **one API key**. Kubernetes, Postgres and the gateways scale by adding replicas; the provider's quota does not. It is also the largest cost line and the only place every tenant contends, so it is where multi-tenant fairness is real. The part I was least sure of is correctness under imperfect information: a call's cost is unknown until it finishes, the provider's window semantics are unknown, and a fair scheduler with priorities still has to bound a quiet tenant's wait. I did not pick the sandbox, the other risky area, because gVisor does not run on macOS, so a local demo would have been a stand-in. The choice was for impact, not familiarity.

*What I learned building it.*
- A sliding-window accountant keeps you **safe but not smooth**. Greedy dispatch fills the window in one burst and then stalls until that burst expires a window later, over and over. The scenario test showed it: waits were bimodal, mostly under 1 s with some at 25 to 37 s. Fix: pace dispatches (generic cell rate algorithm) in front of the window.
- "A quiet tenant waits at most one round" is only true for a request arriving to an **empty own queue**. Behind its own earlier requests it queues like any M/G/1 system. My first bound ignored that and was wrong.
- A negative control must switch off exactly the mechanism under test. Once queue bounds and wait estimates existed, a plain FIFO was no longer unfair, so the FIFO control had to drop them too. Forgetting an unknown-outcome call in the window alone did not break safety because the pacer independently held the rate down: two layers back each other up.
- A bounded queue can let low-priority work crowd out high-priority work (see known gaps).

*What I would build second.* The sandboxed executor (gVisor on a Linux node) with measured cold start and an escape-attempt test, then the tool gateway.

## What is real and what is faked

| Real (and tested) | Faked or simplified |
|---|---|
| Admission, per-tenant queues, deficit round robin in tokens, wait-not-skip | The LLM provider: a deterministic fake that enforces limits and reports usage |
| Sliding-window quota accounting, reserve and reconcile | Agents: seeded arrival processes |
| Dispatch pacing | Postgres: SQLite for the ledger |
| P0 lane with per-tenant allowance, P1/P2 with aging | Kubernetes: not used (it adds nothing to the property shown) |
| Backpressure, deadline expiry, cancel, retry hints | Time: virtual, so ten minutes of traffic runs in seconds |
| 429 cooldown, unknown-outcome handling | Run identity token: plain fields, not verified |
| Token and run budgets with one alert | Prices: illustrative integers, not a price list |
| Exact ledger, JSON event stream, snapshot | |

The fake provider is an independent oracle: it never imports the scheduler, and every safety assertion reads *its* observations, not the scheduler's own counters. It models two counting modes (at start, at completion) and two window modes (rolling, fixed); the tests run both.

## How the claims are tested

`make test` runs about 230 tests, in seconds because time is simulated. Every scenario test has a **negative control**: a deliberately broken variant that must fail the same assertion, so no test is vacuous.

| Property | Test file | Control that must fail |
|---|---|---|
| Quiet tenants are not starved; weights honoured; above-share tenant gets its max-min share | `scenarios/test_noisy_neighbour.py` | FIFO |
| Dispatch has no stop-and-go cycles | `scenarios/test_noisy_neighbour.py` | no pacing |
| Provider never refuses a call (both limits, both window modes) | `scenarios/test_quota_safety.py` | no quota check |
| Human-blocking requests jump the queue; flooding P0 gains nothing | `scenarios/test_priority.py` | no allowance |
| Batch requests are not starved | `scenarios/test_aging.py` | no aging |
| A large request is served, not skipped | `scenarios/test_large_request.py` | cheapest-first queue |
| Bounded queue, exactly one outcome per request | `scenarios/test_backpressure.py` | unbounded queue |
| Unknown outcomes are safe; outages absorbed | `scenarios/test_provider_faults.py` | forget-on-error, no cooldown |
| Budgets and run caps hold | `scenarios/test_budget_enforcement.py` | no budget |
| Ledger equals provider usage exactly | `scenarios/test_accounting.py` | bills reservations |
| The guarantee's documented boundary | `scenarios/test_estimate_boundary.py` | |
| Same seed, same run | `scenarios/test_determinism.py` | |

Thresholds are derived from formulas (for example the wait bound is `(ceil(cost/quantum)+1) * round_max + max_cost/rate`), written next to the test, not tuned until green. Where a derivation was wrong the formula was corrected; that history is in `docs/plan.md`.

## Decisions I took a position on

Each has the rejected alternative and a reversal condition in `docs/plan.md` section 4.

- An agent is a durable record, not a pod; stateless workers pull and hold a lease.
- Postgres plus a queue, not a workflow engine.
- One logical dispatcher (leader plus standby), not distributed counters: global fairness needs a global view and the quota, not CPU, is the limit.
- Fairness in **tokens**, deficit round robin; the head request waits and is never skipped.
- Priority is derived by the platform from run state, never set by the caller; P0 has a per-tenant allowance and is charged to the tenant's fairness account.
- A sliding-window accountant that mirrors the provider, plus pacing, rather than a token bucket.

## Known gaps and what I cut

- **Live service not built.** The plan had a thin HTTP wrapper and fake provider server as a live smoke test; I cut it. The proof is the deterministic suite. Virtual time is exact but cannot reveal real concurrency or timing bugs.
- **Designed, not built (stretch):** adaptive rate control (AIMD) and dispatcher failover (new leader pauses one window). A reactive cooldown on 429 is built.
- **Everything outside the slice** is design only: tool gateway, sandbox, scheduler/timers, worker pool, secret store. See `docs/DESIGN.docx`.
- **Queue-bound priority inversion:** with a per-tenant bound, waiting P2 requests can fill the queue and cause P1 arrivals to be rejected. A fix is per-class bounds or evicting the lowest class.
- **Window semantics are an assumption.** Real providers differ in when they count tokens and how windows align. Reservations are conservative under both modelled modes; this is a modelling choice, not a fact about any vendor.
- **The safety guarantee has a boundary:** a slow provider combined with badly low input estimates can briefly push usage past the headroom. The cooldown contains it (`test_estimate_boundary.py`); exact token counting would remove it.
- **Not addressed:** secret and PII redaction in the ledger and event log, sandbox image supply chain, authentication of human replies, definition signing, multi-region, streaming responses, multi-key routing.
- **Time:** the work ran past the 8-hour budget, which is why the live wrapper and stretch items were cut rather than finished.

## Repository

```
README.md           this file
AI_LOG.md           how AI was used: overrides, where it was better, what I did by hand
docs/DESIGN.docx    architecture proposal (system diagram, lifecycle, tool path, sandbox, scale, failures)
docs/plan.md        implementation plan: decision log, issue catalogue, test plan
docs/*.mmd          diagrams (Mermaid): system and limiter
llmgw/              the slice (scheduler, fair queue, window, pacer, P0 lane, budgets, ledger, events)
llmgw/sim/          deterministic simulator, fake provider, workload, negative controls
tests/              unit tests and scenario tests
Makefile            make / make demo / make test
```

No proprietary or employer code is included. No secrets or real provider keys exist anywhere in the repo.
