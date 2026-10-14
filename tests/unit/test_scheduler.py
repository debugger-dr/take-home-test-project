import pytest

from llmgw.config import ProviderLimits, SchedulerConfig, TenantConfig
from llmgw.scheduler import Scheduler
from llmgw.sim.controls import FifoQueue
from llmgw.types import Accepted, Klass, Reason, Rejected, Usage


def make_config(
    tpm=1_000, rpm=100, tenants=("a", "b"), quantum=10_000, pacing_burst=1.0, **settings
) -> SchedulerConfig:
    return SchedulerConfig(
        limits=ProviderLimits(rpm=rpm, tpm=tpm, window=10.0),
        tenants=tuple(TenantConfig(t) for t in tenants),
        headroom=1.0,
        input_safety=1.0,
        base_quantum=quantum,
        pacing_burst=pacing_burst,
        **settings,
    )


@pytest.fixture
def req(make_request):
    def factory(id: str, tenant: str = "a", cost: int = 200, klass: Klass = Klass.P1):
        return make_request(id=id, tenant=tenant, est_input=cost - 100, max_output=100, klass=klass)

    return factory


def ids(dispatches) -> list[str]:
    return [d.id for d in dispatches]


def test_unknown_tenant_is_rejected_permanently(req):
    result = Scheduler(make_config()).submit(req("r", tenant="zzz"), now=0)
    assert isinstance(result, Rejected)
    assert (result.reason, result.retryable) == (Reason.UNKNOWN_TENANT, False)


def test_request_that_can_never_fit_is_rejected_permanently(req):
    scheduler = Scheduler(make_config(tpm=1_000))
    too_large = scheduler.submit(req("big", cost=1_001), now=0)
    assert isinstance(too_large, Rejected)
    assert (too_large.reason, too_large.retryable) == (Reason.TOO_LARGE, False)
    assert isinstance(scheduler.submit(req("exact", cost=1_000), now=0), Accepted)


def test_duplicate_submission_is_queued_once(req):
    scheduler = Scheduler(make_config())
    scheduler.submit(req("r"), now=0)
    assert isinstance(scheduler.submit(req("r"), now=0), Accepted)
    assert ids(scheduler.poll(0)) == ["r"]


def test_dispatch_stops_when_the_token_window_is_full_and_resumes_when_it_slides(req):
    scheduler = Scheduler(make_config(tpm=1_000))
    for i in (1, 2, 3):
        scheduler.submit(req(f"r{i}", cost=400), now=0)
    assert ids(scheduler.poll(0)) == ["r1", "r2"]
    assert scheduler.next_wakeup(0) == 10.0
    assert ids(scheduler.poll(10.0)) == ["r3"]


def test_request_window_limits_dispatch_independently_of_tokens(req):
    scheduler = Scheduler(make_config(tpm=1_000_000, rpm=2))
    for i in (1, 2, 3):
        scheduler.submit(req(f"r{i}"), now=0)
    assert ids(scheduler.poll(0)) == ["r1", "r2"]
    assert scheduler.next_wakeup(0) == 10.0
    assert ids(scheduler.poll(10.0)) == ["r3"]


def test_pacing_spreads_dispatches_instead_of_bursting(req):
    scheduler = Scheduler(make_config(tpm=6_000, pacing_burst=0.0))
    for i in (1, 2, 3):
        scheduler.submit(req(f"r{i}", cost=300), now=0)
    assert ids(scheduler.poll(0)) == ["r1"]
    assert scheduler.next_wakeup(0) == 0.5
    assert ids(scheduler.poll(0.5)) == ["r2"]
    assert ids(scheduler.poll(1.0)) == ["r3"]


def test_completion_refunds_the_pacer_so_pacing_follows_actual_usage(req):
    scheduler = Scheduler(make_config(tpm=6_000, pacing_burst=0.0))
    scheduler.submit(req("r1", cost=300), now=0)
    scheduler.submit(req("r2", cost=300), now=0)
    scheduler.poll(0)
    scheduler.on_complete("r1", Usage(input_tokens=50, output_tokens=50), now=0.1)
    assert ids(scheduler.poll(0.3)) == ["r2"]


def test_blocked_head_is_never_skipped_for_a_smaller_request(req):
    scheduler = Scheduler(make_config(tpm=1_000))
    scheduler.submit(req("filler", tenant="a", cost=500), now=0)
    assert ids(scheduler.poll(0)) == ["filler"]

    scheduler.submit(req("a-big", tenant="a", cost=600), now=1)
    scheduler.submit(req("b-small", tenant="b", cost=100), now=1)
    assert scheduler.poll(1) == []

    scheduler.on_complete("filler", Usage(input_tokens=50, output_tokens=50), now=2)
    assert ids(scheduler.poll(2)) == ["a-big", "b-small"]


