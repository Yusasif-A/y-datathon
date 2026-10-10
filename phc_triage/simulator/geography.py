"""
Real inputs (spec 3.1): facility points, LGAs, distances and catchments.

Facility locations, state, LGA, ward, type and settlement type are REAL
(GRID3 v3.0). Facility names are replaced with neutral labels, because the
outcomes attached to them are synthetic and must not read as claims about a
real clinic; grid3_id keeps every row traceable to the source.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from . import ROOT

TYPE_ABBR = {
    "Primary Health Center": "PHC", "Primary Health Clinic": "PHClinic", "Health Center": "HC",
    "Health Post": "HP", "Clinic": "Clinic", "Maternity Home": "Maternity",
}


def _xyz(lat, lon):
    lat, lon = np.radians(lat), np.radians(lon)
    return np.c_[np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)]


def _km(chord):
    return 2 * 6371 * np.arcsin(np.clip(chord / 2, 0, 1))


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * np.arcsin(np.sqrt(a))


def load_points(cfg: dict) -> pd.DataFrame:
    return pd.read_csv(ROOT / cfg["geography"]["facilities_file"])


def select_lgas(points: pd.DataFrame, cfg: dict, rng: np.random.Generator) -> pd.DataFrame:
    """Sample whole LGAs per state, in random order, until the state target
    is reached (stopping at whichever LGA lands closer to the target)."""
    chosen = []
    for st in cfg["geography"]["states"]:
        sp = points[points.state == st["name"]]
        sizes = sp.groupby("lga").size()
        order = rng.permutation(sizes.index.values)
        total, picked = 0, []
        for lga in order:
            n = sizes[lga]
            if total and abs(total + n - st["target_facilities"]) > abs(total - st["target_facilities"]):
                break
            picked.append(lga)
            total += n
        chosen.append(sp[sp.lga.isin(picked)])
    return pd.concat(chosen, ignore_index=True)


def build(cfg: dict, rng: np.random.Generator) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (facility geography, LGA table)."""
    geo = cfg["geography"]
    points = load_points(cfg)
    sel = select_lgas(points, cfg, rng).sort_values(["state", "lga", "grid3_id"]).reset_index(drop=True)

    # Nearest alternative: any other public primary facility in the full real
    # list (patients can cross into an LGA we did not sample).
    tree = cKDTree(_xyz(points.lat.values, points.lon.values))
    d, _ = tree.query(_xyz(sel.lat.values, sel.lon.values), k=2)
    sel["dist_nearest_alt_km"] = _km(d[:, 1]).round(2)

    # LGA HQ proxy: the centroid of the LGA's facilities [Interp].
    lgas = sel.groupby(["state", "lga"]).agg(hq_lat=("lat", "mean"), hq_lon=("lon", "mean"),
                                             facilities=("grid3_id", "size")).reset_index()
    sel = sel.merge(lgas[["lga", "state", "hq_lat", "hq_lon"]], on=["state", "lga"])
    sel["dist_lga_hq_km"] = haversine_km(sel.lat, sel.lon, sel.hq_lat, sel.hq_lon).round(2)

    sel["urban"] = sel.settlement_type.isin(geo["urban_settlement_types"]).astype(int)
    pop = rng.lognormal(np.log(geo["catchment_median"]), geo["catchment_sigma"], len(sel))
    sel["catchment_pop"] = np.round(pop * np.where(sel.urban == 1, geo["catchment_urban_mult"], 1.0), -1).astype(int)

    sel["facility_id"] = "NG-" + sel.grid3_id.str.replace("SN_", "", regex=False)
    seq = sel.groupby("lga").cumcount() + 1
    sel["name"] = sel.lga + " " + sel.facility_type.map(TYPE_ABBR).fillna("Facility") + " " + seq.map("{:03d}".format)
    sel["geometry_source"] = "real"
    zone = {s["name"]: s["zone"] for s in geo["states"]}
    sel["zone"] = sel.state.map(zone)
    lgas["zone"] = lgas.state.map(zone)
    return sel.drop(columns=["hq_lat", "hq_lon"]), lgas
