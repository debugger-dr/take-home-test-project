from llmgw.budgets import Budget


def test_allows_usage_up_to_the_limit():
    budget = Budget(limit=1_000, alert_fraction=0.8)
    budget.commit(600)
    assert budget.allows(400)
    assert not budget.allows(401)


def test_alert_fires_once_when_the_threshold_is_first_crossed():
    budget = Budget(limit=1_000, alert_fraction=0.8)
    assert not budget.commit(700)
    assert budget.commit(100)
    assert not budget.commit(100)


def test_exhausted_when_usage_reaches_the_limit():
    budget = Budget(limit=1_000, alert_fraction=0.8)
    budget.commit(999)
    assert not budget.exhausted
    budget.commit(1)
    assert budget.exhausted


def test_adjust_corrects_usage_to_what_was_actually_spent():
    budget = Budget(limit=1_000, alert_fraction=0.8)
    budget.commit(900)
    budget.adjust(-400)
    assert budget.used == 500
    assert budget.allows(500)
    assert not budget.exhausted
