from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from llmgw.config import TenantConfig
from llmgw.types import LLMRequest


@dataclass(slots=True)
class QueuedRequest:
    request: LLMRequest
    cost: int
    attempt: int = 1

    @property
    def tenant(self) -> str:
        return self.request.tenant


class RequestQueue(Protocol):
    def has_tenant(self, tenant: str) -> bool: ...
    def push(self, item: QueuedRequest) -> None: ...
    def requeue(self, item: QueuedRequest) -> None: ...
    def peek(self) -> QueuedRequest | None: ...
    def pop(self) -> QueuedRequest: ...
    def adjust(self, tenant: str, tokens: int) -> None: ...


class _TenantQueue:
    __slots__ = ("quantum", "items", "deficit", "topped_up")

    def __init__(self, quantum: int) -> None:
        self.quantum = quantum
        self.items: deque[QueuedRequest] = deque()
        self.deficit = 0
        self.topped_up = False


class FairQueue:
    """Deficit round robin over tenants, with cost measured in tokens."""

    def __init__(self, tenants: Iterable[TenantConfig], base_quantum: int) -> None:
        self._tenants = {t.name: _TenantQueue(base_quantum * t.weight) for t in tenants}
        self._ring: list[_TenantQueue] = []
        self._cursor = 0

    def has_tenant(self, tenant: str) -> bool:
        return tenant in self._tenants

    def backlog(self, tenant: str) -> int:
        return len(self._tenants[tenant].items)

    def deficit(self, tenant: str) -> int:
        return self._tenants[tenant].deficit

    def push(self, item: QueuedRequest) -> None:
        queue = self._tenants[item.tenant]
        self._activate_if_idle(queue)
        queue.items.append(item)

    def requeue(self, item: QueuedRequest) -> None:
        queue = self._tenants[item.tenant]
        refund = bool(queue.items)
        self._activate_if_idle(queue)
        item.attempt += 1
        queue.items.appendleft(item)
        if refund:
            queue.deficit += item.cost

    def peek(self) -> QueuedRequest | None:
        while self._ring:
            queue = self._ring[self._cursor]
            if not queue.topped_up:
                queue.deficit += queue.quantum
                queue.topped_up = True
            head = queue.items[0]
            if head.cost <= queue.deficit:
                return head
            queue.topped_up = False
            self._cursor = (self._cursor + 1) % len(self._ring)
        return None

    def pop(self) -> QueuedRequest:
        item = self.peek()
        if item is None:
            raise IndexError("pop from an empty queue")
        queue = self._ring[self._cursor]
        queue.items.popleft()
        queue.deficit -= item.cost
        if not queue.items:
            self._deactivate()
        return item

    def adjust(self, tenant: str, tokens: int) -> None:
        queue = self._tenants[tenant]
        if queue.items:
            queue.deficit += tokens

    def _activate_if_idle(self, queue: _TenantQueue) -> None:
        if not queue.items:
            self._ring.append(queue)

    def _deactivate(self) -> None:
        queue = self._ring.pop(self._cursor)
        queue.deficit = 0
        queue.topped_up = False
        if self._cursor >= len(self._ring):
            self._cursor = 0
