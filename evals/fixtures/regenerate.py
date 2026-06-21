"""
evals/fixtures/regenerate.py

Generates the eval fixtures committed to evals/fixtures/ -- one (or
more) labeled source/target pair per taxonomy pattern, using the real
corruptor functions and writing real ground_truth.json files via
synthetic/ground_truth.py.

This is a DELIBERATE, REVIEWED action per CONTRIBUTING.md -- run it,
look at the git diff, commit it. Fixtures are NOT regenerated silently
or automatically; this script exists for when you want to expand
coverage (new patterns, more fixtures per pattern), not as part of any
automated pipeline.

Usage:
    python3 evals/fixtures/regenerate.py
"""

from __future__ import annotations

from pathlib import Path

from wherefore.synthetic.base_dataset import (
    FINANCIAL_ACCOUNTS,
    HEALTHCARE_PATIENTS,
    generate_dataset,
)
from wherefore.synthetic.corruptors.encoding_mismatch import apply as drift_encoding
from wherefore.synthetic.corruptors.enum_drift import apply as drift_enum
from wherefore.synthetic.corruptors.float_precision import apply as drift_float
from wherefore.synthetic.corruptors.null_type_coercion import apply as coerce_null
from wherefore.synthetic.corruptors.timezone_shift import apply as shift_timezone
from wherefore.synthetic.corruptors.truncation import apply as truncate
from wherefore.synthetic.ground_truth import GroundTruth, InjectedCorruption, write_fixture

FIXTURES_DIR = Path(__file__).parent


def build_timezone_shift_fixture(fixture_id: str, seed: int) -> None:
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=seed)
    target, affected_rows = shift_timezone(
        source, column="opened_at", offset_hours=5.0, affected_fraction=0.3, seed=seed
    )
    gt = GroundTruth(
        fixture_id=fixture_id,
        source_file=f"{fixture_id}_source.csv",
        target_file=f"{fixture_id}_target.csv",
        injected_corruptions=[
            InjectedCorruption(
                pattern_id="timezone_shift",
                params={"offset_hours": 5.0, "affected_fraction": 0.3},
                affected_rows=affected_rows,
                affected_column="opened_at",
            )
        ],
        generation_seed=seed,
        domain="FINANCIAL_ACCOUNTS",
        join_column="account_id",
    )
    write_fixture(gt, source, target, FIXTURES_DIR)


def build_truncation_fixture(fixture_id: str, seed: int) -> None:
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=seed)
    target, affected_rows = truncate(
        source, column="patient_name", max_length=8, affected_fraction=0.5, seed=seed
    )
    gt = GroundTruth(
        fixture_id=fixture_id,
        source_file=f"{fixture_id}_source.csv",
        target_file=f"{fixture_id}_target.csv",
        injected_corruptions=[
            InjectedCorruption(
                pattern_id="truncation",
                params={"max_length": 8, "affected_fraction": 0.5},
                affected_rows=affected_rows,
                affected_column="patient_name",
            )
        ],
        generation_seed=seed,
        domain="HEALTHCARE_PATIENTS",
        join_column="patient_id",
    )
    write_fixture(gt, source, target, FIXTURES_DIR)


def build_enum_drift_fixture(fixture_id: str, seed: int) -> None:
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=seed)
    mapping = {"approved": "APPROVED", "denied": "REJECTED"}
    target, affected_rows = drift_enum(
        source, column="claim_status", value_mapping=mapping, affected_fraction=0.5, seed=seed
    )
    gt = GroundTruth(
        fixture_id=fixture_id,
        source_file=f"{fixture_id}_source.csv",
        target_file=f"{fixture_id}_target.csv",
        injected_corruptions=[
            InjectedCorruption(
                pattern_id="enum_drift",
                params={"value_mapping": mapping, "affected_fraction": 0.5},
                affected_rows=affected_rows,
                affected_column="claim_status",
            )
        ],
        generation_seed=seed,
        domain="HEALTHCARE_PATIENTS",
        join_column="patient_id",
    )
    write_fixture(gt, source, target, FIXTURES_DIR)


