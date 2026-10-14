import pytest

from llmgw.config import TenantConfig
from llmgw.fair_queue import FairQueue, QueuedRequest


@pytest.fixture
def item(make_request):
    def factory(id: str, tenant: str, cost: int = 100) -> QueuedRequest:
        return QueuedRequest(make_request(id=id, tenant=tenant), cost)

    return factory


def make_queue(weights=None, quantum=100) -> FairQueue:
    weights = weights or {"a": 1, "b": 1}
    return FairQueue([TenantConfig(name, w) for name, w in weights.items()], quantum)


def drain(queue: FairQueue) -> list[str]:
    ids = []
    while queue.peek() is not None:
        ids.append(queue.pop().request.id)
    return ids


def test_single_tenant_is_served_first_in_first_out(item):
    queue = make_queue()
    for i in (1, 2, 3):
        queue.push(item(f"a{i}", "a"))
    assert drain(queue) == ["a1", "a2", "a3"]


def test_equal_weights_alternate_between_tenants(item):
    queue = make_queue()
    for i in (1, 2, 3):
        queue.push(item(f"a{i}", "a"))
        queue.push(item(f"b{i}", "b"))
    assert drain(queue) == ["a1", "b1", "a2", "b2", "a3", "b3"]


def test_service_is_proportional_to_weight(item):
    queue = make_queue({"a": 1, "b": 2})
    for i in range(30):
        queue.push(item(f"a{i}", "a"))
        queue.push(item(f"b{i}", "b"))
    served = [queue.pop().tenant for _ in range(30)]
    assert (served.count("a"), served.count("b")) == (10, 20)


def test_large_request_accumulates_deficit_and_is_not_starved(item):
    queue = make_queue()
    queue.push(item("big", "a", cost=250))
    for i in range(1, 7):
        queue.push(item(f"b{i}", "b", cost=50))
    assert [queue.pop().request.id for _ in range(5)] == ["b1", "b2", "b3", "b4", "big"]


def test_drained_tenant_does_not_hoard_credit(item):
    queue = make_queue(quantum=1_000)
    for i in range(3):
        queue.push(item(f"a{i}", "a"))
    drain(queue)
    assert queue.deficit("a") == 0


def test_peek_is_idempotent(item):
    queue = make_queue()
    queue.push(item("a1", "a"))
    first = queue.peek()
    assert queue.peek() is first and queue.peek() is first
    assert queue.deficit("a") == 100


def test_requeue_refunds_the_charge_for_an_active_tenant(item):
    queue = make_queue(quantum=500)
    queue.push(item("a1", "a"))
    queue.push(item("a2", "a"))
    first = queue.pop()
    assert queue.deficit("a") == 400
    queue.requeue(first)
    assert queue.deficit("a") == 500
    assert queue.peek() is first
    assert first.attempt == 2


def test_requeue_to_an_idle_tenant_grants_no_credit(item):
    queue = make_queue()
    queue.push(item("a1", "a"))
    first = queue.pop()
    queue.requeue(first)
    assert queue.deficit("a") == 0
    assert queue.backlog("a") == 1
    assert queue.peek() is first


def test_adjust_applies_to_active_tenants_only(item):
    queue = make_queue()
    queue.push(item("a1", "a"))
    queue.peek()
    queue.adjust("a", 50)
    queue.adjust("b", 50)
    assert queue.deficit("a") == 150
    assert queue.deficit("b") == 0


def test_tenant_leaving_the_ring_does_not_disturb_the_order(item):
    queue = make_queue({"a": 1, "b": 1, "c": 1})
    for request_id, tenant in [("a1", "a"), ("a2", "a"), ("b1", "b"), ("c1", "c"), ("c2", "c")]:
        queue.push(item(request_id, tenant))
    assert drain(queue) == ["a1", "b1", "c1", "a2", "c2"]


def test_unknown_tenant_is_rejected(item):
    queue = make_queue()
    assert not queue.has_tenant("zzz")
    with pytest.raises(KeyError):
        queue.push(item("x", "zzz"))


def test_pop_from_empty_queue_raises():
    with pytest.raises(IndexError):
        make_queue().pop()
