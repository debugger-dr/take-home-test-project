import random
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from llmgw.types import LLMRequest, RunState, derive_class


@dataclass(frozen=True)
class SizeRange:
    low: int
    high: int

    def draw(self, rng: random.Random) -> int:
        return rng.randint(self.low, self.high)


@dataclass(frozen=True)
class TenantProfile:
    tenant: str
    rate: float
    est_input: SizeRange = SizeRange(2_000, 4_000)
    max_output: SizeRange = SizeRange(256, 1_024)
    human_waiting_fraction: float = 0.0
    interactive_fraction: float = 1.0
    deadline: float = 120.0
    agents: int = 5

    def __post_init__(self) -> None:
        if self.rate <= 0 or self.agents <= 0:
            raise ValueError("rate and agents must be positive")
        if not (0 <= self.human_waiting_fraction <= 1 and 0 <= self.interactive_fraction <= 1):
            raise ValueError("fractions must be within [0, 1]")


class Workload:
    """Poisson arrivals per tenant. Each tenant has its own stream, so profiles do not disturb each other."""

    def __init__(self, profiles: Sequence[TenantProfile], seed: int) -> None:
        self._profiles = profiles
        self._seed = seed

    def generate(self, duration: float) -> list[LLMRequest]:
        requests = [r for profile in self._profiles for r in self._arrivals(profile, duration)]
        requests.sort(key=lambda r: (r.submitted_at, r.id))
        return requests

    def _arrivals(self, profile: TenantProfile, duration: float) -> Iterator[LLMRequest]:
        rng = random.Random(f"{self._seed}/{profile.tenant}")
        at = rng.expovariate(profile.rate)
        index = 0
        while at < duration:
            yield self._request(profile, index, at, rng)
            index += 1
            at += rng.expovariate(profile.rate)

    @staticmethod
    def _request(profile: TenantProfile, index: int, at: float, rng: random.Random) -> LLMRequest:
        state = RunState(
            human_waiting=rng.random() < profile.human_waiting_fraction,
            interactive=rng.random() < profile.interactive_fraction,
        )
        agent = f"{profile.tenant}-agent-{index % profile.agents}"
        return LLMRequest(
            id=f"{profile.tenant}-{index}",
            tenant=profile.tenant,
            run=f"{agent}-run",
            agent=agent,
            model="fake-model",
            klass=derive_class(state),
            est_input_tokens=profile.est_input.draw(rng),
            max_output_tokens=profile.max_output.draw(rng),
            deadline=at + profile.deadline,
            submitted_at=at,
        )