def build_null_type_coercion_fixture(fixture_id: str, seed: int) -> None:
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=50, seed=seed)
    target, affected_rows = coerce_null(
        source, column="last_transaction_at", sentinel="NULL", affected_fraction=1.0, seed=seed
    )
    gt = GroundTruth(
        fixture_id=fixture_id,
        source_file=f"{fixture_id}_source.csv",
        target_file=f"{fixture_id}_target.csv",
        injected_corruptions=[
            InjectedCorruption(
                pattern_id="null_type_coercion",
                params={"sentinel": "NULL", "affected_fraction": 1.0},
                affected_rows=affected_rows,
                affected_column="last_transaction_at",
            )
        ],
        generation_seed=seed,
        domain="FINANCIAL_ACCOUNTS",
        join_column="account_id",
    )
    write_fixture(gt, source, target, FIXTURES_DIR)


def build_float_precision_fixture(fixture_id: str, seed: int) -> None:
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=seed)
    target, affected_rows = drift_float(source, column="balance", affected_fraction=0.5, seed=seed)
    gt = GroundTruth(
        fixture_id=fixture_id,
        source_file=f"{fixture_id}_source.csv",
        target_file=f"{fixture_id}_target.csv",
        injected_corruptions=[
            InjectedCorruption(
                pattern_id="float_precision",
                params={"affected_fraction": 0.5},
                affected_rows=affected_rows,
                affected_column="balance",
            )
        ],
        generation_seed=seed,
        domain="FINANCIAL_ACCOUNTS",
        join_column="account_id",
    )
    write_fixture(gt, source, target, FIXTURES_DIR)


def build_encoding_mismatch_fixture(fixture_id: str, seed: int) -> None:
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=50, seed=seed)
    target, affected_rows = drift_encoding(source, column="customer_name", affected_fraction=1.0, seed=seed)
    gt = GroundTruth(
        fixture_id=fixture_id,
        source_file=f"{fixture_id}_source.csv",
        target_file=f"{fixture_id}_target.csv",
        injected_corruptions=[
            InjectedCorruption(
                pattern_id="encoding_mismatch",
                params={"affected_fraction": 1.0},
                affected_rows=affected_rows,
                affected_column="customer_name",
            )
        ],
        generation_seed=seed,
        domain="FINANCIAL_ACCOUNTS",
        join_column="account_id",
    )
    write_fixture(gt, source, target, FIXTURES_DIR)


def build_unrecognized_fixture(fixture_id: str, seed: int) -> None:
    """
    A genuinely unrecognized case -- random, non-matching corruption
    with no consistent pattern. injected_corruptions is empty
    (pattern_id=None is the implicit ground truth: no real pattern was
    injected), so scoring.py can check whether the system correctly
    reports "unrecognized" rather than force-fitting a guess -- this
    is the "honest_abstain" outcome type from scoring.py's design.
    """
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=20, seed=seed)
    target = source.copy()
    target.loc[0, "account_type"] = "xq7z"
    target.loc[1, "account_type"] = "random_garbage_2"
    target.loc[2, "account_type"] = "totally_different_value"
    gt = GroundTruth(
        fixture_id=fixture_id,
        source_file=f"{fixture_id}_source.csv",
        target_file=f"{fixture_id}_target.csv",
        injected_corruptions=[],  # deliberately empty -- no real pattern injected
        generation_seed=seed,
        domain="FINANCIAL_ACCOUNTS",
        join_column="account_id",
    )
    write_fixture(gt, source, target, FIXTURES_DIR)


def main() -> None:
    build_timezone_shift_fixture("fixture_timezone_shift_001", seed=42)
    build_truncation_fixture("fixture_truncation_001", seed=42)
    build_enum_drift_fixture("fixture_enum_drift_001", seed=42)
    build_null_type_coercion_fixture("fixture_null_type_coercion_001", seed=1)
    build_float_precision_fixture("fixture_float_precision_001", seed=1)
    build_encoding_mismatch_fixture("fixture_encoding_mismatch_001", seed=1)
    build_unrecognized_fixture("fixture_unrecognized_001", seed=42)
    print(f"Wrote 7 fixtures to {FIXTURES_DIR}")


if __name__ == "__main__":
    main()
