"""
Trains the three models from the solution doc on a simulated world and
grades them against the hidden truth they never saw. All numbers this
produces are synthetic-data numbers — label them as such wherever shown.

Model 1 — Expected-report: LightGBM regressor, expected service volume given
          history/season/traits. Big gap between expected and actual = warning.
Model 2 — Silence-type: LightGBM multiclass classifier for WHY a facility is
          quiet (late entry / chronic poor reporter / sudden drop / LGA-wide /
          insecure), trained against the simulator's true cause.
Model 3 — Trouble forecast: LightGBM binary classifier, calibrated, for the
          chance of running short of something in the next 1-2 months.
          Uncertainty = std across a small bootstrap ensemble (a light stand-in
          for full conformal prediction, documented as such).
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier, LGBMRegressor
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import brier_score_loss, log_loss

import data_loader
import features
import simulator

warnings.filterwarnings("ignore")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# The hand-built dataset in stimulate-data/ (real Oyo State LGAs, 311
# facilities, 36 months, two pre-built worlds) is richer than our own
# generator — prefer it when present, fall back to simulator.py otherwise.
USE_STIMULATE_DATA = data_loader.available()

TRAIN_SEED = 1
STRESS_SEED = 7
TRAIN_LINK = "medium"
STRESS_LINK = "weak"
N_ENSEMBLE = 5


def _load_train_world():
    if USE_STIMULATE_DATA:
        return data_loader.load_world("A")
    return simulator.generate_world(seed=TRAIN_SEED, link_strength=TRAIN_LINK)


def _load_stress_world():
    if USE_STIMULATE_DATA:
        return data_loader.load_world("B")
    return simulator.generate_world(seed=STRESS_SEED, link_strength=STRESS_LINK)


def _time_split(panel: pd.DataFrame, train_frac: float = 0.7):
    labelled = panel.dropna(subset=["trouble_next_1_2m"])
    cutoff = int(labelled["month_idx"].max() * train_frac)
    train = labelled[labelled["month_idx"] <= cutoff]
    test = labelled[labelled["month_idx"] > cutoff]
    return train, test


def train_expected_report_model(panel: pd.DataFrame) -> LGBMRegressor:
    reported = panel[panel["reported"]].dropna(subset=["service_total"])
    cols = ["population_log", "urban", "power_reliability", "connectivity", "staff_ratio",
            "distance_km", "records_officer_present", "season_sin", "season_cos",
            "reported_rate_12m", "month_idx"]
    model = LGBMRegressor(n_estimators=150, max_depth=5, learning_rate=0.08, verbose=-1)
    model.fit(reported[cols], reported["service_total"])
    return model, cols


def train_silence_model(panel: pd.DataFrame):
    silent = panel[panel["months_since_last_report"] >= 1].copy()
    silent["true_cause"] = silent["true_cause"].fillna("none")
    cols = ["months_since_last_report", "reported_rate_3m", "reported_rate_6m", "reported_rate_12m",
            "reported_rate_24m", "sudden_drop_signal", "lga_concurrent_silent_frac",
            "connectivity", "records_officer_present", "insecurity"]
    y = silent["true_cause"].astype("category")
    cats = list(y.cat.categories)
    model = LGBMClassifier(n_estimators=150, max_depth=5, learning_rate=0.08, verbose=-1)
    model.fit(silent[cols], y.cat.codes)
    return model, cols, cats


def train_trouble_model(train_df: pd.DataFrame):
    cols = features.FEATURE_COLUMNS
    base = LGBMClassifier(n_estimators=200, max_depth=5, learning_rate=0.06, subsample=0.85,
                          colsample_bytree=0.85, verbose=-1)
    calibrated = CalibratedClassifierCV(base, method="isotonic", cv=3)
    calibrated.fit(train_df[cols], train_df["trouble_next_1_2m"].astype(int))

    rng = np.random.default_rng(0)
    ensemble = []
    for i in range(N_ENSEMBLE):
        idx = rng.choice(len(train_df), size=len(train_df), replace=True)
        sample = train_df.iloc[idx]
        m = LGBMClassifier(n_estimators=150, max_depth=5, learning_rate=0.08,
                           subsample=0.8, colsample_bytree=0.8, verbose=-1, random_state=i)
        m.fit(sample[cols], sample["trouble_next_1_2m"].astype(int))
        ensemble.append(m)
    return calibrated, ensemble, cols


def predict_trouble(calibrated, ensemble, cols, df: pd.DataFrame):
    p_main = calibrated.predict_proba(df[cols])[:, 1]
    ens_preds = np.stack([m.predict_proba(df[cols])[:, 1] for m in ensemble], axis=1)
    p_ens_mean = ens_preds.mean(axis=1)
    p_uncertainty = ens_preds.std(axis=1)
    return p_main, p_ens_mean, p_uncertainty


def precision_recall_at_k(scores: pd.Series, labels: pd.Series, k: int) -> dict:
    order = scores.sort_values(ascending=False).index
    top_k = order[:k]
    tp = labels.loc[top_k].sum()
    precision = tp / k if k else 0.0
    recall = tp / labels.sum() if labels.sum() else 0.0
    return {"k": int(k), "precision": round(float(precision), 3), "recall": round(float(recall), 3)}


def calibration_table(probs: np.ndarray, labels: np.ndarray, bins: int = 5) -> list:
    df = pd.DataFrame({"p": probs, "y": labels})
    df["bucket"] = pd.qcut(df["p"], bins, duplicates="drop")
    out = []
    for b, g in df.groupby("bucket"):
        out.append({"bucket": str(b), "predicted_mean": round(float(g["p"].mean()), 3),
                    "actual_rate": round(float(g["y"].mean()), 3), "n": int(len(g))})
    return out


def run_training() -> dict:
    train_world = _load_train_world()
    stress_world = _load_stress_world()

    panel = features.build_panel(train_world)
    stress_panel = features.build_panel(stress_world)

    expected_model, expected_cols = train_expected_report_model(panel)
    silence_model, silence_cols, silence_cats = train_silence_model(panel)

    train_df, test_df = _time_split(panel)
    calibrated, ensemble, trouble_cols = train_trouble_model(train_df)

    p_main, p_ens, p_unc = predict_trouble(calibrated, ensemble, trouble_cols, test_df)
    test_df = test_df.assign(p_trouble=p_main, p_uncertainty=p_unc)

    k = max(10, int(len(test_df) * 0.15))
    model_pr = precision_recall_at_k(test_df["p_trouble"], test_df["trouble_next_1_2m"], k)

    baseline_score = test_df["months_since_last_report"]
    baseline_pr = precision_recall_at_k(baseline_score, test_df["trouble_next_1_2m"], k)

    cal_table = calibration_table(test_df["p_trouble"].values, test_df["trouble_next_1_2m"].values)
    brier = float(brier_score_loss(test_df["trouble_next_1_2m"], test_df["p_trouble"]))

    rural = test_df[test_df["urban"] == 0]
    urban = test_df[test_df["urban"] == 1]
    fairness = {
        "rural": precision_recall_at_k(rural["p_trouble"], rural["trouble_next_1_2m"],
                                       max(5, int(len(rural) * 0.15))),
        "urban": precision_recall_at_k(urban["p_trouble"], urban["trouble_next_1_2m"],
                                       max(5, int(len(urban) * 0.15))),
    }

    # Stress test: model trained on TRAIN_LINK world, graded on a weaker-link,
    # noisier world it has never seen.
    stress_labelled = stress_panel.dropna(subset=["trouble_next_1_2m"])
    sp_main, sp_ens, sp_unc = predict_trouble(calibrated, ensemble, trouble_cols, stress_labelled)
    stress_labelled = stress_labelled.assign(p_trouble=sp_main)
    stress_k = max(10, int(len(stress_labelled) * 0.15))
    stress_pr = precision_recall_at_k(stress_labelled["p_trouble"], stress_labelled["trouble_next_1_2m"], stress_k)
    stress_baseline_pr = precision_recall_at_k(stress_labelled["months_since_last_report"],
                                               stress_labelled["trouble_next_1_2m"], stress_k)

    # Detection lag: among facilities later confirmed in trouble, how many
    # months BEFORE the baseline fires (silent >=2m) does our score already
    # rank them in the top band?
    lag_rows = []
    for fid, g in test_df.sort_values("month_idx").groupby("facility_id"):
        trouble_months = g[g["trouble_next_1_2m"] == 1]["month_idx"]
        baseline_months = g[g["months_since_last_report"] >= 2]["month_idx"]
        flagged_months = g[g["p_trouble"] >= 0.5]["month_idx"]
        if len(trouble_months) and len(flagged_months) and len(baseline_months):
            lag_rows.append(int(baseline_months.min()) - int(flagged_months.min()))
    detection_lag_months = round(float(np.mean(lag_rows)), 2) if lag_rows else 0.0

    validation = {
        "note": f"All numbers below are from SYNTHETIC data ({train_world.get('source', 'simulator.py')}), labelled as such for the demo.",
        "train_world": {"seed": train_world["seed"], "link_strength": train_world["link_strength"],
                       "source": train_world.get("source", "simulator.py")},
        "stress_world": {"seed": stress_world["seed"], "link_strength": stress_world["link_strength"],
                         "source": stress_world.get("source", "simulator.py")},
        "k_evaluated": k,
        "model_precision_recall_at_k": model_pr,
        "baseline_precision_recall_at_k": baseline_pr,
        "beats_baseline": model_pr["precision"] > baseline_pr["precision"],
        "calibration": cal_table,
        "brier_score": round(brier, 4),
        "fairness_rural_vs_urban": fairness,
        "stress_test": {
            "model_precision_recall_at_k": stress_pr,
            "baseline_precision_recall_at_k": stress_baseline_pr,
            "beats_baseline": stress_pr["precision"] > stress_baseline_pr["precision"],
        },
        "detection_lag_months_vs_baseline": detection_lag_months,
        "silence_cause_label_counts": panel["true_cause"].value_counts(dropna=False).to_dict(),
    }

    bundle = {
        "expected_model": expected_model, "expected_cols": expected_cols,
        "silence_model": silence_model, "silence_cols": silence_cols, "silence_cats": silence_cats,
        "trouble_model": calibrated, "trouble_ensemble": ensemble, "trouble_cols": trouble_cols,
    }
    joblib.dump(bundle, DATA_DIR / "models.joblib")
    (DATA_DIR / "validation.json").write_text(json.dumps(validation, indent=2, default=str))
    joblib.dump(train_world, DATA_DIR / "train_world.joblib")

    print(json.dumps(validation, indent=2, default=str))
    return {"validation": validation, "bundle": bundle, "world": train_world}


if __name__ == "__main__":
    run_training()
