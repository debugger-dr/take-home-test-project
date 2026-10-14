import math
from dataclasses import dataclass

from llmgw.types import Usage


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True)
class ProviderLimits:
    rpm: int
    tpm: int
    window: float = 60.0

    def __post_init__(self) -> None:
        _require(self.rpm > 0, "rpm must be positive")
        _require(self.tpm > 0, "tpm must be positive")
        _require(self.window > 0, "window must be positive")


@dataclass(frozen=True)
class TenantConfig:
    name: str
    weight: int = 1
    token_budget: int | None = None

    def __post_init__(self) -> None:
        _require(bool(self.name), "tenant name must not be empty")
        _require(self.weight > 0, f"tenant {self.name!r}: weight must be positive")
        _require(
            self.token_budget is None or self.token_budget > 0,
            f"tenant {self.name!r}: token_budget must be positive",
        )


@dataclass(frozen=True)
class PriceTable:
    """Illustrative prices in micro-dollars per token; integers keep every total exact."""

    input_micros: int = 3
    output_micros: int = 15

    def __post_init__(self) -> None:
        _require(self.input_micros >= 0 and self.output_micros >= 0, "prices must not be negative")

    def price(self, usage: Usage) -> int:
        return usage.input_tokens * self.input_micros + usage.output_tokens * self.output_micros


@dataclass(frozen=True)
class SchedulerConfig:
    limits: ProviderLimits
    tenants: tuple[TenantConfig, ...]
    headroom: float = 0.9
    input_safety: float = 1.15
    base_quantum: int = 4_000
    pacing_burst: float = 0.05
    age_p2_to_p1: float = 30.0
    p0_rate: float = 300.0
    p0_burst: int = 8_000
    max_queue_len: int = 200
    max_queue_tokens: int = 1_000_000
    run_token_cap: int | None = None
    budget_alert: float = 0.8
    prices: PriceTable = PriceTable()

    def __post_init__(self) -> None:
        names = [t.name for t in self.tenants]
        _require(bool(names), "at least one tenant is required")
        _require(len(set(names)) == len(names), "tenant names must be unique")
        _require(0 < self.headroom <= 1, "headroom must be in (0, 1]")
        _require(self.input_safety >= 1, "input_safety must be at least 1")
        _require(self.base_quantum > 0, "base_quantum must be positive")
        _require(0 <= self.pacing_burst <= 1, "pacing_burst must be in [0, 1]")
        _require(self.age_p2_to_p1 > 0, "age_p2_to_p1 must be positive")
        _require(self.p0_rate > 0 and self.p0_burst > 0, "p0_rate and p0_burst must be positive")
        _require(self.max_queue_len > 0 and self.max_queue_tokens > 0, "queue bounds must be positive")
        _require(self.run_token_cap is None or self.run_token_cap > 0, "run_token_cap must be positive")
        _require(0 < self.budget_alert <= 1, "budget_alert must be in (0, 1]")

    @property
    def usable_tokens(self) -> int:
        return int(self.limits.tpm * self.headroom)

    @property
    def usable_requests(self) -> int:
        return int(self.limits.rpm * self.headroom)

    @property
    def usable_token_rate(self) -> float:
        return self.usable_tokens / self.limits.window

    def reservation_cost(self, est_input_tokens: int, max_output_tokens: int) -> int:
        return math.ceil(est_input_tokens * self.input_safety) + max_output_tokens
