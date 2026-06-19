# wherefore

**Status: early scaffold, not yet functional.** See `TAXONOMY_TODO.md`
for current build state.

## What this is

Data diffing tools (data-diff, Great Expectations, OpenMetadata data
diff, datacompy) detect *that* two datasets differ. `wherefore` answers
the next question — *for what reason* — in plain English, across
patterns of failures rather than row-by-row listings — and backs
accuracy claims with a real eval harness scored against labeled
synthetic ground truth, not vibes.

A full comparison-table README (vs. data-diff / Great Expectations /
OpenMetadata, with eval numbers once they exist) is planned for the
CLI/README polish phase — see project plan. This is a placeholder so
the repo isn't literally undocumented while mid-build.

## Architecture, in one paragraph

Comparison engine (wraps `datacompy`) produces a normalized diff →
deterministic clustering groups mismatches and runs cheap statistical
signature checks → an AI reasoning layer (Claude, behind a swappable
`explain()` interface) takes statistically-flagged clusters and writes
the causal narrative, citing real example rows, honestly flagging
anything that doesn't fit the known taxonomy → a Markdown report.
Failure patterns are data (YAML), not code — see `CONTRIBUTING.md`.

## Setup (once functional)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage (once functional)

```bash
wherefore compare source.csv target.csv --output report.md
```

## Contributing

See `CONTRIBUTING.md`, especially before adding a new taxonomy pattern.
