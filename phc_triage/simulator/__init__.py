"""
PHC Triage simulator (Simulator Specification v0.2). Builds a fake but
realistic world of primary health centres on real GRID3 facility points.
ALL REPORTING, STOCK AND OUTCOME DATA IS SYNTHETIC.

Layers, each its own module and testable alone:
    geography.py       real inputs (GRID3 points), LGAs, distances, catchments
    facilities.py      Layer A: facility attributes
    state.py           Layer B: hidden state (stock, shocks, staff)
    observe.py         Layer C: what a DHIS2-like system sees
    labels.py          Layer D: failure flags and silence causes
    checks.py          sanity checks (spec 12)
    prepare_inputs.py  one-off: builds inputs/ from GRID3 and NMIS downloads

Entry point: phc_triage/run.py
"""

from __future__ import annotations

import copy
import hashlib
import json
import zlib
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: str | Path, overrides: list[dict] | None = None) -> dict:
    """Load a scenario YAML, following `inherits:` to its base config."""
    path = Path(path)
    cfg = yaml.safe_load(path.read_text())
    parent = cfg.pop("inherits", None)
    scenario = path.stem
    if parent:
        cfg = _merge(load_config(path.parent / parent), cfg)
    cfg["scenario"] = scenario
    for o in overrides or []:
        cfg = _merge(cfg, o)
    return cfg


def parse_override(text: str) -> dict:
    """'reporting.c=0.5' -> {'reporting': {'c': 0.5}}"""
    key, value = text.split("=", 1)
    out: dict = {}
    node = out
    parts = key.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = yaml.safe_load(value)
    return out


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:12]


def layer_rng(seed: int, layer: str) -> np.random.Generator:
    """One generator per layer, sub-seeded from the run seed, so changing one
    layer does not reshuffle the others (spec 13)."""
    return np.random.default_rng(np.random.SeedSequence([int(seed), zlib.crc32(layer.encode())]))


def make_periods(start: int, months: int) -> list[str]:
    y, m = divmod(int(start), 100)
    out = []
    for _ in range(months):
        out.append(f"{y}{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def logistic(x):
    return 1.0 / (1.0 + np.exp(-x))


def draw_int(rng: np.random.Generator, bounds, size=None):
    """Uniform integer in [lo, hi] inclusive."""
    lo, hi = bounds
    return rng.integers(lo, hi + 1, size=size)
