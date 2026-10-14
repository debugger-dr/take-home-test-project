import math

from llmgw.config import SchedulerConfig
from llmgw.fair_queue import FairQueue, QueuedRequest, RequestQueue
from llmgw.p0_lane import P0Lane
from llmgw.pacer import Pacer
from llmgw.types import Accepted, Dispatch, Klass, LLMRequest, Reason, Rejected, Usage
from llmgw.window import WindowAccountant


class Scheduler:
    """Admits requests, dispatches them fairly, and keeps the provider inside its rate window."""

    def __init__(self, config: SchedulerConfig, queue: RequestQueue | None = None) -> None:
        window = config.limits.window
        tolerance = config.pacing_burst * window
        self._config = config
        self._queue = queue or FairQueue(config.tenants, config.base_quantum, config.age_p2_to_p1)
        self._p0 = P0Lane([t.name for t in config.tenants], config.p0_rate, config.p0_burst, self._demote)
        self._tokens = WindowAccountant(config.usable_tokens, window)
        self._requests = WindowAccountant(config.usable_requests, window)
        self._token_pace = Pacer(config.usable_token_rate, tolerance)
        self._request_pace = Pacer(config.usable_requests / window, tolerance)
        self._pending: dict[str, QueuedRequest] = {}
        self._in_flight: dict[str, QueuedRequest] = {}
        self._cooldown_until = 0.0

    def submit(self, req: LLMRequest, now: float) -> Accepted | Rejected:
        if req.id in self._pending:
            return Accepted(req.id, est_wait=0.0)
        if not self._queue.has_tenant(req.tenant):
            return Rejected(req.id, Reason.UNKNOWN_TENANT, retryable=False)
        cost = self._config.reservation_cost(req.est_input_tokens, req.max_output_tokens)
        if cost > self._config.usable_tokens:
            return Rejected(req.id, Reason.TOO_LARGE, retryable=False)

        item = QueuedRequest.of(req, cost, now)
        self._pending[req.id] = item
        self._lane_for(item).push(item)
        return Accepted(req.id, est_wait=0.0)

    def poll(self, now: float) -> list[Dispatch]:
        dispatches: list[Dispatch] = []
        while now >= self._cooldown_until:
            candidate = self._next(now)
            if candidate is None:
                break
            source, item = candidate
            if not self._fits(item.cost, now):
                break
            source.pop(now)
            if source is self._p0:
                self._queue.adjust(item.tenant, -item.cost)
            self._reserve(item, now)
            dispatches.append(Dispatch(item.request.id, item.tenant, item.cost, item.attempt))
        return dispatches

    def on_complete(self, request_id: str, usage: Usage, now: float) -> None:
        item = self._in_flight.pop(request_id, None)
        if item is None:
            return
        del self._pending[request_id]
        overcharge = item.cost - usage.total
        self._tokens.reconcile(request_id, usage.total)
        self._token_pace.refund(overcharge)
        self._queue.adjust(item.tenant, overcharge)

    def on_provider_rejected(self, request_id: str, retry_after: float, now: float) -> None:
        item = self._in_flight.pop(request_id, None)
        if item is None:
            return
        self._tokens.release(request_id)
        self._requests.release(request_id)
        self._token_pace.refund(item.cost)
        self._request_pace.refund(1)
        self._lane_for(item).requeue(item)
        if item.klass is Klass.P0:
            self._queue.adjust(item.tenant, item.cost)
        self._cooldown_until = max(self._cooldown_until, now + retry_after)

    def next_wakeup(self, now: float) -> float | None:
        candidate = self._next(now)
        if candidate is None:
            return None
        _, item = candidate
        ready = max(
            self._cooldown_until,
            self._token_pace.ready_at(now),
            self._request_pace.ready_at(now),
            self._tokens.next_free_time(item.cost, now),
            self._requests.next_free_time(1, now),
        )
        return ready if now < ready < math.inf else None

    def _next(self, now: float) -> tuple[P0Lane | RequestQueue, QueuedRequest] | None:
        for source in (self._p0, self._queue):
            item = source.peek(now)
            if item is not None:
                return source, item
        return None

    def _lane_for(self, item: QueuedRequest) -> P0Lane | RequestQueue:
        return self._p0 if item.klass is Klass.P0 else self._queue

    def _demote(self, item: QueuedRequest, now: float) -> None:
        item.klass = Klass.P1
        self._queue.push(item)

    def _fits(self, cost: int, now: float) -> bool:
        return (
            self._token_pace.allows(now)
            and self._request_pace.allows(now)
            and self._tokens.fits(cost, now)
            and self._requests.fits(1, now)
        )

    def _reserve(self, item: QueuedRequest, now: float) -> None:
        request_id = item.request.id
        self._tokens.reserve(request_id, item.cost, now)
        self._requests.reserve(request_id, 1, now)
        self._token_pace.commit(item.cost, now)
        self._request_pace.commit(1, now)
        self._in_flight[request_id] = item
