"""
Shared wording and rules for the risk table spec_pipeline.py builds: the
plain-language reason, the suggested action per silence cause, and the
LGA-wide cluster alerts.

Priority = chance of trouble x people affected, exactly as the doc specifies.
Thin/missing data never lowers a facility's rank — a facility with lots of
history simply gets a more confident score; it is never penalised for being
hard to read.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

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
