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

import csv
import io
import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import data_loader
import scoring
import simulator
import train
import whatsapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s")
logger = logging.getLogger("phc-triage")

DATA_DIR = Path(__file__).parent / "data"
FEEDBACK_PATH = DATA_DIR / "feedback.json"
_feedback_lock = threading.Lock()

STATE = {}  # world, bundle, risk_table, validation, built lazily at startup


def _load_feedback() -> dict:
    if FEEDBACK_PATH.exists():
        return json.loads(FEEDBACK_PATH.read_text())
    return {}


def _save_feedback(data: dict) -> None:
    FEEDBACK_PATH.write_text(json.dumps(data, indent=2))


def _refresh_risk_table():
    STATE["risk_table"] = scoring.build_risk_table(STATE["world"], STATE["bundle"])
    STATE["cluster_alerts"] = scoring.cluster_alerts(STATE["world"], STATE["risk_table"])


def _bootstrap():
    model_path = DATA_DIR / "models.joblib"
    world_path = DATA_DIR / "train_world.joblib"
    validation_path = DATA_DIR / "validation.json"
    if model_path.exists() and world_path.exists() and validation_path.exists():
        logger.info("Loading cached models + world + validation from data/")
        STATE["bundle"] = joblib.load(model_path)
        STATE["world"] = joblib.load(world_path)
        STATE["validation"] = json.loads(validation_path.read_text())
    else:
        logger.info("No cached models found — training now (first boot only)")
        result = train.run_training()
        STATE["bundle"] = result["bundle"]
        STATE["world"] = result["world"]
        STATE["validation"] = result["validation"]
    _refresh_risk_table()
    STATE["lga_phones"] = data_loader.lga_coordinator_phones(STATE["world"].get("seed", "A")) \
        if data_loader.available() else {}
    if not FEEDBACK_PATH.exists():
        _save_feedback({})
    logger.info("PHC Triage ready — %d facilities, %d LGAs", len(STATE["world"]["facilities"]), len(STATE["world"]["lgas"]))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap()
    yield


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


def _risk_with_feedback() -> pd.DataFrame:
    fb = _load_feedback()
    rt = STATE["risk_table"].copy()
    rt["officer_status"] = _nullable_object_col(rt["facility_id"].map(lambda f: (fb.get(f) or {}).get("status")))
    rt["visited"] = rt["facility_id"].map(lambda f: bool((fb.get(f) or {}).get("visited")))
    rt["override_tag"] = _nullable_object_col(rt["facility_id"].map(lambda f: (fb.get(f) or {}).get("override_tag")))
    rt["tag"] = rt.apply(lambda r: r["override_tag"] if r["override_tag"] else r["tag"], axis=1)
    return rt


