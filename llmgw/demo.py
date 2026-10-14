"""Runs the demonstrations: each act compares the real scheduler with a control that lacks one mechanism."""

import io
import sys
import time
from collections.abc import Callable, Sequence

from llmgw.config import PriceTable, ProviderLimits
from llmgw.events import JsonLinesSink
from llmgw.ledger import Ledger
from llmgw.scheduler import Scheduler
from llmgw.sim.controls import NoCooldownScheduler, NoQuotaScheduler, fifo_scheduler
from llmgw.sim.report import Report, percentile
from llmgw.sim.scenario import MEAN_TOKENS_PER_REQUEST, Scenario, rate_for
from llmgw.sim.workload import TenantProfile

LIMITS = ProviderLimits(rpm=600, tpm=400_000)
PRICES = PriceTable()
OUTAGE = (200.0, 220.0)
RECOVERED = (280.0, 340.0)
QUIET_TARGET = 0.95

NOISY = (
    TenantProfile("a", rate=rate_for(1_200_000)),
    TenantProfile("b", rate=rate_for(60_000)),
    TenantProfile("c", rate=rate_for(60_000)),
)
TWO_TENANTS = NOISY[:2]


def _mark(ok: bool) -> str:
    return "[ok]  " if ok else "[FAIL]"


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    columns = list(zip(headers, *rows))
    widths = [max(len(str(cell)) for cell in column) for column in columns]

    def line(cells: Sequence[object]) -> str:
        return "  ".join(
            str(cell).ljust(width) if index == 0 else str(cell).rjust(width)
            for index, (cell, width) in enumerate(zip(cells, widths))
        )

    return "\n".join([line(headers), "  ".join("-" * w for w in widths), *(line(r) for r in rows)])


def _offered(profile: TenantProfile) -> float:
    return profile.rate * 60 * MEAN_TOKENS_PER_REQUEST


def _delivered(report: Report, tenant: str, start: float, end: float) -> float:
    return report.goodput_per_minute(tenant, start, end)


def _p99_wait(report: Report, tenant: str, start: float, end: float) -> float | None:
    waits = report.waits(tenant, start, end)
    return percentile(waits, 99) if waits else None


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def act_fairness() -> None:
    print("Act 1: a noisy tenant cannot starve the others")
    print("  Tenant a offers 10x its fair share of the provider; b and c offer far less than theirs.")
    print("  Compared with one global first-in-first-out line (no per-tenant fairness).\n")
    scenario = Scenario(LIMITS, NOISY)
    start, end = scenario.measure
    fair, fifo = scenario.run(), scenario.run(fifo_scheduler)

    rows, quiet_fair, quiet_fifo = [], [], []
    for profile in NOISY:
        name, offered = profile.tenant, _offered(profile)
        fair_served, fifo_served = fair.completion_ratio(name, start, end), fifo.completion_ratio(name, start, end)
        fair_wait, fifo_wait = _p99_wait(fair, name, start, end), _p99_wait(fifo, name, start, end)
        rows.append([
            name, f"{offered:,.0f}",
            f"{_delivered(fair, name, start, end):,.0f}", f"{fair_served:.0%}", _seconds(fair_wait),
            f"{_delivered(fifo, name, start, end):,.0f}", f"{fifo_served:.0%}", _seconds(fifo_wait),
        ])
        if name != "a":
            quiet_fair.append((fair_served, fair_wait))
            quiet_fifo.append((fifo_served, fifo_wait))
    print(_table(
        ["tenant", "offered tok/min", "fair: delivered", "requests served", "p99 wait s",
         "FIFO: delivered", "requests served", "p99 wait s"],
        rows,
    ))

    served_ok = all(share >= QUIET_TARGET for share, _ in quiet_fair)
    slower_ok = all(f is not None and g is not None and g > f for (_, f), (_, g) in zip(quiet_fair, quiet_fifo))
    print(f"\n  {_mark(served_ok)} with fair scheduling at least {QUIET_TARGET:.0%} of the quiet tenants' requests are served")
    print(f"  {_mark(slower_ok)} the FIFO control makes them wait far longer for the same traffic\n")


