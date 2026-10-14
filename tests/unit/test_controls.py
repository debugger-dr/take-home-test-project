import pytest

from llmgw.config import ProviderLimits, SchedulerConfig, TenantConfig
from llmgw.fair_queue import QueuedRequest
from llmgw.sim.controls import SmallestFirstQueue, fifo_scheduler
from llmgw.types import Accepted


def make_config(**settings) -> SchedulerConfig:
    return SchedulerConfig(
        limits=ProviderLimits(rpm=100, tpm=100_000, window=10.0),
        tenants=(TenantConfig("a"),),
        **settings,
    )


@pytest.fixture
def item(make_request):
    def factory(id: str, cost: int) -> QueuedRequest:
        return QueuedRequest.of(make_request(id=id), cost, now=0.0)

    return factory


def test_smallest_first_serves_cheap_requests_before_expensive_ones(item):
    queue = SmallestFirstQueue(["a"])
    for request_id, cost in (("big", 900), ("small", 100), ("mid", 400)):
        queue.push(item(request_id, cost))
    assert [queue.pop(0).request.id for _ in range(3)] == ["small", "mid", "big"]


def test_smallest_first_can_drop_a_waiting_request(item):
    queue = SmallestFirstQueue(["a"])
    small, big = item("small", 100), item("big", 900)
    queue.push(small)
    queue.push(big)
    assert queue.remove(small)
    assert not queue.remove(small)
    assert queue.peek(0) is big


def test_fifo_control_ignores_queue_bounds_and_wait_estimates(make_request):
    scheduler = fifo_scheduler(make_config(max_queue_len=2, max_queue_tokens=500))
    results = [scheduler.submit(make_request(id=f"r{i}", deadline=1.0 + i), now=0) for i in range(1, 8)]
    assert all(isinstance(r, Accepted) for r in results)
    assert scheduler.queued("a") == 7
