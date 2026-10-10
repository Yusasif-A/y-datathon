"""
Evaluation (spec 11): precision@k, recall@k, detection lag and calibration,
overall and by rural/urban and state, against two baselines:
  (a) "no report for 2 months"  - rank by months silent
  (b) "low reporting rate"      - rank by 1 - share of the last 12 months received
All numbers come from SYNTHETIC data.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, f1_score, roc_auc_score

from .features import CAUSES

K_PER_LGA = 5
K_PER_STATE = 20
LABEL = "y_serious_next12"


def baseline_scores(df: pd.DataFrame, seed: int = 0) -> pd.DataFrame:
    """Baselines rank with random tie-breaking (many facilities share a value)."""
    rng = np.random.default_rng(seed)
    jitter = rng.random(len(df)) * 1e-3
    return pd.DataFrame({
        "base_silent2": df.months_silent.values + jitter,
        "base_low_rate": (1 - df.known_rate_12.values) + jitter,
        "random": rng.random(len(df)),
    }, index=df.index)


def topk_flags(df: pd.DataFrame, score: str, k: int, by: list[str]) -> pd.Series:
    rank = df.groupby(by)[score].rank(ascending=False, method="first")
    return rank <= k


def at_k(df: pd.DataFrame, score: str, k: int, by: list[str], label: str = LABEL) -> dict:
    d = df.dropna(subset=[label])
    flag = topk_flags(d, score, k, by)
    y = d[label].astype(int)
    tp = int((flag & (y == 1)).sum())
    return {"k": k, "per": "+".join(by[1:]), "flagged": int(flag.sum()),
            "precision": round(tp / max(int(flag.sum()), 1), 3),
            "recall": round(tp / max(int(y.sum()), 1), 3), "base_rate": round(float(y.mean()), 3)}


def detection(df: pd.DataFrame, score: str, k: int = K_PER_LGA) -> dict:
    """For each new serious-trouble episode starting in month s, was the
    facility already in its LGA's top k in month s-1 or s-2 (early warning)?"""
    d = df.sort_values(["facility_id", "t"]).copy()
    d["flag"] = topk_flags(d, score, k, ["t", "lga"])
    d["prev_now"] = d.groupby("facility_id").y_serious_now.shift(1)
    onsets = d[(d.y_serious_now == 1) & (d.prev_now == 0)][["facility_id", "t"]]
    flags = d.set_index(["facility_id", "t"]).flag
    early, warn = 0, []
    for f, s in onsets.itertuples(index=False):
        lead = [s - t for t in (s - 2, s - 1) if flags.get((f, t), False)]
        if lead:
            early += 1
            warn.append(max(lead))
    n = len(onsets)
    return {"onsets": n, "flagged_early": early, "early_rate": round(early / max(n, 1), 3),
            "mean_months_warning": round(float(np.mean(warn)), 2) if warn else 0.0}


def calibration(p: np.ndarray, y: np.ndarray, bins: int = 10) -> dict:
    df = pd.DataFrame({"p": p, "y": y}).dropna()
    df["bin"] = pd.cut(df.p, np.linspace(0, 1, bins + 1), include_lowest=True)
    table = [{"bin": str(b), "n": int(len(g)), "predicted": round(float(g.p.mean()), 3),
              "observed": round(float(g.y.mean()), 3)} for b, g in df.groupby("bin", observed=True) if len(g)]
    return {"brier": round(float(brier_score_loss(df.y, df.p)), 4),
            "auc": round(float(roc_auc_score(df.y, df.p)), 3), "table": table}


def compare(df: pd.DataFrame) -> dict:
    """Model vs both baselines on one scored test set."""
    df = pd.concat([df, baseline_scores(df)], axis=1)
    methods = {"model": "p_trouble", "baseline_a_silent_2_months": "base_silent2",
               "baseline_b_low_reporting_rate": "base_low_rate", "random": "random"}
    out = {}
    for name, col in methods.items():
        out[name] = {
            "per_lga_k5": at_k(df, col, K_PER_LGA, ["t", "state", "lga"]),
            "per_state_k20": at_k(df, col, K_PER_STATE, ["t", "state"]),
            "detection": detection(df, col),
        }
    # The literal rule: flag every facility silent 2+ months, however many that is.
    rule = df.months_silent >= 2
    y = df[LABEL]
    out["baseline_a_rule_as_written"] = {
        "flagged_per_month": round(float(rule.sum() / df.t.nunique()), 1),
        "precision": round(float(y[rule].mean()), 3) if rule.any() else None,
        "recall": round(float(y[rule].sum() / max(y.sum(), 1)), 3),
    }
    return out


def fairness(df: pd.DataFrame) -> dict:
    out = {}
    df = df.assign(flag=topk_flags(df.dropna(subset=[LABEL]), "p_trouble", K_PER_LGA, ["t", "state", "lga"]))
    for col, groups in (("urban", {0: "rural", 1: "urban"}), ("state", None)):
        for key, g in df.groupby(col):
            name = groups[key] if groups else key
            y = g[LABEL]
            fl = g.flag.fillna(False).astype(bool)
            out[str(name)] = {
                "facility_months": int(len(g)), "base_rate": round(float(y.mean()), 3),
                "flag_rate": round(float(fl.mean()), 3),
                "precision": round(float(y[fl].mean()), 3) if fl.any() else None,
                "recall": round(float(y[fl].sum() / max(y.sum(), 1)), 3),
                "mean_predicted": round(float(g.p_trouble.mean()), 3),
            }
    return out


def silence_report(df: pd.DataFrame) -> dict:
    q = df[(df.on_time_now == 0) & df.y_cause.notna() & df.pred_cause.notna()]
    if not len(q):
        return {}
    conf = pd.crosstab(q.y_cause, q.pred_cause).reindex(index=CAUSES, columns=CAUSES, fill_value=0)
    per_class = {c: round(float((q[q.y_cause == c].pred_cause == c).mean()), 3) for c in CAUSES if (q.y_cause == c).any()}
    ins = df[df.y_insecure.notna()]
    return {
        "rows": int(len(q)), "accuracy": round(float((q.y_cause == q.pred_cause).mean()), 3),
        "macro_f1": round(float(f1_score(q.y_cause, q.pred_cause, average="macro")), 3),
        "majority_class_accuracy": round(float(q.y_cause.value_counts(normalize=True).iloc[0]), 3),
        "recall_by_cause": per_class, "confusion": conf.to_dict(orient="index"),
        "insecure_modifier_agreement": round(float((ins.insecure_modifier == ins.y_insecure).mean()), 3),
    }


def type_report(df: pd.DataFrame) -> dict:
    out = {}
    for k in ("medicine", "vaccine", "staff", "closed"):
        lab = f"y_{k}_next12"
        d = df.dropna(subset=[lab])
        if d[lab].nunique() > 1:
            out[k] = {"base_rate": round(float(d[lab].mean()), 3),
                      "auc": round(float(roc_auc_score(d[lab], d[f"p_{k}"])), 3),
                      "brier": round(float(brier_score_loss(d[lab], d[f"p_{k}"])), 4)}
    return out


def full_report(scored_test: pd.DataFrame) -> dict:
    d = scored_test.dropna(subset=[LABEL])
    return {
        "facility_months": int(len(d)), "months": sorted(int(t) for t in d.t.unique()),
        "comparison": compare(d),
        "calibration": calibration(d.p_trouble.values, d[LABEL].values),
        "fairness": fairness(d),
        "failure_types": type_report(scored_test),
        "silence_type": silence_report(scored_test),
    }
