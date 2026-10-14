import math

import pytest

from llmgw.window import WindowAccountant


@pytest.fixture
def acc() -> WindowAccountant:
    return WindowAccountant(capacity=100, window=10.0)


def test_admits_up_to_capacity_and_rejects_over(acc):
    acc.reserve("a", 60, now=0)
    assert acc.fits(40, now=0)
    assert not acc.fits(41, now=0)


def test_entry_expires_exactly_at_window_boundary(acc):
    acc.reserve("a", 100, now=0)
    assert not acc.fits(1, now=9.99)
    assert acc.fits(1, now=10.0)


def test_used_reflects_only_entries_inside_window(acc):
    acc.reserve("a", 30, now=0)
    acc.reserve("b", 20, now=5)
    assert acc.used(now=9) == 50
    assert acc.used(now=10) == 20
    assert acc.used(now=15) == 0


def test_reconcile_down_frees_headroom(acc):
    acc.reserve("a", 80, now=0)
    assert not acc.fits(50, now=1)
    assert acc.reconcile("a", 30)
    assert acc.fits(50, now=1)


def test_reconcile_up_consumes_headroom(acc):
    acc.reserve("a", 30, now=0)
    acc.reconcile("a", 90)
    assert not acc.fits(20, now=1)
    assert acc.used(now=1) == 90


def test_reconcile_unknown_or_expired_returns_false(acc):
    assert not acc.reconcile("missing", 10)
    acc.reserve("a", 10, now=0)
    acc.used(now=10)
    assert not acc.reconcile("a", 5)


def test_release_removes_reservation(acc):
    acc.reserve("a", 70, now=0)
    assert acc.release("a")
    assert acc.used(now=0) == 0
    assert not acc.release("a")


def test_duplicate_key_is_rejected(acc):
    acc.reserve("a", 10, now=0)
    with pytest.raises(ValueError):
        acc.reserve("a", 10, now=1)


def test_time_going_backwards_is_rejected(acc):
    acc.reserve("a", 10, now=5)
    with pytest.raises(ValueError):
        acc.reserve("b", 10, now=4)


def test_next_free_time_is_now_when_it_fits(acc):
    acc.reserve("a", 50, now=0)
    assert acc.next_free_time(50, now=1) == 1


def test_next_free_time_waits_for_enough_entries_to_expire(acc):
    acc.reserve("a", 50, now=0)
    acc.reserve("b", 50, now=4)
    assert acc.next_free_time(40, now=5) == 10
    assert acc.next_free_time(60, now=5) == 14
    assert acc.next_free_time(100, now=5) == 14


def test_next_free_time_is_infinite_when_amount_exceeds_capacity(acc):
    assert acc.next_free_time(101, now=0) == math.inf


def test_next_free_time_matches_when_fits_becomes_true(acc):
    acc.reserve("a", 40, now=0)
    acc.reserve("b", 40, now=3)
    free_at = acc.next_free_time(70, now=4)
    assert not acc.fits(70, now=free_at - 0.001)
    assert acc.fits(70, now=free_at)


def test_accountants_are_independent():
    tokens = WindowAccountant(capacity=100, window=10.0)
    requests = WindowAccountant(capacity=2, window=10.0)
    tokens.reserve("a", 100, now=0)
    assert requests.fits(1, now=0)


@pytest.mark.parametrize("capacity,window", [(0, 10.0), (-1, 10.0), (10, 0), (10, -5.0)])
def test_invalid_construction_is_rejected(capacity, window):
    with pytest.raises(ValueError):
        WindowAccountant(capacity=capacity, window=window)
