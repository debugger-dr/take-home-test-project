import pytest

from llmgw.config import ProviderLimits, SchedulerConfig, TenantConfig

LIMITS = ProviderLimits(rpm=600, tpm=400_000)
TENANTS = (TenantConfig("a"), TenantConfig("b"))


def make_config(**overrides) -> SchedulerConfig:
    return SchedulerConfig(limits=overrides.pop("limits", LIMITS), tenants=overrides.pop("tenants", TENANTS), **overrides)


def test_usable_capacity_applies_headroom():
    cfg = make_config()
    assert cfg.usable_tokens == 360_000
    assert cfg.usable_requests == 540
    assert cfg.usable_token_rate == 6_000


def test_usable_capacity_never_rounds_up():
    cfg = make_config(limits=ProviderLimits(rpm=11, tpm=11), headroom=0.9)
    assert cfg.usable_tokens == 9
    assert cfg.usable_requests == 9


def test_reservation_cost_scales_input_and_adds_max_output():
    cfg = make_config(input_safety=1.15)
    assert cfg.reservation_cost(est_input_tokens=2_000, max_output_tokens=1_000) == 3_300


def test_reservation_cost_rounds_input_up():
    cfg = make_config(input_safety=1.15)
    assert cfg.reservation_cost(est_input_tokens=1, max_output_tokens=0) == 2


@pytest.mark.parametrize("rpm,tpm,window", [(0, 1, 60), (1, 0, 60), (-1, 1, 60), (1, 1, 0)])
def test_invalid_provider_limits_are_rejected(rpm, tpm, window):
    with pytest.raises(ValueError):
        ProviderLimits(rpm=rpm, tpm=tpm, window=window)


@pytest.mark.parametrize("weight", [0, -1])
def test_non_positive_weight_is_rejected(weight):
    with pytest.raises(ValueError):
        TenantConfig("a", weight=weight)


def test_non_positive_tenant_budget_is_rejected():
    with pytest.raises(ValueError):
        TenantConfig("a", token_budget=0)
    assert TenantConfig("a", token_budget=None).token_budget is None


def test_empty_tenant_name_is_rejected():
    with pytest.raises(ValueError):
        TenantConfig("")


def test_requires_at_least_one_tenant():
    with pytest.raises(ValueError):
        make_config(tenants=())


def test_tenant_names_must_be_unique():
    with pytest.raises(ValueError):
        make_config(tenants=(TenantConfig("a"), TenantConfig("a")))


@pytest.mark.parametrize(
    "field,value",
    [
        ("headroom", 0),
        ("headroom", 1.1),
        ("input_safety", 0.9),
        ("base_quantum", 0),
        ("pacing_burst", -0.1),
        ("pacing_burst", 1.5),
        ("age_p2_to_p1", 0),
        ("p0_rate", 0),
        ("p0_burst", 0),
        ("max_queue_len", 0),
        ("max_queue_tokens", 0),
        ("run_token_cap", 0),
        ("budget_alert", 0),
        ("budget_alert", 1.5),
    ],
)
def test_invalid_scheduler_settings_are_rejected(field, value):
    with pytest.raises(ValueError):
        make_config(**{field: value})
