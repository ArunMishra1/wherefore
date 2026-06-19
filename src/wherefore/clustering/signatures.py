"""
clustering/signatures.py

NEXT TURN: implement this.

Purpose: the actual detector functions that taxonomy YAML files
reference by string key (e.g. "constant_offset_subset"). This module
holds a dict mapping signature names -> callables, separate from
cluster_mismatches.py so that:
  - adding a new signature is additive (register a function here,
    reference it by name in a new pattern's YAML)
  - signature functions are independently unit-testable against
    synthetic fixtures without needing the full clustering pipeline

Each signature function takes a cluster of mismatches (shape TBD when
DiffResult is finalized) and returns a bool or confidence float --
purely statistical, e.g.:

    def constant_offset_subset(cluster) -> float:
        '''Returns confidence 0-1 that mismatched values in this
        cluster differ by a constant time delta.'''
        ...

SIGNATURE_REGISTRY: dict[str, Callable] = {
    "constant_offset_subset": constant_offset_subset,
    # add more here as patterns are added
}
"""
