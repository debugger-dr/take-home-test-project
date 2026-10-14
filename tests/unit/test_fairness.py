import pytest

from llmgw.sim.fairness import max_min_fair_share

EQUAL = {"a": 1, "b": 1, "c": 1}


def test_everyone_is_satisfied_when_capacity_is_ample():
    demands = {"a": 100, "b": 50, "c": 10}
    assert max_min_fair_share(1_000, demands, EQUAL) == demands


def test_small_demands_are_met_and_the_rest_is_split_evenly():
    share = max_min_fair_share(360, {"a": 1_200, "b": 200, "c": 60}, EQUAL)
    assert share == pytest.approx({"a": 150, "b": 150, "c": 60})


def test_all_backlogged_tenants_split_by_weight():
    share = max_min_fair_share(400, {"a": 1_000, "b": 1_000, "c": 1_000}, {"a": 1, "b": 2, "c": 1})
    assert share == pytest.approx({"a": 100, "b": 200, "c": 100})


def test_shares_never_exceed_capacity_or_demand():
    demands = {"a": 70, "b": 500, "c": 20}
    share = max_min_fair_share(300, demands, EQUAL)
    assert sum(share.values()) == pytest.approx(300)
    assert all(share[name] <= demands[name] for name in demands)
