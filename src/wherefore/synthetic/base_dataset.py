"""
synthetic/base_dataset.py

Generates realistic-looking "clean" tabular data that serves as the
SOURCE dataset before any corruption is applied. The target dataset is
a copy of this with one or more corruptors from synthetic/corruptors/
applied to it.

Design: ONE generic engine (`generate_dataset`) parameterized by a
`DomainSpec` (a list of `FieldSpec`s), rather than one hardcoded
function per domain. This mirrors the taxonomy's "data, not code"
philosophy -- adding a third domain later (or a user's own real-world
schema) means writing a new DomainSpec, not a new generator function.
Two domains are defined below: `FINANCIAL_ACCOUNTS` and
`HEALTHCARE_PATIENTS`. Corruptors are domain-agnostic by design -- the
same timezone_shift corruptor should work against either domain's
datetime column, which is itself a test of whether corruptors are
written generically (see CONTRIBUTING.md on the corruptor contract).

Determinism: every generator function takes an explicit `seed` and is
fully reproducible -- required since fixtures are committed to git
(see CONTRIBUTING.md on why fixtures aren't regenerated on demand).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import numpy as np
import pandas as pd

FieldKind = Literal["id", "name", "enum", "float", "datetime", "date", "code"]


@dataclass
class FieldSpec:
    """
    Describes one column's generation rule. `kind` selects which
    built-in generator function in GENERATORS is used; `params` are
    passed through to it. `nullable_fraction` lets any field carry
    natural nulls, independent of `kind` -- this is what gives
    null_type_coercion corruptors something realistic to work against
    even on non-numeric columns.
    """

    name: str
    kind: FieldKind
    params: dict[str, Any] = field(default_factory=dict)
    nullable_fraction: float = 0.0


@dataclass
class DomainSpec:
    """A named collection of FieldSpecs defining one synthetic domain."""

    domain_name: str
    fields: list[FieldSpec]
    key_field: str  # which field is the natural join key


# ---------------------------------------------------------------------------
# Field-level generators. Each takes (n_rows, rng, **params) -> np.ndarray
# ---------------------------------------------------------------------------


def _gen_id(n_rows: int, rng: np.random.Generator, prefix: str, start: int = 1000) -> np.ndarray:
    return np.array([f"{prefix}-{start + i}" for i in range(n_rows)])


def _gen_name(n_rows: int, rng: np.random.Generator, include_non_ascii: bool = False) -> np.ndarray:
    """
    Generates plausible person names. When include_non_ascii=True, a
    fraction of names include accented/non-ASCII characters (e.g.
    'José', 'Müller', 'Nguyễn') -- deliberately, since encoding_mismatch
    corruption needs real non-ASCII content in the source to corrupt
    meaningfully; corrupting pure-ASCII text can't demonstrate a UTF-8
    vs Latin-1 mismatch.
    """
    first_ascii = ["James", "Mary", "Robert", "Linda", "Michael", "Susan", "David", "Karen"]
    first_non_ascii = ["José", "François", "Müller", "Nguyễn", "Zoë", "Renée", "Søren", "Çağla"]
    last_ascii = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis"]

    names = []
    for _ in range(n_rows):
        use_non_ascii = include_non_ascii and rng.random() < 0.25
        first = rng.choice(first_non_ascii if use_non_ascii else first_ascii)
        last = rng.choice(last_ascii)
        names.append(f"{first} {last}")
    return np.array(names)


def _gen_enum(n_rows: int, rng: np.random.Generator, values: list[str], weights: list[float] | None = None) -> np.ndarray:
    return rng.choice(values, size=n_rows, p=weights)


def _gen_float(n_rows: int, rng: np.random.Generator, low: float, high: float, decimals: int = 2) -> np.ndarray:
    return np.round(rng.uniform(low, high, size=n_rows), decimals)


def _gen_datetime(
    n_rows: int,
    rng: np.random.Generator,
    start: str,
    end: str,
) -> np.ndarray:
    """
    Generates random timestamps between start and end. Rounded to
    whole seconds -- raw nanosecond-resolution randomness produces
    timestamps like '12:06:48.702750148', which no real-world system
    actually emits and would make every fixture look obviously
    synthetic rather than realistic.

    Explicitly forces datetime64[s] resolution via .astype() rather
    than relying on pd.to_datetime(unit="s")'s default inference --
    confirmed by direct testing that this default behavior is NOT
    stable across pandas versions: pandas 3.0.3 infers [s] from
    second-precision input, but this is a relatively recent inference
    behavior (see pandas-dev/pandas#55901 and related issues) -- an
    older pandas 2.x install, which our `pandas>=2.0` floor in
    pyproject.toml still permits, can return [ns] for the exact same
    call. This caused a real CI failure: the test asserting
    datetime64[s] passed locally (pandas 3.0.3 installed) and failed
    in a fresh CI install (an earlier pandas 2.x resolved instead).
    .astype("datetime64[s]") makes the resulting dtype an explicit
    guarantee of this function, not an inference side-effect that
    drifts with whichever pandas version happens to be installed.
    """
    start_ts = pd.Timestamp(start).value // 10**9  # convert to whole seconds
    end_ts = pd.Timestamp(end).value // 10**9
    raw_seconds = rng.integers(start_ts, end_ts, size=n_rows)
    return pd.to_datetime(raw_seconds, unit="s").astype("datetime64[s]")


def _gen_date(n_rows: int, rng: np.random.Generator, start: str, end: str) -> np.ndarray:
    return _gen_datetime(n_rows, rng, start, end).normalize()


def _gen_code(n_rows: int, rng: np.random.Generator, codes: list[str]) -> np.ndarray:
    """For fixed-format reference codes, e.g. ICD-10-style diagnosis codes."""
    return rng.choice(codes, size=n_rows)


GENERATORS: dict[FieldKind, Callable] = {
    "id": _gen_id,
    "name": _gen_name,
    "enum": _gen_enum,
    "float": _gen_float,
    "datetime": _gen_datetime,
    "date": _gen_date,
    "code": _gen_code,
}


def generate_dataset(domain: DomainSpec, n_rows: int, seed: int) -> pd.DataFrame:
    """
    Generates a clean synthetic DataFrame matching `domain`'s schema.
    Deterministic given the same (domain, n_rows, seed).
    """
    rng = np.random.default_rng(seed)
    columns: dict[str, np.ndarray] = {}

    for spec in domain.fields:
        generator = GENERATORS[spec.kind]
        values = generator(n_rows, rng, **spec.params)

        if spec.nullable_fraction > 0:
            null_mask = rng.random(n_rows) < spec.nullable_fraction
            # Preserve proper dtype per kind rather than collapsing
            # everything to generic object dtype -- a float column with
            # nulls should stay numeric (pandas' nullable Float64) so
            # downstream numeric operations (e.g. float_precision
            # detection) still work; only string/enum kinds need plain
            # object dtype to hold None.
            if spec.kind == "float":
                values = pd.array(values, dtype="Float64")
                values[null_mask] = None
            elif spec.kind in ("datetime", "date"):
                series = pd.Series(values)
                series[null_mask] = pd.NaT
                values = series
            else:
                values = pd.array(values, dtype="object")
                values[null_mask] = None

        columns[spec.name] = values

    df = pd.DataFrame(columns)

    # Introduce a small number of near-duplicate rows by design, so
    # dedup_failure has something realistic to detect later. Duplicates
    # share the same key but are otherwise full copies of an existing
    # row -- this models a re-ingested record from a failed migration
    # retry, not a coincidental data collision.
    n_dupes = max(1, n_rows // 50)
    dupe_indices = rng.choice(n_rows, size=n_dupes, replace=False)
    dupes = df.iloc[dupe_indices].copy()
    df = pd.concat([df, dupes], ignore_index=True)

    return df


# ---------------------------------------------------------------------------
# Domain specs
# ---------------------------------------------------------------------------

FINANCIAL_ACCOUNTS = DomainSpec(
    domain_name="financial_accounts",
    key_field="account_id",
    fields=[
        FieldSpec("account_id", "id", {"prefix": "ACCT", "start": 100000}),
        FieldSpec("customer_name", "name", {"include_non_ascii": True}),
        FieldSpec(
            "account_type", "enum",
            {"values": ["checking", "savings", "credit"], "weights": [0.5, 0.35, 0.15]},
        ),
        FieldSpec("balance", "float", {"low": -500.0, "high": 250000.0, "decimals": 2}),
        FieldSpec("currency", "enum", {"values": ["USD", "EUR", "GBP"], "weights": [0.8, 0.15, 0.05]}),
        FieldSpec("opened_at", "datetime", {"start": "2015-01-01", "end": "2024-01-01"}),
        FieldSpec(
            "last_transaction_at", "datetime",
            {"start": "2024-01-01", "end": "2026-06-01"},
            nullable_fraction=0.05,
        ),
        FieldSpec(
            "status", "enum",
            {"values": ["active", "closed", "frozen"], "weights": [0.85, 0.1, 0.05]},
        ),
    ],
)

# Diagnosis codes styled after real ICD-10 format (letter + digits +
# optional decimal subcode) -- realistic enough to give truncation
# corruption a meaningful fixed-length field to cut, without using
# actual real-world ICD-10 codes verbatim.
_SYNTHETIC_DIAGNOSIS_CODES = [
    "E11.9", "I10", "J45.909", "M54.5", "K21.9", "F41.1", "N39.0", "R07.9",
]

HEALTHCARE_PATIENTS = DomainSpec(
    domain_name="healthcare_patients",
    key_field="patient_id",
    fields=[
        FieldSpec("patient_id", "id", {"prefix": "PT", "start": 500000}),
        FieldSpec("patient_name", "name", {"include_non_ascii": True}),
        FieldSpec("date_of_birth", "date", {"start": "1940-01-01", "end": "2020-01-01"}),
        FieldSpec("diagnosis_code", "code", {"codes": _SYNTHETIC_DIAGNOSIS_CODES}),
        FieldSpec("provider_id", "id", {"prefix": "PROV", "start": 8000}),
        FieldSpec(
            "encounter_date", "datetime",
            {"start": "2024-01-01", "end": "2026-06-01"},
        ),
        FieldSpec(
            "claim_status", "enum",
            {"values": ["submitted", "approved", "denied", "pending"], "weights": [0.2, 0.5, 0.1, 0.2]},
        ),
        FieldSpec("billed_amount", "float", {"low": 50.0, "high": 50000.0, "decimals": 2}, nullable_fraction=0.03),
    ],
)
