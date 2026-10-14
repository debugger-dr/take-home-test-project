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


@dataclass(frozen=True, slots=True)
class Report:
    records: dict[str, RequestRecord]
    provider: ProviderStats
