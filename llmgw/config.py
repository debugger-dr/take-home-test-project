import math
from dataclasses import dataclass


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

    def __post_init__(self) -> None:
        _require(bool(self.name), "tenant name must not be empty")
        _require(self.weight > 0, f"tenant {self.name!r}: weight must be positive")


@dataclass(frozen=True)
class SchedulerConfig:
    limits: ProviderLimits
    tenants: tuple[TenantConfig, ...]
    headroom: float = 0.9
    input_safety: float = 1.15
    base_quantum: int = 4_000

    def __post_init__(self) -> None:
        names = [t.name for t in self.tenants]
        _require(bool(names), "at least one tenant is required")
        _require(len(set(names)) == len(names), "tenant names must be unique")
        _require(0 < self.headroom <= 1, "headroom must be in (0, 1]")
        _require(self.input_safety >= 1, "input_safety must be at least 1")
        _require(self.base_quantum > 0, "base_quantum must be positive")

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
