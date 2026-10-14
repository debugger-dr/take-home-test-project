import random

from llmgw.config import ProviderLimits, SchedulerConfig, TenantConfig
from llmgw.scheduler import Scheduler
from llmgw.sim.controls import PassThroughScheduler
from llmgw.sim.engine import Simulator
from llmgw.sim.provider import FakeProvider, ProviderModel
from llmgw.sim.workload import TenantProfile, Workload
from llmgw.types import Accepted, Dispatch, Failure, Klass, LLMRequest, Reason, Rejected

ROOMY = ProviderLimits(rpm=10_000, tpm=10_000_000)


def make_provider(limits=ROOMY, **overrides) -> FakeProvider:
    model = ProviderModel(
        limits=limits,
        base_latency=2.0,
        seconds_per_output_token=0.0,
        input_ratio=(1.0, 1.0),
        output_ratio=(1.0, 1.0),
        **overrides,
    )
    return FakeProvider(model, random.Random(0))


def make_request(request_id: str, at: float, deadline: float | None = None) -> LLMRequest:
    return LLMRequest(
        id=request_id, tenant="a", run="r", agent="g", model="m", klass=Klass.P1,
        est_input_tokens=100, max_output_tokens=100,
        deadline=at + 100 if deadline is None else deadline, submitted_at=at,
    )


class _Idle:
    def on_complete(self, request_id, usage, now):
        pass

    def on_provider_rejected(self, request_id, retry_after, now):
        pass

    def on_provider_error(self, request_id, kind, now):
        pass

    def take_failures(self):
        return []

    def next_wakeup(self, now):
        return None

    def poll(self, now):
        return []


class HoldUntil(_Idle):
    def __init__(self, release_at: float) -> None:
        self._release_at = release_at
        self._held: list[LLMRequest] = []

    def submit(self, req, now):
        self._held.append(req)
        return Accepted(req.id, 0.0)

    def poll(self, now):
        if now < self._release_at:
            return []
        dispatches = [Dispatch(r.id, r.tenant, 200, 1) for r in self._held]
        self._held.clear()
        return dispatches

    def next_wakeup(self, now):
        return self._release_at if self._held and now < self._release_at else None


class RejectAll(_Idle):
    def __init__(self, retry_after: float | None = None) -> None:
        self._retry_after = retry_after

    def submit(self, req, now):
        retryable = self._retry_after is not None
        return Rejected(req.id, Reason.QUEUE_FULL, retryable, self._retry_after)


class RejectOnce(HoldUntil):
    def __init__(self, retry_after: float) -> None:
        super().__init__(release_at=0.0)
        self._retry_after = retry_after
        self._rejected = False

    def submit(self, req, now):
        if not self._rejected:
            self._rejected = True
            return Rejected(req.id, Reason.QUEUE_FULL, True, self._retry_after)
        return super().submit(req, now)


class ReportsFailure(_Idle):
    def __init__(self) -> None:
        self._failures: list[Failure] = []

    def submit(self, req, now):
        self._failures.append(Failure(req.id, Reason.EXPIRED))
        return Accepted(req.id, 0.0)

    def take_failures(self):
        failures, self._failures = self._failures, []
        return failures


class RecordingPassThrough(PassThroughScheduler):
    def __init__(self) -> None:
        super().__init__()
        self.rejections: list[tuple[str, float, float]] = []
        self.errors: list[tuple[str, str]] = []

    def on_provider_rejected(self, request_id, retry_after, now):
        self.rejections.append((request_id, now, retry_after))

    def on_provider_error(self, request_id, kind, now):
        self.errors.append((request_id, kind))


def test_light_load_completes_every_request_in_order():
    requests = Workload([TenantProfile("a", rate=0.2)], seed=1).generate(100)
    provider = make_provider()
    report = Simulator(PassThroughScheduler(), provider).run(requests, until=500)
    assert len(report.records) == len(requests)
    for record in report.records.values():
        assert record.throttled == 0
        assert record.submitted_at <= record.dispatched_at <= record.completed_at
    assert report.provider.accepted == len(requests)


def test_completion_time_is_arrival_plus_provider_latency():
    report = Simulator(PassThroughScheduler(), make_provider()).run([make_request("r1", 1.0)], until=50)
    assert report.records["r1"].completed_at == 3.0
    assert report.records["r1"].tokens == 200


