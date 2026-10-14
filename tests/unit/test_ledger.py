import pytest

from llmgw.ledger import Ledger, LedgerRow, NullLedger, Spend


def row(id="r1", tenant="a", agent="a-agent", run="a-run", input_tokens=100, output_tokens=50, cost=1_050, outcome="ok"):
    return LedgerRow(
        id=id, tenant=tenant, run=run, agent=agent, model="m", klass=1, est_input=90, max_output=80,
        reserved=200, input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=0, cost=cost,
        queue_wait=1.5, attempt=1, outcome=outcome, ts_submit=0.0, ts_dispatch=1.5, ts_complete=4.0,
    )


@pytest.fixture
def ledger() -> Ledger:
    return Ledger()


def test_empty_ledger_has_zero_spend(ledger):
    assert ledger.total() == Spend(0, 0, 0)


def test_totals_sum_tokens_cost_and_requests(ledger):
    ledger.record(row("r1"))
    ledger.record(row("r2", input_tokens=10, output_tokens=5, cost=105))
    assert ledger.total() == Spend(tokens=165, cost=1_155, requests=2)


def test_recording_the_same_request_twice_keeps_one_row(ledger):
    ledger.record(row("r1", output_tokens=50, cost=1_050))
    ledger.record(row("r1", output_tokens=70, cost=1_350))
    assert ledger.total() == Spend(tokens=170, cost=1_350, requests=1)


def test_spend_can_be_filtered_by_agent_tenant_and_run(ledger):
    ledger.record(row("r1", tenant="a", agent="x", run="x-run"))
    ledger.record(row("r2", tenant="a", agent="y", run="y-run", cost=2_000))
    ledger.record(row("r3", tenant="b", agent="x", run="z-run"))
    assert ledger.spend(agent="y").cost == 2_000
    assert ledger.spend(tenant="a").requests == 2
    assert ledger.spend(tenant="a", agent="x").requests == 1
    assert ledger.spend(run="z-run").requests == 1


def test_spend_can_be_grouped(ledger):
    ledger.record(row("r1", agent="x"))
    ledger.record(row("r2", agent="x"))
    ledger.record(row("r3", agent="y"))
    grouped = ledger.by("agent")
    assert grouped["x"].requests == 2
    assert grouped["y"].requests == 1


def test_outcomes_are_counted_by_label(ledger):
    ledger.record(row("r1"))
    ledger.record(row("r2", outcome="expired", input_tokens=0, output_tokens=0, cost=0))
    ledger.record(row("r3", outcome="expired", input_tokens=0, output_tokens=0, cost=0))
    assert ledger.outcomes() == {"ok": 1, "expired": 2}


@pytest.mark.parametrize("column", ["id; DROP TABLE llm_calls", "outcome", "cost"])
def test_only_known_columns_can_be_used_to_group_or_filter(ledger, column):
    with pytest.raises(ValueError):
        ledger.by(column)
    with pytest.raises(ValueError):
        ledger.spend(**{column: "x"})


def test_null_ledger_accepts_rows_and_keeps_nothing():
    NullLedger().record(row())
