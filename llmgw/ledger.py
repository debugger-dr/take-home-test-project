import sqlite3
from dataclasses import dataclass, fields
from typing import Protocol

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL, run TEXT NOT NULL, agent TEXT NOT NULL, model TEXT NOT NULL,
    klass INTEGER NOT NULL,
    est_input INTEGER NOT NULL, max_output INTEGER NOT NULL, reserved INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cached_tokens INTEGER NOT NULL,
    cost INTEGER NOT NULL, queue_wait REAL NOT NULL, attempt INTEGER NOT NULL, outcome TEXT NOT NULL,
    ts_submit REAL NOT NULL, ts_dispatch REAL, ts_complete REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_tenant ON llm_calls (tenant, ts_complete);
CREATE INDEX IF NOT EXISTS idx_llm_agent ON llm_calls (agent, ts_complete);
"""
_SUMS = "COALESCE(SUM(input_tokens + output_tokens), 0), COALESCE(SUM(cost), 0), COUNT(*)"
_GROUPABLE = frozenset({"tenant", "run", "agent", "model"})


@dataclass(frozen=True, slots=True)
class LedgerRow:
    id: str
    tenant: str
    run: str
    agent: str
    model: str
    klass: int
    est_input: int
    max_output: int
    reserved: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost: int
    queue_wait: float
    attempt: int
    outcome: str
    ts_submit: float
    ts_dispatch: float | None
    ts_complete: float


_COLUMNS = tuple(f.name for f in fields(LedgerRow))
_INSERT = f"INSERT OR REPLACE INTO llm_calls ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})"


@dataclass(frozen=True, slots=True)
class Spend:
    tokens: int
    cost: int
    requests: int


class LedgerSink(Protocol):
    def record(self, row: LedgerRow) -> None: ...


class NullLedger:
    def record(self, row: LedgerRow) -> None:
        pass


class Ledger:
    """One row per request, written when the request reaches its final outcome."""

    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)

    def record(self, row: LedgerRow) -> None:
        self._db.execute(_INSERT, [getattr(row, name) for name in _COLUMNS])

    def total(self) -> Spend:
        return self.spend()

    def spend(self, **filters: str) -> Spend:
        for column in filters:
            self._check(column)
        where = " WHERE " + " AND ".join(f"{c} = ?" for c in filters) if filters else ""
        tokens, cost, requests = self._db.execute(
            f"SELECT {_SUMS} FROM llm_calls{where}", list(filters.values())
        ).fetchone()
        return Spend(tokens, cost, requests)

    def by(self, column: str) -> dict[str, Spend]:
        self._check(column)
        rows = self._db.execute(f"SELECT {column}, {_SUMS} FROM llm_calls GROUP BY {column}").fetchall()
        return {key: Spend(tokens, cost, requests) for key, tokens, cost, requests in rows}

    def outcomes(self) -> dict[str, int]:
        rows = self._db.execute("SELECT outcome, COUNT(*) FROM llm_calls GROUP BY outcome").fetchall()
        return dict(rows)

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _check(column: str) -> None:
        if column not in _GROUPABLE:
            raise ValueError(f"cannot group or filter by {column!r}")
