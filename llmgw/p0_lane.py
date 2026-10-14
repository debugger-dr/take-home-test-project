from collections import deque
from collections.abc import Callable, Sequence

from llmgw.fair_queue import QueuedRequest
from llmgw.pacer import Pacer


class P0Lane:
    """Human-blocking requests, served ahead of everything else but only within each tenant's own allowance."""

    def __init__(
        self,
        tenants: Sequence[str],
        rate: float,
        burst_tokens: float,
        on_demote: Callable[[QueuedRequest, float], None],
    ) -> None:
        tolerance = burst_tokens / rate
        self._names = list(tenants)
        self._items: dict[str, deque[QueuedRequest]] = {name: deque() for name in tenants}
        self._allowance = {name: Pacer(rate, tolerance) for name in tenants}
        self._on_demote = on_demote
        self._cursor = 0

    def push(self, item: QueuedRequest) -> None:
        self._items[item.tenant].append(item)

    def requeue(self, item: QueuedRequest) -> None:
        item.attempt += 1
        self._items[item.tenant].appendleft(item)

    def remove(self, item: QueuedRequest) -> bool:
        try:
            self._items[item.tenant].remove(item)
        except ValueError:
            return False
        return True

    def peek(self, now: float) -> QueuedRequest | None:
        for name in self._rotation():
            items = self._items[name]
            while items:
                if self._allowance[name].allows(now):
                    return items[0]
                self._on_demote(items.popleft(), now)
        return None

    def pop(self, now: float) -> QueuedRequest:
        item = self.peek(now)
        if item is None:
            raise IndexError("pop from an empty lane")
        self._items[item.tenant].popleft()
        self._allowance[item.tenant].commit(item.cost, now)
        self._cursor = (self._names.index(item.tenant) + 1) % len(self._names)
        return item

    def _rotation(self) -> list[str]:
        return self._names[self._cursor:] + self._names[:self._cursor]
