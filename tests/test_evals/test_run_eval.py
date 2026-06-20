"""
Tests for evals/harness/run_eval.py. Includes a real integration test
against the actual committed fixtures in evals/fixtures/ -- this is
deliberately NOT mocked, since the entire point of the eval harness is
proving the real pipeline (load -> diff -> cluster) produces correct
results against real, committed, labeled data. If this test breaks,
it usually means something upstream genuinely regressed -- which is
exactly the kind of regression an eval harness exists to catch.
"""

from evals.harness.run_eval import run_statistical_eval
from evals.harness.scoring import Outcome, summarize_run


def test_real_fixtures_score_correctly_via_statistical_eval():
    """
    The actual eval run against the four real, committed fixtures.
    Expects 100% statistical accuracy -- if this regresses, something
    upstream broke (a corruptor's output shape changed, a signature's
    confidence threshold logic regressed, dtype-family matching broke
    again, etc.) -- this is the harness doing exactly its job.
    """
    scored_cases = run_statistical_eval()
    assert len(scored_cases) == 4

    summary = summarize_run(scored_cases)
    assert summary.overall_accuracy() == 1.0

    by_fixture = {c.fixture_id: c for c in scored_cases}
    assert by_fixture["fixture_timezone_shift_001"].outcome == Outcome.TRUE_POSITIVE
    assert by_fixture["fixture_truncation_001"].outcome == Outcome.TRUE_POSITIVE
    assert by_fixture["fixture_enum_drift_001"].outcome == Outcome.TRUE_POSITIVE
    assert by_fixture["fixture_unrecognized_001"].outcome == Outcome.HONEST_ABSTAIN


def test_each_fixture_predicts_its_own_pattern_not_a_different_one():
    """
    Stronger check than just 'correct outcome' -- confirms the
    PREDICTED pattern_id for each real pattern fixture matches its own
    ground truth exactly, catching the specific failure mode of two
    patterns swapping (e.g. truncation fixture matching enum_drift).
    """
    scored_cases = run_statistical_eval()
    by_fixture = {c.fixture_id: c for c in scored_cases}

    assert by_fixture["fixture_timezone_shift_001"].predicted_pattern_id == "timezone_shift"
    assert by_fixture["fixture_truncation_001"].predicted_pattern_id == "truncation"
    assert by_fixture["fixture_enum_drift_001"].predicted_pattern_id == "enum_drift"
    assert by_fixture["fixture_unrecognized_001"].predicted_pattern_id is None
