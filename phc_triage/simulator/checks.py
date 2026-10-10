"""
Sanity checks (spec 12). A world that fails them is not ready for ML.
Each check returns {check, value, target, passed}.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .observe import LATE, MISSING, ON_TIME

TRUTH_ONLY_COLUMNS = {
    "stock", "stockout_share", "fridge_down", "power_out", "staff_ratio", "closed", "distress", "fail_medicine",
    "fail_vaccine", "fail_staff", "fail_closed", "fail_any", "fail_next12", "fail_serious", "cause", "entry_backlog",
    "funding_delay", "insecurity_event", "strike", "habit", "utilisation",
}


def _row(name, value, target, passed):
    return {"check": name, "value": value, "target": target, "passed": bool(passed)}


def run_checks(fac, truth, obs, reports, stock_reports, cfg, runtime_s) -> list[dict]:
    ck = cfg["checks"]
    status = obs["status"]
    out = []

    rep_rate = float((status != MISSING).mean())
    lo, hi = ck["reporting_rate_range"]
    out.append(_row("Reporting rate (received on time or late)", round(rep_rate, 3), f"{lo:.0%}-{hi:.0%}",
                    lo <= rep_rate <= hi))

    on_time = float((status == ON_TIME).mean())
    lo, hi = ck["on_time_range"]
    out.append(_row("On-time reporting rate", round(on_time, 3), f"{lo:.0%}-{hi:.0%}", lo <= on_time <= hi))

    late = float((status == LATE).mean())
    lo, hi = ck["late_range"]
    out.append(_row("Share of facility-months reported late", round(late, 3), f"{lo:.0%}-{hi:.0%}", lo <= late <= hi))

    urban = fac.urban.values == 1
    gap = float((status[urban] != MISSING).mean() - (status[~urban] != MISSING).mean())
    out.append(_row("Rural reporting rate below urban by", round(gap, 3), f">= {ck['rural_gap_min']:.0%}",
                    gap >= ck["rural_gap_min"]))

    bl = float(obs["backlog"].mean())
    lo, hi = ck["backlog_range"]
    out.append(_row("LGA-months with an entry backlog", round(bl, 3), f"{lo:.0%}-{hi:.0%}", lo <= bl <= hi))

    in_stock = truth["stock_end"] > 0.5                              # [F, I, T]
    avail = float(in_stock.mean())
    lo, hi = ck["availability_range"]
    out.append(_row("Tracer availability (facility-item-months in stock at month end)", round(avail, 3),
                    f"{lo:.0%}-{hi:.0%}", lo <= avail <= hi))
    by_state = {s: round(float(in_stock[truth["s_idx"] == i].mean()), 3) for i, s in enumerate(truth["states"])}
    lo, hi = ck["state_availability_range"]
    out.append(_row("Tracer availability by state", by_state, f"each {lo:.0%}-{hi:.0%}, and they differ",
                    all(lo <= v <= hi for v in by_state.values()) and max(by_state.values()) - min(by_state.values()) > 0.02))

    # Stockouts lower related service volumes.
    items = [it["code"] for it in cfg["items"]]
    period_idx = {p: i for i, p in enumerate(sorted(reports.period.unique()))}
    fid_idx = pd.Series(np.arange(len(fac)), index=fac.facility_id)
    corrs = {}
    for de in cfg["data_elements"]:
        if not de.get("item"):
            continue
        r = reports[(reports.dataElement == de["code"]) & reports.value.notna()]
        f_ = fid_idx[r.orgUnit].values
        t_ = r.period.map(period_idx).values
        share = truth["share"][f_, items.index(de["item"]), t_]
        per_cap = r.value.values / fac.catchment_pop.values[f_]
        corrs[de["code"]] = round(float(np.corrcoef(share, per_cap)[0, 1]), 3)
    out.append(_row("Stockouts lower related volumes (correlation)", corrs, "all clearly negative (< -0.05)",
                    all(v < -0.05 for v in corrs.values())))

    v = reports.value.dropna()
    counts_ok = bool((v >= 0).all() and (v == np.round(v)).all())
    received = int((status != MISSING).sum())
    rows_ok = reports.groupby(["orgUnit", "period"]).ngroups == received
    out.append(_row("No negative or non-integer counts; no rows for missing reports; blanks stay blank",
                    {"counts_ok": counts_ok, "facility_months_with_rows": rows_ok,
                     "blank_values": int(reports.value.isna().sum())}, "pass", counts_ok and rows_ok))

    visible_cols = set(fac.columns) | set(reports.columns) | set(stock_reports.columns)
    leaked = sorted(c for c in visible_cols if c in TRUTH_ONLY_COLUMNS or c.startswith("truth_"))
    out.append(_row("Truth columns absent from model-visible tables", leaked or "none", "none", not leaked))

    out.append(_row("Runtime (seconds)", round(runtime_s, 1), f"< {ck['max_runtime_seconds']}",
                    runtime_s < ck["max_runtime_seconds"]))
    return out
