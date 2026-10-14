from llmgw.config import ProviderLimits
from llmgw.ledger import Ledger
from llmgw.scheduler import Scheduler
from llmgw.sim.report import Report
from llmgw.sim.scenario import Scenario, rate_for
from llmgw.sim.workload import TenantProfile

LIMITS = ProviderLimits(rpm=600, tpm=400_000)
PROFILES = (
    TenantProfile("a", rate=rate_for(1_200_000)),
    TenantProfile("b", rate=rate_for(60_000)),
)
LAST_QUARTER = (420.0, 540.0)

# With 30 s latency about half the window is in flight, so estimate errors are not corrected for a long time.
SLOW = {"base_latency": 30.0}
# The reservation allows 1.15x the estimate; here the provider counts 1.5x to 1.7x.
SLOW_AND_UNDERESTIMATED = {"base_latency": 30.0, "input_ratio": (1.5, 1.7)}


def run(provider: dict) -> tuple[Report, Ledger]:
    ledger = Ledger()
    scenario = Scenario(LIMITS, PROFILES, provider=provider, duration=600.0, drain=150.0)
    return scenario.run(lambda config: Scheduler(config, ledger=ledger)), ledger


def test_slow_provider_alone_is_safe():
    report, _ = run(SLOW)
    assert report.provider.throttled == 0


def test_the_guarantee_has_a_boundary_but_the_cooldown_contains_it():
    baseline, _ = run(SLOW)
    biased, ledger = run(SLOW_AND_UNDERESTIMATED)

    assert biased.provider.throttled > 0
    assert biased.provider.throttle_rate <= 0.05
    assert biased.goodput_per_minute("a", *LAST_QUARTER) >= 0.9 * baseline.goodput_per_minute("a", *LAST_QUARTER)
    assert ledger.total().tokens == biased.provider.total_tokens
