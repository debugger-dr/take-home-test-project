from llmgw.config import ProviderLimits
from llmgw.sim.scenario import Scenario, rate_for
from llmgw.sim.workload import TenantProfile

LIMITS = ProviderLimits(rpm=600, tpm=400_000)
PROFILES = (
    TenantProfile("a", rate=rate_for(1_200_000)),
    TenantProfile("b", rate=rate_for(60_000)),
)


def fingerprint(seed: int) -> str:
    return Scenario(LIMITS, PROFILES, duration=120.0, drain=60.0, seed=seed).run().fingerprint()


def test_the_same_seed_reproduces_the_run_exactly():
    assert fingerprint(1) == fingerprint(1)


def test_a_different_seed_gives_a_different_run():
    assert fingerprint(1) != fingerprint(2)
