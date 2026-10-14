import pytest

from llmgw.types import Klass, RunState, Usage, derive_class


@pytest.mark.parametrize(
    "human_waiting,interactive,expected",
    [
        (True, True, Klass.P0),
        (True, False, Klass.P0),
        (False, True, Klass.P1),
        (False, False, Klass.P2),
    ],
)
def test_class_is_derived_from_run_state(human_waiting, interactive, expected):
    assert derive_class(RunState(human_waiting=human_waiting, interactive=interactive)) == expected


def test_class_ordering_puts_p0_first():
    assert Klass.P0 < Klass.P1 < Klass.P2


def test_usage_total_sums_input_and_output():
    assert Usage(input_tokens=700, output_tokens=300, cached_tokens=100).total == 1_000
