"""
Layer B (spec 6): the hidden state, month by month. Nothing here is visible
to the models; it becomes the truth_ tables and drives Layer C.

Monthly order (spec 6.2): shocks -> demand -> receive deliveries -> consume
-> expire -> reorder -> staffing -> record.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import draw_int

POWER_OUTAGE_KEYS = ("reliable", "unreliable", "none")


def _spells(rng, n_units, months, p_start, dur_bounds, start_mask=None):
    """Boolean [units, months] of shock spells: each unit-month starts a spell
    with probability p_start (array or scalar) unless one is already active."""
    active = np.zeros((n_units, months), dtype=bool)
    p = np.broadcast_to(np.asarray(p_start, dtype=float), (n_units,))
    for t in range(months):
        if start_mask is not None and not start_mask[t]:
            continue
        start = (~active[:, t]) & (rng.random(n_units) < p)
        for u in np.flatnonzero(start):
            active[u, t:t + draw_int(rng, dur_bounds)] = True
    return active


def simulate(fac: pd.DataFrame, lgas: pd.DataFrame, cfg: dict, rng: np.random.Generator, calendar_months: np.ndarray):
    T = int(cfg["run"]["months"])
    F = len(fac)
    sh, sup, items = cfg["shocks"], cfg["supply"], cfg["items"]
    I = len(items)
    pad = T + 24

    states = sorted(fac.state.unique())
    s_idx = fac.state.map({s: i for i, s in enumerate(states)}).values
    lga_keys = list(zip(lgas.state, lgas.lga))
    l_idx = np.array([lga_keys.index(k) for k in zip(fac.state, fac.lga)])
    S, L = len(states), len(lga_keys)
    rural = fac.urban.values == 0

    # State-level supply chain quality [Assumed ranges].
    lead_mean = rng.uniform(*sup["lead_time_mean"], S)
    fill_rate = rng.uniform(*sup["fill_rate"], S)
    # Facility-level reliability [Assumed]: some facilities are reliably
    # supplied, others chronically short; remote ones a little worse.
    fac_fill = rng.lognormal(0, sup["facility_fill_sd"], F)
    fac_fill *= np.clip(1 - sup["remote_fill_penalty_per_10km"] * fac.dist_lga_hq_km.values / 10, 0.5, 1)

    # ---- shocks (spec 6.3) ------------------------------------------------
    quarter_start = (np.arange(T) % 3) == 0
    funding = _spells(rng, S, T, sh["funding_delay_per_quarter"], sh["funding_delay_months"], quarter_start)
    strike = _spells(rng, S, T, sh["strike"], sh["strike_months"])
    insec_p = sh["insecurity_event_base"] * lgas.insecurity_exposure.values * 10
    insecurity = _spells(rng, L, T, insec_p, sh["insecurity_months"])

    wave_mult = np.ones((F, T))
    wave_events = []
    lat, lon = fac.lat.values, fac.lon.values
    for s in range(S):
        members = np.flatnonzero(s_idx == s)
        for t in range(T):
            if rng.random() < sh["supply_wave"]:
                centre = rng.choice(members)
                d2 = (lat[members] - lat[centre]) ** 2 + (lon[members] - lon[centre]) ** 2
                hit = members[np.argsort(d2)[: int(round(sh["supply_wave_share"] * len(members)))]]
                wave_mult[hit, t:t + draw_int(rng, sh["supply_wave_months"])] = sh["supply_wave_fill_mult"]
                wave_events.append((states[s], t))

    # Per-facility response to an insecurity event: close, or lose half the staff.
    insec_close_draw = rng.random((F, T)) < sh["insecurity_closure_prob"]
    insec_fac = insecurity[l_idx]                                     # [F, T]
    # Hold one decision per spell: the draw at the spell's first month.
    spell_start = insec_fac & ~np.concatenate([np.zeros((F, 1), bool), insec_fac[:, :-1]], axis=1)
    insec_closed = np.zeros((F, T), bool)
    for f_, t_ in zip(*np.nonzero(spell_start)):
        end = t_
        while end < T and insec_fac[f_, end]:
            end += 1
        insec_closed[f_, t_:end] = insec_close_draw[f_, t_]
    insec_staff_hit = insec_fac & ~insec_closed

    p_out = np.select([fac.power.values == k for k in POWER_OUTAGE_KEYS],
                      [sh["power_outage"][k] for k in POWER_OUTAGE_KEYS])
    power_out = rng.random((F, T)) < p_out[:, None]

    # Fridges: working ones fail at random; ones broken at the start wait a
    # long repair [Interp].
    fridge_down = np.zeros((F, T), bool)
    broken = fac.fridge_functional.values == 0
    for f_ in np.flatnonzero(broken):
        fridge_down[f_, : draw_int(rng, sh["fridge_initial_repair_months"])] = True
    for t in range(T):
        fails = (~fridge_down[:, t]) & (rng.random(F) < sh["fridge_failure"])
        for f_ in np.flatnonzero(fails):
            dur = draw_int(rng, sh["fridge_repair_months"]) + (sh["fridge_repair_rural_extra"] if rural[f_] else 0)
            fridge_down[f_, t:t + dur] = True

    closed_perm = np.zeros((F, T), bool)
    for f_ in np.flatnonzero(rng.random(F) < 1 - (1 - sh["closure"]) ** T):
        closed_perm[f_, rng.integers(0, T):] = True

    # ---- monthly process (spec 6.2) ----------------------------------------
    pop = fac.catchment_pop.values.astype(float)
    rate = np.array([it["demand"] for it in items], float)
    mean_demand = pop[:, None] / 1000 * rate[None, :]                 # [F, I]
    reorder = np.array([it["reorder"] for it in items])
    target = np.array([it["target"] for it in items])
    expiry = np.array([it["expiry"] for it in items])
    is_vac = np.array([bool(it.get("vaccine")) for it in items])
    peaks = [set(it.get("season_peak") or []) for it in items]

    stock = mean_demand * rng.uniform(reorder, target, (F, I))
    pending = np.zeros((F, I, pad))
    outstanding_until = np.full((F, I), -1)
    util = np.ones(F)
    streak = np.zeros(F, int)
    staff_base = np.ones(F)
    hires = np.zeros((F, pad))

    out = {k: np.zeros((F, T)) for k in ("staff_ratio", "distress", "utilisation")}
    stock_end = np.zeros((F, I, T))
    share = np.zeros((F, I, T))
    closed = closed_perm | insec_closed

    for t in range(T):
        cm = calendar_months[t]
        season = np.array([cfg["season_multiplier"] if cm in p else 1.0 for p in peaks])
        demand = mean_demand * season[None, :] * util[:, None] * (~closed[:, t])[:, None]

        received = pending[:, :, t]
        avail = stock + received
        consumed = np.minimum(demand, avail)
        unmet = demand - consumed
        share[:, :, t] = np.divide(unmet, demand, out=np.zeros_like(unmet), where=demand > 0)
        stock = avail - consumed

        fridge_blocked = fridge_down[:, t] | power_out[:, t]
        loss = np.where(is_vac[None, :] & fridge_blocked[:, None], sup["vaccine_spoil_fridge_down"], expiry[None, :])
        stock = stock * (1 - loss)

        need = (stock < reorder * mean_demand) & (outstanding_until < t)
        if need.any():
            ff, ii = np.nonzero(need)
            s_of = s_idx[ff]
            fill = np.clip(fill_rate[s_of] * fac_fill[ff] * rng.normal(1, sup["fill_noise_sd"], len(ff)), 0, 1)
            fill *= np.where(funding[s_of, t], sh["funding_fill_mult"], 1.0) * wave_mult[ff, t]
            m = lead_mean[s_of]
            lead = 1 + np.rint(rng.gamma(sup["lead_time_shape"], np.maximum(m - 1, 1e-9) / sup["lead_time_shape"]))
            arrive = np.minimum(t + lead.astype(int), pad - 1)
            qty = np.maximum(target[ii] * mean_demand[ff, ii] - stock[ff, ii], 0) * fill
            np.add.at(pending, (ff, ii, arrive), qty)
            outstanding_until[ff, ii] = arrive

        stock_end[:, :, t] = stock

        bad = share[:, :, t].max(axis=1) >= cfg["labels"]["stockout_threshold"]
        streak = np.where(bad, streak + 1, 0)
        util = np.where(streak >= 2, np.maximum(util * (1 - sup["utilisation_drop"]), sup["utilisation_floor"]),
                        np.minimum(util + sup["utilisation_recovery"], 1.0))

        staff_base += hires[:, t] * sh["staff_loss_size"]
        lost = rng.random(F) < sh["staff_loss"]
        for f_ in np.flatnonzero(lost & (staff_base > sh["staff_loss_size"])):
            staff_base[f_] -= sh["staff_loss_size"]
            hires[f_, min(t + draw_int(rng, sh["hiring_months"]), pad - 1)] += 1
        staff_base = np.minimum(staff_base, 1.0)
        ratio = staff_base * np.where(strike[s_idx, t], sh["strike_staff_mult"], 1.0) \
            * np.where(insec_staff_hit[:, t], sh["insecurity_staff_mult"], 1.0)

        out["staff_ratio"][:, t] = ratio
        out["utilisation"][:, t] = util
        out["distress"][:, t] = np.minimum(1.0, 0.4 * share[:, :, t].max(axis=1) + 0.2 * (1 - ratio)
                                           + 0.2 * fridge_down[:, t] + 0.2 * power_out[:, t])

    lga_state = np.array([states.index(s) for s, _ in lga_keys])
    return {
        "states": states, "s_idx": s_idx, "l_idx": l_idx, "lga_keys": lga_keys,
        "stock_end": stock_end, "share": share, "fridge_down": fridge_down, "power_out": power_out,
        "closed": closed, "closed_perm": closed_perm, "staff_ratio": out["staff_ratio"], "distress": out["distress"],
        "utilisation": out["utilisation"], "insecurity_lga": insecurity, "funding_lga": funding[lga_state],
        "strike_lga": strike[lga_state], "lead_mean": dict(zip(states, lead_mean.round(2))),
        "fill_rate": dict(zip(states, fill_rate.round(3))), "supply_waves": len(wave_events),
    }
