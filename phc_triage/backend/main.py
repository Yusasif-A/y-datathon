"""
PHC Triage API — FastAPI backend.

Serves the ranked "who to visit first" list, facility detail, the state
overview, the officer feedback loop (Confirmed / Cleared / Just late), CSV
and printable export, and a weekly brief (template by default; the doc asks
for an LLM to draft the prose on top of fixed numbers, never the other way
round — nothing here lets a model invent a number).

Everything is read-only with respect to DHIS2: there is no DHIS2 connection
in this build at all, by design — we read a synthetic stand-in shaped the
same way (facility, data element, month, value) and never write anywhere
but our own store.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import alerts
import calls
import scoring
import spec_pipeline
import whatsapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s")
logger = logging.getLogger("phc-triage")

DATA_DIR = Path(__file__).parent / "data"
FEEDBACK_PATH = DATA_DIR / "feedback.json"
_feedback_lock = threading.Lock()

STATE = {}  # world, bundle, risk_table, validation, built lazily at startup

# Status control (solution doc v2, section 7.4). A status records the RESULT
# of a check the officer made; the app verifies nothing itself.
STATUSES = ("checking", "confirmed", "cleared", "just_late", "could_not_verify")
RESULT_STATUSES = ("confirmed", "cleared", "just_late", "could_not_verify")
METHODS = ("phone", "visit", "committee_sms")
PROBLEM_TYPES = ("medicine", "vaccine", "staff", "closed", "other")
SNOOZE_DAYS = {"cleared": 45, "just_late": 45, "could_not_verify": 30}
OVERRIDE_SNOOZE_DAYS = 30
REFLAG_JUMP = 0.2  # a snoozed facility comes back if its trouble chance rises this much
JUST_LATE_ALERT_COUNT = 3  # this many "just late" answers in one LGA alerts the data office


def _migrate_entry(entry: dict) -> dict:
    """feedback.json used to hold one flat status per facility."""
    if "history" in entry:
        return entry
    history = []
    if entry.get("status"):
        history.append({"status": entry["status"], "at": entry.get("at"), "method": None})
    return {"history": history, "override": None}


def _load_feedback() -> dict:
    if FEEDBACK_PATH.exists():
        return {f: _migrate_entry(e) for f, e in json.loads(FEEDBACK_PATH.read_text()).items()}
    return {}


def _save_feedback(data: dict) -> None:
    FEEDBACK_PATH.write_text(json.dumps(data, indent=2))


def _model_version() -> str:
    """Saved with every status so a label can be traced to the model that raised the flag."""
    w = STATE["world"]
    model_path = spec_pipeline.BUNDLE
    stamp = pd.Timestamp(model_path.stat().st_mtime, unit="s").strftime("%Y%m%d-%H%M") if model_path.exists() else "unsaved"
    return f"{w.get('source', 'simulator')}-{w.get('seed')}-{stamp}"


def _refresh_risk_table():
    STATE["risk_table"] = spec_pipeline.build_risk_table(STATE["world"], STATE["bundle"])
    STATE["cluster_alerts"] = scoring.cluster_alerts(STATE["world"], STATE["risk_table"], spec_pipeline.CLUSTER_MIN_FRAC)


def _bootstrap():
    """Load the simulated baseline world (phc_triage/data/baseline) and the
    trained models (phc_triage/models), then score every facility."""
    STATE["world"], STATE["bundle"], STATE["validation"] = spec_pipeline.load()
    STATE["model_version"] = _model_version()
    _refresh_risk_table()
    DATA_DIR.mkdir(exist_ok=True)
    if not FEEDBACK_PATH.exists():
        _save_feedback({})
    logger.info("PHC Triage ready — %d facilities, %d LGAs", len(STATE["world"]["facilities"]), len(STATE["world"]["lgas"]))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap()
    task = asyncio.create_task(_alert_loop())
    yield
    task.cancel()


app = FastAPI(title="PHC Triage API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"],
)


def _nullable_object_col(series: pd.Series) -> pd.Series:
    """pandas infers a nullable StringDtype for .map() output on this pandas
    version, which stores missing values as NaN/NA. JSON can't encode those,
    so force back to plain-object dtype with real `None`."""
    s = series.astype(object)
    return s.where(s.notna(), None)


def _days_since(iso: str | None, now: pd.Timestamp) -> float:
    if not iso:
        return float("inf")
    return (now - pd.Timestamp(iso)).total_seconds() / 86400


def _override_active(entry: dict, now: pd.Timestamp) -> bool:
    ov = entry.get("override")
    return bool(ov) and _days_since(ov.get("at"), now) < OVERRIDE_SNOOZE_DAYS


def _queue(entry: dict, trouble_prob: float, now: pd.Timestamp) -> str:
    """Which list a facility belongs on: to_do, checking, follow_up or snoozed."""
    if _override_active(entry, now):
        return "snoozed"
    history = entry.get("history") or []
    if not history:
        return "to_do"
    last = history[-1]
    status = last["status"]
    if status == "checking":
        return "checking"
    if status == "confirmed":
        return "follow_up"
    snooze = SNOOZE_DAYS.get(status)
    if snooze and _days_since(last.get("at"), now) < snooze:
        # A strong new signal brings a cleared facility back early.
        at_flag = last.get("trouble_prob_at_flag")
        if at_flag is not None and trouble_prob - at_flag >= REFLAG_JUMP:
            return "to_do"
        return "snoozed"
    return "to_do"


def _risk_with_feedback() -> pd.DataFrame:
    fb = _load_feedback()
    now = pd.Timestamp.utcnow()
    rt = STATE["risk_table"].copy()
    entries = rt["facility_id"].map(lambda f: fb.get(f) or {})
    last = entries.map(lambda e: (e.get("history") or [None])[-1] or {})
    rt["officer_status"] = _nullable_object_col(last.map(lambda r: r.get("status")))
    rt["check_method"] = _nullable_object_col(last.map(lambda r: r.get("method")))
    rt["queue"] = [_queue(e, float(p), now) for e, p in zip(entries, rt["trouble_prob"])]
    rt["override_active"] = entries.map(lambda e: _override_active(e, now))
    return rt


def _row_to_card(r: pd.Series, rank: int | None = None) -> dict:
    return {
        "rank": int(rank if rank is not None else r["rank"]), "state_rank": int(r["rank"]),
        "facility_id": r["facility_id"], "name": r["name"], "lga": r["lga"],
        "tag": str(r["tag"]), "population": int(r["population"]), "people_served_label": f"About {int(round(r['population'], -2)):,}",
        "reason": r["reason"], "suggested_action": r["suggested_action"],
        "silence_type": r["silence_label"], "trouble_prob": round(float(r["trouble_prob"]), 3),
        "trouble_uncertainty": round(float(r["trouble_uncertainty"]), 3),
        "priority": round(float(r["priority"]), 4), "months_since_last_report": int(r["months_since_last_report"]),
        "distance_km": float(r["distance_km"]), "lat": float(r["lat"]), "lon": float(r["lon"]),
        "low_confidence": bool(r["low_confidence"]),
        "officer_status": r.get("officer_status"), "check_method": r.get("check_method"), "queue": r["queue"],
        "sample_data": True,
    }


def _to_do(g: pd.DataFrame, capacity: int) -> pd.DataFrame:
    """Top `capacity` facilities still needing a check. Low-confidence
    facilities are capped so thin data cannot flood the list."""
    todo = g[g["queue"] == "to_do"].sort_values("priority", ascending=False)
    low_cap = max(1, capacity // 4)
    keep = todo[~todo["low_confidence"]].index.union(todo[todo["low_confidence"]].head(low_cap).index)
    return todo.loc[todo.index.isin(keep)].head(capacity)


def _just_late_alerts(lga: str) -> list:
    """Several 'just late' answers in one LGA point to a data-entry backlog."""
    fb = _load_feedback()
    now = pd.Timestamp.utcnow()
    rt = STATE["risk_table"]
    ids = rt.loc[rt["lga"] == lga, "facility_id"]
    recent = [f for f in ids
              if (h := (fb.get(f) or {}).get("history")) and h[-1]["status"] == "just_late"
              and _days_since(h[-1].get("at"), now) < SNOOZE_DAYS["just_late"]]
    if len(recent) < JUST_LATE_ALERT_COUNT:
        return []
    return [{"lga": lga, "count": len(recent), "cause": "just_late_backlog",
             "message": f"{len(recent)} facilities in {lga} were checked and found 'just late'. "
                        f"Alert the LGA data office about a possible data-entry backlog."}]


@app.get("/health")
async def health():
    return {"ok": True, "service": "PHC Triage", "facilities": len(STATE["world"]["facilities"])}


@app.get("/api/meta")
async def meta():
    w = STATE["world"]
    return {
        "state": w["state"], "lgas": w["lgas"], "months": w["periods"],
        "data_as_of": w["periods"][-1], "sample_data": True,
        "default_capacity": 12,
    }


@app.get("/api/lgas")
async def lgas_summary():
    rt = _risk_with_feedback()
    out = []
    for lga, g in rt.groupby("lga"):
        out.append({
            "lga": lga, "facilities": int(len(g)),
            "high": int((g["tag"] == "HIGH").sum()), "medium": int((g["tag"] == "MEDIUM").sum()),
            "low": int((g["tag"] == "LOW").sum()),
            "flagged_this_week": int((g["tag"].isin(["HIGH", "MEDIUM"])).sum()),
        })
    out.sort(key=lambda r: -(r["high"] * 2 + r["medium"]))
    return {"lgas": out, "cluster_alerts": STATE["cluster_alerts"], "sample_data": True}


@app.get("/api/officer/{lga}")
async def officer_list(lga: str, capacity: int = Query(12, ge=1, le=100)):
    rt = _risk_with_feedback()
    g = rt[rt["lga"] == lga]
    if g.empty:
        raise HTTPException(404, f"Unknown LGA: {lga}")
    top = _to_do(g, capacity)
    by_priority = lambda q: g[g["queue"] == q].sort_values("priority", ascending=False)
    alerts = [a for a in STATE["cluster_alerts"] if a["lga"] == lga] + _just_late_alerts(lga)
    return {
        "lga": lga, "state": STATE["world"]["state"], "data_as_of": STATE["world"]["periods"][-1],
        "facilities_in_lga": int(len(g)), "flagged_this_week": int((g["tag"].isin(["HIGH", "MEDIUM"])).sum()),
        "lga_wide_alerts": len(alerts), "warning_banners": alerts, "capacity": capacity,
        "cards": [_row_to_card(r, i) for i, (_, r) in enumerate(top.iterrows(), 1)],
        "follow_up": [_row_to_card(r) for _, r in by_priority("follow_up").iterrows()],
        "checking": [_row_to_card(r) for _, r in by_priority("checking").iterrows()],
        "snoozed_count": int((g["queue"] == "snoozed").sum()),
        "sample_data": True,
    }


@app.get("/api/facility/{facility_id}")
async def facility_detail(facility_id: str):
    rt = _risk_with_feedback()
    row = rt[rt["facility_id"] == facility_id]
    if row.empty:
        raise HTTPException(404, "Unknown facility")
    r = row.iloc[0]
    reports = STATE["world"]["reports"]
    hist = reports[reports["facility_id"] == facility_id].sort_values("month_idx")
    chart = [{
        "month": m, "reported": bool(rep), "service_total": (None if pd.isna(st) else round(float(st), 1)),
    } for m, rep, st in zip(hist["month"], hist["reported"], hist["service_total"])]

    fb = (_load_feedback().get(facility_id) or {})
    override = fb.get("override") if r["override_active"] else None
    return {
        "facility_id": facility_id, "name": r["name"], "lga": r["lga"], "state": r["state"],
        "type": r["facility_type"] if "facility_type" in r else "Primary Health Centre",
        "lat": float(r["lat"]), "lon": float(r["lon"]),
        "population": int(r["population"]), "people_served_label": f"About {int(round(r['population'], -2)):,}",
        "tag": str(r["tag"]), "urban": bool(r["urban"]),
        "reporting_chart": chart, "expected_level": round(float(r["expected_service_total"]), 1),
        "reason": r["reason"], "reason_breakdown": r["reason_breakdown"], "suggested_action": r["suggested_action"],
        "silence_type": r["silence_label"], "trouble_prob": round(float(r["trouble_prob"]), 3),
        "trouble_uncertainty": round(float(r["trouble_uncertainty"]), 3),
        "months_since_last_report": int(r["months_since_last_report"]),
        "tiles": {
            # The spec pipeline supplies its own staff / cold chain / connectivity text.
            **(r["tiles"] if "tiles" in r and isinstance(r["tiles"], dict) else {
                "staff": f"{int(r['staff_count'])} of {int(r['staff_standard'])} standard",
                "cold_chain": "Needs check" if r["pred_cause"] in ("sudden_drop", "late_entry") and r["power_reliability"] < 0.5 else "No flag",
                "connectivity": f"{round(float(r['connectivity']) * 100)}%",
            }),
            "last_report": f"{int(r['months_since_last_report'])} month(s) ago" if r["months_since_last_report"] else "This month",
            "distance_km": float(r["distance_km"]),
        },
        "failure_types": r["failure_types"] if "failure_types" in r else None,
        "low_confidence": bool(r["low_confidence"]), "history_months": int(r["history_months"]),
        "officer_status": r["officer_status"], "check_method": r["check_method"], "queue": r["queue"],
        "status_history": fb.get("history", []), "override": override, "sample_data": True,
        "contact_hint": {
            "phone_sample": None if pd.isna(r.get("in_charge_phone_sample")) else str(r.get("in_charge_phone_sample")),
            "whatsapp_opt_in": None if pd.isna(r.get("whatsapp_opt_in")) else bool(r.get("whatsapp_opt_in")),
        },
    }


def _risk_row(facility_id: str) -> pd.Series:
    row = STATE["risk_table"][STATE["risk_table"]["facility_id"] == facility_id]
    if row.empty:
        raise HTTPException(404, "Unknown facility")
    return row.iloc[0]


@app.post("/api/facility/{facility_id}/feedback")
async def set_feedback(facility_id: str, body: dict):
    """Record the result of a check. Everything needed to turn it into a
    training label later is saved with it: score and model at the time,
    who answered, and how the check was done."""
    status = body.get("status")
    if status not in STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(STATUSES)}")
    method = body.get("method")
    if status in RESULT_STATUSES and method not in METHODS:
        raise HTTPException(400, f"Say how the check was done: method must be one of {', '.join(METHODS)}")
    problem_type = body.get("problem_type") or None
    if problem_type and (status != "confirmed" or problem_type not in PROBLEM_TYPES):
        raise HTTPException(400, f"problem_type is only for confirmed, one of {', '.join(PROBLEM_TYPES)}")
    r = _risk_row(facility_id)
    record = {
        "status": status, "at": pd.Timestamp.utcnow().isoformat(),
        "method": method if status != "checking" else None, "problem_type": problem_type,
        "answered_by": (body.get("answered_by") or "").strip() or None,
        "role": (body.get("role") or "").strip() or None,
        "score_at_flag": round(float(r["priority"]), 4),
        "trouble_prob_at_flag": round(float(r["trouble_prob"]), 4),
        "model_version": STATE["model_version"], "data_as_of": r["month"],
    }
    with _feedback_lock:
        fb = _load_feedback()
        entry = fb.setdefault(facility_id, {"history": [], "override": None})
        entry["history"].append(record)
        _save_feedback(fb)
    return {"ok": True, "facility_id": facility_id, **record}


@app.post("/api/facility/{facility_id}/override")
async def override_flag(facility_id: str, body: dict):
    """The officer disagrees with the flag WITHOUT checking. Needs a reason,
    snoozes the flag for 30 days, and is only a weak signal, never a label.
    Send {"clear": true} to lift it."""
    _risk_row(facility_id)
    clear = bool(body.get("clear"))
    reason = (body.get("reason") or "").strip()
    if not clear and not reason:
        raise HTTPException(400, "An override needs a short reason")
    with _feedback_lock:
        fb = _load_feedback()
        entry = fb.setdefault(facility_id, {"history": [], "override": None})
        entry["override"] = None if clear else {
            "reason": reason, "at": pd.Timestamp.utcnow().isoformat(),
            "snooze_days": OVERRIDE_SNOOZE_DAYS, "model_version": STATE["model_version"],
        }
        _save_feedback(fb)
    return {"ok": True, "facility_id": facility_id, "override": entry["override"]}


@app.get("/api/state/overview")
async def state_overview():
    rt = _risk_with_feedback()
    by_lga = []
    for lga, g in rt.groupby("lga"):
        by_lga.append({
            "lga": lga, "high": int((g["tag"] == "HIGH").sum()), "medium": int((g["tag"] == "MEDIUM").sum()),
            "low": int((g["tag"] == "LOW").sum()), "flagged": int((g["tag"].isin(["HIGH", "MEDIUM"])).sum()),
            "facilities": int(len(g)), "avg_lat": float(g["lat"].mean()), "avg_lon": float(g["lon"].mean()),
        })
    by_lga.sort(key=lambda r: -r["flagged"])
    return {
        "state": STATE["world"]["state"], "data_as_of": STATE["world"]["periods"][-1],
        "total_phcs": int(len(rt)), "high": int((rt["tag"] == "HIGH").sum()),
        "medium": int((rt["tag"] == "MEDIUM").sum()), "low": int((rt["tag"] == "LOW").sum()),
        "lga_tiles": by_lga, "cluster_alerts": STATE["cluster_alerts"], "sample_data": True,
    }


@app.get("/api/export.csv")
async def export_csv(lga: str | None = None, capacity: int = Query(1000, ge=1, le=5000)):
    rt = _risk_with_feedback()
    if lga:
        rt = rt[rt["lga"] == lga]
    rt = rt.sort_values("priority", ascending=False).head(capacity)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["rank", "facility", "lga", "tag", "people_served", "reason", "suggested_action",
                     "silence_type", "trouble_probability", "months_since_last_report", "low_confidence",
                     "officer_status", "check_method"])
    for _, r in rt.iterrows():
        writer.writerow([r["rank"], r["name"], r["lga"], r["tag"], int(r["population"]), r["reason"],
                         r["suggested_action"], r["silence_label"], round(float(r["trouble_prob"]), 3),
                         int(r["months_since_last_report"]), bool(r["low_confidence"]),
                         r["officer_status"] or "", r["check_method"] or ""])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=phc-triage-{lga or 'all'}.csv"})


@app.get("/api/printable")
async def printable(lga: str, capacity: int = Query(12, ge=1, le=100)):
    data = await officer_list(lga, capacity)
    return data


def _facility_message(kind: str, name: str) -> str:
    if kind == "reminder":
        return (f"Hello {name} in-charge, we have not received your report for this month. "
                f"Please send it to your LGA records officer. (Sample data — no blame, just a nudge.)")
    if kind == "confirmation":
        return f"Please confirm if your vaccine fridge is working at {name}. Reply YES or NO."
    raise HTTPException(400, "kind must be reminder or confirmation")


def _weekly_brief_template(lga: str, cards: list, alerts: list) -> str:
    lines = [f"*Weekly priority list, {lga}* (sample data)"]
    for c in cards[:5]:
        lines.append(f"{c['rank']}) {c['name']}: {c['tag']}. {c['reason']} Suggested: {c['suggested_action']}")
    if alerts:
        lines.append("")
        for a in alerts:
            lines.append(f"⚠ {a['message']}")
    lines.append("")
    lines.append("Open the full list on the dashboard and record the result of each check: "
                 "Confirmed / Cleared / Just late / Couldn't verify.")
    return "\n".join(lines)


@app.get("/api/weekly_brief", response_class=PlainTextResponse)
async def weekly_brief(lga: str, capacity: int = Query(12, ge=1, le=100)):
    data = await officer_list(lga, capacity)
    return _weekly_brief_template(lga, data["cards"], data["warning_banners"])


@app.get("/api/whatsapp/status")
async def whatsapp_status():
    return {"configured": whatsapp.configured()}


@app.post("/api/officer/{lga}/send_whatsapp")
async def send_officer_brief(lga: str, body: dict | None = None):
    """Send the weekly brief to the LGA officer's WhatsApp — "build this
    first" per the doc.

    There is no real phone list yet (the doc is explicit: in a pilot, numbers
    come from the LGA PHC coordinator with the in-charge's consent). So a
    fabricated "sample" number is never actually dialled through the real
    WhatsApp Business account — that would mean texting a real stranger on
    this account's quota. Without an explicit `to`, this is a dry run: it
    returns exactly the text that would be sent, with sent=false.
    """
    body = body or {}
    capacity = int(body.get("capacity") or 12)
    data = await officer_list(lga, capacity)
    text = _weekly_brief_template(lga, data["cards"], data["warning_banners"])
    to = body.get("to")
    if not to:
        return {"lga": lga, "text": text, "sent": False, "dry_run": True,
                "reason": "No real recipient number given — this is a preview, not configured to text a fabricated sample number."}
    result = await whatsapp.send_text(to, text)
    return {"lga": lga, "to": to, "text": text, "sent": result["ok"], "dry_run": False, **result}


@app.post("/api/facility/{facility_id}/send_whatsapp")
async def send_facility_message(facility_id: str, body: dict):
    """Mock-up path from section 8: a reminder or a YES/NO fridge check,
    sent only after the officer reviews it here — never automatic, and never
    dialled to a fabricated number (see send_officer_brief)."""
    kind = body.get("kind")
    rt = _risk_with_feedback()
    row = rt[rt["facility_id"] == facility_id]
    if row.empty:
        raise HTTPException(404, "Unknown facility")
    rrow = row.iloc[0]
    if kind == "reminder" and int(rrow["months_since_last_report"]) == 0:
        raise HTTPException(409, "This facility reported on time this month — a reminder would be factually wrong.")
    name = rrow["name"]
    text = _facility_message(kind, name)
    sample_hint = None if pd.isna(rrow.get("in_charge_phone_sample")) else str(rrow.get("in_charge_phone_sample"))
    opt_in = None if pd.isna(rrow.get("whatsapp_opt_in")) else bool(rrow.get("whatsapp_opt_in"))
    to = body.get("to")
    if not to:
        return {"facility_id": facility_id, "text": text, "sent": False, "dry_run": True,
                "sample_contact_hint": sample_hint, "whatsapp_opt_in": opt_in,
                "reason": "No real recipient number given — this is a preview, not configured to text a fabricated sample number."}
    result = await whatsapp.send_text(to, text)
    return {"facility_id": facility_id, "to": to, "text": text, "sent": result["ok"], "dry_run": False, **result}


@app.get("/api/validation")
async def validation():
    return STATE["validation"]


def _urgent_cards(lga: str) -> list:
    """HIGH facilities still on the officer's to-do list, highest priority first."""
    rt = _risk_with_feedback()
    g = rt[(rt["lga"] == lga) & (rt["tag"] == "HIGH") & (rt["queue"] == "to_do")]
    return [_row_to_card(r) for _, r in g.sort_values("priority", ascending=False).iterrows()]


async def _check_all_alerts():
    for lga in alerts.subscribed_lgas():
        if lga in STATE["world"]["lgas"]:
            try:
                await alerts.run_check(lga, _urgent_cards(lga))
            except Exception:
                logger.exception("Alert check failed for %s", lga)


async def _alert_loop():
    """Re-check every subscribed LGA on a timer, so new urgent facilities
    reach the officer's WhatsApp without anyone opening the dashboard."""
    while True:
        await _check_all_alerts()
        await asyncio.sleep(alerts.CHECK_INTERVAL_MIN * 60)


def _known_lga(lga: str) -> None:
    if lga not in STATE["world"]["lgas"]:
        raise HTTPException(404, "Unknown LGA")


@app.get("/api/alerts/{lga}")
async def alert_status(lga: str):
    _known_lga(lga)
    return {**alerts.status(lga), "urgent_now": len(_urgent_cards(lga))}


@app.post("/api/alerts/{lga}/subscribe")
async def alert_subscribe(lga: str, body: dict):
    """Save the officer's own number and send the first alert now.
    call=true also phones them (BimpeAI) whenever a facility becomes urgent."""
    _known_lga(lga)
    phone = alerts.clean_phone(str(body.get("phone") or ""))
    if not phone:
        raise HTTPException(400, "Enter a phone number, e.g. 08012345678 or 2348012345678.")
    alerts.save_contact(lga, phone, call=bool(body.get("call")))
    result = await alerts.run_check(lga, _urgent_cards(lga))
    return {**alerts.status(lga), "check": result}


@app.post("/api/facility/{facility_id}/call")
async def call_facility(facility_id: str, body: dict):
    """Phone a facility in-charge (or anyone the officer names) with a reminder
    or a fridge check. Like the WhatsApp path, only an explicit number is dialled."""
    kind = body.get("kind")
    if kind not in ("reminder", "confirmation"):
        raise HTTPException(400, "kind must be reminder or confirmation")
    r = _risk_row(facility_id)
    if kind == "reminder" and int(r["months_since_last_report"]) == 0:
        raise HTTPException(409, "This facility reported on time this month — a reminder would be factually wrong.")
    message, facts = calls.facility_message(kind, r["name"], r.to_dict())
    if not body.get("to"):
        return {"ok": False, "dry_run": True, "message": message}
    return {**await calls.place_call(str(body["to"]), message, facts), "message": message}


@app.post("/api/alerts/{lga}/unsubscribe")
async def alert_unsubscribe(lga: str):
    _known_lga(lga)
    alerts.remove_contact(lga)
    return alerts.status(lga)


@app.post("/api/alerts/{lga}/check")
async def alert_check(lga: str, body: dict | None = None):
    """Run the urgent check now (the timer also runs it). force=true re-sends everything urgent."""
    _known_lga(lga)
    result = await alerts.run_check(lga, _urgent_cards(lga), force=bool((body or {}).get("force")))
    return {**alerts.status(lga), "check": result}


@app.get("/whatsapp")
async def whatsapp_verify(request: Request):
    """Meta's one-time webhook verification (Callback URL .../whatsapp)."""
    p = request.query_params
    if whatsapp.VERIFY_TOKEN and p.get("hub.verify_token") == whatsapp.VERIFY_TOKEN:
        return PlainTextResponse(p.get("hub.challenge") or "")
    return PlainTextResponse("Verification token mismatch", status_code=403)


@app.post("/whatsapp")
async def whatsapp_incoming(request: Request):
    """Incoming messages. Any message opens the 24-hour window for that
    number; "START alerts <LGA>" also subscribes it, and "STOP" unsubscribes."""
    raw = await request.body()
    if not whatsapp.signature_ok(raw, request.headers.get("X-Hub-Signature-256")):
        return Response(status_code=403)
    try:
        value = json.loads(raw)["entry"][0]["changes"][0]["value"]
        message = value["messages"][0]
    except (ValueError, KeyError, IndexError):
        return Response(status_code=200)  # delivery receipts and other events
    sender = message.get("from", "")
    text = ((message.get("text") or {}).get("body") or "").strip()
    asyncio.create_task(_handle_incoming(sender, text))
    return Response(status_code=200)


async def _handle_incoming(sender: str, text: str):
    try:
        reply = await _incoming_reply(sender, text)
        if reply:
            await whatsapp.send_text(sender, reply)
    except Exception:
        logger.exception("Handling WhatsApp message from %s failed", sender)


async def _incoming_reply(sender: str, text: str) -> str | None:
    words = text.split()
    command = words[0].upper() if words else ""
    lgas = STATE["world"]["lgas"]
    mine = alerts.lgas_for_phone(sender)

    if command == "STOP":
        for lga in mine:
            alerts.remove_contact(lga)
        return (f"Alerts stopped for {', '.join(mine)}." if mine else "You had no alerts on.")

    if command == "START":
        asked = " ".join(w for w in words[1:] if w.lower() != "alerts")
        lga = next((l for l in lgas if l.lower() == asked.lower()), None)
        if not lga:
            return ("Reply START alerts <your LGA> to get urgent alerts. LGAs: " + ", ".join(lgas))
        if lga not in mine:
            alerts.save_contact(lga, alerts.clean_phone(sender))
        result = await alerts.run_check(lga, _urgent_cards(lga), force=True)
        return None if result["sent"] else (
            f"Alerts are on for {lga}. Nothing is urgent right now; you will get a message when a facility becomes HIGH priority.")

    # Any other message reopens the window: deliver alerts that were blocked by it.
    for lga in mine:
        if alerts.reopen(lga):
            await alerts.run_check(lga, _urgent_cards(lga), force=True)
    if mine:
        return None
    return "Hello from Idon-Lafia. Reply START alerts <your LGA> to get urgent facility alerts."


@app.post("/api/admin/retrain")
async def retrain():
    """Reload the world and models from disk after an offline retrain
    (python -m ml.train), rescore, and alert on anything newly urgent."""
    _bootstrap()
    await _check_all_alerts()
    return {"ok": True}


dashboard_dir = Path(__file__).parent.parent / "dashboard"
if dashboard_dir.exists():
    app.mount("/", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5005)
