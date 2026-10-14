import heapq
import itertools
import math
from collections import deque
from collections.abc import Iterable
from dataclasses import replace

from llmgw.config import SchedulerConfig
from llmgw.fair_queue import QueuedRequest
from llmgw.scheduler import Scheduler
from llmgw.types import Accepted, Dispatch, Failure, LLMRequest, Rejected, Usage


class PassThroughScheduler:
    """Negative control: dispatches everything immediately with no quota check and no fairness."""

    def __init__(self) -> None:
        self._queue: deque[LLMRequest] = deque()

    def submit(self, req: LLMRequest, now: float) -> Accepted | Rejected:
        self._queue.append(req)
        return Accepted(req.id, 0.0)

    def poll(self, now: float) -> list[Dispatch]:
        dispatches = [
            Dispatch(r.id, r.tenant, r.est_input_tokens + r.max_output_tokens, attempt=1)
            for r in self._queue
        ]
        self._queue.clear()
        return dispatches

    def take_failures(self) -> list[Failure]:
        return []

    def on_complete(self, request_id: str, usage: Usage, now: float) -> None:
        pass

    def on_provider_rejected(self, request_id: str, retry_after: float, now: float) -> None:
        pass

    def on_provider_error(self, request_id: str, kind: str, now: float) -> None:
        pass

    def next_wakeup(self, now: float) -> float | None:
        return None


class FifoQueue:
    """Negative control: one global first-in-first-out line, no per-tenant fairness."""

    def __init__(self, tenants: Iterable[str]) -> None:
        self._tenants = frozenset(tenants)
        self._items: deque[QueuedRequest] = deque()

    def has_tenant(self, tenant: str) -> bool:
        return tenant in self._tenants

    def push(self, item: QueuedRequest) -> None:
        self._items.append(item)

    def requeue(self, item: QueuedRequest) -> None:
        item.attempt += 1
        self._items.appendleft(item)

    def peek(self, now: float) -> QueuedRequest | None:
        return self._items[0] if self._items else None

    def pop(self, now: float) -> QueuedRequest:
        return self._items.popleft()

    def remove(self, item: QueuedRequest) -> bool:
        try:
            self._items.remove(item)
        except ValueError:
            return False
        return True

    def adjust(self, tenant: str, tokens: int) -> None:
        pass


class SmallestFirstQueue:
    """Negative control: always serves the cheapest request, which starves large ones indefinitely."""

    def __init__(self, tenants: Iterable[str]) -> None:
        self._tenants = frozenset(tenants)
        self._heap: list[tuple[int, int, QueuedRequest]] = []
        self._sequence = itertools.count()

    def has_tenant(self, tenant: str) -> bool:
        return tenant in self._tenants

    def push(self, item: QueuedRequest) -> None:
        heapq.heappush(self._heap, (item.cost, next(self._sequence), item))

    def requeue(self, item: QueuedRequest) -> None:
        item.attempt += 1
        self.push(item)

    def peek(self, now: float) -> QueuedRequest | None:
        return self._heap[0][2] if self._heap else None

    def pop(self, now: float) -> QueuedRequest:
        return heapq.heappop(self._heap)[2]

    def remove(self, item: QueuedRequest) -> bool:
        remaining = [entry for entry in self._heap if entry[2] is not item]
        if len(remaining) == len(self._heap):
            return False
        heapq.heapify(remaining)
        self._heap = remaining
        return True

    def adjust(self, tenant: str, tokens: int) -> None:
        pass


class FifoScheduler(Scheduler):
    """Negative control: one global line with no per-tenant bounds and no wait estimates."""

    def __init__(self, config: SchedulerConfig) -> None:
        unbounded = replace(config, max_queue_len=10**9, max_queue_tokens=10**12)
        super().__init__(unbounded, FifoQueue(t.name for t in config.tenants))

    def _share(self, tenant: str) -> float:
        return math.inf


def fifo_scheduler(config: SchedulerConfig) -> Scheduler:
    return FifoScheduler(config)


def smallest_first_scheduler(config: SchedulerConfig) -> Scheduler:
    return Scheduler(config, SmallestFirstQueue(t.name for t in config.tenants))


def no_p0_allowance_scheduler(config: SchedulerConfig) -> Scheduler:
    """Negative control: P0 traffic is not capped, so a flood of it starves everything else."""
    return Scheduler(replace(config, p0_rate=1e12, p0_burst=10**12))


def unpaced_scheduler(config: SchedulerConfig) -> Scheduler:
    """Negative control: safe against the window, but dispatches in bursts followed by stalls."""
    return Scheduler(replace(config, pacing_burst=1.0))


def no_aging_scheduler(config: SchedulerConfig) -> Scheduler:
    """Negative control: P2 requests are never promoted, so constant P1 load starves them."""
    return Scheduler(replace(config, age_p2_to_p1=10**9))


class NoQuotaScheduler(Scheduler):
    """Negative control: fair dispatch, but ignores the provider's rate window."""

    def _fits(self, cost: int, now: float) -> bool:
        return True


class ReservationBillingScheduler(Scheduler):
    """Negative control: bills what it reserved instead of what the provider reports was used."""

    def on_complete(self, request_id: str, usage: Usage, now: float) -> None:
        item = self._in_flight.get(request_id)
        super().on_complete(request_id, Usage(item.cost, 0) if item is not None else usage, now)


class NoCooldownScheduler(Scheduler):
    """Negative control: keeps dispatching straight into a provider that is refusing everything."""

    def on_provider_rejected(self, request_id: str, retry_after: float, now: float) -> None:
        super().on_provider_rejected(request_id, 0.0, now)


class ReleaseOnErrorScheduler(Scheduler):
    """Negative control: treats a call with an unknown outcome as never sent, in the window and in the pacer."""

    def on_provider_error(self, request_id: str, kind: str, now: float) -> None:
        item = self._in_flight.get(request_id)
        if item is not None:
            self._tokens.release(item.key)
            self._requests.release(item.key)
            self._token_pace.refund(item.cost)
            self._request_pace.refund(1)
        super().on_provider_error(request_id, kind, now)
