import pytest

from llmgw.fair_queue import QueuedRequest
from llmgw.p0_lane import P0Lane
from llmgw.types import Klass


@pytest.fixture
def item(make_request):
    def factory(id: str, tenant: str = "a", cost: int = 100) -> QueuedRequest:
        return QueuedRequest.of(make_request(id=id, tenant=tenant, klass=Klass.P0), cost, now=0.0)

    return factory


class Demotions:
    def __init__(self) -> None:
        self.items: list[QueuedRequest] = []

    def __call__(self, item: QueuedRequest, now: float) -> None:
        self.items.append(item)


def make_lane(rate=100, burst=200, tenants=("a", "b")):
    demoted = Demotions()
    return P0Lane(list(tenants), rate, burst, demoted), demoted


def test_burst_is_served_then_excess_is_demoted(item):
    lane, demoted = make_lane()
    for i in range(1, 6):
        lane.push(item(f"a{i}"))
    served = [lane.pop(0).request.id for _ in range(3)]
    assert served == ["a1", "a2", "a3"]
    assert lane.peek(0) is None
    assert [d.request.id for d in demoted.items] == ["a4", "a5"]


def test_allowance_refills_over_time(item):
    lane, demoted = make_lane()
    for i in range(1, 5):
        lane.push(item(f"a{i}"))
    for _ in range(3):
        lane.pop(0)
    assert lane.peek(5.0).request.id == "a4"
    assert demoted.items == []


def test_tenants_are_served_round_robin(item):
    lane, _ = make_lane(rate=10_000)
    for i in (1, 2):
        lane.push(item(f"a{i}", "a"))
        lane.push(item(f"b{i}", "b"))
    assert [lane.pop(0).request.id for _ in range(4)] == ["a1", "b1", "a2", "b2"]


def test_one_tenants_allowance_does_not_limit_another(item):
    lane, demoted = make_lane()
    for i in range(1, 6):
        lane.push(item(f"a{i}", "a"))
    lane.push(item("b1", "b"))
    ids = []
    while lane.peek(0) is not None:
        ids.append(lane.pop(0).request.id)
    assert ids == ["a1", "b1", "a2", "a3"]
    assert [d.request.id for d in demoted.items] == ["a4", "a5"]


def test_peek_is_idempotent(item):
    lane, demoted = make_lane()
    lane.push(item("a1"))
    first = lane.peek(0)
    assert lane.peek(0) is first
    assert demoted.items == []


def test_requeue_returns_the_request_to_the_front(item):
    lane, _ = make_lane(rate=10_000)
    lane.push(item("a1"))
    lane.push(item("a2"))
    first = lane.pop(0)
    lane.requeue(first)
    assert lane.peek(0) is first
    assert first.attempt == 2


def test_remove_takes_a_waiting_request_out_of_the_lane(item):
    lane, _ = make_lane(rate=10_000)
    first, second = item("a1"), item("a2")
    lane.push(first)
    lane.push(second)
    assert lane.remove(first)
    assert not lane.remove(first)
    assert lane.pop(0) is second


def test_pop_from_empty_lane_raises():
    lane, _ = make_lane()
    with pytest.raises(IndexError):
        lane.pop(0)
