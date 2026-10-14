import pytest

from llmgw.pacer import Pacer


def test_first_dispatch_is_allowed_and_the_next_waits_for_its_cost():
    pacer = Pacer(rate=100, tolerance=0)
    assert pacer.allows(0)
    pacer.commit(200, now=0)
    assert not pacer.allows(1.9)
    assert pacer.allows(2.0)
    assert pacer.ready_at(0) == 2.0


def test_tolerance_permits_a_small_burst():
    pacer = Pacer(rate=100, tolerance=1.0)
    pacer.commit(200, now=0)
    assert not pacer.allows(0.9)
    assert pacer.allows(1.0)


def test_idle_time_is_not_banked():
    pacer = Pacer(rate=100, tolerance=0)
    pacer.commit(100, now=0)
    pacer.commit(100, now=100)
    assert pacer.ready_at(100) == 101.0


def test_refund_returns_unused_capacity():
    pacer = Pacer(rate=100, tolerance=0)
    pacer.commit(200, now=0)
    pacer.refund(100)
    assert pacer.ready_at(0) == 1.0


def test_long_run_rate_never_exceeds_the_configured_rate():
    pacer = Pacer(rate=100, tolerance=0)
    now = 0.0
    for _ in range(10):
        now = pacer.ready_at(now)
        pacer.commit(100, now)
    assert now == pytest.approx(9.0)
    assert pacer.ready_at(now) == pytest.approx(10.0)


@pytest.mark.parametrize("rate,tolerance", [(0, 0), (-1, 0), (1, -0.1)])
def test_invalid_construction_is_rejected(rate, tolerance):
    with pytest.raises(ValueError):
        Pacer(rate=rate, tolerance=tolerance)
