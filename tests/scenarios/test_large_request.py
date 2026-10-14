import math

from llmgw.config import ProviderLimits
from llmgw.sim.controls import smallest_first_scheduler
from llmgw.sim.report import percentile
from llmgw.sim.scenario import Scenario
from llmgw.sim.workload import SizeRange, TenantProfile

LIMITS = ProviderLimits(rpm=600, tpm=400_000)
USABLE_PER_MINUTE = 360_000
RATE = USABLE_PER_MINUTE / 60
QUANTUM = 4_000
LARGE_COST = math.ceil(6_000 * 1.15) + 1_000
# Two tenants are active; each is served at most one quantum plus one maximal request per round.
ROUND_MAX = 2 * (QUANTUM + LARGE_COST) / RATE
WAIT_BOUND = (math.ceil(LARGE_COST / QUANTUM) + 1) * ROUND_MAX + LARGE_COST / RATE

SMALL_FLOOD = TenantProfile(
    "a", rate=20.0, est_input=SizeRange(1_000, 1_400), max_output=SizeRange(150, 250)
)
LARGE_REQUESTS = TenantProfile(
    "b", rate=1 / 20, est_input=SizeRange(6_000, 6_000), max_output=SizeRange(1_000, 1_000)
)
SCENARIO = Scenario(LIMITS, (SMALL_FLOOD, LARGE_REQUESTS))


def test_a_large_request_waits_for_room_instead_of_being_skipped_and_is_served_in_bounded_time():
    report = SCENARIO.run()
    start, end = SCENARIO.measure
    goodput = sum(report.goodput_per_minute(t, start, end) for t in ("a", "b"))

    waits = report.waits_when_idle("b", start, end)
    assert len(waits) >= 10
    assert report.served_ratio("b", start, end) >= 0.95
    assert percentile(waits, 99) <= WAIT_BOUND
    assert goodput >= 0.85 * USABLE_PER_MINUTE


def test_control_that_always_serves_the_cheapest_request_starves_the_large_one():
    report = SCENARIO.run(smallest_first_scheduler)
    start, end = SCENARIO.measure
    assert report.served_ratio("b", start, end) < 0.2
