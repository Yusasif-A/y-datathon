"""
Layer C (spec 7): what a DHIS2-like system sees. Produces the model-visible
reports.csv (DHIS2 long shape) and stock_reports.csv from the hidden state.

Reporting (spec 7.1):
  p_report = logistic(b0 + b_rec*records_officer + b_conn*connectivity_score
                      - b_work*workload + lga_effect + state_effect + facility_habit
                      - c*distress)
  p_late   = logistic(d0 - d_conn*connectivity_score + d_work*workload + 0.5*c*distress)
b0 is tuned so the reporting rate (reports received, on time or late, as
DHIS2 counts it) hits national_target_rate; d0 is tuned so the late share
hits late_target_rate [Interp: tuning b0 to an 80% ON-TIME rate saturates
p_report at ~1, which switches off the distress link c].
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import draw_int, logistic

CONN_SCORE = {"good": 1.0, "poor": 0.0, "none": -1.0}
ON_TIME, LATE, MISSING = 0, 1, 2


def _bisect(fn, target, lo=-15.0, hi=15.0, increasing=True, iters=60):
    for _ in range(iters):
        mid = (lo + hi) / 2
        v = fn(mid)
        if (v < target) == increasing:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def reporting_params(fac, truth, cfg, rng):
    rp = cfg["reporting"]
    F = len(fac)
    L, S = len(truth["lga_keys"]), len(truth["states"])
    habit = rng.normal(0, rp["facility_habit_sd"], F)
    chronic = rng.random(F) < rp["chronic_share"]
    habit[chronic] = rp["chronic_habit"]
    ratio = fac.catchment_pop.values / fac.hr_compliance.values
    workload = (pd.Series(ratio).rank(pct=True).values - 0.5 / F)
    return {
        "habit": habit, "chronic": chronic, "workload": workload,
        "lga_effect": rng.normal(0, rp["lga_effect_sd"], L),
        "state_effect": rng.normal(0, rp["state_effect_sd"], S),
        "conn": fac.connectivity.map(CONN_SCORE).values,
    }


def observe(fac, truth, cfg, rng, periods):
    rp, nz, sr = cfg["reporting"], cfg["noise"], cfg["stock_reporting"]
    F, T = truth["distress"].shape
    c = rp["c"]
    pr = reporting_params(fac, truth, cfg, rng)
    l_idx, s_idx = truth["l_idx"], truth["s_idx"]
    distress, closed = truth["distress"], truth["closed"]

    backlog = rng.random((len(truth["lga_keys"]), T)) < rp["lga_backlog_rate"]
    backlog_f = backlog[l_idx]

    base = (rp["b_rec"] * fac.records_officer.values + rp["b_conn"] * pr["conn"] - rp["b_work"] * pr["workload"]
            + pr["lga_effect"][l_idx] + pr["state_effect"][s_idx] + pr["habit"])[:, None] - c * distress
    late_base = (-rp["d_conn"] * pr["conn"] + rp["d_work"] * pr["workload"])[:, None] + 0.5 * c * distress
    open_ = ~closed
    zero_p = rp["closed_zero_form"]

    def rates(b0, d0):
        p_r, p_l = logistic(b0 + base), logistic(d0 + late_base)
        on = np.where(backlog_f, 0.0, p_r * (1 - p_l))
        late = np.where(backlog_f, p_r * (1 - rp["backlog_missing_share"]), p_r * p_l)
        on = np.where(open_, on, zero_p)
        late = np.where(open_, late, 0.0)
        return on.mean(), late.mean()

    b0, d0 = 0.0, -1.0
    for _ in range(6):
        b0 = _bisect(lambda b: sum(rates(b, d0)), rp["national_target_rate"])
        d0 = _bisect(lambda d: rates(b0, d)[1], rp["late_target_rate"], increasing=True)

    p_r, p_l = logistic(b0 + base), logistic(d0 + late_base)
    reports = rng.random((F, T)) < p_r
    late = rng.random((F, T)) < p_l
    lag = draw_int(rng, rp["late_months"], (F, T))
    # LGA backlog: everyone who would report is late 1-2 months, 30% go missing.
    bl_missing = rng.random((F, T)) < rp["backlog_missing_share"]
    bl_lag = draw_int(rng, rp["backlog_late_months"], (F, T))
    late = np.where(backlog_f, True, late)
    lag = np.where(backlog_f, bl_lag, lag)
    reports = np.where(backlog_f & bl_missing, False, reports)
    # Closed facilities: no report, except an occasional on-time zero form.
    zero_form = closed & (rng.random((F, T)) < zero_p)
    reports = np.where(closed, zero_form, reports)
    late = np.where(closed, False, late)

    status = np.where(~reports, MISSING, np.where(late, LATE, ON_TIME))
    lag = np.where(status == LATE, lag, 0)
    received_t = np.where(status == MISSING, -1, np.arange(T)[None, :] + lag)

    # ---- report values (spec 7.2) and noise (7.3) ----------------------------
    items = [it["code"] for it in cfg["items"]]
    peaks = {it["code"]: set(it.get("season_peak") or []) for it in cfg["items"]}
    cal = np.array([int(p[4:]) for p in periods])
    pop = fac.catchment_pop.values[:, None]
    received = status != MISSING
    under = rng.random((F, T)) < nz["under_reporting_share"]
    under_frac = rng.uniform(*nz["under_reporting_range"], (F, T))
    digit = rng.random((F, T)) < nz["digit_preference"]
    p_blank = nz["incomplete_base"] + nz["incomplete_workload"] * pr["workload"][:, None]

    frames = []
    for de in cfg["data_elements"]:
        item = de.get("item")
        season = np.array([cfg["season_multiplier"] if item and m in peaks[item] else 1.0 for m in cal])[None, :]
        mean = pop / 1000 * de["rate"] * season * truth["utilisation"]
        if item:
            mean = mean * (1 - cfg["availability_k"] * truth["share"][:, items.index(item), :])
        if de.get("immunisation"):
            mean = mean * (1 - cfg["fridge_immunisation_k"] * truth["fridge_down"])
        mean = mean * np.sqrt(truth["staff_ratio"]) * (~closed)
        k = cfg["count_dispersion"]
        val = rng.negative_binomial(k, k / (k + np.maximum(mean, 1e-9))).astype(float)
        val = np.where(mean <= 0, 0.0, val)
        val = np.where(under, rng.binomial(val.astype(np.int64), under_frac), val).astype(float)
        val = np.where(digit, np.round(val / 5) * 5, val)
        blank = rng.random((F, T)) < p_blank
        blank |= (val == 0) & (rng.random((F, T)) < nz["zero_left_blank"])
        val = np.where(zero_form, 0.0, np.where(blank, np.nan, val))
        ff, tt = np.nonzero(received)
        frames.append(pd.DataFrame({
            "dataElement": de["code"], "period": np.array(periods)[tt], "orgUnit": fac.facility_id.values[ff],
            "categoryOptionCombo": "default", "value": val[ff, tt],
            "_t": tt, "_recv": received_t[ff, tt],
        }))
    rep = pd.concat(frames, ignore_index=True)
    rep["received_period"] = _period_at(periods, rep.pop("_recv").values)
    rep = rep.sort_values(["orgUnit", "_t", "dataElement"]).drop(columns="_t").reset_index(drop=True)

    # ---- stock reports (spec 7.5) ------------------------------------------
    part_p = np.where(fac.state.values == sr["concentrated_state"], sr["concentrated_state_share"], sr["other_state_share"])
    participant = rng.random(F) < part_p
    vac = [i for i, it in enumerate(cfg["items"]) if it.get("vaccine")][0]
    others = [i for i in range(len(items)) if i != vac]
    stock_rows = []
    for f_ in np.flatnonzero(participant):
        chosen = [vac] + list(rng.choice(others, sr["other_items_reported"], replace=False))
        months = np.flatnonzero(received[f_] & ~zero_form[f_] & (rng.random(T) < sr["report_probability"]))
        for t_ in months:
            for i in chosen:
                stock_rows.append((fac.facility_id.values[f_], periods[t_], received_t[f_, t_], items[i],
                                   max(0, round(truth["stock_end"][f_, i, t_] * rng.normal(1, sr["stock_noise_sd"]))),
                                   int(round(30 * truth["share"][f_, i, t_]))))
    stock = pd.DataFrame(stock_rows, columns=["orgUnit", "period", "_recv", "item", "stock_on_hand", "stockout_days"])
    stock.insert(2, "received_period", _period_at(periods, stock.pop("_recv").values))

    obs = {"status": status, "lag": lag, "received_t": received_t, "backlog": backlog, "zero_form": zero_form,
           "participant": participant, "b0": round(b0, 4), "d0": round(d0, 4), **pr}
    return rep, stock, obs


def _period_at(periods, t_idx):
    """Month index -> YYYYMM, extending past the simulated window when a late
    report arrives after the last month."""
    start_y, start_m = int(periods[0][:4]), int(periods[0][4:])
    months = start_m - 1 + np.asarray(t_idx, dtype=int)
    return ((start_y + months // 12) * 100 + months % 12 + 1).astype(str)
