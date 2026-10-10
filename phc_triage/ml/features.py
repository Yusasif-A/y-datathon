"""
One row per facility-month t, with features computed AS OF the end of month
t: only reports whose received_period <= t are visible. A late report is
therefore "missing" until it arrives, exactly as an officer would see it.

Labels (y_*) are joined from the hidden truth in a separate step and are
never used as features.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from .data import Truth, Visible

POWER_ORD = {"none": 0, "unreliable": 1, "reliable": 2}
CONN_ORD = {"none": 0, "poor": 1, "good": 2}
STATES = ["Gombe", "Kano", "Oyo"]

STRUCTURAL = [
    "level", "urban", "log_pop", "power_ord", "conn_ord", "records_officer", "hr_compliance", "fridge_functional",
    "bhcpf_supported", "insecurity_exposure", "dist_nearest_alt_km", "dist_lga_hq_km",
] + [f"state_{s}" for s in STATES]
REPORTING = [
    "on_time_now", "months_silent", "known_rate_3", "known_rate_6", "known_rate_12", "habit_rate",
    "on_time_rate_12", "late_share_12", "completeness_3", "history_months", "lga_silent_now", "lga_silent_prev",
    "vs_lga_rate_3",
]
VOLUME_ELEMENTS = {"OPD_visits": "opd", "Malaria_treated": "mal", "FP_acceptors": "fp", "Penta3_doses": "penta",
                   "ANC_visits": "anc", "Facility_deliveries": "deliv"}
VOLUME = [f"{v}_{k}" for v in VOLUME_ELEMENTS.values() for k in ("ratio", "trend")] + ["opd_per_1000", "zero_form_now"]
STOCK = ["stock_participant", "stock_months_since", "stock_vac_days", "stock_other_max_days", "stock_items_out"]
SEASON = ["season_sin", "season_cos"]
FEATURES = STRUCTURAL + REPORTING + VOLUME + STOCK + SEASON

CAUSES = ["entry_delay", "chronic", "real_problem", "lga_backlog", "closure"]
TYPE_LABELS = ["medicine", "vaccine", "staff", "closed"]


def _structural(fac: pd.DataFrame) -> pd.DataFrame:
    s = pd.DataFrame({
        "level": fac.level, "urban": fac.urban, "log_pop": np.log(fac.catchment_pop),
        "power_ord": fac.power.map(POWER_ORD), "conn_ord": fac.connectivity.map(CONN_ORD),
        "records_officer": fac.records_officer, "hr_compliance": fac.hr_compliance,
        "fridge_functional": fac.fridge_functional, "bhcpf_supported": fac.bhcpf_supported,
        "insecurity_exposure": fac.insecurity_exposure, "dist_nearest_alt_km": fac.dist_nearest_alt_km,
        "dist_lga_hq_km": fac.dist_lga_hq_km,
    })
    for st in STATES:
        s[f"state_{st}"] = (fac.state == st).astype(int)
    return s


def build_panel(vis: Visible, months: range | None = None) -> pd.DataFrame:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # empty slices for facilities with no history yet
        return _build_panel(vis, months)


def _build_panel(vis: Visible, months: range | None = None) -> pd.DataFrame:
    fac = vis.facilities
    F, T, E = vis.values.shape
    months = months or range(T)
    struct = _structural(fac)
    lga_codes = pd.factorize(fac.state + "|" + fac.lga)[0]
    n_lga = lga_codes.max() + 1
    lga_size = np.bincount(lga_codes, minlength=n_lga)
    e_idx = {e: i for i, e in enumerate(vis.elements)}
    vac_i = vis.stock_items.index("VAC") if "VAC" in vis.stock_items else None
    pidx = np.arange(T)
    cal = np.array([int(p[4:]) for p in vis.periods])
    rows = []

    for t in months:
        known = (vis.recv_t >= 0) & (vis.recv_t <= t)
        known[:, t + 1:] = False
        f = {}
        f["on_time_now"] = (vis.recv_t[:, t] == t).astype(int)
        silent = np.zeros(F, int)
        still = np.ones(F, bool)
        for p in range(t, -1, -1):
            still &= ~known[:, p]
            silent += still
        f["months_silent"] = silent
        for w in (3, 6, 12):
            f[f"known_rate_{w}"] = known[:, max(0, t - w + 1): t + 1].mean(axis=1)
        old = slice(max(0, t - 14), max(1, t - 2))
        f["habit_rate"] = known[:, old].mean(axis=1) if t >= 3 else np.full(F, np.nan)
        win = slice(max(0, t - 11), t + 1)
        f["on_time_rate_12"] = (vis.recv_t[:, win] == pidx[win]).mean(axis=1)
        kw = known[:, win]
        late = kw & (vis.recv_t[:, win] > pidx[win])
        f["late_share_12"] = np.divide(late.sum(1), kw.sum(1), out=np.full(F, np.nan), where=kw.sum(1) > 0)
        w3 = slice(max(0, t - 2), t + 1)
        filled = (~np.isnan(vis.values[:, w3, :])).mean(axis=2)
        k3 = known[:, w3]
        f["completeness_3"] = np.divide((filled * k3).sum(1), k3.sum(1), out=np.full(F, np.nan), where=k3.sum(1) > 0)
        f["history_months"] = known[:, : t + 1].sum(axis=1)
        silent_now = (~known[:, t]).astype(float)
        silent_prev = (~known[:, t - 1]).astype(float) if t else silent_now
        f["lga_silent_now"] = (np.bincount(lga_codes, silent_now, n_lga) / lga_size)[lga_codes]
        f["lga_silent_prev"] = (np.bincount(lga_codes, silent_prev, n_lga) / lga_size)[lga_codes]
        lga_rate3 = (np.bincount(lga_codes, f["known_rate_3"], n_lga) / lga_size)[lga_codes]
        f["vs_lga_rate_3"] = f["known_rate_3"] - lga_rate3

        # Volumes vs the facility's own recent history (only reports known by t).
        vals = np.where(known[:, :, None], vis.values, np.nan)[:, max(0, t - 12): t + 1, :]
        any_known = known[:, max(0, t - 12): t + 1]
        last = np.where(any_known.any(1), any_known.shape[1] - 1 - np.argmax(any_known[:, ::-1], axis=1), -1)
        rows_i = np.arange(F)
        for el, short in VOLUME_ELEMENTS.items():
            v = vals[:, :, e_idx[el]]
            latest = np.where(last >= 0, v[rows_i, np.maximum(last, 0)], np.nan)
            hist = v.copy()
            hist[rows_i, np.maximum(last, 0)] = np.nan
            with np.errstate(all="ignore"):
                base = np.nanmedian(hist, axis=1)
                recent = np.nanmean(v[:, -3:], axis=1)
            f[f"{short}_ratio"] = np.clip(np.divide(latest, base, out=np.full(F, np.nan), where=base > 0), 0, 5)
            f[f"{short}_trend"] = np.clip(np.divide(recent, base, out=np.full(F, np.nan), where=base > 0), 0, 5)
            if el == "OPD_visits":
                f["opd_base"] = base                          # inputs for model 1, not model 3
                f["opd_now"] = np.where(known[:, t], vis.values[:, t, e_idx[el]], np.nan)
        opd = vals[:, :, e_idx["OPD_visits"]]
        f["opd_per_1000"] = np.where(last >= 0, opd[rows_i, np.maximum(last, 0)], np.nan) / fac.catchment_pop.values * 1000
        # A form that arrives with every count at zero (closed facilities sometimes send one).
        now_vals = np.where(known[:, t, None], vis.values[:, t, :], np.nan)
        f["zero_form_now"] = (known[:, t] & (np.nan_to_num(now_vals, nan=1) == 0).all(axis=1)).astype(int)

        # Stock reports, where the facility sends them.
        sk = (vis.stock_recv_t >= 0) & (vis.stock_recv_t <= t)
        sk[:, t + 1:] = False
        has = sk.any(axis=1)
        s_last = np.where(has, T - 1 - np.argmax(sk[:, ::-1], axis=1), -1)
        f["stock_participant"] = has.astype(int)
        f["stock_months_since"] = np.where(has, t - s_last, 24)
        sod = vis.stockout_days[rows_i, np.maximum(s_last, 0), :]
        sod = np.where(has[:, None], sod, np.nan)
        f["stock_vac_days"] = sod[:, vac_i] if vac_i is not None else np.nan
        other = np.delete(sod, vac_i, axis=1) if vac_i is not None else sod
        with np.errstate(all="ignore"):
            f["stock_other_max_days"] = np.nanmax(other, axis=1) if other.size else np.nan
        f["stock_items_out"] = (np.nan_to_num(sod, nan=0) > 3).sum(axis=1)

        f["season_sin"] = np.full(F, np.sin(2 * np.pi * cal[t] / 12))
        f["season_cos"] = np.full(F, np.cos(2 * np.pi * cal[t] / 12))
        df = pd.DataFrame(f)
        df.insert(0, "t", t)
        df.insert(0, "facility_id", fac.facility_id.values)
        rows.append(pd.concat([df, struct], axis=1))

    panel = pd.concat(rows, ignore_index=True)
    panel["period"] = np.array(vis.periods)[panel.t]
    meta = fac[["facility_id", "state", "lga", "zone", "catchment_pop"]]
    return panel.merge(meta, on="facility_id", how="left")


def attach_labels(panel: pd.DataFrame, truth: Truth, periods: list[str]) -> pd.DataFrame:
    """Join the hidden answers. Kept apart so features can never see them."""
    fm = truth.fm
    cols = {"fail_serious_next12": "y_serious_next12", "fail_serious": "y_serious_now", "fail_any": "y_any_now",
            "fail_next12": "y_any_next12"}
    cols.update({f"fail_{k}_next12": f"y_{k}_next12" for k in TYPE_LABELS})
    lab = fm[["facility_id", "period", *cols]].rename(columns=cols)
    out = panel.merge(lab, on=["facility_id", "period"], how="left")

    # Silence cause for the episode covering each quiet facility-month.
    p_of = {p: i for i, p in enumerate(periods)}
    sil = truth.silence
    cause = {}
    for r in sil.itertuples():
        for t in range(p_of[r.start], p_of[r.end] + 1):
            cause[(r.facility_id, t)] = (r.cause, r.insecure_area)
    got = [cause.get(k, (None, None)) for k in zip(out.facility_id, out.t)]
    out["y_cause"] = [g[0] for g in got]
    out["y_insecure"] = [g[1] for g in got]
    return out
