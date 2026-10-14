import heapq
import itertools
from collections.abc import Sequence
from enum import IntEnum
from typing import Protocol

from llmgw.sim.provider import FakeProvider, ProviderCall, Started, Throttled
from llmgw.sim.report import Report, RequestRecord
from llmgw.types import Accepted, Dispatch, LLMRequest, Rejected, Usage


class SchedulerPort(Protocol):
    def submit(self, req: LLMRequest, now: float) -> Accepted | Rejected: ...
    def poll(self, now: float) -> list[Dispatch]: ...
    def on_complete(self, request_id: str, usage: Usage, now: float) -> None: ...
    def on_provider_rejected(self, request_id: str, retry_after: float, now: float) -> None: ...
    def next_wakeup(self, now: float) -> float | None: ...


class _Kind(IntEnum):
    ARRIVAL = 0
    COMPLETE = 1
    WAKEUP = 2


class Simulator:
    """Discrete-event driver: time only advances to the next event, so a long scenario runs instantly."""

    def __init__(self, scheduler: SchedulerPort, provider: FakeProvider) -> None:
        self._scheduler = scheduler
        self._provider = provider
        self._events: list[tuple[float, int, _Kind, str]] = []
        self._sequence = itertools.count()
        self._requests: dict[str, LLMRequest] = {}
        self._records: dict[str, RequestRecord] = {}
        self._wakeup_at: float | None = None

    def run(self, requests: Sequence[LLMRequest], until: float) -> Report:
        self._requests = {r.id: r for r in requests}
        for request in requests:
            self._push(request.submitted_at, _Kind.ARRIVAL, request.id)

        while self._events:
            now, _, kind, key = heapq.heappop(self._events)
            if now > until:
                break
            if kind is _Kind.ARRIVAL:
                self._arrive(self._requests[key], now)
            elif kind is _Kind.COMPLETE:
                self._complete(key, now)
            self._dispatch_ready(now)
        return Report(self._records, self._provider.stats)

    def _push(self, at: float, kind: _Kind, key: str) -> None:
        heapq.heappush(self._events, (at, next(self._sequence), kind, key))

    def _arrive(self, request: LLMRequest, now: float) -> None:
        record = RequestRecord(request.tenant, request.klass, now)
        self._records[request.id] = record
        result = self._scheduler.submit(request, now)
        if isinstance(result, Rejected):
            record.rejected = result.reason

    def _complete(self, request_id: str, now: float) -> None:
        usage = self._provider.finish(request_id, now)
        record = self._records[request_id]
        record.completed_at = now
        record.tokens = usage.total
        self._scheduler.on_complete(request_id, usage, now)

    def _dispatch_ready(self, now: float) -> None:
        for dispatch in self._scheduler.poll(now):
            self._send(dispatch, now)
        wakeup = self._scheduler.next_wakeup(now)
        if wakeup is not None and wakeup > now and wakeup != self._wakeup_at:
            self._wakeup_at = wakeup
            self._push(wakeup, _Kind.WAKEUP, "")

    def _send(self, dispatch: Dispatch, now: float) -> None:
        request = self._requests[dispatch.id]
        record = self._records[dispatch.id]
        if record.dispatched_at is None:
            record.dispatched_at = now
        call = ProviderCall(request.id, request.est_input_tokens, request.max_output_tokens)
        outcome = self._provider.send(call, now)
        if isinstance(outcome, Throttled):
            record.throttled += 1
            self._scheduler.on_provider_rejected(request.id, outcome.retry_after, now)
        elif isinstance(outcome, Started):
            self._push(outcome.finish_at, _Kind.COMPLETE, request.id)
