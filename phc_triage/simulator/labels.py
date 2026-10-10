"""
Layer D (spec 8): the answers. Failure flags per facility-month and the true
cause of every silence episode. Used only for training labels and grading;
never as model input.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .observe import ON_TIME

# Truth cause -> what the tool shows officers (spec 8.2).
OFFICER_LABEL = {
    "entry_delay": "Late data entry",
    "chronic": "Chronic non-reporter",
    "real_problem": "Sudden drop",
    "lga_backlog": "LGA-wide backlog",
    "closure": "Closure",
}


def failure_flags(truth: dict, cfg: dict) -> dict:
    lab = cfg["labels"]
    items = cfg["items"]
    vac = np.array([bool(it.get("vaccine")) for it in items])
    share = truth["share"]
    fail_medicine = (share[:, ~vac, :] >= lab["stockout_threshold"]).any(axis=1)
    fail_vaccine = (share[:, vac, :] >= lab["stockout_threshold"]).any(axis=1) | truth["fridge_down"]
    fail_staff = truth["staff_ratio"] < lab["staff_threshold"]
    fail_closed = truth["closed"]
    fail_any = fail_medicine | fail_vaccine | fail_staff | fail_closed
    # Serious trouble (team decision, 2026-10-10): the spec's fail_any is true
    # in ~72% of facility-months at realistic availability, so it cannot rank
    # facilities. The forecast target is instead: several medicines short, or
    # vaccine short while the fridge is down, or staff below half, or closed.
    meds_short = (share[:, ~vac, :] >= lab["stockout_threshold"]).sum(axis=1)
    vac_short = (share[:, vac, :] >= lab["stockout_threshold"]).any(axis=1)
    fail_serious = ((meds_short >= lab["serious_min_medicines"]) | (vac_short & truth["fridge_down"])
                    | fail_staff | fail_closed)
    T = fail_any.shape[1]
    h = lab["forecast_horizon_months"]
    out = {"fail_medicine": fail_medicine, "fail_vaccine": fail_vaccine, "fail_staff": fail_staff,
           "fail_closed": fail_closed, "fail_any": fail_any, "fail_serious": fail_serious}
    # fail_next12 is the spec's label; fail_serious_next12 is the forecast
    # target; per-type labels let the tool say WHICH kind of trouble.
    for k in ("any", "serious", "medicine", "vaccine", "staff", "closed"):
        a = out[f"fail_{k}"]
        n = np.full(a.shape, np.nan)
        for t in range(T - max(h)):
            n[:, t] = a[:, [t + j for j in h]].any(axis=1)
        out[f"fail_{k}_next12"] = n
    out["fail_next12"] = out.pop("fail_any_next12")
    return out


def silence_events(fac: pd.DataFrame, truth: dict, obs: dict, cfg: dict, periods: list[str]) -> pd.DataFrame:
    """A silence episode is a run of consecutive facility-months with no
    report received on time (spec 8.2). One cause per episode, first match:
      closure > lga_backlog > real_problem > chronic > entry_delay
    entry_delay also covers a healthy, non-chronic facility whose report went
    missing [Interp: the spec does not name that case]."""
    lab = cfg["labels"]
    status = obs["status"]
    F, T = status.shape
    l_idx = truth["l_idx"]
    backlog_f = obs["backlog"][l_idx]
    insec_f = truth["insecurity_lga"][l_idx]
    exposure = fac.insecurity_exposure.values
    rows = []
    for f_ in range(F):
        quiet = status[f_] != ON_TIME
        t = 0
        while t < T:
            if not quiet[t]:
                t += 1
                continue
            start = t
            while t < T and quiet[t]:
                t += 1
            end = t - 1
            span = slice(start, end + 1)
            if truth["closed"][f_, span].any():
                cause = "closure"
            elif backlog_f[f_, start]:
                cause = "lga_backlog"
            elif truth["distress"][f_, start] >= lab["distress_problem"]:
                cause = "real_problem"
            elif obs["habit"][f_] < lab["chronic_habit_below"]:
                cause = "chronic"
            else:
                cause = "entry_delay"
            insecure = bool(insec_f[f_, span].any() or exposure[f_] >= lab["insecure_exposure"])
            rows.append((fac.facility_id.values[f_], periods[start], periods[end], end - start + 1, cause,
                         OFFICER_LABEL[cause], int(insecure)))
    return pd.DataFrame(rows, columns=["facility_id", "start", "end", "months", "cause", "officer_label",
                                       "insecure_area"])
