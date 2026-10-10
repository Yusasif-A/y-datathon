"""
Generates every world the Simulator Specification asks for (section 9):
the baseline, the eight stress scenarios and the c sweep. ALL SYNTHETIC.

  python generate_all.py            # everything (about 6 minutes)
  python generate_all.py baseline   # just the named worlds

Each world goes to data/<name>/ with its own run_metadata.json and sanity
checks; data/worlds_summary.csv compares them side by side. The CSVs are
git-ignored (about 35 MB per world): rerun this to rebuild them exactly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402
from simulator import checks, load_config  # noqa: E402

SCENARIOS = ["baseline", "weak_link", "strong_link", "high_noise", "many_chronic", "benign_silence",
             "fast_attrition", "insecure", "sparse_stock"]
C_SWEEP = [0, 0.25, 0.5, 1, 2, 3]


def worlds():
    for s in SCENARIOS:
        yield s, ROOT / "configs" / f"{s}.yaml", []
    for c in C_SWEEP:
        yield f"c_sweep/c{c}", ROOT / "configs" / "baseline.yaml", [{"reporting": {"c": c}}]


def main(only: list[str]):
    rows = []
    for name, cfg_path, overrides in worlds():
        if only and name not in only:
            continue
        cfg = load_config(cfg_path, overrides)
        if name.startswith("c_sweep"):
            cfg["scenario"] = name.replace("/", "_")
        w = run.generate_world(cfg)
        results = checks.run_checks(w["facilities"], w["truth"], w["obs"], w["reports"], w["stock_reports"],
                                    cfg, w["runtime"])
        meta = run.write(w, ROOT / "data" / name, results)
        failed = [r["check"] for r in results if not r["passed"]]
        s = meta["summary"]
        rows.append({"world": name, "c": cfg["reporting"]["c"], "facilities": s["facilities"],
                     "on_time": s["on_time"], "late": s["late"], "missing": s["missing"],
                     "fail_serious": s["fail_serious"], "fail_serious_next12": s["fail_serious_next12"],
                     "chronic_episodes": s["silence_causes"].get("chronic", 0),
                     "stock_participants": s["stock_participants"], "checks_failed": "; ".join(failed)})
        print(f"{name:18s} on-time {s['on_time']:.2f}  missing {s['missing']:.2f}  serious {s['fail_serious']:.2f}"
              f"  failed checks: {failed or 'none'}", flush=True)
    if rows:
        summary_path = ROOT / "data" / "worlds_summary.csv"
        old = pd.read_csv(summary_path) if summary_path.exists() and only else pd.DataFrame()
        new = pd.DataFrame(rows)
        if len(old):
            new = pd.concat([old[~old.world.isin(new.world)], new], ignore_index=True)
        new.to_csv(summary_path, index=False)
        print(f"\nSummary -> {summary_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
