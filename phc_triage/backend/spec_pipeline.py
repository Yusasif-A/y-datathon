"""
Serves the spec-built world (phc_triage/data/baseline, real GRID3 locations,
simulated reporting) and the models trained by phc_triage/ml to the
dashboard, in the same risk-table shape scoring.py produces, so main.py and
the dashboard need no other changes. ALL OUTCOME DATA IS SYNTHETIC.

Used when phc_triage/models/bundle.joblib and phc_triage/data/baseline exist;
otherwise main.py falls back to the original stimulate-data pipeline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import shap

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ml import data as mldata  # noqa: E402
from ml import features as mlfeatures  # noqa: E402
from ml import models as mlmodels  # noqa: E402

import scoring  # noqa: E402

WORLD = "baseline"
BUNDLE = ROOT / "models" / "bundle.joblib"
VALIDATION = ROOT / "reports" / "validation.json"

# Model-2 causes -> the keys scoring.py's actions and labels use.
CAUSE_KEY = {"entry_delay": "late_entry", "chronic": "chronic_poor_reporter", "real_problem": "sudden_drop",
             "lga_backlog": "lga_wide", "closure": "closure"}

GROUP = {}
for c in ["on_time_now", "months_silent", "known_rate_3", "known_rate_6", "known_rate_12", "habit_rate",
          "on_time_rate_12", "late_share_12", "completeness_3", "history_months", "lga_silent_now",
          "lga_silent_prev", "vs_lga_rate_3", "exp_gap_now", "exp_missing_per_1000"]:
    GROUP[c] = "Reporting / silence"
for c in ["opd_ratio", "opd_trend", "mal_ratio", "mal_trend", "fp_ratio", "fp_trend", "anc_ratio", "anc_trend",
          "deliv_ratio", "deliv_trend", "opd_per_1000", "stock_participant", "stock_months_since",
          "stock_other_max_days", "stock_items_out", "zero_form_now"]:
    GROUP[c] = "Medicine / family planning"
for c in ["penta_ratio", "penta_trend", "stock_vac_days", "fridge_functional", "power_ord"]:
    GROUP[c] = "Vaccine / cold chain"
for c in ["hr_compliance", "level"]:
    GROUP[c] = "Staff"
for c in ["conn_ord", "records_officer", "bhcpf_supported"]:
    GROUP[c] = "Infrastructure"
for c in ["dist_nearest_alt_km", "dist_lga_hq_km", "log_pop", "urban"]:
    GROUP[c] = "Distance / access"
for c in ["insecurity_exposure", "state_Gombe", "state_Kano", "state_Oyo"]:
    GROUP[c] = "Security" if c == "insecurity_exposure" else "Distance / access"
for c in ["season_sin", "season_cos"]:
    GROUP[c] = "Season"

TYPE_WORDS = {"medicine": "medicine stockouts", "vaccine": "vaccine or cold-chain failure", "staff": "staff shortage",
              "closed": "closure"}
# At the latest month ~35% of reports are normally not in yet; a real backlog
# makes nearly the whole LGA quiet.
CLUSTER_MIN_FRAC = 0.7


def available() -> bool:
    return BUNDLE.exists() and (mldata.DATA / WORLD / "reports.csv").exists()


def _month_label(p: str) -> str:
    return f"{p[:4]}-{p[4:]}"


def load():
    """Returns (world, bundle, validation) for main.py."""
    vis = mldata.load_visible(WORLD)
    bundle = joblib.load(BUNDLE)
    fac = vis.facilities
    T = len(vis.periods)
    as_of = T - 1
    known = (vis.recv_t >= 0) & (vis.recv_t <= as_of)
    total = np.nansum(vis.values, axis=2)
    ff, tt = np.meshgrid(np.arange(len(fac)), np.arange(T), indexing="ij")
    reports = pd.DataFrame({
        "facility_id": fac.facility_id.values[ff.ravel()], "month_idx": tt.ravel(),
        "month": np.array([_month_label(p) for p in vis.periods])[tt.ravel()],
        "reported": known.ravel(), "service_total": np.where(known, total, np.nan).ravel(),
    })
    facilities = fac.rename(columns={"catchment_pop": "population", "dist_nearest_alt_km": "distance_km"})
    world = {
        "seed": "baseline", "source": "simulator (GRID3 v3.0 locations)", "link_strength": "c=1",
        "state": "Oyo, Kano and Gombe (sample)", "periods": [_month_label(p) for p in vis.periods],
        "lgas": sorted(fac.lga.unique()), "months": T, "facilities": facilities, "reports": reports,
        "visible": vis, "as_of": as_of,
    }
    validation = json.loads(VALIDATION.read_text()) if VALIDATION.exists() else {}
    return world, bundle, validation


def _reason(r) -> str:
    bits = []
    m = int(r.months_since_last_report)
    if m >= 1:
        bits.append(("reported reliably, now silent " if r.habit_rate >= 0.85 else "silent ")
                    + f"{m} month{'s' if m != 1 else ''}")
    else:
        bits.append("reports arriving normally")
    likely = [(k, r[f"p_{k}"]) for k in TYPE_WORDS if r[f"p_{k}"] >= 0.4]
    if likely and r.trouble_prob >= scoring.FORECAST_ACTION_MIN_PROB:
        bits.append("at risk of " + " and ".join(TYPE_WORDS[k] for k, _ in sorted(likely, key=lambda x: -x[1])[:2]))
    if r.stock_items_out > 0:
        bits.append(f"last stock report showed {int(r.stock_items_out)} item(s) out")
    if not r.fridge_functional:
        bits.append("no working vaccine fridge")
    if r.hr_compliance < 0.15:
        bits.append("very low staffing")
    if r.distance_km > 5:
        bits.append(f"nearest alternative {r.distance_km:.0f} km away")
    if r.pred_cause == "lga_wide":
        bits.append("other facilities in the LGA are quiet too — looks like a data-entry backlog")
    if r.insecurity:
        bits.append("in an area with an active insecurity concern")
    if r.low_confidence:
        bits.append("little reporting history, so this score is low confidence")
    return ", ".join(bits).capitalize() + "."


def build_risk_table(world: dict, bundle: dict) -> pd.DataFrame:
    vis = world["visible"]
    t = world["as_of"]
    panel = mlfeatures.build_panel(vis, months=range(t, t + 1))
    snap = mlmodels.score(bundle, panel).reset_index(drop=True)

    cols = mlmodels.M3_FEATURES
    sv = shap.TreeExplainer(bundle["m3"]["main"].model).shap_values(snap[cols])
    sv = sv[1] if isinstance(sv, list) else sv
    sv = pd.DataFrame(sv, columns=cols)
    groups = sv.T.groupby(lambda c: GROUP.get(c, "Other")).sum().T
    snap["reason_breakdown"] = [
        sorted(({"group": g, "impact": round(float(v), 4)} for g, v in row.items()), key=lambda x: -abs(x["impact"]))
        for _, row in groups.iterrows()
    ]
    snap["top_factor"] = groups.idxmax(axis=1).where(groups.max(axis=1) > 0, "")

    out = snap.merge(world["facilities"][["facility_id", "name", "lat", "lon", "power", "connectivity", "ward",
                                          "facility_type", "distance_km"]], on="facility_id")
    out = out.rename(columns={"catchment_pop": "population", "p_trouble": "trouble_prob",
                              "p_uncertainty": "trouble_uncertainty", "months_silent": "months_since_last_report",
                              "known_rate_12": "reported_rate_12m", "on_time_rate_12": "on_time_rate_6m",
                              "opd_expected": "expected_service_total", "opd_now": "service_total"})
    out["pred_cause"] = out.pred_cause.map(CAUSE_KEY).fillna("none")
    out["insecurity"] = out.insecure_modifier.astype(bool)
    out["reported"] = out.on_time_now.astype(bool)
    out["power_reliability"] = out.power_ord / 2
    out["low_confidence"] = out.history_months < scoring.LOW_CONFIDENCE_MONTHS
    out["impact_score"] = out.impact / out.impact.max()
    out["priority"] = out.trouble_prob * out.impact_score
    out["tag"] = pd.cut(out.priority, bins=[-1, out.priority.quantile(0.6), out.priority.quantile(0.88), 2],
                        labels=["LOW", "MEDIUM", "HIGH"])
    out["reason"] = out.apply(_reason, axis=1)
    out["suggested_action"] = out.apply(scoring._suggested_action, axis=1)
    out["silence_label"] = out.pred_cause.map(scoring.SILENCE_LABEL).fillna("reporting normally")
    out["month"] = _month_label(vis.periods[t])
    out["month_idx"] = t
    out["staff_count"] = 0
    out["staff_standard"] = 0
    out["tiles"] = [{
        "staff": f"{r.hr_compliance:.0%} of staffing standard",
        "cold_chain": ("Fridge working" if r.fridge_functional else "No working fridge") + f"; power: {r.power}",
        "connectivity": r.connectivity,
    } for r in out.itertuples()]
    out["failure_types"] = [{k: round(float(r[f"p_{k}"]), 3) for k in TYPE_WORDS} for _, r in out.iterrows()]
    out = out.sort_values("priority", ascending=False).reset_index(drop=True)
    out["rank"] = np.arange(1, len(out) + 1)
    return out
