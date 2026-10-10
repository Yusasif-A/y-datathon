"""
Thin WhatsApp sender for PHC Triage, reusing the SAME WhatsApp Business
account already configured for this repo's other project (the root
`.env` — WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_GRAPH_VERSION).

This is deliberately standalone rather than importing the sibling app.py:
that module pulls in a lot of unrelated machinery (DHIS2-unrelated intake,
vision, translation services) and calls SystemExit if some of ITS env vars
are missing. PHC Triage only needs one thing from the WhatsApp Business API
— send a plain text message — so it talks to the Graph API directly.

Per the solution doc, WhatsApp here is a delivery channel only: it carries
the weekly brief (and, as mock-ups, facility reminder/confirmation text) and
a link back to OUR dashboard. It never talks to DHIS2, and a human (the
officer) approves any message to a facility before it is sent.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv

logger = logging.getLogger("phc-triage.whatsapp")

# The credentials live in the sibling ComNote project's .env at the repo root.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
GRAPH_VERSION = os.environ.get("WHATSAPP_GRAPH_VERSION", "v20.0")
GRAPH_API = f"https://graph.facebook.com/{GRAPH_VERSION}"
# The business number people message to open the 24-hour window ("Click to chat" link).
BUSINESS_NUMBER = re.sub(r"\D", "", os.environ.get("PUBLIC_WHATSAPP_NUMBER") or "")
# Optional Meta-approved template, which (unlike free text) can be sent outside the
# 24-hour window. Body must take 3 params: {{1}} LGA, {{2}} number urgent, {{3}} link.
ALERT_TEMPLATE = os.environ.get("WHATSAPP_ALERT_TEMPLATE")
ALERT_TEMPLATE_LANG = os.environ.get("WHATSAPP_ALERT_TEMPLATE_LANG", "en")
# Meta error codes meaning "this person hasn't messaged us in 24 hours", so free text is refused.
WINDOW_CLOSED_CODES = {131047, 131026, 470}


def configured() -> bool:
    return bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID)


def _headers() -> dict:
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}


async def send_text(to_number: str, text: str) -> dict:
    """Send one plain-text WhatsApp message. Never raises — callers (an
    officer clicking "Send") get a clear ok/false plus the reason, instead of
    a 500, if the WhatsApp account isn't configured or Meta rejects it."""
    return await _post(to_number, {"type": "text", "text": {"body": text}})


async def send_template(to_number: str, params: list) -> dict:
    """Send the approved alert template (works outside the 24-hour window)."""
    if not ALERT_TEMPLATE:
        return {"ok": False, "reason": "No WhatsApp alert template configured."}
    return await _post(to_number, {"type": "template", "template": {
        "name": ALERT_TEMPLATE, "language": {"code": ALERT_TEMPLATE_LANG},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]}]}})


async def _post(to_number: str, payload: dict) -> dict:
    if not configured():
        return {"ok": False, "reason": "WhatsApp is not configured (WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID missing)."}

    to_number = to_number.lstrip("+").replace(" ", "")
    body = {"messaging_product": "whatsapp", "to": to_number, **payload}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{GRAPH_API}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
                                         headers=_headers(), json=body)
        if response.status_code != 200:
            logger.error("WhatsApp send to %s failed: %s %s", to_number, response.status_code, response.text)
            try:
                code = (response.json().get("error") or {}).get("code")
            except ValueError:
                code = None
            if code in WINDOW_CLOSED_CODES:
                return {"ok": False, "window_closed": True, "code": code,
                        "reason": "This number has not messaged the WhatsApp business number in the last 24 hours."}
            return {"ok": False, "reason": f"WhatsApp API returned {response.status_code}", "code": code,
                    "detail": response.text}
        data = response.json()
        return {"ok": True, "message_id": (data.get("messages") or [{}])[0].get("id")}
    except Exception as e:
        logger.error("WhatsApp send to %s errored: %s", to_number, e)
        return {"ok": False, "reason": str(e)}
