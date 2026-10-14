import random

import pytest

from llmgw.config import ProviderLimits
from llmgw.sim.provider import (
    CountingMode,
    FakeProvider,
    Faulted,
    ProviderCall,
    ProviderModel,
    Started,
    Throttled,
    WindowMode,
)

LIMITS = ProviderLimits(rpm=10, tpm=1_000, window=60.0)


def make_provider(limits=LIMITS, seed=0, exact=True, **overrides) -> FakeProvider:
    if exact:
        overrides.setdefault("input_ratio", (1.0, 1.0))
        overrides.setdefault("output_ratio", (1.0, 1.0))
    overrides.setdefault("base_latency", 1.0)
    overrides.setdefault("seconds_per_output_token", 0.0)
    return FakeProvider(ProviderModel(limits=limits, **overrides), random.Random(seed))


def call(call_id: str, est_input: int = 100, max_output: int = 100) -> ProviderCall:
    return ProviderCall(call_id, est_input, max_output)


def test_accepts_call_within_limits_and_reports_usage():
    result = make_provider().send(call("a"), now=5.0)
    assert isinstance(result, Started)
    assert result.finish_at == 6.0
    assert (result.usage.input_tokens, result.usage.output_tokens) == (100, 100)


def test_throttles_when_token_window_is_full():
    provider = make_provider()
    assert all(isinstance(provider.send(call(f"c{i}"), now=0), Started) for i in range(5))
    assert isinstance(provider.send(call("c5"), now=0), Throttled)


def test_throttles_when_request_limit_is_reached():
    provider = make_provider(limits=ProviderLimits(rpm=3, tpm=1_000_000))
    assert all(isinstance(provider.send(call(f"c{i}"), now=0), Started) for i in range(3))
    assert isinstance(provider.send(call("c3"), now=0), Throttled)


def test_retry_after_is_time_until_enough_window_frees():
    provider = make_provider()
    provider.send(call("a", 250, 250), now=0)
    provider.send(call("b", 250, 250), now=10)
    blocked = provider.send(call("c", 250, 250), now=20)
    assert blocked == Throttled(retry_after=40)
    assert isinstance(provider.send(call("c", 250, 250), now=60), Started)


def test_completion_corrects_reservation_to_actual_usage():
    provider = make_provider(output_ratio=(0.5, 0.5))
    for i in range(5):
        provider.send(call(f"c{i}"), now=0)
    assert isinstance(provider.send(call("late"), now=0), Throttled)
    for i in range(4):
        provider.finish(f"c{i}", now=1)
    assert isinstance(provider.send(call("late"), now=1), Started)


def test_count_at_completion_ignores_calls_still_in_flight():
    provider = make_provider(
        limits=ProviderLimits(rpm=100, tpm=1_000), counting=CountingMode.COUNT_AT_COMPLETION
    )
    assert all(isinstance(provider.send(call(f"c{i}"), now=0), Started) for i in range(10))
    for i in range(10):
        provider.finish(f"c{i}", now=1)
    assert provider.stats.peak_tokens == 2_000
    assert isinstance(provider.send(call("next"), now=2), Throttled)


def test_fixed_window_resets_at_boundary():
    provider = make_provider(window_mode=WindowMode.FIXED)
    for i in range(5):
        provider.send(call(f"c{i}"), now=59)
    assert provider.send(call("late"), now=59.5) == Throttled(retry_after=0.5)
    assert isinstance(provider.send(call("late"), now=60), Started)


def test_usage_stays_within_configured_ratios():
    provider = make_provider(
        limits=ProviderLimits(rpm=10_000, tpm=10_000_000), exact=False, seed=3
    )
    for i in range(200):
        result = provider.send(call(f"c{i}", est_input=1_000, max_output=500), now=float(i))
        assert 850 <= result.usage.input_tokens <= 1_150
        assert 1 <= result.usage.output_tokens <= 500


def test_same_seed_gives_same_usage_and_different_seed_differs():
    def usages(seed: int):
        provider = make_provider(
            limits=ProviderLimits(rpm=10_000, tpm=10_000_000), exact=False, seed=seed
        )
        return [provider.send(call(f"c{i}", 1_000, 500), now=float(i)).usage for i in range(20)]

    assert usages(1) == usages(1)
    assert usages(1) != usages(2)


def test_faulted_call_still_consumes_provider_capacity():
    provider = make_provider(fault_rate=1.0)
    result = provider.send(call("a", 250, 250), now=0)
    assert isinstance(result, Faulted)
    assert provider.stats.faulted == 1
    assert provider.stats.peak_tokens == 500
    with pytest.raises(KeyError):
        provider.finish("a", now=1)


@pytest.mark.parametrize("window_mode", list(WindowMode))
def test_resending_a_faulted_call_counts_both_attempts(window_mode):
    provider = make_provider(fault_rate=1.0, window_mode=window_mode)
    provider.send(call("a", 100, 100), now=0)
    provider.send(call("a", 100, 100), now=1)
    assert provider.stats.peak_tokens == 400
    assert isinstance(provider.send(call("b", 350, 350), now=2), Throttled)


def test_outage_throttles_everything_until_it_ends():
    provider = make_provider(outage=(10.0, 30.0))
    assert isinstance(provider.send(call("a"), now=5.0), Started)
    assert provider.send(call("b"), now=12.0) == Throttled(retry_after=18.0)
    assert isinstance(provider.send(call("b"), now=30.0), Started)
    assert provider.stats.throttled == 1


def test_finishing_an_unknown_call_raises():
    with pytest.raises(KeyError):
        make_provider().finish("missing", now=0)


def test_stats_track_counts_totals_and_peaks():
    provider = make_provider()
    for i in range(5):
        provider.send(call(f"c{i}"), now=0)
    provider.send(call("over"), now=0)
    provider.finish("c0", now=1)
    stats = provider.stats
    assert (stats.accepted, stats.throttled) == (5, 1)
    assert stats.throttle_rate == pytest.approx(1 / 6)
    assert stats.total_tokens == 200
    assert stats.peak_tokens == 1_000
    assert stats.peak_requests == 5