def test_events_after_the_horizon_are_not_processed():
    requests = [make_request("early", 1.0), make_request("late", 10.0)]
    report = Simulator(PassThroughScheduler(), make_provider()).run(requests, until=5)
    assert set(report.records) == {"early"}


def test_scheduler_is_polled_at_its_requested_wakeup():
    report = Simulator(HoldUntil(5.0), make_provider()).run([make_request("r1", 1.0)], until=50)
    assert report.records["r1"].dispatched_at == 5.0
    assert report.records["r1"].completed_at == 7.0


def test_rejected_submission_is_recorded_and_never_dispatched():
    report = Simulator(RejectAll(), make_provider()).run([make_request("r1", 1.0)], until=50)
    record = report.records["r1"]
    assert record.rejected is Reason.QUEUE_FULL
    assert record.dispatched_at is None
    assert record.submissions == 1


def test_provider_throttle_is_reported_back_to_the_scheduler():
    scheduler = RecordingPassThrough()
    provider = make_provider(ProviderLimits(rpm=1, tpm=10_000_000))
    requests = [make_request("first", 0.0), make_request("second", 10.0)]
    report = Simulator(scheduler, provider).run(requests, until=100)
    assert report.records["second"].throttled == 1
    assert scheduler.rejections == [("second", 10.0, 50.0)]


def test_provider_fault_is_reported_back_as_an_unknown_outcome():
    scheduler = RecordingPassThrough()
    report = Simulator(scheduler, make_provider(fault_rate=1.0)).run([make_request("r1", 1.0)], until=50)
    assert report.records["r1"].faulted == 1
    assert scheduler.errors == [("r1", "server_error")]
    assert report.provider.faulted == 1


def test_scheduler_failures_are_recorded_with_their_time():
    report = Simulator(ReportsFailure(), make_provider()).run([make_request("r1", 4.0)], until=50)
    record = report.records["r1"]
    assert (record.failed, record.failed_at) == (Reason.EXPIRED, 4.0)


def test_a_rejected_caller_comes_back_after_the_retry_hint():
    simulator = Simulator(RejectOnce(retry_after=2.0), make_provider(), max_attempts=3, retry_jitter=0.0)
    report = simulator.run([make_request("r1", 1.0)], until=50)
    record = report.records["r1"]
    assert record.submissions == 2
    assert record.rejected is None
    assert record.dispatched_at == 3.0


def test_retry_delay_is_stretched_by_jitter():
    simulator = Simulator(RejectOnce(retry_after=2.0), make_provider(), max_attempts=3, retry_jitter=1.0)
    report = simulator.run([make_request("r1", 1.0)], until=50)
    assert 3.0 < report.records["r1"].dispatched_at <= 5.0


def test_caller_gives_up_after_max_attempts():
    simulator = Simulator(RejectAll(retry_after=1.0), make_provider(), max_attempts=3, retry_jitter=0.0)
    report = simulator.run([make_request("r1", 1.0)], until=50)
    record = report.records["r1"]
    assert record.submissions == 3
    assert record.rejected is Reason.QUEUE_FULL


def test_non_retryable_rejection_is_not_retried():
    simulator = Simulator(RejectAll(retry_after=None), make_provider(), max_attempts=3)
    report = simulator.run([make_request("r1", 1.0)], until=50)
    assert report.records["r1"].submissions == 1


def test_request_expires_while_waiting_for_quota():
    config = SchedulerConfig(
        limits=ProviderLimits(rpm=100, tpm=1_000, window=10.0),
        tenants=(TenantConfig("a"),),
        headroom=1.0,
        input_safety=1.0,
        pacing_burst=1.0,
    )
    big = LLMRequest(
        id="big", tenant="a", run="r", agent="g", model="m", klass=Klass.P1,
        est_input_tokens=900, max_output_tokens=100, deadline=100.0, submitted_at=0.0,
    )
    late = make_request("late", 0.5, deadline=3.0)
    provider = make_provider(ProviderLimits(rpm=100, tpm=10_000))
    report = Simulator(Scheduler(config), provider).run([big, late], until=50)
    record = report.records["late"]
    assert (record.failed, record.failed_at) == (Reason.EXPIRED, 3.0)
    assert record.dispatched_at is None
