import pytest

from llmgw.config import ProviderLimits
from llmgw.sim.controls import NoQuotaScheduler
from llmgw.sim.provider import CountingMode, WindowMode
from llmgw.sim.scenario import Scenario, rate_for
from llmgw.sim.workload import SizeRange, TenantProfile

LIMITS = ProviderLimits(rpm=600, tpm=400_000)

TOKEN_BOUND = (
    TenantProfile("a", rate=rate_for(1_200_000)),
    TenantProfile("b", rate=rate_for(60_000)),
)
REQUEST_BOUND = (
    TenantProfile(
        "a", rate=20.0, est_input=SizeRange(100, 200), max_output=SizeRange(20, 60)
    ),
)


def scenario(profiles, counting=CountingMode.RESERVE_AT_START, window_mode=WindowMode.ROLLING) -> Scenario:
    return Scenario(LIMITS, profiles, counting=counting, window_mode=window_mode, duration=300.0)


@pytest.mark.parametrize("counting", list(CountingMode))
@pytest.mark.parametrize("window_mode", list(WindowMode))
def test_provider_is_never_pushed_over_its_token_limit(counting, window_mode):
    stats = scenario(TOKEN_BOUND, counting, window_mode).run().provider
    assert stats.throttle_rate <= 0.01
    assert stats.peak_tokens <= LIMITS.tpm
    assert stats.peak_requests <= LIMITS.rpm


@pytest.mark.parametrize("window_mode", list(WindowMode))
def test_control_without_quota_check_is_throttled(window_mode):
    sc = scenario(TOKEN_BOUND, window_mode=window_mode)
    assert sc.run(NoQuotaScheduler).provider.throttle_rate > 0.10


@pytest.mark.parametrize("window_mode", list(WindowMode))
def test_provider_is_never_pushed_over_its_request_limit(window_mode):
    sc = scenario(REQUEST_BOUND, window_mode=window_mode)
    report = sc.run()
    start, end = sc.measure
    assert report.provider.throttle_rate <= 0.01
    assert report.provider.peak_requests <= LIMITS.rpm
    assert report.requests_per_minute("a", start, end) >= 0.9 * int(LIMITS.rpm * 0.9)


@pytest.mark.parametrize("window_mode", list(WindowMode))
def test_request_bound_control_without_quota_check_is_throttled(window_mode):
    sc = scenario(REQUEST_BOUND, window_mode=window_mode)
    assert sc.run(NoQuotaScheduler).provider.throttle_rate > 0.10
