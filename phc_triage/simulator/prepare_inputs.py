"""
Builds the simulator's real inputs from public downloads (run once; outputs
are committed under phc_triage/inputs/ so nobody needs the raw files):

  GRID3 NGA Health Facilities v3.0 (HDX, CC BY 4.0)
    https://data.humdata.org/dataset/grid3-nga-health-facilities-v3-0
    -> inputs/grid3_public_primary.csv: public, primary-level facilities in the
       chosen states, excluding closed / not-functional / duplicated points.

  NMIS health facility data 2014 (energydata.info, CC BY 4.0)
    https://energydata.info/dataset/nigeria-nmis-health-facility-data-2014
    -> inputs/nmis_calibration.json: rates used to calibrate Layer A
       (grid power, vaccine fridge, staffing, ACT and FP availability), split
       urban / rural using the GRID3 settlement type of the nearest point.

Usage:
  python -m simulator.prepare_inputs --grid3 path/to.gpkg --nmis path/to.csv
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

INPUTS = Path(__file__).resolve().parents[1] / "inputs"
STATES = ["Oyo", "Kano", "Gombe"]

NMIS_PRIMARY_TYPES = [
    "Primary Health Center", "Health Post", "Primary Health Centre (PHC)", "Clinic",
    "Basic Health Centre or Primary Health Clinic", "Dispensary", "Basic Health Centre / Primary Health Clinic",
]
NMIS_STAFF = ["num_chews_fulltime", "num_nurses_fulltime", "num_nursemidwives_fulltime", "num_doctors_fulltime"]
NMIS_BOOL = ["phcn_electricity", "vaccines_fridge_freezer", "malaria_treatment_artemisinin", "family_planning_yn"]


def _unit_xyz(lat, lon):
    lat, lon = np.radians(lat), np.radians(lon)
    return np.c_[np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)]


def _chord_to_km(d):
    return 2 * 6371 * np.arcsin(np.clip(d / 2, 0, 1))


def load_grid3(path: Path) -> pd.DataFrame:
    g = pd.read_sql("select * from GRID3_NGA_health_facilities_v3_0", sqlite3.connect(path))
    return g.drop(columns=["Shape"]).dropna(subset=["latitude", "longitude"]).reset_index(drop=True)


def grid3_facilities(g: pd.DataFrame) -> pd.DataFrame:
    keep = (
        (g.facility_level == "Primary") & g.state_standard.isin(STATES)
        & (g.facility_ownership == "Public")
        & ~g.functional.isin(["Closed", "Not-Functional"])
        & ~g.issues.fillna("").str.contains("duplicat")
    )
    f = g[keep]
    return pd.DataFrame({
        "grid3_id": f.unique_id, "name": f.facility_name.str.strip(), "state": f.state_standard,
        "lga": f.lga_standard, "ward": f.ward_standard, "lat": f.latitude.round(6), "lon": f.longitude.round(6),
        "facility_type": f.facility_type.replace("", "Unknown"), "ownership": f.facility_ownership,
        "settlement_type": f.sett_ext_type.replace("", "Unknown"),
    }).sort_values(["state", "lga", "grid3_id"]).reset_index(drop=True)


def nmis_calibration(nmis_path: Path, g: pd.DataFrame) -> dict:
    n = pd.read_csv(nmis_path, low_memory=False)
    n = n[n.facility_type_display.isin(NMIS_PRIMARY_TYPES)].dropna(subset=["latitude", "longitude"]).copy()
    for c in NMIS_BOOL:
        n[c] = n[c].map({True: 1.0, False: 0.0, "True": 1.0, "False": 0.0})
    for c in NMIS_STAFF:
        n[c] = pd.to_numeric(n[c], errors="coerce").clip(upper=100)
    n["clinical_staff"] = n[NMIS_STAFF].sum(axis=1, min_count=1)

    d, idx = cKDTree(_unit_xyz(g.latitude.values, g.longitude.values)).query(
        _unit_xyz(n.latitude.values, n.longitude.values))
    sett = np.where(_chord_to_km(d) < 1, g.sett_ext_type.values[idx], "")
    n["urban"] = np.where(sett == "Built-up Area", 1.0, np.where(np.isin(sett, ["Small Settlement Area", "Hamlet"]), 0.0, np.nan))

    def rates(df):
        grid = df.phcn_electricity
        return {
            "n": int(len(df)),
            "grid_power": round(float(grid.mean()), 3),
            "fridge": round(float(df.vaccines_fridge_freezer.mean()), 3),
            "fridge_if_grid": round(float(df.loc[grid == 1, "vaccines_fridge_freezer"].mean()), 3),
            "fridge_if_no_grid": round(float(df.loc[grid == 0, "vaccines_fridge_freezer"].mean()), 3),
            "act_available": round(float(df.malaria_treatment_artemisinin.mean()), 3),
            "fp_offered": round(float(df.family_planning_yn.mean()), 3),
            "clinical_staff_median": float(df.clinical_staff.median()),
            "clinical_staff_share_le2": round(float((df.clinical_staff <= 2).mean()), 3),
        }

    return {
        "source": "NMIS health facility data 2014 (surveyed 2010-2014), primary facility types only",
        "urban_matching": "GRID3 v3.0 settlement type of the nearest GRID3 point within 1 km",
        "all": rates(n),
        "urban": rates(n[n.urban == 1]),
        "rural": rates(n[n.urban == 0]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid3", required=True, type=Path)
    ap.add_argument("--nmis", required=True, type=Path)
    args = ap.parse_args()
    INPUTS.mkdir(exist_ok=True)
    g = load_grid3(args.grid3)
    fac = grid3_facilities(g)
    fac.to_csv(INPUTS / "grid3_public_primary.csv", index=False)
    cal = nmis_calibration(args.nmis, g)
    (INPUTS / "nmis_calibration.json").write_text(json.dumps(cal, indent=2))
    print(f"{len(fac)} facilities -> {INPUTS / 'grid3_public_primary.csv'}")
    print(fac.groupby("state").agg(facilities=("grid3_id", "size"), lgas=("lga", "nunique")))
    print(json.dumps(cal, indent=2))


if __name__ == "__main__":
    main()
