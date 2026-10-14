import math

from llmgw.config import ProviderLimits
from llmgw.sim.controls import NoCooldownScheduler, ReleaseOnErrorScheduler
from llmgw.sim.report import Report
from llmgw.sim.scenario import Scenario, rate_for
from llmgw.sim.workload import TenantProfile

LIMITS = ProviderLimits(rpm=600, tpm=400_000)
PROFILES = (
    TenantProfile("a", rate=rate_for(1_200_000)),
    TenantProfile("b", rate=rate_for(60_000)),
)
OUTAGE = (200.0, 220.0)
RECOVERED = (OUTAGE[1] + 60.0, 340.0)

# One poll can send at most the pacing burst (5% of the window at the usable rate) before the first refusal comes back.
BURST_TOKENS = 0.05 * LIMITS.window * (0.9 * LIMITS.tpm / LIMITS.window)
MIN_COST = math.ceil(2_000 * 1.15) + 256
THROTTLES_PER_OUTAGE = math.ceil(BURST_TOKENS / MIN_COST) + 1


def scenario(**overrides) -> Scenario:
    return Scenario(LIMITS, PROFILES, duration=300.0, drain=150.0, **overrides)


def has_exactly_one_outcome(report: Report) -> bool:
    return all(
        (r.completed_at is not None) + (r.failed is not None) + (r.rejected is not None) == 1
        for r in report.records.values()
    )


def test_unknown_outcomes_never_push_the_provider_over_its_limit_and_lose_no_request():
    report = scenario(fault_rate=0.2).run()

    assert report.provider.faulted > 0
    assert report.provider.throttle_rate <= 0.01
    assert report.provider.peak_tokens <= LIMITS.tpm
    assert has_exactly_one_outcome(report)


def test_control_that_forgets_unknown_outcomes_overruns_the_provider():
    report = scenario(fault_rate=0.2).run(ReleaseOnErrorScheduler)
    assert report.provider.throttled > 0


def test_outage_is_absorbed_by_a_cooldown_and_throughput_recovers():
    report = scenario(outage=OUTAGE).run()
    baseline = scenario().run()

    assert report.provider.throttled <= THROTTLES_PER_OUTAGE
    assert has_exactly_one_outcome(report)
    assert report.goodput_per_minute("a", *RECOVERED) >= 0.9 * baseline.goodput_per_minute("a", *RECOVERED)


def test_control_without_a_cooldown_keeps_hammering_the_provider_during_the_outage():
    report = scenario(outage=OUTAGE).run(NoCooldownScheduler)
    assert report.provider.throttled > THROTTLES_PER_OUTAGE
