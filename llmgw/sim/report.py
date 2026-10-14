import math
from collections.abc import Sequence
from dataclasses import dataclass

from llmgw.sim.provider import ProviderStats
from llmgw.types import Klass, Reason


@dataclass(slots=True)
class RequestRecord:
    tenant: str
    klass: Klass
    submitted_at: float
    dispatched_at: float | None = None
    completed_at: float | None = None
    tokens: int = 0
    throttled: int = 0
    rejected: Reason | None = None
    failed: Reason | None = None
    failed_at: float | None = None
    submissions: int = 0
    faulted: int = 0
    run: str = ""


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        raise ValueError("percentile of no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True, slots=True)
class Report:
    records: dict[str, RequestRecord]
    provider: ProviderStats
    horizon: float

    def waits(self, tenant: str, start: float, end: float, klass: Klass | None = None) -> list[float]:
        """Queue wait of accepted requests submitted in [start, end); a request still waiting counts up to its failure or the horizon."""
        return [
            self._left_queue_at(r) - r.submitted_at
            for r in self._submitted(tenant, start, end, klass)
            if r.rejected is None
        ]

    def waits_when_idle(self, tenant: str, start: float, end: float) -> list[float]:
        """Waits of requests that arrived while none of the tenant's earlier requests was still queued."""
        own = sorted(
            (r for r in self.records.values() if r.tenant == tenant and r.rejected is None),
            key=lambda r: r.submitted_at,
        )
        waits: list[float] = []
        drained_at = -math.inf
        for record in own:
            left = self._left_queue_at(record)
            if start <= record.submitted_at < end and drained_at <= record.submitted_at:
                waits.append(left - record.submitted_at)
            drained_at = max(drained_at, left)
        return waits

    def served_ratio(self, tenant: str, start: float, end: float, klass: Klass | None = None) -> float:
        """Share of accepted requests that completed."""
        accepted = [r for r in self._submitted(tenant, start, end, klass) if r.rejected is None]
        return sum(r.completed_at is not None for r in accepted) / len(accepted)

    def _left_queue_at(self, record: RequestRecord) -> float:
        if record.dispatched_at is not None:
            return record.dispatched_at
        return record.failed_at if record.failed_at is not None else self.horizon

    def max_dispatch_gap(self, start: float, end: float) -> float:
        times = sorted(
            r.dispatched_at for r in self.records.values()
            if r.dispatched_at is not None and start <= r.dispatched_at < end
        )
        return max(later - earlier for earlier, later in zip(times, times[1:]))

    def completion_ratio(self, tenant: str, start: float, end: float) -> float:
        submitted = self._submitted(tenant, start, end)
        return sum(r.completed_at is not None for r in submitted) / len(submitted)

    def goodput_per_minute(self, tenant: str, start: float, end: float) -> float:
        return sum(r.tokens for r in self._completed(tenant, start, end)) * 60 / (end - start)

    def requests_per_minute(self, tenant: str, start: float, end: float) -> float:
        return sum(1 for _ in self._completed(tenant, start, end)) * 60 / (end - start)

    def _submitted(
        self, tenant: str, start: float, end: float, klass: Klass | None = None
    ) -> list[RequestRecord]:
        return [
            r for r in self.records.values()
            if r.tenant == tenant and start <= r.submitted_at < end and klass in (None, r.klass)
        ]

    def _completed(self, tenant: str, start: float, end: float):
        return (
            r for r in self.records.values()
            if r.tenant == tenant and r.completed_at is not None and start <= r.completed_at < end
        )
