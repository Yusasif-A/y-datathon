"""
PHC Triage — synthetic world generator.

Builds a fake state of primary health centres, plays out a HIDDEN monthly
truth (stock, staffing, fridges, closures), and turns that truth into messy
DHIS2-shaped reports the way a real records office would: struggling
facilities report late, incompletely, or not at all, and whole LGAs
occasionally go quiet together because of a records-office backlog that has
nothing to do with any single facility's trouble.

Everything here is SYNTHETIC. Facility names, LGA names and numbers are
sample data for the demo, not real geography or real DHIS2 records.

The model-facing output (facilities, reports) must never leak the hidden
truth (stock levels, closures, the real cause of a silence). Hidden truth is
returned separately and used only for training labels / validation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRACER_ITEMS = ["antimalarial", "maternal_drug", "family_planning", "vaccine", "ors"]

# Link strength = how strongly a facility's real trouble pushes its reporting
# behaviour off course. Unknown in real life, so the doc asks us to sweep it.
LINK_C = {"weak": 0.5, "medium": 1.5, "strong": 3.0}

STATE_NAME = "Amara State (sample)"
LGA_NAMES = [
    "Amara Central", "Amara North", "Amara South", "Amara East",
    "Igbodo", "Okuta", "Ladoke", "Bamidele",
]
# LGAs with an active insecurity context this world — escalate, never visit.
INSECURE_LGAS = {"Okuta"}

# Nigeria-ish bounding box, just for a plausible-looking map.
LAT_RANGE = (6.2, 9.8)
LON_RANGE = (3.2, 8.0)


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _month_index(months):
    return pd.period_range("2024-01", periods=months, freq="M")


def generate_world(seed: int = 1, n_lgas: int = 8, facilities_per_lga: int = 25,
                    months: int = 24, link_strength: str = "medium") -> dict:
    rng = np.random.default_rng(seed)
    c = LINK_C[link_strength]
    lgas = LGA_NAMES[:n_lgas]
    periods = _month_index(months)

    # ── LGA centres (so facilities cluster sensibly on the map) ───────────
    lga_center = {
        lga: (rng.uniform(*LAT_RANGE), rng.uniform(*LON_RANGE))
        for lga in lgas
    }
    lga_effect = {lga: rng.normal(0, 0.4) for lga in lgas}
    state_effect = rng.normal(0, 0.1)

    # ── Facilities ──────────────────────────────────────────────────────
    rows = []
    fid = 0
    for lga in lgas:
        clat, clon = lga_center[lga]
        for i in range(facilities_per_lga):
            urban = rng.random() < 0.28
            population = int(rng.lognormal(mean=9.8 if urban else 9.1, sigma=0.35))
            population = int(np.clip(population, 1500, 60000))
            power_reliability = float(np.clip(rng.beta(5, 2) if urban else rng.beta(2, 3), 0.02, 0.98))
            connectivity = float(np.clip(rng.beta(5, 2) if urban else rng.beta(2, 4), 0.02, 0.98))
            staff_standard = 6 if urban else 4
            staff_count = max(1, int(round(staff_standard * rng.normal(0.95 if urban else 0.86, 0.16))))
            distance_km = float(np.clip(rng.exponential(4 if urban else 14), 1, 60))
            funding_status = rng.choice(["on_time", "delayed", "severely_delayed"], p=[0.5, 0.35, 0.15])
            insecurity = lga in INSECURE_LGAS and rng.random() < 0.5
            records_officer_present = rng.random() < (0.85 if urban else 0.55)
            lat = float(np.clip(clat + rng.normal(0, 0.12), *LAT_RANGE))
            lon = float(np.clip(clon + rng.normal(0, 0.12), *LON_RANGE))
            # Intrinsic habit: some facilities are just chronically poor reporters
            # regardless of trouble — a trait, not an event.
            chronic_poor_habit = rng.random() < (0.08 if records_officer_present else 0.35)

            name = f"{lga} PHC {i + 1}"
            if lga == lgas[0] and i == 0:
                name = "Ada's Health Centre"  # the doc's worked example

            rows.append(dict(
                facility_id=f"F{fid:04d}", name=name, lga=lga, state=STATE_NAME,
                lat=lat, lon=lon, urban=urban, population=population,
                power_reliability=round(power_reliability, 3),
                connectivity=round(connectivity, 3),
                staff_count=staff_count, staff_standard=staff_standard,
                distance_km=round(distance_km, 1), funding_status=funding_status,
                insecurity=bool(insecurity), records_officer_present=bool(records_officer_present),
                chronic_poor_habit=bool(chronic_poor_habit),
            ))
            fid += 1
    facilities = pd.DataFrame(rows)

    # ── LGA-wide data-entry backlog months (not a facility problem) ───────
    n_backlogs = max(1, months // 10)
    backlog_events = set()
    for _ in range(n_backlogs):
        backlog_events.add((rng.choice(lgas), int(rng.integers(3, months))))

    # ── Hidden truth + messy reports, month by month ──────────────────────
    stock = {(f, item): 100.0 for f in facilities.facility_id for item in TRACER_ITEMS}
    fridge_working = {f: True for f in facilities.facility_id}
    staff_now = dict(zip(facilities.facility_id, facilities.staff_count))
    closed_until = {f: -1 for f in facilities.facility_id}
    good_history = {f: 0 for f in facilities.facility_id}  # consecutive on-time months

    hidden_rows = []
    report_rows = []

    fac_by_id = facilities.set_index("facility_id")

    for m_idx, period in enumerate(periods):
        season = 1.0 + 0.12 * np.sin(2 * np.pi * (m_idx % 12) / 12)
        for fac in facilities.itertuples():
            f = fac.facility_id
            lga = fac.lga
            fill_prob = {"on_time": 0.85, "delayed": 0.6, "severely_delayed": 0.35}[fac.funding_status]
            is_insecurity_event = fac.insecurity and rng.random() < 0.06
            closed_now = closed_until[f] >= m_idx or (is_insecurity_event and rng.random() < 0.3)
            if closed_now and closed_until[f] < m_idx:
                closed_until[f] = m_idx + int(rng.integers(1, 3))

            # Staff: small chance of attrition, slow recovery.
            if rng.random() < 0.03:
                staff_now[f] = max(1, staff_now[f] - 1)
            elif staff_now[f] < fac.staff_standard and rng.random() < 0.05:
                staff_now[f] += 1
            understaffed = staff_now[f] < 0.75 * fac.staff_standard

            # Fridge: depends on power reliability, with inertia. Failure is a
            # rare, noteworthy event, not a coin-flip steady state.
            if fridge_working[f]:
                breakdown_p = 0.07 * (1 - fac.power_reliability)
                if rng.random() < breakdown_p:
                    fridge_working[f] = False
            else:
                if rng.random() < 0.55:
                    fridge_working[f] = True

            # Demand & stock per tracer item.
            base_demand = fac.population / 12000.0 * season
            stockouts = {}
            for item in TRACER_ITEMS:
                demand = base_demand * rng.lognormal(0, 0.25) * (1.4 if item == "vaccine" and not fridge_working[f] else 1.0)
                consumed = min(stock[(f, item)], demand)
                stock[(f, item)] -= consumed
                reorder_threshold = demand * 1.5
                if stock[(f, item)] < reorder_threshold and rng.random() < fill_prob:
                    stock[(f, item)] += demand * rng.uniform(2.5, 4.5)
                stock[(f, item)] = min(stock[(f, item)], demand * 6 + 10)
                stockouts[item] = stock[(f, item)] <= 0.01

            trouble_now = closed_now or understaffed or (not fridge_working[f]) or any(stockouts.values())
            distress = np.mean([closed_now, understaffed, not fridge_working[f], any(stockouts.values())])

            backlog_month = (lga, m_idx) in backlog_events
            true_cause = "none"
            if backlog_month:
                true_cause = "lga_wide"
            elif is_insecurity_event:
                true_cause = "insecure_silence"
            elif distress > 0 and good_history[f] >= 6:
                true_cause = "sudden_drop"
            elif fac.chronic_poor_habit:
                true_cause = "chronic_poor_reporter"
            elif distress > 0:
                true_cause = "late_entry"

            hidden_rows.append(dict(
                facility_id=f, month=str(period), month_idx=m_idx,
                **{f"stockout_{k}": v for k, v in stockouts.items()},
                fridge_working=fridge_working[f], closed=bool(closed_now),
                understaffed=bool(understaffed), staff_count=staff_now[f],
                distress=round(float(distress), 3), trouble_now=bool(trouble_now),
                lga_backlog_month=bool(backlog_month), true_cause=true_cause,
            ))

            # ── Reporting behaviour ────────────────────────────────────────
            backlog_penalty = 3.5 if backlog_month else 0.0
            habit_penalty = 0.9 if fac.chronic_poor_habit else 0.0
            logit_report = (
                0.35
                + 1.4 * fac.records_officer_present
                + 1.1 * fac.connectivity
                - c * distress
                - backlog_penalty
                - habit_penalty
                + lga_effect[lga] + state_effect
                + rng.normal(0, 0.5)
            )
            p_report = _sigmoid(logit_report)
            reported = rng.random() < p_report

            if reported:
                good_history[f] += 1
                logit_ontime = logit_report + 0.6 - 0.6 * backlog_month
                on_time = rng.random() < _sigmoid(logit_ontime)
                completeness = float(np.clip(rng.beta(6, 1.2) - 0.3 * distress, 0.05, 1.0))

                svc_noise = rng.lognormal(0, 0.18)
                fp_mult = 0.25 if stockouts["family_planning"] else 1.0
                vacc_mult = 0.2 if (stockouts["vaccine"] or not fridge_working[f]) else 1.0
                ors_mult = 0.3 if stockouts["ors"] else 1.0
                staff_mult = 0.5 if understaffed else 1.0

                anc_visits = base_demand * 3.2 * svc_noise * staff_mult * (0 if closed_now else 1)
                family_planning_visits = base_demand * 2.0 * svc_noise * fp_mult * staff_mult * (0 if closed_now else 1)
                penta3_vaccinations = base_demand * 1.6 * svc_noise * vacc_mult * staff_mult * (0 if closed_now else 1)
                ors_given = base_demand * 1.1 * svc_noise * ors_mult * (0 if closed_now else 1)
                deliveries = base_demand * 0.5 * svc_noise * staff_mult * (0 if closed_now else 1)

                report_rows.append(dict(
                    facility_id=f, month=str(period), month_idx=m_idx,
                    reported=True, on_time=bool(on_time), completeness=round(completeness, 3),
                    anc_visits=round(anc_visits, 1), family_planning_visits=round(family_planning_visits, 1),
                    penta3_vaccinations=round(penta3_vaccinations, 1), ors_given=round(ors_given, 1),
                    deliveries=round(deliveries, 1),
                    service_total=round(anc_visits + family_planning_visits + penta3_vaccinations + ors_given + deliveries, 1),
                ))
            else:
                good_history[f] = 0
                report_rows.append(dict(
                    facility_id=f, month=str(period), month_idx=m_idx,
                    reported=False, on_time=False, completeness=0.0,
                    anc_visits=np.nan, family_planning_visits=np.nan,
                    penta3_vaccinations=np.nan, ors_given=np.nan, deliveries=np.nan,
                    service_total=np.nan,
                ))

    reports = pd.DataFrame(report_rows)
    hidden = pd.DataFrame(hidden_rows)

    return dict(
        seed=seed, link_strength=link_strength, months=months,
        periods=[str(p) for p in periods], lgas=lgas, state=STATE_NAME,
        facilities=facilities, reports=reports, hidden=hidden,
        backlog_events=sorted(backlog_events),
    )


def long_dhis2_table(world: dict) -> pd.DataFrame:
    """The reports in the actual long DHIS2 shape: facility, data element, period, value.

    This is the "it matches DHIS2 so real data can replace it" artefact — the
    wide `reports` frame above is what the models and API use internally.
    """
    elements = ["anc_visits", "family_planning_visits", "penta3_vaccinations", "ors_given", "deliveries"]
    long = world["reports"].melt(
        id_vars=["facility_id", "month", "reported"], value_vars=elements,
        var_name="data_element", value_name="value",
    )
    long = long.dropna(subset=["value"])
    return long[["facility_id", "data_element", "month", "value"]].reset_index(drop=True)
