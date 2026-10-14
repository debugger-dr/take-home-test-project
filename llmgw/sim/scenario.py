import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from llmgw.config import ProviderLimits, SchedulerConfig, TenantConfig
from llmgw.scheduler import Scheduler
from llmgw.sim.engine import SchedulerPort, Simulator
from llmgw.sim.provider import CountingMode, FakeProvider, ProviderModel, WindowMode
from llmgw.sim.report import Report
from llmgw.sim.workload import TenantProfile, Workload

# Default profile: input ~3,000 tokens (provider ratio averages 1.0) plus output ~640 * 0.65 average ratio.
MEAN_TOKENS_PER_REQUEST = 3_416


def rate_for(tokens_per_minute: float) -> float:
    return tokens_per_minute / MEAN_TOKENS_PER_REQUEST / 60


@dataclass(frozen=True)
class Scenario:
    limits: ProviderLimits
    profiles: tuple[TenantProfile, ...]
    weights: Mapping[str, int] = field(default_factory=dict)
    settings: Mapping[str, object] = field(default_factory=dict)
    counting: CountingMode = CountingMode.RESERVE_AT_START
    window_mode: WindowMode = WindowMode.ROLLING
    duration: float = 600.0
    drain: float = 60.0
    warmup: float = 60.0
    seed: int = 1

    @property
    def measure(self) -> tuple[float, float]:
        return self.warmup, self.duration - self.warmup

    def scheduler_config(self) -> SchedulerConfig:
        tenants = tuple(TenantConfig(p.tenant, self.weights.get(p.tenant, 1)) for p in self.profiles)
        return SchedulerConfig(self.limits, tenants, **self.settings)

    def run(self, make_scheduler: Callable[[SchedulerConfig], SchedulerPort] = Scheduler) -> Report:
        requests = Workload(self.profiles, self.seed).generate(self.duration)
        model = ProviderModel(self.limits, self.counting, self.window_mode)
        provider = FakeProvider(model, random.Random(self.seed + 1))
        simulator = Simulator(make_scheduler(self.scheduler_config()), provider)
        return simulator.run(requests, until=self.duration + self.drain)
