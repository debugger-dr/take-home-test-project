from collections import deque
from collections.abc import Iterable
from dataclasses import replace

from llmgw.config import SchedulerConfig
from llmgw.fair_queue import QueuedRequest
from llmgw.scheduler import Scheduler
from llmgw.types import Accepted, Dispatch, LLMRequest, Rejected, Usage


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

    def on_complete(self, request_id: str, usage: Usage, now: float) -> None:
        pass

    def on_provider_rejected(self, request_id: str, retry_after: float, now: float) -> None:
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

    def adjust(self, tenant: str, tokens: int) -> None:
        pass


def fifo_scheduler(config: SchedulerConfig) -> Scheduler:
    return Scheduler(config, FifoQueue(t.name for t in config.tenants))


def no_p0_allowance_scheduler(config: SchedulerConfig) -> Scheduler:
    """Negative control: P0 traffic is not capped, so a flood of it starves everything else."""
    return Scheduler(replace(config, p0_rate=1e12, p0_burst=10**12))


def unpaced_scheduler(config: SchedulerConfig) -> Scheduler:
    """Negative control: safe against the window, but dispatches in bursts followed by stalls."""
    return Scheduler(replace(config, pacing_burst=1.0))


class NoQuotaScheduler(Scheduler):
    """Negative control: fair dispatch, but ignores the provider's rate window."""

    def _fits(self, cost: int, now: float) -> bool:
        return True
