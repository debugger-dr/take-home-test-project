from collections import defaultdict

from llmgw.config import ProviderLimits
from llmgw.sim.scenario import Scenario, rate_for
from llmgw.sim.workload import TenantProfile
from llmgw.types import Reason

LIMITS = ProviderLimits(rpm=600, tpm=400_000)
TENANT_BUDGET = 1_000_000
RUN_CAP = 150_000


def spent(report, tenant: str) -> int:
    return sum(r.tokens for r in report.records.values() if r.tenant == tenant)


def tenant_scenario(budgets: dict[str, int]) -> Scenario:
    profiles = (
        TenantProfile("a", rate=rate_for(1_200_000)),
        TenantProfile("b", rate=rate_for(60_000)),
    )
    return Scenario(LIMITS, profiles, budgets=budgets)


def test_tenant_budget_is_never_exceeded_and_other_tenants_are_unaffected():
    scenario = tenant_scenario({"a": TENANT_BUDGET})
    report = scenario.run()
    start, end = scenario.measure

    assert 0.9 * TENANT_BUDGET <= spent(report, "a") <= TENANT_BUDGET
    outcomes = {r.failed or r.rejected for r in report.records.values() if r.tenant == "a"}
    assert Reason.TENANT_BUDGET_EXHAUSTED in outcomes
    assert report.completion_ratio("b", start, end) >= 0.95


def test_control_without_a_budget_overspends():
    assert spent(tenant_scenario({}).run(), "a") > TENANT_BUDGET


def run_loop(settings: dict) -> dict[str, int]:
    """One tenant whose five runs all loop on the provider as fast as they are allowed to."""
    scenario = Scenario(LIMITS, (TenantProfile("a", rate=rate_for(1_200_000)),), settings=settings)
    per_run: dict[str, int] = defaultdict(int)
    for record in scenario.run().records.values():
        per_run[record.run] += record.tokens
    return per_run


def test_run_cap_stops_a_looping_run_at_its_cap():
    per_run = run_loop({"run_token_cap": RUN_CAP})
    assert len(per_run) == 5
    assert all(0.85 * RUN_CAP <= tokens <= RUN_CAP for tokens in per_run.values())


def test_control_without_a_run_cap_lets_a_run_loop_past_it():
    assert max(run_loop({}).values()) > RUN_CAP
