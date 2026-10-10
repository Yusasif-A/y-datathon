"""
Turns trained models + a world into the thing the officer actually sees:
one ranked row per facility, with a plain-language reason, a suggested
action and a SHAP-based "why it's flagged" breakdown.

Priority = chance of trouble x people affected, exactly as the doc specifies.
Thin/missing data never lowers a facility's rank — a facility with lots of
history simply gets a more confident score; it is never penalised for being
hard to read.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import shap

import features
from train import predict_trouble

ACTIONS = {
    "lga_wide": "Send one note to the LGA data office, not separate visits to each facility.",
    "closure": "Confirm whether the facility is open. If it has closed, redirect patients and flag the registry.",
    "sudden_drop": "Call the officer-in-charge soon, then verify through the ward committee.",
    "chronic_poor_reporter": "Schedule a support check and help the facility to report. "
                             "Not low priority: chronic non-reporters are often the least-resourced facilities.",
    "late_entry": "Chase the paperwork with the records officer; not urgent on its own.",
    "none": "No action needed this week.",
}

# Older training data merged closure and insecurity into one silence class.
ACTIONS["insecure_silence"] = ACTIONS["closure"]

# Insecurity is a modifier, not a cause: whatever the cause, never suggest a visit.
INSECURE_ACTION = "Escalate to the state desk and check by phone. Do not suggest a visit while the area is insecure."

# A facility that reports normally can still be at high risk. Its action then
# comes from the strongest factor pushing its forecast up.
FORECAST_ACTIONS = {
    "Medicine / family planning": "Call the officer-in-charge to check stock of tracer medicines; arrange resupply if short.",
    "Vaccine / cold chain": "Ask the officer-in-charge to confirm the vaccine fridge is working and check vaccine stock.",
    "Staff": "Check staffing with the officer-in-charge and discuss cover with the LGA.",
    "Infrastructure": "Check power, funding and the last supply delivery with the officer-in-charge.",
    "Distance / access": "Call the officer-in-charge; there is no nearby alternative if this facility struggles.",
    "Reporting / silence": "Call the officer-in-charge to ask why reports are slipping.",
    "Security": INSECURE_ACTION,
    "Season": "Call the officer-in-charge to check stock ahead of the seasonal peak.",
}
FORECAST_ACTION_MIN_PROB = 0.3

SILENCE_LABEL = {
    "lga_wide": "LGA-wide backlog",
    "closure": "possible closure",
    "insecure_silence": "possible closure or insecurity",
    "sudden_drop": "sudden drop",
    "chronic_poor_reporter": "chronic non-reporter",
    "late_entry": "late data entry",
    "none": "reporting normally",
}

# Facilities with fewer reported months than this get a "low confidence" tag.
LOW_CONFIDENCE_MONTHS = 6

FEATURE_GROUP = {
    "months_since_last_report": "Reporting / silence", "reported_rate_3m": "Reporting / silence",
    "reported_rate_6m": "Reporting / silence", "reported_rate_12m": "Reporting / silence",
    "reported_rate_24m": "Reporting / silence", "sudden_drop_signal": "Reporting / silence",
    "lga_concurrent_silent_frac": "Reporting / silence", "on_time_rate_6m": "Reporting / silence",
    "completeness_6m": "Reporting / silence",
    "fp_z": "Medicine / family planning", "service_trend_3m": "Medicine / family planning",
    "vacc_z": "Vaccine / cold chain", "power_reliability": "Vaccine / cold chain",
    "ors_z": "Medicine / family planning", "mal_z": "Medicine / family planning",
    "stock_days_out_now": "Medicine / family planning", "stock_reported_flag": "Medicine / family planning",
    "staff_ratio": "Staff", "service_total_z": "Staff",
    "connectivity": "Infrastructure", "records_officer_present": "Infrastructure",
    "funding_delayed": "Infrastructure", "funding_severely_delayed": "Infrastructure",
    "distance_km": "Distance / access", "population_log": "Distance / access",
    "insecurity": "Security", "urban": "Distance / access",
    "season_sin": "Season", "season_cos": "Season",
}


def _impact_score(df: pd.DataFrame) -> pd.Series:
    """People affected: population, weighted up when there's no nearby
    alternative (bigger distance = more impact if this facility fails)."""
    pop_norm = np.log1p(df["population"]) / np.log1p(df["population"]).max()
    distance_factor = 1 + np.clip(df["distance_km"], 0, 40) / 40  # 1.0 .. 2.0
    impact = pop_norm * distance_factor
    return impact / impact.max()


def _reason_sentence(row) -> str:
    pred_cause = row["pred_cause"]
    bits = []
    msr = int(row["months_since_last_report"])
    if msr >= 1:
        hist = row["reported_rate_24m"]
        if hist >= 0.85:
            bits.append(f"reported reliably for a long stretch, now silent {msr} month{'s' if msr != 1 else ''}")
        else:
            bits.append(f"silent {msr} month{'s' if msr != 1 else ''}")
    else:
        bits.append("reports arriving normally")
    if row["trouble_prob"] >= FORECAST_ACTION_MIN_PROB and row["top_factor"]:
        bits.append(f"main risk factor: {row['top_factor'].lower()}")
    if row["power_reliability"] < 0.35:
        bits.append("unreliable power")
    if row["staff_ratio"] < 0.75:
        bits.append("fewer staff than the standard")
    if row["distance_km"] > 15:
        bits.append(f"nearest alternative {row['distance_km']:.0f} km away")
    if pred_cause == "lga_wide":
        bits.append("other facilities in the same LGA are quiet too — looks like a data-entry backlog")
    if row["insecurity"]:
        bits.append("in an area with an active insecurity concern")
    if row["low_confidence"]:
        bits.append("little reporting history, so this score is low confidence")
    return ", ".join(bits).capitalize() + "."


def _suggested_action(row) -> str:
    if row["insecurity"]:
        return INSECURE_ACTION
    cause = row["pred_cause"]
    if cause == "none":
        if row["trouble_prob"] >= FORECAST_ACTION_MIN_PROB and row["top_factor"]:
            return FORECAST_ACTIONS.get(row["top_factor"], ACTIONS["sudden_drop"])
        return ACTIONS["none"]
    return ACTIONS.get(cause, ACTIONS["none"])


def build_risk_table(world: dict, bundle: dict, as_of_month: int | None = None) -> pd.DataFrame:
    panel = features.build_panel(world)
    if as_of_month is None:
        as_of_month = world["months"] - 1
    snap = panel[panel["month_idx"] == as_of_month].copy()

    cols = bundle["trouble_cols"]
    p_main, p_ens, p_unc = predict_trouble(bundle["trouble_model"], bundle["trouble_ensemble"], cols, snap)
    snap["trouble_prob"] = p_main
    snap["trouble_uncertainty"] = p_unc

    # Silence-type model (only meaningful once a facility has missed at least
    # one month — a facility that reported this month has nothing to classify).
    s_cols = bundle["silence_cols"]
    s_cats = bundle["silence_cats"]
    quiet_mask = snap["months_since_last_report"] >= 1
    snap["pred_cause"] = "none"
    if quiet_mask.any():
        proba = bundle["silence_model"].predict(snap.loc[quiet_mask, s_cols])
        snap.loc[quiet_mask, "pred_cause"] = [s_cats[int(c)] for c in proba]
        # A facility that IS silent can't be labelled "none" (no concern) —
        # that class only means "no identifiable cause found", which for an
        # actual gap defaults to the mildest, least alarming category.
        snap.loc[quiet_mask & (snap["pred_cause"] == "none"), "pred_cause"] = "late_entry"

    # Expected-report gap.
    e_cols = bundle["expected_cols"]
    snap["expected_service_total"] = bundle["expected_model"].predict(snap[e_cols])
    snap["report_gap"] = np.where(
        snap["reported"], 0.0,
        np.clip(snap["expected_service_total"] / snap["expected_service_total"].clip(lower=1).mean(), 0, 3),
    )

    snap["impact_score"] = _impact_score(snap)
    snap["priority"] = snap["trouble_prob"] * snap["impact_score"]

    # SHAP explanation on the main calibrated trouble model's base estimator
    # when available; otherwise fall back to the first ensemble member (both
    # are plain LightGBM trees, so TreeExplainer works either way).
    try:
        base_est = bundle["trouble_model"].calibrated_classifiers_[0].estimator
        explainer = shap.TreeExplainer(base_est)
        shap_values = explainer.shap_values(snap[cols])
        if isinstance(shap_values, list):
            shap_values = shap_values[1]
    except Exception:
        explainer = shap.TreeExplainer(bundle["trouble_ensemble"][0])
        shap_values = explainer.shap_values(snap[cols])
        if isinstance(shap_values, list):
            shap_values = shap_values[1]

    shap_df = pd.DataFrame(shap_values, columns=cols, index=snap.index)
    group_shap = pd.DataFrame(index=snap.index)
    for col, grp in FEATURE_GROUP.items():
        if col in shap_df.columns:
            group_shap[grp] = group_shap.get(grp, 0) + shap_df[col]
    snap["reason_breakdown"] = [
        sorted(({"group": g, "impact": round(float(v), 4)} for g, v in row.items()),
              key=lambda x: -abs(x["impact"]))
        for _, row in group_shap.iterrows()
    ]
    top_group = group_shap.idxmax(axis=1)
    snap["top_factor"] = top_group.where(group_shap.max(axis=1) > 0, "")

    # Thin history is never read as "fine": the score stands, but it is tagged
    # low confidence so the officer knows, and capped in the officer list.
    reports = world["reports"]
    history = (reports[reports["month_idx"] <= as_of_month]
               .groupby("facility_id")["reported"].sum().rename("history_months"))
    snap = snap.merge(history, on="facility_id", how="left")
    snap["history_months"] = snap["history_months"].fillna(0).astype(int)
    snap["low_confidence"] = snap["history_months"] < LOW_CONFIDENCE_MONTHS

    snap["tag"] = pd.cut(snap["priority"], bins=[-1, snap["priority"].quantile(0.6),
                                                 snap["priority"].quantile(0.88), 2],
                        labels=["LOW", "MEDIUM", "HIGH"])
    snap["reason"] = snap.apply(_reason_sentence, axis=1)
    snap["suggested_action"] = snap.apply(_suggested_action, axis=1)
    snap["silence_label"] = snap["pred_cause"].map(SILENCE_LABEL).fillna("reporting normally")

    name_cols = ["facility_id", "name", "lga", "state", "lat", "lon", "population", "urban",
                "power_reliability", "connectivity", "staff_count", "staff_standard", "distance_km",
                "insecurity"]
    optional_cols = [c for c in ("in_charge_phone_sample", "whatsapp_opt_in") if c in world["facilities"].columns]
    name_cols = name_cols + optional_cols
    out = snap.merge(world["facilities"][name_cols], on="facility_id", suffixes=("", "_fac"))
    keep = name_cols + [
        "reported", "months_since_last_report", "reported_rate_12m", "on_time_rate_6m",
        "trouble_prob", "trouble_uncertainty", "pred_cause", "silence_label", "impact_score",
        "priority", "tag", "reason", "suggested_action", "reason_breakdown", "expected_service_total",
        "service_total", "month", "month_idx", "top_factor", "history_months", "low_confidence",
    ]
    out = out[[c for c in keep if c in out.columns]].sort_values("priority", ascending=False).reset_index(drop=True)
    out["rank"] = np.arange(1, len(out) + 1)
    return out


def cluster_alerts(world: dict, risk_table: pd.DataFrame, min_frac: float = 0.4) -> list:
    """LGAs where a large share of facilities went quiet together this
    month — the state-overview / officer-banner 'likely a backlog' alert."""
    month = risk_table["month"].iloc[0] if len(risk_table) else None
    alerts = []
    for lga, g in risk_table.groupby("lga"):
        silent = g[g["months_since_last_report"] >= 1]
        frac = len(silent) / len(g) if len(g) else 0
        if len(silent) >= 3 and frac >= min_frac:
            cause = silent["pred_cause"].mode().iat[0] if len(silent) else "lga_wide"
            if silent["insecurity"].astype(bool).mean() >= 0.5:
                cause = "insecure"
                msg = f"{len(silent)} facilities in {lga} are quiet after an insecurity report. Escalate — do not send visits."
            else:
                msg = f"{len(silent)} facilities in {lga} went quiet together in {month}. Likely a data-entry backlog — contact the LGA data office."
            alerts.append({"lga": lga, "count": int(len(silent)), "month": month, "message": msg, "cause": cause})
    return alerts
