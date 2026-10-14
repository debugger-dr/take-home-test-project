import math
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(slots=True)
class _Entry:
    at: float
    amount: int


class WindowAccountant:
    """Sliding-window ledger of reservations that mirrors the provider's rate window."""

    def __init__(self, capacity: int, window: float) -> None:
        if capacity <= 0 or window <= 0:
            raise ValueError("capacity and window must be positive")
        self._capacity = capacity
        self._window = window
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._used = 0
        self._last_at = -math.inf

    def used(self, now: float) -> int:
        self._expire(now)
        return self._used

    def fits(self, amount: int, now: float) -> bool:
        self._expire(now)
        return self._used + amount <= self._capacity

    def reserve(self, key: str, amount: int, now: float) -> None:
        if key in self._entries:
            raise ValueError(f"duplicate reservation {key!r}")
        if now < self._last_at:
            raise ValueError("time must not go backwards")
        self._expire(now)
        self._entries[key] = _Entry(now, amount)
        self._used += amount
        self._last_at = now

    def reconcile(self, key: str, actual: int) -> bool:
        entry = self._entries.get(key)
        if entry is None:
            return False
        self._used += actual - entry.amount
        entry.amount = actual
        return True

    def release(self, key: str) -> bool:
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        self._used -= entry.amount
        return True

    def next_free_time(self, amount: int, now: float) -> float:
        self._expire(now)
        if amount > self._capacity:
            return math.inf
        remaining = self._used
        if remaining + amount <= self._capacity:
            return now
        for entry in self._entries.values():
            remaining -= entry.amount
            if remaining + amount <= self._capacity:
                return entry.at + self._window
        return math.inf

    def _expire(self, now: float) -> None:
        cutoff = now - self._window
        while self._entries:
            oldest = next(iter(self._entries.values()))
            if oldest.at > cutoff:
                return
            self._entries.popitem(last=False)
            self._used -= oldest.amount
