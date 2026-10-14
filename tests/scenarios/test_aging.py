from llmgw.config import ProviderLimits
from llmgw.sim.controls import no_aging_scheduler
from llmgw.sim.scenario import Scenario, rate_for
from llmgw.sim.workload import TenantProfile
from llmgw.types import Klass

LIMITS = ProviderLimits(rpm=600, tpm=400_000)
# A bounded queue keeps the P1 backlog short: a promoted request waits about age + queue / dispatch rate, well inside its deadline.
SCENARIO = Scenario(
    LIMITS,
    (TenantProfile("a", rate=rate_for(1_200_000), interactive_fraction=0.9),),
    settings={"max_queue_len": 30},
)


def test_batch_requests_are_served_under_constant_interactive_load():
    start, end = SCENARIO.measure
    report = SCENARIO.run()
    assert report.served_ratio("a", start, end, Klass.P2) >= 0.9


def test_control_without_aging_leaves_most_batch_requests_unserved():
    # Not zero: once the bounded queue has filled with waiting P2 requests, P1 arrivals are turned away and P2 is served.
    start, end = SCENARIO.measure
    report = SCENARIO.run(no_aging_scheduler)
    assert report.served_ratio("a", start, end, Klass.P2) < 0.5
