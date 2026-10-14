import pytest

from llmgw.sim.workload import SizeRange, TenantProfile, Workload
from llmgw.types import Klass


def profile(tenant="a", rate=1.0, **overrides) -> TenantProfile:
    return TenantProfile(tenant=tenant, rate=rate, **overrides)


def test_same_seed_gives_identical_requests_and_different_seed_differs():
    first = Workload([profile()], seed=1).generate(100)
    assert first == Workload([profile()], seed=1).generate(100)
    assert first != Workload([profile()], seed=2).generate(100)


def test_requests_are_sorted_and_inside_the_duration():
    requests = Workload([profile("a", 2.0), profile("b", 3.0)], seed=1).generate(50)
    times = [r.submitted_at for r in requests]
    assert times == sorted(times)
    assert 0 < times[0] and times[-1] < 50


def test_arrival_count_follows_the_rate():
    requests = Workload([profile(rate=10.0)], seed=1).generate(200)
    assert len(requests) == pytest.approx(2_000, rel=0.1)


def test_tenant_streams_are_independent():
    alone = Workload([profile("a", 2.0)], seed=1).generate(100)
    together = Workload([profile("a", 2.0), profile("b", 50.0)], seed=1).generate(100)
    assert [r for r in together if r.tenant == "a"] == alone


def test_ids_are_unique_across_tenants():
    requests = Workload([profile("a", 5.0), profile("b", 5.0)], seed=1).generate(60)
    assert len({r.id for r in requests}) == len(requests)


def test_sizes_and_deadlines_follow_the_profile():
    p = profile(est_input=SizeRange(100, 200), max_output=SizeRange(10, 20), deadline=30.0)
    for r in Workload([p], seed=1).generate(60):
        assert 100 <= r.est_input_tokens <= 200
        assert 10 <= r.max_output_tokens <= 20
        assert r.deadline == r.submitted_at + 30.0


def test_class_comes_from_run_state():
    all_p0 = Workload([profile(human_waiting_fraction=1.0)], seed=1).generate(30)
    all_p2 = Workload([profile(interactive_fraction=0.0)], seed=1).generate(30)
    assert {r.klass for r in all_p0} == {Klass.P0}
    assert {r.klass for r in all_p2} == {Klass.P2}


@pytest.mark.parametrize(
    "overrides",
    [{"rate": 0}, {"agents": 0}, {"human_waiting_fraction": 1.5}, {"interactive_fraction": -0.1}],
)
def test_invalid_profile_is_rejected(overrides):
    with pytest.raises(ValueError):
        profile(**overrides)
