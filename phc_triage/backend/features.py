"""
Turn a simulated world into a feature table the models are allowed to see,
plus the hidden-truth labels used only for training and grading.

Hard rule: every column under FEATURE_COLUMNS is something a real system would
actually have (facility traits + what was reported). Nothing derived from
`world["hidden"]` is ever a feature — only a label.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

STRUCTURAL_COLS = [
    "population", "urban", "power_reliability", "connectivity",
    "staff_count", "staff_standard", "distance_km", "insecurity",
    "records_officer_present",
]

FEATURE_COLUMNS = [
    "population_log", "urban", "power_reliability", "connectivity",
    "staff_ratio", "distance_km", "insecurity", "records_officer_present",
    "funding_delayed", "funding_severely_delayed",
    "months_since_last_report", "reported_rate_3m", "reported_rate_6m",
    "reported_rate_12m", "reported_rate_24m", "on_time_rate_6m",
    "completeness_6m", "sudden_drop_signal", "lga_concurrent_silent_frac",
    "service_total_z", "service_trend_3m", "fp_z", "vacc_z", "ors_z", "mal_z",
    "stock_days_out_now", "stock_reported_flag",
    "season_sin", "season_cos",
]

CAUSE_LABELS = ["none", "late_entry", "chronic_poor_reporter", "sudden_drop", "lga_wide", "insecure_silence"]


def _funding_dummies(df):
    df = df.copy()
    df["funding_delayed"] = (df["funding_status"] == "delayed").astype(int)
    df["funding_severely_delayed"] = (df["funding_status"] == "severely_delayed").astype(int)
    return df


def build_panel(world: dict) -> pd.DataFrame:
    """One row per facility-month, with only-the-past rolling features
    (no leakage: a feature for month m never uses month m's own report)."""
    facilities = _funding_dummies(world["facilities"])
    reports = world["reports"].sort_values(["facility_id", "month_idx"]).reset_index(drop=True)
    hidden = world["hidden"].sort_values(["facility_id", "month_idx"]).reset_index(drop=True)
    months = world["months"]

    panel = reports.merge(facilities, on="facility_id", how="left")

    # Rolling reporting features computed causally per facility.
    g = panel.groupby("facility_id")
    panel["reported_int"] = panel["reported"].astype(int)
    for window, name in [(3, "3m"), (6, "6m"), (12, "12m"), (24, "24m")]:
        panel[f"reported_rate_{name}"] = (
            g["reported_int"].transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
        )
    panel["on_time_int"] = panel["on_time"].astype(int)
    panel["on_time_rate_6m"] = g["on_time_int"].transform(lambda s: s.shift(1).rolling(6, min_periods=1).mean())
    panel["completeness_6m"] = g["completeness"].transform(lambda s: s.shift(1).rolling(6, min_periods=1).mean())

    def months_since_report(s):
        out = np.zeros(len(s))
        gap = np.nan
        prev_reported = s.shift(1).values
        idx = np.arange(len(s))
        last_true = -1
        res = []
        for i, v in enumerate(prev_reported):
            if v == 1:
                last_true = i
            res.append(i - last_true if last_true >= 0 else i + 1)
        return pd.Series(res, index=s.index)
    panel["months_since_last_report"] = g["reported_int"].transform(months_since_report)

    # Sudden-drop signal: was a very reliable reporter, recent rate fell off a cliff.
    panel["sudden_drop_signal"] = (
        (panel["reported_rate_24m"].fillna(0) > 0.85) & (panel["reported_rate_3m"].fillna(0) < 0.4)
    ).astype(int)

    # Service-volume anomaly vs the facility's OWN trailing baseline (causal).
    panel["service_total_filled"] = panel["service_total"]
    base_mean = g["service_total"].transform(lambda s: s.shift(1).rolling(12, min_periods=3).mean())
    base_std = g["service_total"].transform(lambda s: s.shift(1).rolling(12, min_periods=3).std())
    panel["service_total_z"] = ((panel["service_total"] - base_mean) / base_std.replace(0, np.nan)).clip(-5, 5)
    recent_mean = g["service_total"].transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    panel["service_trend_3m"] = ((recent_mean - base_mean) / base_mean.replace(0, np.nan)).clip(-1, 1)

    for col, out in [("family_planning_visits", "fp_z"), ("penta3_vaccinations", "vacc_z"),
                     ("ors_given", "ors_z"), ("antimalarial_treatments", "mal_z")]:
        if col not in panel.columns:
            continue
        bm = g[col].transform(lambda s: s.shift(1).rolling(12, min_periods=3).mean())
        bs = g[col].transform(lambda s: s.shift(1).rolling(12, min_periods=3).std())
        panel[out] = ((panel[col] - bm) / bs.replace(0, np.nan)).clip(-5, 5)

    # Stock days-out: real signal "where it exists" (the doc's own phrasing) —
    # only some facilities report it, so it's sparse by design, never faked.
    stock_cols = [c for c in panel.columns if c.startswith("days_out_")]
    if stock_cols:
        panel["stock_days_out_now"] = panel[stock_cols].fillna(0).sum(axis=1)
        panel["stock_reported_flag"] = panel[stock_cols].notna().any(axis=1).astype(int)

    panel["population_log"] = np.log1p(panel["population"])
    panel["staff_ratio"] = panel["staff_count"] / panel["staff_standard"]
    panel["season_sin"] = np.sin(2 * np.pi * (panel["month_idx"] % 12) / 12)
    panel["season_cos"] = np.cos(2 * np.pi * (panel["month_idx"] % 12) / 12)

    # LGA-concurrent-silence fraction: how many OTHER facilities in the same
    # LGA were also silent this month (observable — it's just counting
    # reports, no hidden truth involved).
    silent_by_lga_month = (
        panel.assign(silent=1 - panel["reported_int"])
        .groupby(["lga", "month_idx"])["silent"].mean()
        .rename("lga_concurrent_silent_frac")
    )
    panel = panel.merge(silent_by_lga_month, on=["lga", "month_idx"], how="left")

    for c in FEATURE_COLUMNS:
        if c not in panel.columns:
            panel[c] = 0.0
    panel[FEATURE_COLUMNS] = panel[FEATURE_COLUMNS].fillna(0.0)
    panel["urban"] = panel["urban"].astype(int)
    panel["insecurity"] = panel["insecurity"].astype(int)
    panel["records_officer_present"] = panel["records_officer_present"].astype(int)

    # ── Labels (hidden truth — never a feature) ────────────────────────────
    hid = hidden[["facility_id", "month_idx", "trouble_now", "true_cause", "lga_backlog_month", "distress"]]
    panel = panel.merge(hid, on=["facility_id", "month_idx"], how="left", suffixes=("", "_hidden"))

    hid_by_fac = hidden.set_index(["facility_id", "month_idx"])["trouble_now"]
    def trouble_next_1_2(row):
        f, m = row["facility_id"], row["month_idx"]
        vals = []
        for k in (1, 2):
            key = (f, m + k)
            if key in hid_by_fac.index:
                vals.append(bool(hid_by_fac.loc[key]))
        return int(any(vals)) if vals else np.nan
    panel["trouble_next_1_2m"] = panel.apply(trouble_next_1_2, axis=1)

    # Expected-report model target: service_total on months that WERE reported.
    panel["is_last_month"] = panel["month_idx"] == (months - 1)
    return panel
