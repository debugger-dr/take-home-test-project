import random
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from llmgw.config import ProviderLimits
from llmgw.types import Usage


class CountingMode(StrEnum):
    RESERVE_AT_START = "reserve_at_start"
    COUNT_AT_COMPLETION = "count_at_completion"


class WindowMode(StrEnum):
    ROLLING = "rolling"
    FIXED = "fixed"


@dataclass(frozen=True)
class ProviderModel:
    limits: ProviderLimits
    counting: CountingMode = CountingMode.RESERVE_AT_START
    window_mode: WindowMode = WindowMode.ROLLING
    base_latency: float = 0.5
    seconds_per_output_token: float = 0.01
    latency_jitter: float = 0.0
    input_ratio: tuple[float, float] = (0.85, 1.15)
    output_ratio: tuple[float, float] = (0.3, 1.0)


@dataclass(frozen=True, slots=True)
class ProviderCall:
    id: str
    est_input_tokens: int
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class Throttled:
    retry_after: float


@dataclass(frozen=True, slots=True)
class Started:
    id: str
    finish_at: float
    usage: Usage


@dataclass(frozen=True, slots=True)
class ProviderStats:
    accepted: int
    throttled: int
    total_input_tokens: int
    total_output_tokens: int
    peak_tokens: int
    peak_requests: int

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def throttle_rate(self) -> float:
        attempts = self.accepted + self.throttled
        return self.throttled / attempts if attempts else 0.0


class _Counter(Protocol):
    peak: int

    def total(self, now: float) -> int: ...
    def add(self, key: str, amount: int, now: float) -> None: ...
    def adjust(self, key: str, amount: int) -> None: ...
    def retry_after(self, needed: int, limit: int, now: float) -> float: ...


class _RollingCounter:
    def __init__(self, window: float) -> None:
        self._window = window
        self._events: deque[tuple[float, str]] = deque()
        self._amounts: dict[str, int] = {}
        self._total = 0
        self.peak = 0

    def total(self, now: float) -> int:
        cutoff = now - self._window
        while self._events and self._events[0][0] <= cutoff:
            _, key = self._events.popleft()
            self._total -= self._amounts.pop(key, 0)
        return self._total

    def add(self, key: str, amount: int, now: float) -> None:
        self.total(now)
        self._events.append((now, key))
        self._amounts[key] = amount
        self._total += amount
        self.peak = max(self.peak, self._total)

    def adjust(self, key: str, amount: int) -> None:
        if key in self._amounts:
            self._total += amount - self._amounts[key]
            self._amounts[key] = amount
            self.peak = max(self.peak, self._total)

    def retry_after(self, needed: int, limit: int, now: float) -> float:
        remaining = self.total(now)
        for at, key in self._events:
            remaining -= self._amounts[key]
            if remaining + needed <= limit:
                return at + self._window - now
        return self._window


class _FixedCounter:
    def __init__(self, window: float) -> None:
        self._window = window
        self._index = -1
        self._amounts: dict[str, int] = {}
        self._total = 0
        self.peak = 0

    def total(self, now: float) -> int:
        self._roll(now)
        return self._total

    def add(self, key: str, amount: int, now: float) -> None:
        self._roll(now)
        self._amounts[key] = amount
        self._total += amount
        self.peak = max(self.peak, self._total)

    def adjust(self, key: str, amount: int) -> None:
        if key in self._amounts:
            self._total += amount - self._amounts[key]
            self._amounts[key] = amount
            self.peak = max(self.peak, self._total)

    def retry_after(self, needed: int, limit: int, now: float) -> float:
        self._roll(now)
        return (self._index + 1) * self._window - now

    def _roll(self, now: float) -> None:
        index = int(now // self._window)
        if index != self._index:
            self._index = index
            self._amounts.clear()
            self._total = 0


class FakeProvider:
    """Ground-truth provider. It must never depend on the scheduler under test."""

    def __init__(self, model: ProviderModel, rng: random.Random) -> None:
        counter = _RollingCounter if model.window_mode is WindowMode.ROLLING else _FixedCounter
        self._model = model
        self._rng = rng
        self._tokens: _Counter = counter(model.limits.window)
        self._requests: _Counter = counter(model.limits.window)
        self._in_flight: dict[str, Usage] = {}
        self._accepted = 0
        self._throttled = 0
        self._total_input = 0
        self._total_output = 0

    @property
    def stats(self) -> ProviderStats:
        return ProviderStats(
            accepted=self._accepted,
            throttled=self._throttled,
            total_input_tokens=self._total_input,
            total_output_tokens=self._total_output,
            peak_tokens=self._tokens.peak,
            peak_requests=self._requests.peak,
        )

    def send(self, call: ProviderCall, now: float) -> Started | Throttled:
        usage = self._draw_usage(call)
        reserved = self._reserved_tokens(call, usage)
        limits = self._model.limits
        token_blocked = self._tokens.total(now) + reserved > limits.tpm
        request_blocked = self._requests.total(now) + 1 > limits.rpm
        if token_blocked or request_blocked:
            self._throttled += 1
            waits = [0.0]
            if token_blocked:
                waits.append(self._tokens.retry_after(reserved, limits.tpm, now))
            if request_blocked:
                waits.append(self._requests.retry_after(1, limits.rpm, now))
            return Throttled(max(waits))

        if reserved:
            self._tokens.add(call.id, reserved, now)
        self._requests.add(call.id, 1, now)
        self._in_flight[call.id] = usage
        self._accepted += 1
        return Started(call.id, now + self._latency(usage), usage)

    def finish(self, request_id: str, now: float) -> Usage:
        usage = self._in_flight.pop(request_id)
        if self._model.counting is CountingMode.RESERVE_AT_START:
            self._tokens.adjust(request_id, usage.total)
        else:
            self._tokens.add(request_id, usage.total, now)
        self._total_input += usage.input_tokens
        self._total_output += usage.output_tokens
        return usage

    def _reserved_tokens(self, call: ProviderCall, usage: Usage) -> int:
        if self._model.counting is CountingMode.RESERVE_AT_START:
            return usage.input_tokens + call.max_output_tokens
        return 0

    def _draw_usage(self, call: ProviderCall) -> Usage:
        low, high = self._model.input_ratio
        input_tokens = max(1, round(call.est_input_tokens * self._rng.uniform(low, high)))
        low, high = self._model.output_ratio
        output_tokens = round(call.max_output_tokens * self._rng.uniform(low, high))
        return Usage(input_tokens, min(max(1, output_tokens), call.max_output_tokens))

    def _latency(self, usage: Usage) -> float:
        model = self._model
        return (
            model.base_latency
            + model.seconds_per_output_token * usage.output_tokens
            + self._rng.uniform(0, model.latency_jitter)
        )
