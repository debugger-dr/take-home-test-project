class Pacer:
    """Generic cell rate algorithm: spaces dispatches evenly so the rate window never alternates between bursts and stalls."""

    def __init__(self, rate: float, tolerance: float) -> None:
        if rate <= 0 or tolerance < 0:
            raise ValueError("rate must be positive and tolerance must not be negative")
        self._rate = rate
        self._tolerance = tolerance
        self._next = 0.0

    def allows(self, now: float) -> bool:
        return now >= self._next - self._tolerance

    def ready_at(self, now: float) -> float:
        return max(now, self._next - self._tolerance)

    def commit(self, amount: float, now: float) -> None:
        self._next = max(self._next, now) + amount / self._rate

    def refund(self, amount: float) -> None:
        self._next -= amount / self._rate
