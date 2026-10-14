import heapq
import math
from collections import Counter
from dataclasses import dataclass

from llmgw.budgets import Budget
from llmgw.config import SchedulerConfig
from llmgw.events import EventSink, NullSink
from llmgw.fair_queue import FairQueue, QueuedRequest, RequestQueue
from llmgw.ledger import LedgerRow, LedgerSink, NullLedger
from llmgw.p0_lane import P0Lane
from llmgw.pacer import Pacer
from llmgw.types import Accepted, Dispatch, Failure, Klass, LLMRequest, Reason, Rejected, Usage
from llmgw.window import WindowAccountant

MAX_ATTEMPTS = 2
_EXHAUSTED = {"run": Reason.RUN_BUDGET_EXHAUSTED, "tenant": Reason.TENANT_BUDGET_EXHAUSTED}
_OUTCOME = {
    Reason.EXPIRED: "expired",
    Reason.PROVIDER_ERROR: "provider_error",
    Reason.RUN_BUDGET_EXHAUSTED: "budget_exhausted",
    Reason.TENANT_BUDGET_EXHAUSTED: "budget_exhausted",
}


@dataclass(frozen=True, slots=True)
class Snapshot:
    queued: dict[str, int]
    queued_tokens: dict[str, int]
    in_flight: int
    token_utilisation: float
    request_utilisation: float
    cooling_down: bool
    rejections: dict[str, int]


