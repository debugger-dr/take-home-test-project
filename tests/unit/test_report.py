import pytest

from llmgw.sim.provider import ProviderStats
from llmgw.sim.report import Report, RequestRecord, percentile
from llmgw.types import Klass

STATS = ProviderStats(0, 0, 0, 0, 0, 0)


def record(tenant="a", submitted=0.0, dispatched=None, completed=None, tokens=0) -> RequestRecord:
    return RequestRecord(tenant, Klass.P1, submitted, dispatched, completed, tokens)


def test_percentile_uses_nearest_rank():
    values = list(range(1, 101))
    assert percentile(values, 50) == 50
    assert percentile(values, 99) == 99
    assert percentile(values, 100) == 100
    assert percentile([7], 99) == 7


def test_percentile_of_nothing_is_an_error():
    with pytest.raises(ValueError):
        percentile([], 99)


def test_waits_count_undispatched_requests_up_to_the_horizon():
    report = Report({"x": record(submitted=2, dispatched=5), "y": record(submitted=4)}, STATS, horizon=10)
    assert sorted(report.waits("a", 0, 100)) == [3, 6]


def test_idle_waits_exclude_requests_that_queued_behind_their_own_tenant():
    records = {
        "r1": record(submitted=0, dispatched=2),
        "r2": record(submitted=1, dispatched=5),
        "r3": record(submitted=6, dispatched=8),
        "r4": record(submitted=7),
        "r5": record(submitted=9),
        "other": record(tenant="b", submitted=1, dispatched=50),
    }
    report = Report(records, STATS, horizon=100)
    assert report.waits_when_idle("a", 0, 100) == [2, 2]


def test_max_dispatch_gap_only_considers_dispatches_inside_the_window():
    times = [1, 2, 5, 6, 40]
    records = {str(i): record(submitted=0, dispatched=t) for i, t in enumerate(times)}
    assert Report(records, STATS, horizon=100).max_dispatch_gap(0, 10) == 3


def test_windows_and_tenants_filter_by_submission_time():
    records = {
        "early": record(submitted=1, dispatched=2),
        "inside": record(submitted=10, dispatched=13),
        "other": record(tenant="b", submitted=10, dispatched=11),
    }
    report = Report(records, STATS, horizon=100)
    assert report.waits("a", 5, 20) == [3]


def test_completion_ratio_counts_completed_over_submitted():
    records = {
        "1": record(submitted=1, completed=5),
        "2": record(submitted=2, completed=6),
        "3": record(submitted=3),
        "4": record(submitted=4),
    }
    assert Report(records, STATS, horizon=100).completion_ratio("a", 0, 10) == 0.5


def test_goodput_and_request_rate_are_per_minute_over_the_completion_window():
    records = {
        "1": record(completed=10, tokens=600),
        "2": record(completed=20, tokens=600),
        "late": record(completed=50, tokens=999),
    }
    report = Report(records, STATS, horizon=100)
    assert report.goodput_per_minute("a", 0, 30) == 2_400
    assert report.requests_per_minute("a", 0, 30) == 4
