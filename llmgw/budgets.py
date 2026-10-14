class Budget:
    """A token allowance that counts reservations at dispatch and is corrected to actual usage at completion."""

    def __init__(self, limit: int, alert_fraction: float) -> None:
        self._limit = limit
        self._alert_at = limit * alert_fraction
        self._used = 0
        self._alerted = False

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def used(self) -> int:
        return self._used

    @property
    def exhausted(self) -> bool:
        return self._used >= self._limit

    def allows(self, tokens: int) -> bool:
        return self._used + tokens <= self._limit

    def commit(self, tokens: int) -> bool:
        """Adds usage and returns True only the first time the alert threshold is crossed."""
        self._used += tokens
        if self._alerted or self._used < self._alert_at:
            return False
        self._alerted = True
        return True

    def adjust(self, tokens: int) -> None:
        self._used += tokens
