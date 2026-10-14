from collections import defaultdict

from llmgw.config import PriceTable, ProviderLimits
from llmgw.ledger import Ledger
from llmgw.scheduler import Scheduler
from llmgw.sim.controls import ReservationBillingScheduler
from llmgw.sim.report import Report
from llmgw.sim.scenario import Scenario, rate_for
from llmgw.sim.workload import TenantProfile

LIMITS = ProviderLimits(rpm=600, tpm=400_000)
PROFILES = (
    TenantProfile("a", rate=rate_for(1_200_000)),
    TenantProfile("b", rate=rate_for(60_000)),
)
SCENARIO = Scenario(LIMITS, PROFILES, duration=300.0, drain=150.0)
PRICES = PriceTable()


def run_with_ledger(make=Scheduler) -> tuple[Report, Ledger]:
    ledger = Ledger()
    return SCENARIO.run(lambda config: make(config, ledger=ledger)), ledger


def test_ledger_matches_the_providers_own_usage_exactly():
    report, ledger = run_with_ledger()
    provider = report.provider
    expected_cost = provider.total_input_tokens * PRICES.input_micros + provider.total_output_tokens * PRICES.output_micros

    assert ledger.total().tokens == provider.total_tokens
    assert ledger.total().cost == expected_cost


def test_every_accepted_request_has_exactly_one_row():
    report, ledger = run_with_ledger()
    accepted = sum(1 for r in report.records.values() if r.rejected is None)
    assert ledger.total().requests == accepted
    assert set(ledger.outcomes()) <= {"ok", "expired"}


def test_per_run_spend_matches_what_each_run_actually_received():
    report, ledger = run_with_ledger()
    received: dict[str, int] = defaultdict(int)
    for record in report.records.values():
        received[record.run] += record.tokens

    billed = {run: spend.tokens for run, spend in ledger.by("run").items() if spend.tokens}
    assert billed == {run: tokens for run, tokens in received.items() if tokens}


def test_control_that_bills_reservations_overstates_spend():
    report, ledger = run_with_ledger(ReservationBillingScheduler)
    assert ledger.total().tokens > report.provider.total_tokens
