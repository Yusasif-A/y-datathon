"""
Loads one simulated world. Model-visible tables and hidden truth are loaded
by separate functions so a model can never be handed the truth by accident
(spec 11: models read only facilities, reports and stock_reports).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def make_periods(start: int, months: int) -> list[str]:
    """Same as simulator.make_periods; duplicated so ml/ never imports the
    simulator (backend/ has its own module named simulator)."""
    y, m = divmod(int(start), 100)
    out = []
    for _ in range(months):
        out.append(f"{y}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


@dataclass
class Visible:
    """What a real system would have. Arrays are [facility, period(, ...)]."""
    facilities: pd.DataFrame
    periods: list[str]
    elements: list[str]
    values: np.ndarray        # [F, T, E] reported value, NaN if blank or never received
    recv_t: np.ndarray        # [F, T] index of the month the report arrived; -1 = never
    stock_items: list[str]
    stockout_days: np.ndarray  # [F, T, I] NaN if no stock report for that item/month
    stock_recv_t: np.ndarray   # [F, T] index of the month the stock report arrived; -1 = none
    world: str


@dataclass
class Truth:
    """Hidden answers, for labels and grading only."""
    fm: pd.DataFrame          # truth_facility_month
    silence: pd.DataFrame     # truth_silence_events
    lga_month: pd.DataFrame


def _period_index(periods: list[str]):
    start_y, start_m = int(periods[0][:4]), int(periods[0][4:])

    def idx(p):
        p = np.asarray(p).astype(int)
        return (p // 100 - start_y) * 12 + (p % 100 - start_m)
    return idx


def load_visible(world: str | Path) -> Visible:
    d = Path(world) if Path(world).is_absolute() else DATA / world
    fac = pd.read_csv(d / "facilities.csv")
    rep = pd.read_csv(d / "reports.csv", dtype={"period": str, "received_period": str})
    stock = pd.read_csv(d / "stock_reports.csv", dtype={"period": str, "received_period": str})
    run = json.loads((d / "run_metadata.json").read_text())["parameters"]["run"]
    periods = make_periods(run["start_period"], run["months"])
    idx = _period_index(periods)
    F, T = len(fac), len(periods)
    f_of = pd.Series(np.arange(F), index=fac.facility_id)
    elements = sorted(rep.dataElement.unique())
    e_of = {e: i for i, e in enumerate(elements)}

    values = np.full((F, T, len(elements)), np.nan)
    fi, ti = f_of[rep.orgUnit].values, idx(rep.period)
    values[fi, ti, rep.dataElement.map(e_of).values] = rep.value.values
    recv_t = np.full((F, T), -1)
    recv_t[fi, ti] = idx(rep.received_period)

    items = sorted(stock["item"].unique()) if len(stock) else []
    i_of = {it: i for i, it in enumerate(items)}
    sod = np.full((F, T, max(len(items), 1)), np.nan)
    srecv = np.full((F, T), -1)
    if len(stock):
        sf, st = f_of[stock.orgUnit].values, idx(stock.period)
        sod[sf, st, stock["item"].map(i_of).values] = stock.stockout_days.values
        srecv[sf, st] = idx(stock.received_period)
    return Visible(fac, periods, elements, values, recv_t, items, sod, srecv, str(world))


def load_truth(world: str | Path) -> Truth:
    d = Path(world) if Path(world).is_absolute() else DATA / world
    fm = pd.read_csv(d / "truth_facility_month.csv", dtype={"period": str})
    sil = pd.read_csv(d / "truth_silence_events.csv", dtype={"start": str, "end": str})
    lm = pd.read_csv(d / "truth_lga_month.csv", dtype={"period": str})
    return Truth(fm, sil, lm)
