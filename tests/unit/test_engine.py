import random

from llmgw.config import ProviderLimits
from llmgw.sim.controls import PassThroughScheduler
from llmgw.sim.engine import Simulator
from llmgw.sim.provider import FakeProvider, ProviderModel
from llmgw.sim.workload import TenantProfile, Workload
from llmgw.types import Accepted, Dispatch, Klass, LLMRequest, Reason, Rejected

ROOMY = ProviderLimits(rpm=10_000, tpm=10_000_000)


def make_provider(limits=ROOMY) -> FakeProvider:
    model = ProviderModel(
        limits=limits,
        base_latency=2.0,
        seconds_per_output_token=0.0,
        input_ratio=(1.0, 1.0),
        output_ratio=(1.0, 1.0),
    )
    return FakeProvider(model, random.Random(0))


def make_request(request_id: str, at: float) -> LLMRequest:
    return LLMRequest(
        id=request_id, tenant="a", run="r", agent="g", model="m", klass=Klass.P1,
        est_input_tokens=100, max_output_tokens=100, deadline=at + 100, submitted_at=at,
    )


class _Idle:
    def on_complete(self, request_id, usage, now):
        pass

    def on_provider_rejected(self, request_id, retry_after, now):
        pass

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
    def submit(self, req, now):
        return Rejected(req.id, Reason.QUEUE_FULL, retryable=True, retry_after=1.0)


class RecordingPassThrough(PassThroughScheduler):
    def __init__(self) -> None:
        super().__init__()
        self.rejections: list[tuple[str, float, float]] = []

    def on_provider_rejected(self, request_id, retry_after, now):
        self.rejections.append((request_id, now, retry_after))


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


def test_provider_throttle_is_reported_back_to_the_scheduler():
    scheduler = RecordingPassThrough()
    provider = make_provider(ProviderLimits(rpm=1, tpm=10_000_000))
    requests = [make_request("first", 0.0), make_request("second", 10.0)]
    report = Simulator(scheduler, provider).run(requests, until=100)
    assert report.records["second"].throttled == 1
    assert scheduler.rejections == [("second", 10.0, 50.0)]