def _row_to_card(r: pd.Series) -> dict:
    return {
        "rank": int(r["rank"]), "facility_id": r["facility_id"], "name": r["name"], "lga": r["lga"],
        "tag": str(r["tag"]), "population": int(r["population"]), "people_served_label": f"About {int(round(r['population'], -2)):,}",
        "reason": r["reason"], "suggested_action": r["suggested_action"],
        "silence_type": r["silence_label"], "trouble_prob": round(float(r["trouble_prob"]), 3),
        "trouble_uncertainty": round(float(r["trouble_uncertainty"]), 3),
        "priority": round(float(r["priority"]), 4), "months_since_last_report": int(r["months_since_last_report"]),
        "distance_km": float(r["distance_km"]), "lat": float(r["lat"]), "lon": float(r["lon"]),
        "officer_status": r.get("officer_status"), "visited": bool(r.get("visited", False)),
        "sample_data": True,
    }


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
    top = g.sort_values("priority", ascending=False).head(capacity)
    alerts = [a for a in STATE["cluster_alerts"] if a["lga"] == lga]
    return {
        "lga": lga, "state": STATE["world"]["state"], "data_as_of": STATE["world"]["periods"][-1],
        "facilities_in_lga": int(len(g)), "flagged_this_week": int((g["tag"].isin(["HIGH", "MEDIUM"])).sum()),
        "lga_wide_alerts": len(alerts), "warning_banners": alerts, "capacity": capacity,
        "cards": [_row_to_card(r) for _, r in top.iterrows()], "sample_data": True,
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
    return {
        "facility_id": facility_id, "name": r["name"], "lga": r["lga"], "state": r["state"],
        "type": "Primary Health Centre", "lat": float(r["lat"]), "lon": float(r["lon"]),
        "population": int(r["population"]), "people_served_label": f"About {int(round(r['population'], -2)):,}",
        "tag": str(r["tag"]), "urban": bool(r["urban"]),
        "reporting_chart": chart, "expected_level": round(float(r["expected_service_total"]), 1),
        "reason": r["reason"], "reason_breakdown": r["reason_breakdown"], "suggested_action": r["suggested_action"],
        "silence_type": r["silence_label"], "trouble_prob": round(float(r["trouble_prob"]), 3),
        "trouble_uncertainty": round(float(r["trouble_uncertainty"]), 3),
        "months_since_last_report": int(r["months_since_last_report"]),
        "tiles": {
            "staff": f"{int(r['staff_count'])} of {int(r['staff_standard'])} standard",
            "cold_chain": "Needs check" if r["pred_cause"] in ("sudden_drop", "late_entry") and r["power_reliability"] < 0.5 else "No flag",
            "connectivity": f"{round(float(r['connectivity']) * 100)}%",
            "last_report": f"{int(r['months_since_last_report'])} month(s) ago" if r["months_since_last_report"] else "This month",
            "distance_km": float(r["distance_km"]),
        },
        "officer_status": fb.get("status"), "visited": bool(fb.get("visited", False)),
        "override_tag": fb.get("override_tag"), "sample_data": True,
        "contact_hint": {
            "phone_sample": None if pd.isna(r.get("in_charge_phone_sample")) else str(r.get("in_charge_phone_sample")),
            "whatsapp_opt_in": None if pd.isna(r.get("whatsapp_opt_in")) else bool(r.get("whatsapp_opt_in")),
        },
    }


@app.post("/api/facility/{facility_id}/feedback")
async def set_feedback(facility_id: str, body: dict):
    status = body.get("status")
    if status not in ("confirmed", "cleared", "just_late"):
        raise HTTPException(400, "status must be confirmed, cleared or just_late")
    with _feedback_lock:
        fb = _load_feedback()
        entry = fb.get(facility_id, {})
        entry["status"] = status
        entry["at"] = pd.Timestamp.utcnow().isoformat()
        fb[facility_id] = entry
        _save_feedback(fb)
    return {"ok": True, "facility_id": facility_id, "status": status}


@app.post("/api/facility/{facility_id}/visited")
async def mark_visited(facility_id: str, body: dict | None = None):
    with _feedback_lock:
        fb = _load_feedback()
        entry = fb.get(facility_id, {})
        entry["visited"] = True
        entry["visited_at"] = pd.Timestamp.utcnow().isoformat()
        fb[facility_id] = entry
        _save_feedback(fb)
    return {"ok": True, "facility_id": facility_id, "visited": True}


@app.post("/api/facility/{facility_id}/override")
async def override_flag(facility_id: str, body: dict):
    tag = body.get("tag")
    if tag not in ("HIGH", "MEDIUM", "LOW", None):
        raise HTTPException(400, "tag must be HIGH, MEDIUM, LOW or null")
    with _feedback_lock:
        fb = _load_feedback()
        entry = fb.get(facility_id, {})
        entry["override_tag"] = tag
        fb[facility_id] = entry
        _save_feedback(fb)
    return {"ok": True, "facility_id": facility_id, "override_tag": tag}


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
                     "silence_type", "trouble_probability", "months_since_last_report", "officer_status"])
    for _, r in rt.iterrows():
        status = r.get("officer_status")
        status = "" if pd.isna(status) else status
        writer.writerow([r["rank"], r["name"], r["lga"], r["tag"], int(r["population"]), r["reason"],
                         r["suggested_action"], r["silence_label"], round(float(r["trouble_prob"]), 3),
                         int(r["months_since_last_report"]), status])
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
    lines.append("Open the full list on the dashboard. Reply Confirmed / Cleared / Just late after each visit.")
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
    sample_hint = STATE.get("lga_phones", {}).get(lga)
    to = body.get("to")
    if not to:
        return {"lga": lga, "text": text, "sent": False, "dry_run": True, "sample_contact_hint": sample_hint,
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


@app.post("/api/admin/retrain")
async def retrain():
    """Regenerate the world and retrain all three models from scratch."""
    result = train.run_training()
    STATE["bundle"] = result["bundle"]
    STATE["world"] = result["world"]
    STATE["validation"] = result["validation"]
    _refresh_risk_table()
    return {"ok": True}


dashboard_dir = Path(__file__).parent.parent / "dashboard"
if dashboard_dir.exists():
    app.mount("/", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010)