def act_safety() -> None:
    print("Act 2: the provider is never pushed over its limit")
    print("  The provider enforces 600 requests and 400,000 tokens per minute and refuses calls above that.")
    print("  Compared with a scheduler that ignores the rate window.\n")
    scenario = Scenario(LIMITS, TWO_TENANTS, duration=300.0, drain=60.0)
    start, end = scenario.measure
    real, naive = scenario.run(), scenario.run(NoQuotaScheduler)

    def row(label: str, report: Report) -> list[object]:
        delivered = sum(_delivered(report, p.tenant, start, end) for p in TWO_TENANTS)
        return [label, report.provider.throttled, f"{report.provider.peak_tokens:,}", f"{delivered:,.0f}"]

    print(_table(
        ["scheduler", "calls refused", "peak tokens in 60 s", "delivered tok/min"],
        [row("window + pacing (real)", real), row("no quota check (control)", naive)],
    ))
    print(f"\n  {_mark(real.provider.throttled == 0)} the real scheduler never gives the provider a call it has to refuse")
    print(f"  {_mark(naive.provider.throttled > 0)} the control does\n")


def act_outage() -> None:
    print("Act 3: a provider outage is absorbed, not hammered")
    print("  The provider refuses every call for 20 seconds (t=200 to t=220), then recovers.")
    print("  Compared with a scheduler that ignores the provider's retry-after hint.\n")
    scenario = Scenario(LIMITS, TWO_TENANTS, outage=OUTAGE, duration=300.0, drain=150.0)
    baseline = Scenario(LIMITS, TWO_TENANTS, duration=300.0, drain=150.0).run()
    real, hammer = scenario.run(), scenario.run(NoCooldownScheduler)

    def row(label: str, report: Report) -> list[object]:
        recovered = _delivered(report, "a", *RECOVERED)
        return [label, report.provider.throttled, f"{recovered:,.0f}"]

    normal = _delivered(baseline, "a", *RECOVERED)
    print(_table(
        ["scheduler", "calls refused", "delivered after recovery tok/min"],
        [row("honours retry-after (real)", real), row("ignores it (control)", hammer)],
    ))
    print(f"  (without any outage the same window delivers {normal:,.0f} tok/min)")
    print(f"\n  {_mark(real.provider.throttled < hammer.provider.throttled)} the real scheduler pauses and sends far fewer doomed calls")
    print(f"  {_mark(_delivered(real, 'a', *RECOVERED) >= 0.9 * normal)} throughput recovers to at least 90% of normal after the outage\n")


def act_accounting() -> None:
    print("Act 4: the ledger answers 'what has this tenant cost so far', exactly")
    print("  Every request is billed from the usage the provider reports, in whole micro-dollars.\n")
    ledger, buffer = Ledger(), io.StringIO()
    scenario = Scenario(LIMITS, TWO_TENANTS, duration=300.0, drain=150.0)
    report = scenario.run(lambda config: Scheduler(config, events=JsonLinesSink(buffer), ledger=ledger))

    provider = report.provider
    provider_cost = provider.total_input_tokens * PRICES.input_micros + provider.total_output_tokens * PRICES.output_micros
    total = ledger.total()
    print(_table(
        ["tenant", "requests recorded", "tokens", "cost"],
        [[t, s.requests, f"{s.tokens:,}", f"${s.cost / 1e6:,.2f}"] for t, s in sorted(ledger.by("tenant").items())],
    ))
    print(f"\n  provider reports  {provider.total_tokens:>12,} tokens  ${provider_cost / 1e6:,.2f}")
    print(f"  ledger records    {total.tokens:>12,} tokens  ${total.cost / 1e6:,.2f}")
    print(f"  {_mark(total.tokens == provider.total_tokens and total.cost == provider_cost)} the two agree exactly\n")

    events = buffer.getvalue().splitlines()
    sample = events[:3] + [e for e in events if '"llm.rejected"' in e][:1]
    print("  what the log pipeline receives (one JSON object per line):")
    for line in sample:
        print(f"    {line}")
    print()


ACTS: dict[str, Callable[[], None]] = {
    "fairness": act_fairness,
    "safety": act_safety,
    "outage": act_outage,
    "accounting": act_accounting,
}


def main(argv: Sequence[str] = ()) -> int:
    unknown = [name for name in argv if name not in ACTS]
    if unknown:
        print(f"unknown act {unknown[0]!r}; choose from: {', '.join(ACTS)}", file=sys.stderr)
        return 2

    print("LLM gateway proof of concept")
    print("  A fake, deterministic provider limits 600 requests and 400,000 tokens per minute; the scheduler may use 90%.")
    print("  Time is simulated, so ten minutes of traffic runs in a couple of seconds.\n")
    for name in argv or ACTS:
        started = time.perf_counter()
        ACTS[name]()
        print(f"  ({name} took {time.perf_counter() - started:.1f}s)\n")
    return 0