class Scheduler:
    """Admits requests, dispatches them fairly, and keeps the provider inside its rate window."""

    def __init__(
        self,
        config: SchedulerConfig,
        queue: RequestQueue | None = None,
        events: EventSink | None = None,
        ledger: LedgerSink | None = None,
    ) -> None:
        window = config.limits.window
        tolerance = config.pacing_burst * window
        self._config = config
        self._events = events or NullSink()
        self._ledger = ledger or NullLedger()
        self._queue = queue or FairQueue(config.tenants, config.base_quantum, config.age_p2_to_p1)
        self._p0 = P0Lane([t.name for t in config.tenants], config.p0_rate, config.p0_burst, self._demote)
        self._tokens = WindowAccountant(config.usable_tokens, window)
        self._requests = WindowAccountant(config.usable_requests, window)
        self._token_pace = Pacer(config.usable_token_rate, tolerance)
        self._request_pace = Pacer(config.usable_requests / window, tolerance)
        self._weights = {t.name: t.weight for t in config.tenants}
        self._queued_len = dict.fromkeys(self._weights, 0)
        self._queued_tokens = dict.fromkeys(self._weights, 0)
        self._tenant_budgets = {
            t.name: Budget(t.token_budget, config.budget_alert)
            for t in config.tenants
            if t.token_budget is not None
        }
        self._run_budgets: dict[str, Budget] = {}
        self._pending: dict[str, QueuedRequest] = {}
        self._in_flight: dict[str, QueuedRequest] = {}
        self._deadlines: list[tuple[float, str]] = []
        self._failures: list[Failure] = []
        self._rejections: Counter[str] = Counter()
        self._cooldown_until = 0.0

    def queued(self, tenant: str) -> int:
        return self._queued_len[tenant]

    def snapshot(self, now: float) -> Snapshot:
        return Snapshot(
            queued=dict(self._queued_len),
            queued_tokens=dict(self._queued_tokens),
            in_flight=len(self._in_flight),
            token_utilisation=self._tokens.used(now) / self._config.usable_tokens,
            request_utilisation=self._requests.used(now) / self._config.usable_requests,
            cooling_down=now < self._cooldown_until,
            rejections=dict(self._rejections),
        )

    def submit(self, req: LLMRequest, now: float) -> Accepted | Rejected:
        if req.id in self._pending:
            return Accepted(req.id, est_wait=0.0)
        if not self._queue.has_tenant(req.tenant):
            return self._reject(req, now, Reason.UNKNOWN_TENANT)
        cost = self._config.reservation_cost(req.est_input_tokens, req.max_output_tokens)
        if cost > self._config.usable_tokens:
            return self._reject(req, now, Reason.TOO_LARGE)
        if req.deadline <= now:
            return self._reject(req, now, Reason.EXPIRED)
        exhausted = self._exhausted_budget(req)
        if exhausted is not None:
            return self._reject(req, now, exhausted)

        share = self._share(req.tenant)
        est_wait = self._estimate_wait(req.tenant, cost, share)
        if self._queue_is_full(req.tenant, cost):
            return self._reject(req, now, Reason.QUEUE_FULL, retry_after=cost / (self._config.usable_token_rate * share))
        time_left = req.deadline - now
        if req.klass is not Klass.P0 and est_wait > time_left:
            return self._reject(req, now, Reason.WAIT_EXCEEDS_DEADLINE, retry_after=est_wait - time_left)

        item = QueuedRequest.of(req, cost, now)
        self._pending[req.id] = item
        self._lane_for(item).push(item)
        self._track(item)
        self._events.emit(
            "llm.queued", now, request_id=req.id, tenant=req.tenant, klass=int(req.klass),
            est_wait=round(est_wait, 3), queue_len=self._queued_len[req.tenant],
        )
        return Accepted(req.id, est_wait)

    def cancel(self, request_id: str, now: float) -> bool:
        item = self._pending.get(request_id)
        if item is None or request_id in self._in_flight:
            return False
        self._lane_for(item).remove(item)
        self._untrack(item)
        del self._pending[request_id]
        self._events.emit("llm.cancelled", now, request_id=request_id, tenant=item.tenant)
        return True

    def poll(self, now: float) -> list[Dispatch]:
        self._expire(now)
        dispatches: list[Dispatch] = []
        while now >= self._cooldown_until:
            candidate = self._next(now)
            if candidate is None:
                break
            source, item = candidate
            blocked = self._blocked_budget(item)
            if blocked is None and not self._fits(item.cost, now):
                break
            source.pop(now)
            self._untrack(item)
            if blocked is not None:
                self._fail(item, blocked, now)
                continue
            if source is self._p0:
                self._queue.adjust(item.tenant, -item.cost)
            self._reserve(item, now)
            dispatches.append(Dispatch(item.request.id, item.tenant, item.cost, item.attempt))
        return dispatches

    def take_failures(self) -> list[Failure]:
        failures, self._failures = self._failures, []
        return failures

    def on_complete(self, request_id: str, usage: Usage, now: float) -> None:
        item = self._in_flight.pop(request_id, None)
        if item is None:
            return
        del self._pending[request_id]
        overcharge = item.cost - usage.total
        self._tokens.reconcile(item.key, usage.total)
        self._token_pace.refund(overcharge)
        self._queue.adjust(item.tenant, overcharge)
        self._adjust_budgets(item, -overcharge)
        cost = self._record(item, now, "ok", usage)
        self._events.emit(
            "llm.completed", now, request_id=request_id, tenant=item.tenant,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, cost=cost,
            input_estimate_error_pct=round(100 * (usage.input_tokens / max(1, item.request.est_input_tokens) - 1), 1),
        )

    def on_provider_rejected(self, request_id: str, retry_after: float, now: float) -> None:
        item = self._in_flight.pop(request_id, None)
        if item is None:
            return
        self._tokens.release(item.key)
        self._requests.release(item.key)
        self._token_pace.refund(item.cost)
        self._request_pace.refund(1)
        self._adjust_budgets(item, -item.cost)
        self._requeue(item)
        self._cooldown_until = max(self._cooldown_until, now + retry_after)
        self._events.emit("llm.provider_429", now, request_id=request_id, retry_after=retry_after)

    def on_provider_error(self, request_id: str, kind: str, now: float) -> None:
        """The outcome is unknown, so the reservation is kept: the provider may have counted the call."""
        item = self._in_flight.pop(request_id, None)
        if item is None:
            return
        self._events.emit("llm.provider_error", now, request_id=request_id, kind=kind, attempt=item.attempt)
        if item.attempt >= MAX_ATTEMPTS:
            self._fail(item, Reason.PROVIDER_ERROR, now)
            return
        self._requeue(item)

    def next_wakeup(self, now: float) -> float | None:
        wakeups = []
        candidate = self._next(now)
        if candidate is not None:
            _, item = candidate
            ready = max(
                self._cooldown_until,
                self._token_pace.ready_at(now),
                self._request_pace.ready_at(now),
                self._tokens.next_free_time(item.cost, now),
                self._requests.next_free_time(1, now),
            )
            wakeups.append(ready)
        deadline = self._next_deadline()
        if deadline is not None:
            wakeups.append(deadline)
        future = [w for w in wakeups if now < w < math.inf]
        return min(future) if future else None

    def _share(self, tenant: str) -> float:
        others = sum(w for t, w in self._weights.items() if t != tenant and self._queued_len[t] > 0)
        return self._weights[tenant] / (others + self._weights[tenant])

    def _estimate_wait(self, tenant: str, cost: int, share: float) -> float:
        token_wait = (self._queued_tokens[tenant] + cost) / (self._config.usable_token_rate * share)
        request_rate = self._config.usable_requests / self._config.limits.window
        request_wait = (self._queued_len[tenant] + 1) / (request_rate * share)
        return max(token_wait, request_wait)

    def _queue_is_full(self, tenant: str, cost: int) -> bool:
        return (
            self._queued_len[tenant] >= self._config.max_queue_len
            or self._queued_tokens[tenant] + cost > self._config.max_queue_tokens
        )

    def _reject(
        self, req: LLMRequest, now: float, reason: Reason, retry_after: float | None = None
    ) -> Rejected:
        self._rejections[str(reason)] += 1
        self._events.emit("llm.rejected", now, request_id=req.id, tenant=req.tenant, reason=str(reason))
        return Rejected(req.id, reason, retryable=retry_after is not None, retry_after=retry_after)

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
        self._events.emit("llm.p0_demoted", now, request_id=item.request.id, tenant=item.tenant)

    def _track(self, item: QueuedRequest) -> None:
        self._queued_len[item.tenant] += 1
        self._queued_tokens[item.tenant] += item.cost
        heapq.heappush(self._deadlines, (item.request.deadline, item.request.id))

    def _untrack(self, item: QueuedRequest) -> None:
        self._queued_len[item.tenant] -= 1
        self._queued_tokens[item.tenant] -= item.cost

    def _requeue(self, item: QueuedRequest) -> None:
        self._lane_for(item).requeue(item)
        if item.klass is Klass.P0:
            self._queue.adjust(item.tenant, item.cost)
        self._track(item)

    def _fail(self, item: QueuedRequest, reason: Reason, now: float) -> None:
        request_id = item.request.id
        self._pending.pop(request_id, None)
        self._failures.append(Failure(request_id, reason))
        self._record(item, now, _OUTCOME[reason])
        self._events.emit("llm.failed", now, request_id=request_id, tenant=item.tenant, reason=str(reason))

    def _record(self, item: QueuedRequest, now: float, outcome: str, usage: Usage | None = None) -> int:
        usage = usage or Usage(0, 0)
        request = item.request
        cost = self._config.prices.price(usage)
        left_queue = item.dispatched_at if item.dispatched_at is not None else now
        self._ledger.record(
            LedgerRow(
                id=request.id, tenant=request.tenant, run=request.run, agent=request.agent,
                model=request.model, klass=int(request.klass), est_input=request.est_input_tokens,
                max_output=request.max_output_tokens, reserved=item.cost,
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens, cost=cost, queue_wait=left_queue - request.submitted_at,
                attempt=item.attempt, outcome=outcome, ts_submit=request.submitted_at,
                ts_dispatch=item.dispatched_at, ts_complete=now,
            )
        )
        return cost

    def _expire(self, now: float) -> None:
        while self._deadlines and self._deadlines[0][0] <= now:
            _, request_id = heapq.heappop(self._deadlines)
            item = self._pending.get(request_id)
            if item is None or request_id in self._in_flight:
                continue
            self._lane_for(item).remove(item)
            self._untrack(item)
            self._fail(item, Reason.EXPIRED, now)

    def _next_deadline(self) -> float | None:
        while self._deadlines:
            deadline, request_id = self._deadlines[0]
            if request_id in self._pending and request_id not in self._in_flight:
                return deadline
            heapq.heappop(self._deadlines)
        return None

    def _budgets(self, request: LLMRequest) -> list[tuple[str, Budget]]:
        budgets = []
        cap = self._config.run_token_cap
        if cap is not None:
            if request.run not in self._run_budgets:
                self._run_budgets[request.run] = Budget(cap, self._config.budget_alert)
            budgets.append(("run", self._run_budgets[request.run]))
        if request.tenant in self._tenant_budgets:
            budgets.append(("tenant", self._tenant_budgets[request.tenant]))
        return budgets

    def _exhausted_budget(self, request: LLMRequest) -> Reason | None:
        return next((_EXHAUSTED[s] for s, b in self._budgets(request) if b.exhausted), None)

    def _blocked_budget(self, item: QueuedRequest) -> Reason | None:
        return next((_EXHAUSTED[s] for s, b in self._budgets(item.request) if not b.allows(item.cost)), None)

    def _adjust_budgets(self, item: QueuedRequest, tokens: int) -> None:
        for _, budget in self._budgets(item.request):
            budget.adjust(tokens)

    def _fits(self, cost: int, now: float) -> bool:
        return (
            self._token_pace.allows(now)
            and self._request_pace.allows(now)
            and self._tokens.fits(cost, now)
            and self._requests.fits(1, now)
        )

    def _reserve(self, item: QueuedRequest, now: float) -> None:
        self._tokens.reserve(item.key, item.cost, now)
        self._requests.reserve(item.key, 1, now)
        self._token_pace.commit(item.cost, now)
        self._request_pace.commit(1, now)
        item.dispatched_at = now
        self._in_flight[item.request.id] = item
        self._events.emit(
            "llm.dispatched", now, request_id=item.request.id, tenant=item.tenant, attempt=item.attempt,
            queue_wait=round(now - item.request.submitted_at, 3), window_tokens_used=self._tokens.used(now),
        )
        for scope, budget in self._budgets(item.request):
            if budget.commit(item.cost):
                self._events.emit(
                    "llm.budget_alert", now, scope=scope, tenant=item.tenant, used=budget.used, limit=budget.limit
                )
