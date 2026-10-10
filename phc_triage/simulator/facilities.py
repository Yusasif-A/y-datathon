"""
Layer A (spec 5): facility attributes, drawn once per facility.

Location, LGA, urban/rural, facility type and distances come from real GRID3
points (geography.py). Attributes below are drawn with the correlations the
spec asks for, calibrated to NMIS 2014 where real data exists:
  rural -> less grid power, worse connectivity, fewer staff (NMIS);
  grid power -> more likely a working fridge (NMIS odds ratio);
  northern zones -> higher insecurity; Level 2 -> better staffed;
  BHCPF-supported -> more often Level 2.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import logistic

VISIBLE_COLUMNS = [
    "facility_id", "name", "state", "lga", "ward", "zone", "lat", "lon", "level", "facility_type", "ownership",
    "urban", "settlement_type", "dist_lga_hq_km", "dist_nearest_alt_km", "catchment_pop", "power", "connectivity",
    "records_officer", "hr_compliance", "fridge_functional", "bhcpf_supported", "insecurity_exposure",
    "geometry_source", "grid3_id",
]


def build(geo: pd.DataFrame, lgas: pd.DataFrame, cfg: dict, rng: np.random.Generator) -> pd.DataFrame:
    fc = cfg["facilities"]
    f = geo.copy()
    n = len(f)
    urban = f.urban.values == 1
    by_setting = lambda d: np.where(urban, d["urban"], d["rural"])

    f["level"] = np.where(f.facility_type.isin(cfg["geography"]["level2_types"]), 2, 1)

    # BHCPF: overall share as reported, tilted towards Level 2.
    w = np.where(f.level == 2, fc["bhcpf_level2_mult"], 1.0)
    f["bhcpf_supported"] = (rng.random(n) < np.clip(fc["bhcpf_supported"] * w / w.mean(), 0, 1)).astype(int)

    grid = rng.random(n) < by_setting(fc["grid_power"])
    power = np.where(grid,
                     np.where(rng.random(n) < fc["reliable_if_grid"], "reliable", "unreliable"),
                     np.where(rng.random(n) < fc["none_if_no_grid"], "none", "unreliable"))
    f["power"] = power

    u = rng.random(n)
    good, none = by_setting(fc["connectivity_good"]), by_setting(fc["connectivity_none"])
    f["connectivity"] = np.where(u < good, "good", np.where(u < good + none, "none", "poor"))

    f["records_officer"] = (rng.random(n) < fc["records_officer"]).astype(int)

    lo, hi = fc["hr_compliance_range"]
    median = by_setting(fc["hr_compliance_median"]) + np.where(f.level == 2, fc["hr_level2_bonus"], 0.0)
    f["hr_compliance"] = np.clip(rng.lognormal(np.log(median), fc["hr_compliance_sigma"]), lo, hi).round(3)

    p_fridge = logistic(fc["fridge_logit_base"] + fc["fridge_grid_effect"] * grid)
    f["fridge_functional"] = (rng.random(n) < p_fridge).astype(int)

    # Insecurity exposure is an LGA-level value (spec 5).
    zone_base = fc["insecurity_by_zone"]
    lga_exp = {
        (r.state, r.lga): float(np.clip(zone_base.get(r.zone, zone_base["default"])
                                        + rng.normal(0, fc["insecurity_lga_sd"]), 0, 1))
        for r in lgas.itertuples()
    }
    f["insecurity_exposure"] = [round(lga_exp[(s, l)], 3) for s, l in zip(f.state, f.lga)]
    lgas["insecurity_exposure"] = [round(lga_exp[(s, l)], 3) for s, l in zip(lgas.state, lgas.lga)]

    return f[VISIBLE_COLUMNS].reset_index(drop=True)
