from dataclasses import dataclass
from enum import IntEnum, StrEnum


class Klass(IntEnum):
    P0 = 0
    P1 = 1
    P2 = 2


class Reason(StrEnum):
    UNKNOWN_TENANT = "unknown_tenant"
    TOO_LARGE = "too_large"
    RUN_BUDGET_EXHAUSTED = "run_budget_exhausted"
    TENANT_BUDGET_EXHAUSTED = "tenant_budget_exhausted"
    QUEUE_FULL = "queue_full"
    WAIT_EXCEEDS_DEADLINE = "wait_exceeds_deadline"
    EXPIRED = "expired"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True, slots=True)
class RunState:
    human_waiting: bool
    interactive: bool


def derive_class(state: RunState) -> Klass:
    if state.human_waiting:
        return Klass.P0
    return Klass.P1 if state.interactive else Klass.P2


@dataclass(frozen=True, slots=True)
class LLMRequest:
    id: str
    tenant: str
    run: str
    agent: str
    model: str
    klass: Klass
    est_input_tokens: int
    max_output_tokens: int
    deadline: float
    submitted_at: float


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class Accepted:
    id: str
    est_wait: float


@dataclass(frozen=True, slots=True)
class Rejected:
    id: str
    reason: Reason
    retryable: bool
    retry_after: float | None = None


@dataclass(frozen=True, slots=True)
class Failure:
    id: str
    reason: Reason


@dataclass(frozen=True, slots=True)
class Dispatch:
    id: str
    tenant: str
    reserved_tokens: int
    attempt: int
