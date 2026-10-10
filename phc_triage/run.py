"""
PHC Triage simulator entry point (Simulator Specification v0.2, section 13).
ALL REPORTING, STOCK AND OUTCOME DATA IS SYNTHETIC.

  python run.py --config configs/baseline.yaml --seed 42
  python run.py --config configs/baseline.yaml --set reporting.c=0.5 --out data/c_sweep/c0.5
  python run.py --config configs/baseline.yaml --check-repro      # same seed twice -> identical files
  python run.py --config configs/baseline.yaml --preview 50 12    # also write a small sample to eyeball

Writes to data/<scenario>/ (spec 4):
  model-visible: facilities.csv, reports.csv, stock_reports.csv
  hidden:        truth_facility_month.csv, truth_silence_events.csv, truth_lga_month.csv
  run_metadata.json (seed, config hash, tuned parameters, sanity checks)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from simulator import (checks, config_hash, facilities, geography, labels, layer_rng,  # noqa: E402
                       load_config, make_periods, observe, parse_override, state)


def generate_world(cfg: dict) -> dict:
    """Run all four layers. Returns every table plus the raw arrays."""
    t0 = time.time()
    seed = int(cfg["run"]["seed"])
    periods = make_periods(cfg["run"]["start_period"], int(cfg["run"]["months"]))
    calendar = np.array([int(p[4:]) for p in periods])

    geo, lgas = geography.build(cfg, layer_rng(seed, "geography"))
    fac = facilities.build(geo, lgas, cfg, layer_rng(seed, "facilities"))
    truth = state.simulate(fac, lgas, cfg, layer_rng(seed, "state"), calendar)
    reports, stock_reports, obs = observe.observe(fac, truth, cfg, layer_rng(seed, "observe"), periods)
    flags = labels.failure_flags(truth, cfg)
    silence = labels.silence_events(fac, truth, obs, cfg, periods)
    runtime = time.time() - t0

    return {"cfg": cfg, "periods": periods, "lgas": lgas, "facilities": fac, "truth": truth, "obs": obs,
            "reports": reports, "stock_reports": stock_reports, "flags": flags, "silence": silence,
            "runtime": runtime}


def truth_tables(w: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    fac, truth, flags, periods = w["facilities"], w["truth"], w["flags"], w["periods"]
    F, T = truth["distress"].shape
    ff, tt = np.meshgrid(np.arange(F), np.arange(T), indexing="ij")
    ff, tt = ff.ravel(), tt.ravel()
    fm = {"facility_id": fac.facility_id.values[ff], "period": np.array(periods)[tt]}
    for i, it in enumerate(w["cfg"]["items"]):
        fm[f"stock_{it['code']}"] = np.round(truth["stock_end"][:, i, :].ravel(), 1)
        fm[f"stockout_share_{it['code']}"] = np.round(truth["share"][:, i, :].ravel(), 3)
    for k in ("fridge_down", "power_out", "closed"):
        fm[k] = truth[k].ravel().astype(int)
    fm["staff_ratio"] = np.round(truth["staff_ratio"].ravel(), 3)
    fm["distress"] = np.round(truth["distress"].ravel(), 3)
    for k in ("fail_medicine", "fail_vaccine", "fail_staff", "fail_closed", "fail_any", "fail_serious"):
        fm[k] = flags[k].ravel().astype(int)
    for k in ("fail_next12", "fail_serious_next12", "fail_medicine_next12", "fail_vaccine_next12",
              "fail_staff_next12", "fail_closed_next12"):
        fm[k] = pd.array(flags[k].ravel(), dtype="Int64")
    tfm = pd.DataFrame(fm)

    L = len(truth["lga_keys"])
    ll, lt = np.meshgrid(np.arange(L), np.arange(T), indexing="ij")
    ll, lt = ll.ravel(), lt.ravel()
    tlm = pd.DataFrame({
        "state": [truth["lga_keys"][i][0] for i in ll], "lga": [truth["lga_keys"][i][1] for i in ll],
        "period": np.array(periods)[lt],
        "entry_backlog": w["obs"]["backlog"].ravel().astype(int),
        "funding_delay": truth["funding_lga"].ravel().astype(int),
        "insecurity_event": truth["insecurity_lga"].ravel().astype(int),
        "strike": truth["strike_lga"].ravel().astype(int),
    })
    return tfm, tlm


def summary(w: dict) -> dict:
    fac, flags, obs, truth = w["facilities"], w["flags"], w["obs"], w["truth"]
    status = obs["status"]
    return {
        "facilities": int(len(fac)), "lgas": int(len(w["lgas"])),
        "facilities_by_state": fac.state.value_counts().to_dict(),
        "lgas_by_state": w["lgas"].state.value_counts().to_dict(),
        "months": len(w["periods"]), "periods": f"{w['periods'][0]}-{w['periods'][-1]}",
        "on_time": round(float((status == 0).mean()), 3), "late": round(float((status == 1).mean()), 3),
        "missing": round(float((status == 2).mean()), 3),
        "fail_any": round(float(flags["fail_any"].mean()), 3),
        "fail_medicine": round(float(flags["fail_medicine"].mean()), 3),
        "fail_vaccine": round(float(flags["fail_vaccine"].mean()), 3),
        "fail_staff": round(float(flags["fail_staff"].mean()), 3),
        "fail_closed": round(float(flags["fail_closed"].mean()), 3),
        "fail_next12": round(float(np.nanmean(flags["fail_next12"])), 3),
        "fail_serious": round(float(flags["fail_serious"].mean()), 3),
        "fail_serious_next12": round(float(np.nanmean(flags["fail_serious_next12"])), 3),
        "silence_causes": w["silence"].cause.value_counts().to_dict(),
        "silence_insecure_share": round(float(w["silence"].insecure_area.mean()), 3),
        "stock_participants": int(obs["participant"].sum()),
        "tuned_b0": obs["b0"], "tuned_d0": obs["d0"],
        "state_lead_time_mean": truth["lead_mean"], "state_fill_rate": truth["fill_rate"],
        "supply_waves": truth["supply_waves"],
    }


def write(w: dict, out: Path, check_results: list) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    tfm, tlm = truth_tables(w)
    files = {
        "facilities.csv": w["facilities"], "reports.csv": w["reports"], "stock_reports.csv": w["stock_reports"],
        "truth_facility_month.csv": tfm, "truth_silence_events.csv": w["silence"], "truth_lga_month.csv": tlm,
    }
    hashes = {}
    for name, df in files.items():
        df.to_csv(out / name, index=False)
        hashes[name] = hashlib.sha256((out / name).read_bytes()).hexdigest()[:16]
    meta = {
        "SYNTHETIC": True,
        "note": "All reporting, stock and outcome data is synthetic. Facility locations, LGAs, wards, types and "
                "settlement types are real (GRID3 v3.0); names are neutral labels.",
        "scenario": w["cfg"]["scenario"], "seed": w["cfg"]["run"]["seed"], "config_hash": config_hash(w["cfg"]),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime_seconds": round(w["runtime"], 1), "summary": summary(w), "sanity_checks": check_results,
        "file_hashes": hashes, "parameters": w["cfg"],
    }
    (out / "run_metadata.json").write_text(json.dumps(meta, indent=2, default=str))
    return meta


def write_preview(w: dict, out: Path, n_fac: int, n_months: int, seed: int) -> Path:
    """A small sample for eyeballing: n facilities (spread across states) x
    the last n months, with visible and hidden data side by side."""
    rng = np.random.default_rng(seed)
    fac = w["facilities"]
    per_state = max(1, n_fac // fac.state.nunique())
    pick = pd.concat([g.sample(min(per_state, len(g)), random_state=int(rng.integers(1e9)))
                      for _, g in fac.groupby("state")])
    months = w["periods"][-n_months:]
    rep = w["reports"]
    rep = rep[rep.orgUnit.isin(pick.facility_id) & rep.period.isin(months)]
    wide = rep.pivot(index=["orgUnit", "period"], columns="dataElement", values="value").reset_index()
    wide = wide.merge(rep.groupby(["orgUnit", "period"], as_index=False).received_period.first(),
                      on=["orgUnit", "period"])
    tfm, _ = truth_tables(w)
    tfm = tfm[tfm.facility_id.isin(pick.facility_id) & tfm.period.isin(months)]
    grid = tfm[["facility_id", "period"]].rename(columns={"facility_id": "orgUnit"})
    visible = grid.merge(wide, on=["orgUnit", "period"], how="left")
    visible.insert(2, "report_status", np.where(visible.received_period.isna(), "missing",
                                                 np.where(visible.received_period == visible.period, "on_time", "late")))
    out.mkdir(parents=True, exist_ok=True)
    path = out / "preview.xlsx"
    with pd.ExcelWriter(path) as xl:
        pd.DataFrame({"README": [
            "SYNTHETIC SAMPLE. Locations/LGAs are real (GRID3 v3.0); everything else is simulated.",
            f"{len(pick)} facilities x last {n_months} months of scenario '{w['cfg']['scenario']}'.",
            "facilities = what the models may see about each facility.",
            "reports_visible = what DHIS2 would show (blank = not reported or element left blank).",
            "truth_hidden = what really happened. Models never see this sheet's contents.",
        ]}).to_excel(xl, sheet_name="README", index=False)
        pick.to_excel(xl, sheet_name="facilities", index=False)
        visible.to_excel(xl, sheet_name="reports_visible", index=False)
        tfm.to_excel(xl, sheet_name="truth_hidden", index=False)
        w["silence"][w["silence"].facility_id.isin(pick.facility_id)].to_excel(xl, sheet_name="silence_truth", index=False)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "configs" / "baseline.yaml"))
    ap.add_argument("--seed", type=int)
    ap.add_argument("--set", action="append", default=[], help="override, e.g. reporting.c=0.5")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--check-repro", action="store_true")
    ap.add_argument("--preview", nargs=2, type=int, metavar=("FACILITIES", "MONTHS"))
    args = ap.parse_args()

    overrides = [parse_override(s) for s in args.set]
    if args.seed is not None:
        overrides.append({"run": {"seed": args.seed}})
    cfg = load_config(args.config, overrides)
    out = args.out or ROOT / "data" / cfg["scenario"]
    if not out.is_absolute():
        out = ROOT / out

    w = generate_world(cfg)
    results = checks.run_checks(w["facilities"], w["truth"], w["obs"], w["reports"], w["stock_reports"], cfg,
                                w["runtime"])
    meta = write(w, out, results)

    if args.check_repro:
        meta2 = write(generate_world(cfg), out.parent / (out.name + "_repro"), results)
        same = meta["file_hashes"] == meta2["file_hashes"]
        results.append({"check": "Same seed twice gives identical files", "value": same, "target": "pass",
                        "passed": same})
        meta["sanity_checks"] = results
        (out / "run_metadata.json").write_text(json.dumps(meta, indent=2, default=str))
        for f in (out.parent / (out.name + "_repro")).iterdir():
            f.unlink()
        (out.parent / (out.name + "_repro")).rmdir()

    if args.preview:
        print("preview:", write_preview(w, out, *args.preview, seed=int(cfg["run"]["seed"])))

    print(json.dumps(meta["summary"], indent=2, default=str))
    print("\nSanity checks (spec 12):")
    for r in results:
        print(f"  [{'PASS' if r['passed'] else 'FAIL'}] {r['check']}: {r['value']} (target {r['target']})")
    print(f"\nWrote {out}  (runtime {w['runtime']:.1f}s, SYNTHETIC)")


if __name__ == "__main__":
    main()
