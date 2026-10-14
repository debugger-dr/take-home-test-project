import pytest

from llmgw.config import ProviderLimits, SchedulerConfig, TenantConfig
from llmgw.scheduler import Scheduler
from llmgw.sim.controls import FifoQueue
from llmgw.events import MemorySink
from llmgw.ledger import Ledger, Spend
from llmgw.types import Accepted, Failure, Klass, Reason, Rejected, Usage


def make_config(
    tpm=1_000, rpm=100, tenants=("a", "b"), quantum=10_000, pacing_burst=1.0, budgets=None, **settings
) -> SchedulerConfig:
    budgets = budgets or {}
    return SchedulerConfig(
        limits=ProviderLimits(rpm=rpm, tpm=tpm, window=10.0),
        tenants=tuple(TenantConfig(t, token_budget=budgets.get(t)) for t in tenants),
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


def test_queue_length_bound_rejects_with_a_retry_hint(req):
    scheduler = Scheduler(make_config(tpm=10_000, max_queue_len=2))
    for i in (1, 2):
        scheduler.submit(req(f"r{i}"), now=0)
    result = scheduler.submit(req("r3"), now=0)
    assert isinstance(result, Rejected)
    assert (result.reason, result.retryable) == (Reason.QUEUE_FULL, True)
    assert result.retry_after == pytest.approx(0.2)


def test_queued_token_bound_rejects_when_the_next_request_would_exceed_it(req):
    scheduler = Scheduler(make_config(tpm=10_000, max_queue_tokens=500))
    scheduler.submit(req("r1"), now=0)
    scheduler.submit(req("r2"), now=0)
    result = scheduler.submit(req("r3"), now=0)
    assert isinstance(result, Rejected) and result.reason is Reason.QUEUE_FULL


def test_request_that_cannot_be_served_before_its_deadline_is_rejected_with_a_hint(make_request):
    scheduler = Scheduler(make_config(tpm=1_000))
    scheduler.submit(make_request(id="queued", est_input=300, max_output=100), now=0)
    late = scheduler.submit(make_request(id="late", est_input=300, max_output=100, deadline=5.0), now=0)
    assert isinstance(late, Rejected)
    assert (late.reason, late.retryable) == (Reason.WAIT_EXCEEDS_DEADLINE, True)
    assert late.retry_after == pytest.approx(3.0)


def test_accepted_request_reports_its_estimated_wait(make_request):
    scheduler = Scheduler(make_config(tpm=1_000))
    scheduler.submit(make_request(id="queued", est_input=300, max_output=100), now=0)
    result = scheduler.submit(make_request(id="next", est_input=300, max_output=100, deadline=10.0), now=0)
    assert isinstance(result, Accepted)
    assert result.est_wait == pytest.approx(8.0)


def test_estimated_wait_shrinks_when_the_tenants_share_grows(make_request):
    busy = Scheduler(make_config(tpm=1_000))
    busy.submit(make_request(id="other", tenant="b", est_input=300, max_output=100), now=0)
    shared = busy.submit(make_request(id="mine", tenant="a", est_input=300, max_output=100, deadline=50), now=0)
    alone = Scheduler(make_config(tpm=1_000)).submit(
        make_request(id="mine", tenant="a", est_input=300, max_output=100, deadline=50), now=0
    )
    assert shared.est_wait == pytest.approx(2 * alone.est_wait)


def test_p0_requests_are_not_rejected_for_an_estimated_wait(make_request):
    scheduler = Scheduler(make_config(tpm=1_000))
    scheduler.submit(make_request(id="queued", est_input=300, max_output=100), now=0)
    urgent = make_request(id="urgent", est_input=300, max_output=100, klass=Klass.P0, deadline=5.0)
    assert isinstance(scheduler.submit(urgent, now=0), Accepted)


def test_request_whose_deadline_has_passed_is_rejected_permanently(make_request):
    result = Scheduler(make_config()).submit(make_request(id="old", deadline=3.0), now=3.0)
    assert isinstance(result, Rejected)
    assert (result.reason, result.retryable) == (Reason.EXPIRED, False)


def test_queued_request_expires_at_its_deadline(req, make_request):
    scheduler = Scheduler(make_config(tpm=1_000))
    scheduler.submit(req("r1", cost=1_000), now=0)
    assert ids(scheduler.poll(0)) == ["r1"]
    scheduler.submit(make_request(id="r2", est_input=300, max_output=100, deadline=5.0), now=0)
    assert scheduler.next_wakeup(0) == 5.0

    assert scheduler.poll(5.0) == []
    assert scheduler.take_failures() == [Failure("r2", Reason.EXPIRED)]
    assert scheduler.take_failures() == []
    assert scheduler.queued("a") == 0
    assert scheduler.poll(10.0) == []


def test_cancel_removes_a_queued_request_but_not_one_in_flight(req):
    sink = MemorySink()
    scheduler = Scheduler(make_config(tpm=1_000), events=sink)
    scheduler.submit(req("r1", cost=1_000), now=0)
    scheduler.submit(req("r2", cost=400), now=0)
    assert ids(scheduler.poll(0)) == ["r1"]

    assert not scheduler.cancel("r1", now=1)
    assert scheduler.cancel("r2", now=1)
    assert not scheduler.cancel("r2", now=1)
    assert not scheduler.cancel("missing", now=1)
    assert scheduler.queued("a") == 0
    assert scheduler.poll(10.0) == []
    assert len(sink.named("llm.cancelled")) == 1


def test_unknown_outcome_keeps_the_reservation_and_retries_after_it_ages_out(req):
    scheduler = Scheduler(make_config(tpm=1_000, tenants=("a",)))
    scheduler.submit(req("r1", cost=600), now=0)
    assert ids(scheduler.poll(0)) == ["r1"]

    scheduler.on_provider_error("r1", "timeout", now=1.0)
    assert scheduler.poll(1.0) == []
    assert scheduler.next_wakeup(1.0) == 10.0
    retried = scheduler.poll(10.0)
    assert ids(retried) == ["r1"]
    assert retried[0].attempt == 2


def test_second_unknown_outcome_is_a_terminal_failure(req):
    scheduler = Scheduler(make_config(tpm=10_000, tenants=("a",)))
    scheduler.submit(req("r1"), now=0)
    scheduler.poll(0)
    scheduler.on_provider_error("r1", "timeout", now=1)
    assert ids(scheduler.poll(1)) == ["r1"]

    scheduler.on_provider_error("r1", "timeout", now=2)
    assert scheduler.take_failures() == [Failure("r1", Reason.PROVIDER_ERROR)]
    assert scheduler.poll(3) == []


def test_tenant_budget_alerts_once_and_blocks_dispatch_that_would_exceed_it(req):
    sink = MemorySink()
    scheduler = Scheduler(make_config(tpm=10_000, tenants=("a",), budgets={"a": 1_000}), events=sink)
    for i in (1, 2, 3):
        scheduler.submit(req(f"r{i}", cost=400), now=0)
    assert ids(scheduler.poll(0)) == ["r1", "r2"]
    assert scheduler.take_failures() == [Failure("r3", Reason.TENANT_BUDGET_EXHAUSTED)]
    alerts = sink.named("llm.budget_alert")
    assert len(alerts) == 1
    assert alerts[0].fields["scope"] == "tenant"


def test_exhausted_tenant_budget_rejects_new_requests_permanently(req):
    scheduler = Scheduler(make_config(tpm=10_000, tenants=("a",), budgets={"a": 800}))
    for i in (1, 2):
        scheduler.submit(req(f"r{i}", cost=400), now=0)
    scheduler.poll(0)
    result = scheduler.submit(req("r3", cost=400), now=1)
    assert isinstance(result, Rejected)
    assert (result.reason, result.retryable) == (Reason.TENANT_BUDGET_EXHAUSTED, False)


def test_completion_returns_budget_that_was_reserved_but_not_used(req):
    scheduler = Scheduler(make_config(tpm=10_000, tenants=("a",), budgets={"a": 1_000}))
    scheduler.submit(req("r1", cost=800), now=0)
    scheduler.poll(0)
    scheduler.on_complete("r1", Usage(input_tokens=100, output_tokens=100), now=1)
    scheduler.submit(req("r2", cost=800), now=1)
    assert ids(scheduler.poll(1)) == ["r2"]


def test_provider_rejection_returns_the_budget_for_a_request_that_was_not_served(req):
    scheduler = Scheduler(make_config(tpm=10_000, tenants=("a",), budgets={"a": 1_000}))
    scheduler.submit(req("r1", cost=800), now=0)
    scheduler.poll(0)
    scheduler.on_provider_rejected("r1", retry_after=1.0, now=0.5)
    assert ids(scheduler.poll(2.0)) == ["r1"]
    assert scheduler.take_failures() == []


def test_run_cap_stops_a_looping_run_but_not_other_runs(make_request):
    scheduler = Scheduler(make_config(tpm=10_000, tenants=("a",), run_token_cap=500))
    for request_id, run in (("r1", "loop"), ("r2", "loop"), ("r3", "other")):
        scheduler.submit(make_request(id=request_id, est_input=200, max_output=100, run=run), now=0)
    assert ids(scheduler.poll(0)) == ["r1", "r3"]
    assert scheduler.take_failures() == [Failure("r2", Reason.RUN_BUDGET_EXHAUSTED)]


def test_p0_demotion_is_reported_as_an_event(req):
    sink = MemorySink()
    scheduler = Scheduler(make_config(tpm=10_000, p0_rate=100, p0_burst=200), events=sink)
    for name in ("p0a", "p0b", "p0c", "p0d"):
        scheduler.submit(req(name, klass=Klass.P0), now=0)
    scheduler.poll(0)
    assert [e.fields["request_id"] for e in sink.named("llm.p0_demoted")] == ["p0c", "p0d"]


def test_completion_is_billed_from_what_the_provider_reports(req):
    ledger = Ledger()
    scheduler = Scheduler(make_config(tpm=10_000, tenants=("a",)), ledger=ledger)
    scheduler.submit(req("r1"), now=0)
    scheduler.poll(2.0)
    scheduler.on_complete("r1", Usage(input_tokens=150, output_tokens=50), now=5.0)
    assert ledger.total() == Spend(tokens=200, cost=150 * 3 + 50 * 15, requests=1)
    assert ledger.outcomes() == {"ok": 1}


def test_a_budget_failure_is_recorded_with_its_outcome_and_no_tokens(req):
    ledger = Ledger()
    scheduler = Scheduler(make_config(tpm=10_000, tenants=("a",), run_token_cap=300), ledger=ledger)
    scheduler.submit(req("fits", cost=300), now=0)
    scheduler.submit(req("over-cap", cost=300), now=0)
    scheduler.poll(0)
    assert ledger.outcomes() == {"budget_exhausted": 1}
    assert ledger.total().tokens == 0


def test_an_expiry_is_recorded_with_its_outcome_and_no_tokens(req, make_request):
    ledger = Ledger()
    scheduler = Scheduler(make_config(tpm=1_000, tenants=("a",)), ledger=ledger)
    scheduler.submit(req("filler", cost=1_000), now=0)
    scheduler.poll(0)
    scheduler.submit(make_request(id="late", est_input=300, max_output=100, deadline=5.0), now=0)
    scheduler.poll(5.0)
    assert ledger.outcomes() == {"expired": 1}
    assert ledger.total().tokens == 0


def test_a_request_that_needed_a_retry_is_billed_once(req):
    ledger = Ledger()
    scheduler = Scheduler(make_config(tpm=10_000, tenants=("a",)), ledger=ledger)
    scheduler.submit(req("r1"), now=0)
    scheduler.poll(0)
    scheduler.on_provider_error("r1", "timeout", now=1)
    scheduler.poll(1)
    scheduler.on_complete("r1", Usage(input_tokens=100, output_tokens=100), now=2)
    assert ledger.total().requests == 1
    assert ledger.total().tokens == 200


def test_lifecycle_events_carry_the_numbers_an_operator_needs(req):
    sink = MemorySink()
    scheduler = Scheduler(make_config(tpm=10_000, tenants=("a",)), events=sink)
    scheduler.submit(req("r1"), now=0)
    scheduler.poll(2.0)
    scheduler.on_complete("r1", Usage(input_tokens=150, output_tokens=50), now=5.0)

    queued, = sink.named("llm.queued")
    dispatched, = sink.named("llm.dispatched")
    completed, = sink.named("llm.completed")
    assert queued.fields["queue_len"] == 1
    assert dispatched.fields["queue_wait"] == 2.0
    assert dispatched.fields["window_tokens_used"] == 200
    assert completed.fields["cost"] == 150 * 3 + 50 * 15
    assert completed.fields["input_estimate_error_pct"] == 50.0


def test_snapshot_shows_queues_utilisation_and_rejections(req):
    scheduler = Scheduler(make_config(tpm=1_000, tenants=("a",)))
    scheduler.submit(req("r1", cost=500), now=0)
    scheduler.submit(req("r2", cost=500), now=0)
    scheduler.submit(req("r3", cost=2_000), now=0)
    scheduler.poll(0)

    snap = scheduler.snapshot(0)
    assert snap.in_flight == 2
    assert snap.queued == {"a": 0}
    assert snap.token_utilisation == pytest.approx(1.0)
    assert snap.rejections == {"too_large": 1}
    assert not snap.cooling_down

    scheduler.on_provider_rejected("r1", retry_after=5.0, now=1.0)
    assert scheduler.snapshot(2.0).cooling_down
    assert scheduler.snapshot(2.0).queued == {"a": 1}


def test_fair_queue_interleaves_tenants_while_fifo_does_not(req):
    config = make_config(tpm=10_000, quantum=200)

    def order(scheduler: Scheduler) -> list[str]:
        for request_id, tenant in [("a1", "a"), ("a2", "a"), ("b1", "b")]:
            scheduler.submit(req(request_id, tenant=tenant), now=0)
        return ids(scheduler.poll(0))

    assert order(Scheduler(config)) == ["a1", "b1", "a2"]
    assert order(Scheduler(config, FifoQueue(["a", "b"]))) == ["a1", "a2", "b1"]
