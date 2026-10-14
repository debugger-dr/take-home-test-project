import math

from llmgw.config import SchedulerConfig
from llmgw.fair_queue import FairQueue, QueuedRequest, RequestQueue
from llmgw.pacer import Pacer
from llmgw.types import Accepted, Dispatch, LLMRequest, Reason, Rejected, Usage
from llmgw.window import WindowAccountant


class Scheduler:
    """Admits requests, dispatches them fairly, and keeps the provider inside its rate window."""

    def __init__(self, config: SchedulerConfig, queue: RequestQueue | None = None) -> None:
        window = config.limits.window
        tolerance = config.pacing_burst * window
        self._config = config
        self._queue = queue or FairQueue(config.tenants, config.base_quantum)
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

        item = QueuedRequest(req, cost)
        self._pending[req.id] = item
        self._queue.push(item)
        return Accepted(req.id, est_wait=0.0)

    def poll(self, now: float) -> list[Dispatch]:
        dispatches: list[Dispatch] = []
        while now >= self._cooldown_until:
            item = self._queue.peek()
            if item is None or not self._fits(item.cost, now):
                break
            self._queue.pop()
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
        self._queue.requeue(item)
        self._cooldown_until = max(self._cooldown_until, now + retry_after)

    def next_wakeup(self, now: float) -> float | None:
        item = self._queue.peek()
        if item is None:
            return None
        ready = max(
            self._cooldown_until,
            self._token_pace.ready_at(now),
            self._request_pace.ready_at(now),
            self._tokens.next_free_time(item.cost, now),
            self._requests.next_free_time(1, now),
        )
        return ready if now < ready < math.inf else None

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
