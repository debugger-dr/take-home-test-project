from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from llmgw.config import TenantConfig
from llmgw.types import Klass, LLMRequest


@dataclass(slots=True)
class QueuedRequest:
    request: LLMRequest
    cost: int
    klass: Klass
    enqueued_at: float
    attempt: int = 1

    @classmethod
    def of(cls, request: LLMRequest, cost: int, now: float) -> "QueuedRequest":
        return cls(request, cost, request.klass, now)

    @property
    def tenant(self) -> str:
        return self.request.tenant


class RequestQueue(Protocol):
    def has_tenant(self, tenant: str) -> bool: ...
    def push(self, item: QueuedRequest) -> None: ...
    def requeue(self, item: QueuedRequest) -> None: ...
    def peek(self, now: float) -> QueuedRequest | None: ...
    def pop(self, now: float) -> QueuedRequest: ...
    def adjust(self, tenant: str, tokens: int) -> None: ...


class _TenantQueue:
    __slots__ = ("quantum", "p1", "p2", "deficit", "topped_up")

    def __init__(self, quantum: int) -> None:
        self.quantum = quantum
        self.p1: deque[QueuedRequest] = deque()
        self.p2: deque[QueuedRequest] = deque()
        self.deficit = 0
        self.topped_up = False

    @property
    def empty(self) -> bool:
        return not self.p1 and not self.p2

    def lane(self, klass: Klass) -> deque[QueuedRequest]:
        return self.p2 if klass is Klass.P2 else self.p1

    def head(self) -> QueuedRequest:
        return self.p1[0] if self.p1 else self.p2[0]

    def take_head(self) -> QueuedRequest:
        return self.p1.popleft() if self.p1 else self.p2.popleft()


class FairQueue:
    """Deficit round robin over tenants, with cost measured in tokens and P1 served before P2 inside a tenant."""

    def __init__(self, tenants: Iterable[TenantConfig], base_quantum: int, age_p2_to_p1: float) -> None:
        self._tenants = {t.name: _TenantQueue(base_quantum * t.weight) for t in tenants}
        self._age = age_p2_to_p1
        self._ring: list[_TenantQueue] = []
        self._cursor = 0

    def has_tenant(self, tenant: str) -> bool:
        return tenant in self._tenants

    def backlog(self, tenant: str) -> int:
        queue = self._tenants[tenant]
        return len(queue.p1) + len(queue.p2)

    def deficit(self, tenant: str) -> int:
        return self._tenants[tenant].deficit

    def push(self, item: QueuedRequest) -> None:
        queue = self._tenants[item.tenant]
        self._promote(item.enqueued_at)
        self._activate_if_idle(queue)
        queue.lane(item.klass).append(item)

    def requeue(self, item: QueuedRequest) -> None:
        queue = self._tenants[item.tenant]
        refund = not queue.empty
        self._activate_if_idle(queue)
        item.attempt += 1
        queue.lane(item.klass).appendleft(item)
        if refund:
            queue.deficit += item.cost

    def peek(self, now: float) -> QueuedRequest | None:
        self._promote(now)
        while self._ring:
            queue = self._ring[self._cursor]
            if not queue.topped_up:
                queue.deficit += queue.quantum
                queue.topped_up = True
            head = queue.head()
            if head.cost <= queue.deficit:
                return head
            queue.topped_up = False
            self._cursor = (self._cursor + 1) % len(self._ring)
        return None

    def pop(self, now: float) -> QueuedRequest:
        item = self.peek(now)
        if item is None:
            raise IndexError("pop from an empty queue")
        queue = self._ring[self._cursor]
        queue.take_head()
        queue.deficit -= item.cost
        if queue.empty:
            self._deactivate()
        return item

    def adjust(self, tenant: str, tokens: int) -> None:
        queue = self._tenants[tenant]
        if not queue.empty:
            queue.deficit += tokens

    def _promote(self, now: float) -> None:
        for queue in self._ring:
            while queue.p2 and queue.p2[0].enqueued_at + self._age <= now:
                item = queue.p2.popleft()
                item.klass = Klass.P1
                queue.p1.append(item)

    def _activate_if_idle(self, queue: _TenantQueue) -> None:
        if queue.empty:
            self._ring.append(queue)

    def _deactivate(self) -> None:
        queue = self._ring.pop(self._cursor)
        queue.deficit = 0
        queue.topped_up = False
        if self._cursor >= len(self._ring):
            self._cursor = 0
