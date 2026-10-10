"""
The three models from the solution document, plus calibration.

Model 1  Expected report: how much OPD activity should this facility have
         reported this month, given similar facilities, season and its own
         history? The gap between expected and actual is a warning sign.
Model 2  Silence type: why is a quiet facility quiet? (entry delay, chronic,
         real problem, LGA backlog, closure). Insecurity is a modifier rule.
Model 3  Trouble forecast: calibrated chance of SERIOUS trouble in the next
         1-2 months, plus one calibrated model per failure type, plus an
         uncertainty band from a bootstrap ensemble.

Splits follow spec 11: months 1-30 train, 31-39 validate, 40-48 test (with a
2-month embargo so a training label never peeks into the validation window),
and 20% of LGAs held out of training entirely.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier, LGBMRegressor
from sklearn.isotonic import IsotonicRegression

from .features import CAUSES, FEATURES, SEASON, STRUCTURAL, TYPE_LABELS

HORIZON = 2
TRAIN_MONTHS = range(0, 30 - HORIZON)     # spec months 1-30, last 2 embargoed
VAL_MONTHS = range(30, 39 - HORIZON)      # spec months 31-39
TEST_MONTHS = range(39, 48 - HORIZON)     # spec months 40-48 (last 2 have no label yet)
HELDOUT_SHARE = 0.20
N_ENSEMBLE = 5
INSECURE_EXPOSURE = 0.70

M1_FEATURES = STRUCTURAL + SEASON + ["opd_base", "history_months"]
M3_FEATURES = FEATURES + ["exp_gap_now", "exp_missing_per_1000"]

GBM = dict(n_estimators=300, learning_rate=0.05, num_leaves=31, min_child_samples=40, subsample=0.8,
           subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0, verbose=-1)


def heldout_lgas(panel: pd.DataFrame, seed: int = 7) -> set:
    """20% of LGAs per state, never seen in training (spec 11)."""
    rng = np.random.default_rng(seed)
    out = set()
    for _, g in panel[["state", "lga"]].drop_duplicates().groupby("state"):
        n = max(1, int(round(HELDOUT_SHARE * len(g))))
        out |= set(rng.choice(g.lga.values, n, replace=False))
    return out


def split(panel: pd.DataFrame, held: set) -> dict:
    in_held = panel.lga.isin(held)
    return {
        "train": panel[panel.t.isin(TRAIN_MONTHS) & ~in_held],
        "val": panel[panel.t.isin(VAL_MONTHS) & ~in_held],
        "test": panel[panel.t.isin(TEST_MONTHS)],
        "test_heldout": panel[panel.t.isin(TEST_MONTHS) & in_held],
    }


# ---- Model 1 -----------------------------------------------------------------
def fit_expected(train: pd.DataFrame) -> LGBMRegressor:
    rows = train[train.opd_now.notna()]
    m = LGBMRegressor(**GBM)
    m.fit(rows[M1_FEATURES], np.log1p(rows.opd_now))
    return m


def add_expected(panel: pd.DataFrame, m1: LGBMRegressor) -> pd.DataFrame:
    p = panel.copy()
    p["opd_expected"] = np.expm1(m1.predict(p[M1_FEATURES]))
    p["exp_gap_now"] = np.clip((p.opd_now - p.opd_expected) / p.opd_expected.clip(lower=1), -1, 3)
    # Care that should have been recorded this month but was not (silent months).
    p["exp_missing_per_1000"] = np.where(p.opd_now.isna(), p.opd_expected / p.catchment_pop * 1000, 0.0)
    return p


# ---- Model 2 -----------------------------------------------------------------
def quiet_rows(df: pd.DataFrame) -> pd.DataFrame:
    return df[(df.on_time_now == 0) & df.y_cause.notna()]


def fit_silence(train: pd.DataFrame) -> LGBMClassifier:
    q = quiet_rows(train)
    m = LGBMClassifier(**GBM, objective="multiclass", class_weight="balanced")
    m.fit(q[M3_FEATURES], q.y_cause.map({c: i for i, c in enumerate(CAUSES)}))
    return m


def predict_silence(m2: LGBMClassifier, df: pd.DataFrame) -> np.ndarray:
    return np.array(CAUSES)[m2.predict(df[M3_FEATURES])]


# ---- Model 3 -----------------------------------------------------------------
class Calibrated:
    """LightGBM + isotonic calibration fitted on the validation months."""

    def __init__(self, label: str):
        self.label = label
        self.model = LGBMClassifier(**GBM)
        self.iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)

    def fit(self, train: pd.DataFrame, val: pd.DataFrame, weight_col: str | None = None):
        tr = train.dropna(subset=[self.label])
        weight = tr[weight_col].values if weight_col else None
        self.model.fit(tr[M3_FEATURES], tr[self.label].astype(int), sample_weight=weight)
        va = val.dropna(subset=[self.label])
        self.iso.fit(self.model.predict_proba(va[M3_FEATURES])[:, 1], va[self.label].astype(int))
        return self

    def raw(self, df):
        return self.model.predict_proba(df[M3_FEATURES])[:, 1]

    def predict(self, df):
        return self.iso.predict(self.raw(df))


def fit_trouble(train, val, seed=0) -> dict:
    main = Calibrated("y_serious_next12").fit(train, val)
    rng = np.random.default_rng(seed)
    ensemble = []
    tr = train.dropna(subset=["y_serious_next12"])
    for i in range(N_ENSEMBLE):
        b = tr.iloc[rng.integers(0, len(tr), len(tr))]
        m = LGBMClassifier(**{**GBM, "random_state": i})
        m.fit(b[M3_FEATURES], b.y_serious_next12.astype(int))
        ensemble.append(m)
    types = {k: Calibrated(f"y_{k}_next12").fit(train, val) for k in TYPE_LABELS}
    return {"main": main, "ensemble": ensemble, "types": types}


def predict_trouble(m3: dict, df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    out["p_trouble"] = m3["main"].predict(df)
    raw = np.stack([m.predict_proba(df[M3_FEATURES])[:, 1] for m in m3["ensemble"]], axis=1)
    out["p_uncertainty"] = raw.std(axis=1)
    for k, m in m3["types"].items():
        out[f"p_{k}"] = m.predict(df)
    return out


def fit_all(panel: pd.DataFrame, held: set, seed: int = 0) -> dict:
    sp = split(panel, held)
    m1 = fit_expected(sp["train"])
    p = add_expected(panel, m1)
    sp = split(p, held)
    m2 = fit_silence(sp["train"])
    m3 = fit_trouble(sp["train"], sp["val"], seed)
    return {"m1": m1, "m2": m2, "m3": m3, "held": sorted(held), "features": M3_FEATURES}


def score(bundle: dict, panel: pd.DataFrame) -> pd.DataFrame:
    """Everything the tool outputs for each facility-month."""
    p = add_expected(panel, bundle["m1"])
    p = pd.concat([p, predict_trouble(bundle["m3"], p)], axis=1)
    p["pred_cause"] = None
    q = p.on_time_now == 0
    if q.any():
        p.loc[q, "pred_cause"] = predict_silence(bundle["m2"], p[q])
    p["insecure_modifier"] = (p.insecurity_exposure >= INSECURE_EXPOSURE).astype(int)
    p["impact"] = impact(p)
    p["priority"] = p.p_trouble * p.impact
    return p


def impact(p: pd.DataFrame) -> pd.Series:
    """People affected: catchment, weighted up when the nearest alternative is
    far (real GRID3 distances: median about 1 km, so the weight is modest)."""
    return (p.catchment_pop / 6000) * (1 + np.clip(p.dist_nearest_alt_km, 0, 20) / 10)
