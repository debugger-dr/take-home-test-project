import math

import pytest

from llmgw.config import ProviderLimits
from llmgw.sim.controls import fifo_scheduler, unpaced_scheduler
from llmgw.sim.fairness import max_min_fair_share
from llmgw.sim.report import Report, percentile
from llmgw.sim.scenario import Scenario, rate_for
from llmgw.sim.workload import TenantProfile

LIMITS = ProviderLimits(rpm=600, tpm=400_000)
USABLE_PER_MINUTE = 360_000
QUANTUM = 4_000
MAX_COST = math.ceil(4_000 * 1.15) + 1_024
RATE = USABLE_PER_MINUTE / 60
# Each tenant is served at most one quantum plus one maximal request per round; three tenants are active.
ROUND_MAX = 3 * (QUANTUM + MAX_COST) / RATE
WAIT_BOUND = (math.ceil(MAX_COST / QUANTUM) + 1) * ROUND_MAX + MAX_COST / RATE


def make_scenario(offered: dict[str, int], weights: dict[str, int] | None = None) -> Scenario:
    profiles = tuple(TenantProfile(t, rate=rate_for(tokens)) for t, tokens in offered.items())
    return Scenario(LIMITS, profiles, weights or {})


def goodputs(scenario: Scenario, report: Report) -> dict[str, float]:
    start, end = scenario.measure
    return {p.tenant: report.goodput_per_minute(p.tenant, start, end) for p in scenario.profiles}


def p99_wait(scenario: Scenario, report: Report, tenant: str) -> float:
    start, end = scenario.measure
    return percentile(report.waits(tenant, start, end), 99)


@pytest.fixture(scope="module")
def noisy() -> Scenario:
    return make_scenario({"a": 1_200_000, "b": 60_000, "c": 60_000})


def test_quiet_tenants_are_served_within_the_wait_bound(noisy):
    report = noisy.run()
    start, end = noisy.measure
    for tenant in ("b", "c"):
        idle_waits = report.waits_when_idle(tenant, start, end)
        assert len(idle_waits) >= 30
        assert report.completion_ratio(tenant, start, end) >= 0.95
        assert percentile(idle_waits, 99) <= WAIT_BOUND


def test_unused_share_flows_to_the_noisy_tenant(noisy):
    served = goodputs(noisy, noisy.run())
    assert sum(served.values()) >= 0.9 * USABLE_PER_MINUTE
    assert served["a"] >= 0.85 * (USABLE_PER_MINUTE - served["b"] - served["c"])


def test_saturated_provider_is_fed_smoothly_without_stop_and_go(noisy):
    start, end = noisy.measure
    assert noisy.run().max_dispatch_gap(start, end) <= 3 * MAX_COST / RATE
    assert noisy.run(unpaced_scheduler).max_dispatch_gap(start, end) > noisy.limits.window / 4


def test_fifo_control_makes_quiet_tenants_wait_behind_the_flood(noisy):
    report = noisy.run(fifo_scheduler)
    assert p99_wait(noisy, report, "b") > 10 * WAIT_BOUND


def test_tenant_above_its_share_receives_exactly_its_max_min_share():
    offered = {"a": 1_200_000, "b": 200_000, "c": 60_000}
    scenario = make_scenario(offered)
    fair = max_min_fair_share(USABLE_PER_MINUTE, offered, {t: 1 for t in offered})

    served = goodputs(scenario, scenario.run())
    assert 0.9 * fair["b"] <= served["b"] <= 1.15 * fair["b"]
    assert served["c"] >= 0.95 * fair["c"]

    assert goodputs(scenario, scenario.run(fifo_scheduler))["b"] < 0.9 * fair["b"]


def test_weights_split_a_saturated_provider_in_proportion():
    offered = {t: 1_200_000 for t in "abc"}
    scenario = make_scenario(offered, weights={"a": 1, "b": 2, "c": 1})

    served = goodputs(scenario, scenario.run())
    total = sum(served.values())
    assert served["a"] / total == pytest.approx(0.25, rel=0.05)
    assert served["b"] / total == pytest.approx(0.50, rel=0.05)
    assert served["c"] / total == pytest.approx(0.25, rel=0.05)

    control = goodputs(scenario, scenario.run(fifo_scheduler))
    assert control["b"] / sum(control.values()) < 0.40
