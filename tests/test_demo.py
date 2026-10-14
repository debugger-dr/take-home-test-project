from llmgw.demo import ACTS, main


def test_every_act_runs_and_every_claim_it_prints_holds(capsys):
    assert main() == 0
    output = capsys.readouterr().out
    for number in range(1, len(ACTS) + 1):
        assert f"Act {number}:" in output
    assert "[FAIL]" not in output
    assert output.count("[ok]") == 7


def test_a_single_act_can_be_selected(capsys):
    assert main(["safety"]) == 0
    output = capsys.readouterr().out
    assert "Act 2:" in output and "Act 1:" not in output


def test_an_unknown_act_is_rejected(capsys):
    assert main(["nope"]) == 2
    assert "unknown act" in capsys.readouterr().err
