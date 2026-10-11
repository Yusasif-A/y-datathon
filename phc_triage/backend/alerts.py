"""
Automatic urgent alerts: tell an LGA officer on WhatsApp when a facility in
their LGA has newly become urgent, so they know to open the dashboard.

"Urgent" = tagged HIGH and still on the to-do list (not already being
checked, confirmed, snoozed or overridden by the officer).

Only numbers an officer saved themselves are ever messaged; the sample
phone numbers in the dataset are never used. Each facility is alerted once
while it stays urgent. If it drops out (checked, cleared, risk fell) and
later becomes urgent again, it is alerted again.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

import pandas as pd

import whatsapp

logger = logging.getLogger("phc-triage.alerts")

DATA_DIR = Path(__file__).parent / "data"
CONTACTS_PATH = DATA_DIR / "alert_contacts.json"  # real officer numbers: gitignored
STATE_PATH = DATA_DIR / "alert_state.json"

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://idon-lafiya.web.app").rstrip("/")
CHECK_INTERVAL_MIN = float(os.environ.get("ALERT_CHECK_INTERVAL_MIN", "60"))
MAX_LISTED = 5   # facilities named in one message; the rest are counted
LOG_KEEP = 20


def _read(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


def clean_phone(raw: str) -> str | None:
    digits = re.sub(r"[\s\-()+]", "", raw or "")
    return digits if re.fullmatch(r"\d{10,15}", digits) else None


def mask(phone: str) -> str:
    return f"••••{phone[-4:]}"


def contact(lga: str) -> dict | None:
    return _read(CONTACTS_PATH).get(lga)


def subscribed_lgas() -> list:
    return [lga for lga, c in _read(CONTACTS_PATH).items() if c.get("enabled")]


def save_contact(lga: str, phone: str) -> None:
    contacts = _read(CONTACTS_PATH)
    contacts[lga] = {"phone": phone, "enabled": True, "saved_at": pd.Timestamp.utcnow().isoformat()}
    _write(CONTACTS_PATH, contacts)
    # A new number starts fresh, so its first check lists everything urgent now.
    state = _read(STATE_PATH)
    state.setdefault(lga, {}).update(notified=[], fresh=True)
    _write(STATE_PATH, state)


def remove_contact(lga: str) -> None:
    contacts = _read(CONTACTS_PATH)
    contacts.pop(lga, None)
    _write(CONTACTS_PATH, contacts)


def lgas_for_phone(phone: str) -> list:
    phone = clean_phone(phone) or ""
    return [lga for lga, c in _read(CONTACTS_PATH).items() if c.get("phone") == phone]


def reopen(lga: str) -> bool:
    """The officer messaged us, so the 24-hour window is open. Returns True
    if an alert had been blocked and should be re-sent now."""
    state = _read(STATE_PATH)
    s = state.setdefault(lga, {})
    was_blocked = bool(s.get("needs_opt_in"))
    s["needs_opt_in"] = False
    _write(STATE_PATH, state)
    return was_blocked


def opt_in_link(lga: str) -> str | None:
    """wa.me link that opens WhatsApp with a message to the business number
    ready to send. Sending it opens the 24-hour window for free-text alerts."""
    if not whatsapp.BUSINESS_NUMBER:
        return None
    return f"https://wa.me/{whatsapp.BUSINESS_NUMBER}?text=" + quote(f"START alerts {lga}")


def status(lga: str) -> dict:
    c = contact(lga)
    s = _read(STATE_PATH).get(lga, {})
    return {
        "lga": lga, "subscribed": bool(c and c.get("enabled")),
        "phone": mask(c["phone"]) if c else None,
        "last_check": s.get("last_check"), "log": list(reversed(s.get("log", [])))[:10],
        "needs_opt_in": bool(s.get("needs_opt_in")), "opt_in_link": opt_in_link(lga),
        "business_number": whatsapp.BUSINESS_NUMBER or None, "template_configured": bool(whatsapp.ALERT_TEMPLATE),
        "check_interval_min": CHECK_INTERVAL_MIN, "whatsapp_configured": whatsapp.configured(),
    }


def _message(lga: str, new: list, total_urgent: int, first: bool) -> str:
    head = (f"*Idon-Lafia alerts are on for {lga}* (sample data)\n"
            f"{total_urgent} facilit{'y needs' if total_urgent == 1 else 'ies need'} checking now:"
            if first else
            f"*Urgent: {len(new)} facilit{'y' if len(new) == 1 else 'ies'} in {lga} just became HIGH priority* (sample data)")
    lines = [head]
    for c in new[:MAX_LISTED]:
        lines.append(f"\n• {c['name']}: {round(c['trouble_prob'] * 100)}% chance of trouble. {c['reason']}\n"
                     f"  Suggested: {c['suggested_action']}\n"
                     f"  {DASHBOARD_URL}/facility.html?id={c['facility_id']}")
    if len(new) > MAX_LISTED:
        lines.append(f"\n…and {len(new) - MAX_LISTED} more.")
    lines.append(f"\nOpen your list: {DASHBOARD_URL}/?lga={lga.replace(' ', '%20')}")
    return "\n".join(lines)


async def run_check(lga: str, urgent: list, force: bool = False) -> dict:
    """Compare the LGA's urgent facilities with those already alerted and
    message the officer about the new ones. `urgent` is a list of officer
    cards sorted by priority. `force` re-sends everything urgent now."""
    c = contact(lga)
    if not (c and c.get("enabled")):
        return {"lga": lga, "sent": False, "reason": "No alert number saved for this LGA."}

    state = _read(STATE_PATH)
    s = state.setdefault(lga, {})
    notified = set(s.get("notified", []))
    first = bool(s.get("fresh", True))
    new = urgent if force or first else [u for u in urgent if u["facility_id"] not in notified]
    now = pd.Timestamp.utcnow().isoformat()
    s["last_check"] = now

    result = {"lga": lga, "urgent_now": len(urgent), "new": len(new), "sent": False}
    if new:
        text = _message(lga, new, len(urgent), first or force)
        sent = await whatsapp.send_text(c["phone"], text)
        via = "text"
        if sent.get("window_closed") and whatsapp.ALERT_TEMPLATE:
            sent = await whatsapp.send_template(c["phone"], [lga, len(new), f"{DASHBOARD_URL}/?lga={lga.replace(' ', '%20')}"])
            via = "template"
        # Still blocked by the 24-hour rule: the officer has to message the business number first.
        s["needs_opt_in"] = bool(sent.get("window_closed"))
        result.update(sent=sent["ok"], reason=sent.get("reason"), text=text, via=via,
                      needs_opt_in=s["needs_opt_in"])
        s.setdefault("log", []).append({"at": now, "facilities": [u["facility_id"] for u in new[:MAX_LISTED]],
                                        "count": len(new), "ok": sent["ok"], "via": via,
                                        "window_closed": s["needs_opt_in"], "reason": sent.get("reason")})
        s["log"] = s["log"][-LOG_KEEP:]
        if sent["ok"]:
            # Only facilities that are still urgent stay "notified"; the rest can re-alert later.
            s["notified"] = [u["facility_id"] for u in urgent]
            s["fresh"] = False
    else:
        s["fresh"] = False
        s["notified"] = sorted(notified & {u["facility_id"] for u in urgent})
        result["reason"] = "Nothing newly urgent since the last alert."
    _write(STATE_PATH, state)
    logger.info("Alert check %s: %d urgent, %d new, sent=%s", lga, len(urgent), len(new), result["sent"])
    return result
