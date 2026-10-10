"""
Loads the hand-built synthetic dataset from `stimulate-data/` (real Oyo
State LGA names, 311 facilities, 36 months, two pre-built worlds) into the
exact `world` dict shape `simulator.generate_world()` produces, so the rest
of the pipeline (features.py, train.py, scoring.py) needs no changes.

World A = medium link between trouble and reporting (train here).
World B = weaker link, noisier, more chronic non-reporters (stress test).

The placeholder `rank_in_state` / `flag` / `probability_of_trouble_PLACEHOLDER`
columns in file 1 are the dataset author's own heuristic, explicitly "not the
ML" — they exist so a dashboard could be built before models were ready.
This loader ignores them entirely; our three trained models replace them.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parents[2] / "stimulate-data"

STATE_NAME = "Oyo State (sample)"

POWER_RELIABILITY = {"reliable": 0.85, "unreliable": 0.35, "none": 0.05}
CONNECTIVITY_SCORE_MAX = 2.0
SILENCE_CAUSE_MAP = {
    "late_data_entry": "late_entry",
    "chronic_poor_reporter": "chronic_poor_reporter",
    "sudden_drop": "sudden_drop",
    "lga_quiet": "lga_wide",
    "closure_or_insecurity": "insecure_silence",
}
TRACER_ITEM_CODES = ["ACT", "OXY", "DEPO", "PENTA", "ORS"]


def _period_to_month(period: int) -> str:
    s = str(int(period))
    return f"{s[:4]}-{s[4:]}"


def available() -> bool:
    return (DATA_DIR / "1_facilities_and_risk.csv").exists()


def load_world(world: str = "A") -> dict:
    fac_raw = pd.read_csv(DATA_DIR / "1_facilities_and_risk.csv")
    fac_raw = fac_raw[fac_raw["world"] == world].copy()
    rep_raw = pd.read_csv(DATA_DIR / "2_facility_month_reports.csv")
    rep_raw = rep_raw[rep_raw["world"] == world].copy()
    hid_raw = pd.read_csv(DATA_DIR / "3_HIDDEN_TRUTH_labels_only.csv")
    hid_raw = hid_raw[hid_raw["world"] == world].copy()

    periods = sorted(rep_raw["period"].unique())
    period_to_idx = {p: i for i, p in enumerate(periods)}
    months = [_period_to_month(p) for p in periods]
    lgas = sorted(fac_raw["lga"].unique().tolist())

    facilities = pd.DataFrame({
        "facility_id": fac_raw["facility_id"],
        "name": fac_raw["facility_name"],
        "lga": fac_raw["lga"],
        "state": STATE_NAME,
        "lat": fac_raw["latitude"].astype(float),
        "lon": fac_raw["longitude"].astype(float),
        "urban": fac_raw["setting"].eq("urban"),
        "population": fac_raw["population_served"].astype(int),
        "power_reliability": fac_raw["power_supply"].map(POWER_RELIABILITY).fillna(0.35),
        "connectivity": fac_raw["connectivity_score"].astype(float) / CONNECTIVITY_SCORE_MAX,
        "staff_count": fac_raw["staff_actual"].astype(int),
        "staff_standard": fac_raw["staff_standard"].astype(int),
        "distance_km": fac_raw["distance_to_next_facility_km"].astype(float),
        "funding_status": fac_raw["funding_status"],
        "insecurity": fac_raw["insecurity_flag"].astype(bool),
        "records_officer_present": fac_raw["records_officer_in_lga"].astype(bool),
        # Carried through for the WhatsApp UI hint only — never a model feature.
        "in_charge_phone_sample": fac_raw["in_charge_phone_sample"].astype(str),
        "whatsapp_opt_in": fac_raw["whatsapp_opt_in"].astype(bool),
        "facility_type": fac_raw["facility_type"],
        "ward": fac_raw["ward"],
    }).reset_index(drop=True)

    rep_raw["month_idx"] = rep_raw["period"].map(period_to_idx)
    rep_raw["month"] = rep_raw["period"].map(_period_to_month)
    service_cols = {
        "ANC_1ST": "anc_visits", "DELIVERIES": "deliveries", "FP_NEW": "family_planning_visits",
        "PENTA3": "penta3_vaccinations", "MALARIA_ACT": "antimalarial_treatments", "OPD_TOTAL": "opd_total",
    }
    reports = pd.DataFrame({
        "facility_id": rep_raw["facility_id"], "month": rep_raw["month"], "month_idx": rep_raw["month_idx"],
        "reported": rep_raw["status"].ne("missing"), "on_time": rep_raw["status"].eq("on_time"),
        "completeness": rep_raw["completeness"].astype(float),
    })
    for src, dst in service_cols.items():
        reports[dst] = rep_raw[src]
    reports["service_total"] = reports[list(service_cols.values())].sum(axis=1, skipna=True)
    reports.loc[~reports["reported"], [*service_cols.values(), "service_total"]] = np.nan
    for src in ["ACT", "OXY", "DEPO", "PENTA", "ORS"]:
        col = f"days_out_{src}"
        if col in rep_raw.columns:
            reports[col] = rep_raw[col]
    reports = reports.sort_values(["facility_id", "month_idx"]).reset_index(drop=True)

    hid_raw["month_idx"] = hid_raw["period"].map(period_to_idx)
    items_out = hid_raw["items_out_list"].fillna("")

    def stockout_cols():
        out = {}
        for code in TRACER_ITEM_CODES:
            out[f"stockout_{code}"] = items_out.apply(lambda s, c=code: c in s.split("|"))
        return out

    hidden = pd.DataFrame({
        "facility_id": hid_raw["facility_id"], "month_idx": hid_raw["month_idx"],
        **stockout_cols(),
        "fridge_working": ~hid_raw["fridge_failed"].astype(bool),
        "closed": hid_raw["closed"].astype(bool),
        "understaffed": hid_raw["staff_shortage"].astype(bool),
        "trouble_now": hid_raw["trouble_now"].astype(bool),
        "lga_backlog_month": hid_raw["lga_quiet_event"].astype(bool),
        "true_cause": hid_raw["silence_cause"].map(SILENCE_CAUSE_MAP).fillna("none"),
    })
    hidden["distress"] = hidden[["closed", "understaffed"]].mean(axis=1) * 0.5 + \
        (~hidden["fridge_working"]).astype(float) * 0.25 + \
        (hid_raw["items_out"].astype(float) > 0).astype(float) * 0.25
    hidden = hidden.merge(facilities[["facility_id", "staff_count"]], on="facility_id", how="left")
    hidden = hidden.sort_values(["facility_id", "month_idx"]).reset_index(drop=True)

    return dict(
        seed=world, link_strength={"A": "medium", "B": "weak"}.get(world, world), months=len(periods),
        periods=months, lgas=lgas, state=STATE_NAME,
        facilities=facilities, reports=reports, hidden=hidden,
        backlog_events=[], source="stimulate-data",
    )


def lga_coordinator_phones(world: str = "A") -> dict:
    path = DATA_DIR / "4_state_overview.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df = df[df["world"] == world]
    return dict(zip(df["lga"], df["lga_coordinator_phone_sample"].astype(str)))