def test_completion_reconciles_the_reservation_and_frees_capacity(req):
    scheduler = Scheduler(make_config(tpm=1_000))
    scheduler.submit(req("r1", cost=1_000), now=0)
    scheduler.submit(req("r2", cost=400), now=0)
    assert ids(scheduler.poll(0)) == ["r1"]
    scheduler.on_complete("r1", Usage(input_tokens=100, output_tokens=100), now=1)
    assert ids(scheduler.poll(1)) == ["r2"]


def test_provider_rejection_requeues_at_the_front_and_pauses_dispatch(req):
    scheduler = Scheduler(make_config())
    scheduler.submit(req("r1"), now=0)
    scheduler.submit(req("r2"), now=0)
    assert ids(scheduler.poll(0)) == ["r1", "r2"]

    scheduler.on_provider_rejected("r1", retry_after=5.0, now=1.0)
    assert scheduler.poll(2.0) == []
    assert scheduler.next_wakeup(2.0) == 6.0
    retried = scheduler.poll(6.0)
    assert ids(retried) == ["r1"]
    assert retried[0].attempt == 2


def test_completion_and_rejection_callbacks_are_idempotent(req):
    scheduler = Scheduler(make_config())
    scheduler.submit(req("r1"), now=0)
    scheduler.poll(0)
    usage = Usage(input_tokens=50, output_tokens=50)
    scheduler.on_complete("r1", usage, now=1)
    scheduler.on_complete("r1", usage, now=1)
    scheduler.on_complete("missing", usage, now=1)
    scheduler.on_provider_rejected("missing", retry_after=1.0, now=1)
    assert scheduler.poll(1) == []


def test_no_wakeup_is_needed_for_an_empty_queue():
    assert Scheduler(make_config()).next_wakeup(0) is None


def test_p0_is_dispatched_before_earlier_p1_requests_of_any_tenant(req):
    scheduler = Scheduler(make_config(tpm=10_000))
    scheduler.submit(req("p1", tenant="a"), now=0)
    scheduler.submit(req("p0", tenant="b", klass=Klass.P0), now=0)
    assert ids(scheduler.poll(0)) == ["p0", "p1"]


def test_p0_beyond_the_allowance_is_demoted_behind_queued_p1(req):
    scheduler = Scheduler(make_config(tpm=10_000, p0_rate=100, p0_burst=200))
    scheduler.submit(req("p1"), now=0)
    for name in ("p0a", "p0b", "p0c"):
        scheduler.submit(req(name, klass=Klass.P0), now=0)
    assert ids(scheduler.poll(0)) == ["p0a", "p0b", "p1", "p0c"]


def test_p0_dispatch_is_charged_to_the_tenants_fair_share(req):
    scheduler = Scheduler(make_config(tpm=10_000, quantum=200))
    for request_id, tenant in [("a1", "a"), ("a2", "a"), ("b1", "b"), ("b2", "b")]:
        scheduler.submit(req(request_id, tenant=tenant), now=0)
    scheduler.submit(req("p", tenant="a", klass=Klass.P0), now=0)
    assert ids(scheduler.poll(0)) == ["p", "b1", "a1", "b2", "a2"]


def test_rejected_p0_returns_to_the_front_of_the_p0_lane(req):
    scheduler = Scheduler(make_config(tpm=10_000))
    scheduler.submit(req("p0", klass=Klass.P0), now=0)
    scheduler.submit(req("p1"), now=0)
    assert ids(scheduler.poll(0)) == ["p0", "p1"]

    scheduler.on_provider_rejected("p0", retry_after=1.0, now=0.5)
    scheduler.submit(req("late", klass=Klass.P1), now=1.0)
    retried = scheduler.poll(2.0)
    assert ids(retried) == ["p0", "late"]
    assert retried[0].attempt == 2


def test_fair_queue_interleaves_tenants_while_fifo_does_not(req):
    config = make_config(tpm=10_000, quantum=200)

    def order(scheduler: Scheduler) -> list[str]:
        for request_id, tenant in [("a1", "a"), ("a2", "a"), ("b1", "b")]:
            scheduler.submit(req(request_id, tenant=tenant), now=0)
        return ids(scheduler.poll(0))

    assert order(Scheduler(config)) == ["a1", "b1", "a2"]
    assert order(Scheduler(config, FifoQueue(["a", "b"]))) == ["a1", "a2", "b1"]
