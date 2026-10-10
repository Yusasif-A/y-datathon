"""
Simulated officer feedback loop (spec 10). Shows the MECHANISM by which
Confirmed / Cleared / Just late answers become labels; it is not evidence of
how real officers behave.

Each round (one month): the model scores everyone; the top k per LGA are
checked, plus a random audit of unflagged facilities; a simulated officer
answers from the hidden truth, with method-dependent errors; answers become
weighted labels; the model is retrained every few rounds.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import models

STATUS_ORDER = ["confirmed", "cleared", "just_late", "could_not_verify"]


def run(panel: pd.DataFrame, bundle: dict, cfg: dict, months: range, audit: bool = True,
        accuracy_override: float | None = None, seed: int = 0, retrain: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    fb = cfg["feedback"]
    rng = np.random.default_rng(seed)
    methods = list(fb["method_mix"])
    mix = np.array([fb["method_mix"][m] for m in methods])
    acc = {m: (accuracy_override if accuracy_override is not None else fb["method_accuracy"][m]) for m in methods}

    p = models.add_expected(panel, bundle["m1"])
    held = set(bundle["held"])
    sp = models.split(p, held)
    base_train, val = sp["train"], sp["val"]
    main = bundle["m3"]["main"]
    version = 1

    snooze_until = {}      # facility -> first month it may be flagged again
    follow_up = set()      # confirmed, waiting for the problem to clear
    last_score = {}
    records, rounds = [], []
    feedback_rows = []

    for r, t in enumerate(months, start=1):
        cur = p[p.t == t].copy()
        cur["score"] = main.predict(cur)
        cur["priority"] = cur.score * models.impact(cur)

        # Confirmed facilities leave follow-up once the problem has cleared.
        truth_now = cur.set_index("facility_id").y_serious_now
        follow_up = {f for f in follow_up if truth_now.get(f, 0) == 1}

        def eligible(f, s):
            if f in follow_up:
                return False
            until = snooze_until.get(f, -1)
            if t < until and s - last_score.get(f, s) < fb["rescore_jump"]:
                return False
            return True

        cur["eligible"] = [eligible(f, s) for f, s in zip(cur.facility_id, cur.score)]
        picked = []
        for _, g in cur[cur.eligible].groupby("lga"):
            top = g.nlargest(fb["k_per_lga"], "score")
            picked += [(i, "top_k") for i in top.index]
            if audit:
                rest = g.drop(top.index)
                n = min(fb["random_audit_per_lga"], len(rest))
                picked += [(i, "random_audit") for i in rng.choice(rest.index, n, replace=False)]

        for i, how in picked:
            row = cur.loc[i]
            f = row.facility_id
            method = methods[rng.choice(len(methods), p=mix)]
            if rng.random() < fb["no_answer_share"]:
                status = "could_not_verify"
            else:
                right = rng.random() < acc[method]
                if row.y_serious_now == 1:
                    status = "confirmed" if right else "cleared"
                elif row.y_cause in ("entry_delay", "lga_backlog"):
                    status = "just_late" if right else "cleared"
                else:
                    status = "cleared" if right else "confirmed"
            records.append({
                "facility_id": f, "period_flagged": row.period, "round": r,
                "score_at_flag": round(float(row.priority), 4), "trouble_prob_at_flag": round(float(row.score), 4),
                "model_version": f"v{version}", "selected_by": how, "status": status, "method": method,
                "problem_type": _problem_type(row) if status == "confirmed" else "",
                "response_lag_days": int(rng.integers(fb["response_lag_days"][0], fb["response_lag_days"][1] + 1)),
                "truth_serious_now": int(row.y_serious_now),
            })
            last_score[f] = float(row.score)
            if status == "confirmed":
                follow_up.add(f)
            elif status in ("cleared", "just_late"):
                snooze_until[f] = t + fb["snooze_months"]
            else:
                snooze_until[f] = t + 1
            if status != "could_not_verify":
                fr = p.loc[i].copy()
                fr["y_serious_next12"] = 1.0 if status == "confirmed" else 0.0
                fr["w"] = acc[method]
                feedback_rows.append(fr)

        rec = pd.DataFrame([x for x in records if x["round"] == r])
        topk = rec[rec.selected_by == "top_k"]
        answered = topk[topk.status != "could_not_verify"]
        # True quality of the current model on EVERY facility this month.
        lab = cur.dropna(subset=["y_serious_next12"])
        flag = lab.groupby("lga").score.rank(ascending=False, method="first") <= fb["k_per_lga"]
        rounds.append({
            "round": r, "period": cur.period.iloc[0], "model_version": f"v{version}",
            "checked": int(len(rec)), "confirmed_share_of_answered_topk": round(float((answered.status == "confirmed").mean()), 3)
            if len(answered) else None,
            "could_not_verify_share": round(float((rec.status == "could_not_verify").mean()), 3),
            "audit_confirmed_share": round(float((rec[rec.selected_by == "random_audit"].status == "confirmed").mean()), 3)
            if audit and (rec.selected_by == "random_audit").any() else None,
            "true_precision_at_k": round(float(lab[flag].y_serious_next12.mean()), 3),
        })

        if retrain and r % fb["retrain_every_rounds"] == 0 and feedback_rows:
            fbdf = pd.DataFrame(feedback_rows)
            train = pd.concat([base_train.assign(w=1.0), fbdf], ignore_index=True)
            main = models.Calibrated("y_serious_next12").fit(train, val, weight_col="w")
            version += 1

    return pd.DataFrame(records), pd.DataFrame(rounds)


def _problem_type(row) -> str:
    probs = {k: row.get(f"y_{k}_next12", 0) for k in ("closed", "staff", "vaccine", "medicine")}
    for k, v in probs.items():
        if v == 1:
            return k
    return "other"


def summarise(rounds: pd.DataFrame) -> dict:
    return {
        "rounds": int(len(rounds)),
        "confirmed_share_first_3": round(float(rounds.confirmed_share_of_answered_topk.head(3).mean()), 3),
        "confirmed_share_last_3": round(float(rounds.confirmed_share_of_answered_topk.tail(3).mean()), 3),
        "true_precision_first_3": round(float(rounds.true_precision_at_k.head(3).mean()), 3),
        "true_precision_last_3": round(float(rounds.true_precision_at_k.tail(3).mean()), 3),
        "could_not_verify_share": round(float(rounds.could_not_verify_share.mean()), 3),
    }
