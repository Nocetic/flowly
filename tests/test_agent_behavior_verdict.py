"""How a live behaviour scenario's runs settle (tests/test_agent_behavior_real.py).

The live evaluation spends tokens and is skipped by default; its verdict
rule is plain logic and is checked here on every run.
"""

from tests.test_agent_behavior_real import verdict


def test_a_first_run_that_behaves_settles_it():
    assert verdict([None]) == (True, True)


def test_one_failure_then_two_passes_passes():
    assert verdict(["spawned 2"]) == (False, False)
    assert verdict(["spawned 2", None]) == (False, False)
    assert verdict(["spawned 2", None, None]) == (True, True)


def test_a_second_failure_fails():
    assert verdict(["spawned 2", "spawned 2"]) == (False, True)
    assert verdict(["spawned 2", None, "spawned 4"]) == (False, True)


def test_single_runs_judge_one_run():
    assert verdict(["spawned 2"], attempts=1) == (False, True)
    assert verdict([None], attempts=1) == (True, True)


def test_two_attempts_need_the_retry_to_pass():
    assert verdict(["spawned 2", None], attempts=2) == (True, True)
    assert verdict(["spawned 2", "x"], attempts=2) == (False, True)
