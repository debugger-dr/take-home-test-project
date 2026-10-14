from collections.abc import Mapping


def max_min_fair_share(
    capacity: float, demands: Mapping[str, float], weights: Mapping[str, float]
) -> dict[str, float]:
    """Weighted max-min allocation: demands below their entitlement are met, the rest is split by weight."""
    share = {name: 0.0 for name in demands}
    remaining = capacity
    active = set(demands)
    while active and remaining > 1e-9:
        total_weight = sum(weights[name] for name in active)
        entitled = {name: remaining * weights[name] / total_weight for name in active}
        satisfied = {name for name in active if demands[name] - share[name] <= entitled[name]}
        if not satisfied:
            for name in active:
                share[name] += entitled[name]
            break
        for name in satisfied:
            remaining -= demands[name] - share[name]
            share[name] = demands[name]
        active -= satisfied
    return share
