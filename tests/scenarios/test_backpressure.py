from llmgw.config import ProviderLimits
from llmgw.scheduler import Scheduler
from llmgw.sim.report import Report
from llmgw.sim.scenario import Scenario, rate_for
from llmgw.sim.workload import TenantProfile

LIMITS = ProviderLimits(rpm=600, tpm=400_000)
QUEUE_BOUND = 50
MAX_ATTEMPTS = 5


class WatchedScheduler(Scheduler):
    def __init__(self, config):
        super().__init__(config)
        self.max_queued = 0

    def submit(self, req, now):
        result = super().submit(req, now)
        self.max_queued = max(self.max_queued, self.queued(req.tenant))
        return result


def run_burst(settings: dict) -> tuple[Report, WatchedScheduler]:
    """Five times the provider's capacity for 30 s, with callers that retry up to five times."""
    scenario = Scenario(
        LIMITS,
        (TenantProfile("a", rate=rate_for(1_800_000)),),
        settings=settings,
        max_attempts=MAX_ATTEMPTS,
        duration=30.0,
        drain=400.0,
        warmup=0.0,
    )
    watched: list[WatchedScheduler] = []

    def build(config):
        scheduler = WatchedScheduler(config)
        watched.append(scheduler)
        return scheduler

    return scenario.run(build), watched[0]


def test_queue_stays_within_its_bound_and_every_request_ends_in_exactly_one_state():
    report, scheduler = run_burst({"max_queue_len": QUEUE_BOUND})

    assert scheduler.max_queued <= QUEUE_BOUND
    for record in report.records.values():
        outcomes = (record.completed_at is not None, record.failed is not None, record.rejected is not None)
        assert sum(outcomes) == 1
        assert record.submissions <= MAX_ATTEMPTS


def test_backpressure_was_exercised_and_retrying_callers_were_eventually_served():
    report, _ = run_burst({"max_queue_len": QUEUE_BOUND})
    records = report.records.values()
    assert any(r.rejected is not None for r in records)
    assert any(r.submissions > 1 and r.completed_at is not None for r in records)


def test_control_without_a_queue_bound_exceeds_it():
    _, scheduler = run_burst({"max_queue_len": 10**9, "max_queue_tokens": 10**12})
    assert scheduler.max_queued > QUEUE_BOUND
