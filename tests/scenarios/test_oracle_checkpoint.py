import random

import pytest

from llmgw.config import ProviderLimits
from llmgw.sim.controls import PassThroughScheduler
from llmgw.sim.engine import Simulator
from llmgw.sim.provider import CountingMode, FakeProvider, ProviderModel, WindowMode
from llmgw.sim.report import Report
from llmgw.sim.workload import TenantProfile, Workload

LIMITS = ProviderLimits(rpm=600, tpm=400_000)


def run_pass_through(requests_per_minute: float, counting: CountingMode, window_mode=WindowMode.ROLLING) -> Report:
    profile = TenantProfile("a", rate=requests_per_minute / 60)
    requests = Workload([profile], seed=1).generate(duration=300)
    model = ProviderModel(limits=LIMITS, counting=counting, window_mode=window_mode)
    provider = FakeProvider(model, random.Random(2))
    return Simulator(PassThroughScheduler(), provider).run(requests, until=400)


@pytest.mark.parametrize("counting", list(CountingMode))
@pytest.mark.parametrize("window_mode", list(WindowMode))
def test_light_load_is_never_throttled(counting, window_mode):
    report = run_pass_through(20, counting, window_mode)
    assert report.provider.throttled == 0
    assert report.provider.peak_tokens < LIMITS.tpm


@pytest.mark.parametrize("window_mode", list(WindowMode))
def test_overload_without_quota_control_is_throttled(window_mode):
    report = run_pass_through(400, CountingMode.RESERVE_AT_START, window_mode)
    assert report.provider.throttle_rate > 0.10


@pytest.mark.parametrize("window_mode", list(WindowMode))
def test_overload_without_quota_control_overshoots_when_counting_at_completion(window_mode):
    report = run_pass_through(400, CountingMode.COUNT_AT_COMPLETION, window_mode)
    assert report.provider.peak_tokens > LIMITS.tpm
