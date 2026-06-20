"""
synthetic/ground_truth.py

Writes ground_truth.json alongside each generated source/target
fixture pair. This file is THE eval answer key -- it's what
scoring.py compares clustering's and explain()'s output against.

See InjectedCorruption / GroundTruth below for the exact shape. One
GroundTruth covers one fixture pair, and may list multiple
injected_corruptions if more than one pattern was applied to the same
pair (e.g. a more realistic multi-cause fixture) -- though every
fixture built so far injects exactly one corruption per pair, since
that's what the eval harness's per-pattern precision/recall needs as
its baseline case.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class InjectedCorruption:
    pattern_id: str
    params: dict
    affected_rows: list[int]
    affected_column: str


@dataclass
class GroundTruth:
    fixture_id: str
    source_file: str
    target_file: str
    injected_corruptions: list[InjectedCorruption] = field(default_factory=list)
    generation_seed: int | None = None
    domain: str | None = None
    join_column: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "GroundTruth":
        data = json.loads(text)
        corruptions = [InjectedCorruption(**c) for c in data.pop("injected_corruptions", [])]
        return cls(injected_corruptions=corruptions, **data)


def write_fixture(
    ground_truth: GroundTruth,
    source_df,
    target_df,
    fixtures_dir: Path,
) -> None:
    """
    Writes source CSV, target CSV, and ground_truth.json for one
    fixture into fixtures_dir, using ground_truth.source_file /
    target_file as the filenames (so the JSON and the actual files on
    disk always agree on naming -- no separate convention to keep in sync).
    """
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    source_df.to_csv(fixtures_dir / ground_truth.source_file, index=False)
    target_df.to_csv(fixtures_dir / ground_truth.target_file, index=False)
    (fixtures_dir / f"{ground_truth.fixture_id}_ground_truth.json").write_text(
        ground_truth.to_json()
    )


def load_fixture(fixture_id: str, fixtures_dir: Path) -> GroundTruth:
    gt_path = fixtures_dir / f"{fixture_id}_ground_truth.json"
    return GroundTruth.from_json(gt_path.read_text())


def list_fixture_ids(fixtures_dir: Path) -> list[str]:
    """Returns every fixture_id with a ground_truth.json present in
    fixtures_dir, sorted for deterministic iteration order."""
    return sorted(
        p.stem.removesuffix("_ground_truth")
        for p in fixtures_dir.glob("*_ground_truth.json")
    )
