import math

import pytest

from llmgw.config import ProviderLimits
from llmgw.sim.controls import no_p0_allowance_scheduler
from llmgw.sim.fairness import max_min_fair_share
from llmgw.sim.report import percentile
from llmgw.sim.scenario import Scenario, rate_for
from llmgw.sim.workload import TenantProfile
from llmgw.types import Klass

LIMITS = ProviderLimits(rpm=600, tpm=400_000)
USABLE_PER_MINUTE = 360_000
RATE = USABLE_PER_MINUTE / 60
MAX_COST = math.ceil(4_000 * 1.15) + 1_024
TENANTS = 3
# P0 requests of every tenant share one round-robin lane, so a request can wait for one dispatch per tenant.
# Quota room arrives in chunks of at most one maximal request, so each dispatch needs up to two of them.
P0_BOUND = 2 * TENANTS * MAX_COST / RATE


def make_scenario(flood: bool) -> Scenario:
    profiles = (
        TenantProfile("a", rate=rate_for(1_200_000), human_waiting_fraction=1.0 if flood else 0.0),
        TenantProfile("b", rate=rate_for(60_000), human_waiting_fraction=0.2),
        TenantProfile("c", rate=rate_for(60_000), human_waiting_fraction=0.2),
    )
    return Scenario(LIMITS, profiles, settings={"p0_rate": 600.0})


@pytest.fixture(scope="module")
def calm() -> Scenario:
    return make_scenario(flood=False)


@pytest.fixture(scope="module")
def flood() -> Scenario:
    return make_scenario(flood=True)


def test_human_blocking_requests_jump_the_queue(calm):
    report = calm.run()
    start, end = calm.measure
    for tenant in ("b", "c"):
        urgent = report.waits(tenant, start, end, Klass.P0)
        normal = report.waits(tenant, start, end, Klass.P1)
        assert len(urgent) >= 10
        assert percentile(urgent, 99) <= P0_BOUND
        assert percentile(urgent, 50) < percentile(normal, 50)


def test_flooding_p0_gains_nothing_and_does_not_hurt_other_tenants(flood):
    report = flood.run()
    start, end = flood.measure
    offered = {"a": 1_200_000, "b": 60_000, "c": 60_000}
    fair = max_min_fair_share(USABLE_PER_MINUTE, offered, {t: 1 for t in offered})

    assert report.goodput_per_minute("a", start, end) <= 1.1 * fair["a"]
    for tenant in ("b", "c"):
        assert report.completion_ratio(tenant, start, end) >= 0.95
        assert percentile(report.waits(tenant, start, end, Klass.P0), 99) <= P0_BOUND


def test_control_without_an_allowance_starves_other_tenants(flood):
    report = flood.run(no_p0_allowance_scheduler)
    start, end = flood.measure
    assert report.completion_ratio("b", start, end) < 0.5
