"""
Phone calls through BimpeAI, for when WhatsApp is not enough: an officer or
in-charge with no data, or one who has not messaged the WhatsApp number in
the last 24 hours, still gets a voice call that says what is needed.

BimpeAI dials from its own number and its hosted voice agent does the
talking. It takes no per-call text at dial time, so before each call the
agent's prompt is rewritten with the message for that call, then the call is
placed. Calls are serialised by a lock so two calls never share a prompt.

The agent ("Idon-Lafia caller") is created on first use and its ids are kept
in data/bimpe_agent.json. It is separate from any other agent on the account.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("phc-triage.calls")

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

API_KEY = os.environ.get("BIMPEAI_API_KEY", "").strip().strip('"').strip("'")
BASE_URL = os.environ.get("BIMPEAI_BASE_URL", "").strip() or None
# Test calls use BimpeAI's own test line and no live minutes. Set
# IDON_CALL_LIVE=true only once a live number is assigned to the agent.
LIVE = os.environ.get("IDON_CALL_LIVE", "").strip().lower() in {"1", "true", "yes"}

AGENT_PATH = Path(__file__).parent / "data" / "bimpe_agent.json"
AGENT_NAME = "Idon-Lafia caller"
_lock = asyncio.Lock()

PROMPT = """\
You are Idon-Lafia, an automated health alert service for primary health \
centres in Nigeria. You are making a short outbound phone call. You speak first.

Start by saying exactly this message, clearly and slowly:

"{message}"

Then ask: "Did you get that?" If they ask you to repeat it, repeat the message. \
Answer simple questions using ONLY the facts below; if you do not know, say the \
details are on the Idon-Lafia dashboard. Never invent facts, numbers or names. \
Do not ask for any personal details. Keep the whole call under one minute, then \
say "Thank you, goodbye" and end the call.

Facts for this call:
{facts}
"""


def configured() -> bool:
    return bool(API_KEY)


def clean_phone(raw: str) -> str | None:
    """Accept 0813..., 234813... or +234813...; return +234813..."""
    digits = re.sub(r"[\s\-()+]", "", raw or "")
    if re.fullmatch(r"0\d{10}", digits):
        digits = "234" + digits[1:]
    return f"+{digits}" if re.fullmatch(r"\d{10,15}", digits) else None


def _client():
    import bimpeai
    return bimpeai.BimpeAI(api_key=API_KEY, base_url=BASE_URL)


def _ensure_agent(c) -> dict:
    if AGENT_PATH.exists():
        return json.loads(AGENT_PATH.read_text())
    workflow = c.workflows.create(
        name="Idon-Lafia facility alert calls",
        system_prompt=PROMPT.format(message="Hello, this is Idon-Lafia.", facts="None."),
        description="Phones LGA officers and facility in-charges with short health facility alerts.",
        category="healthcare",
    )
    agent = c.agents.create(
        workflow_id=workflow.id, name=AGENT_NAME,
        description="Short outbound alert calls for the Idon-Lafia PHC triage dashboard.",
        language="en", business_name="Idon-Lafia",
    )
    ids = {"agent_id": agent.id, "workflow_id": agent.workflow_id or workflow.id}
    AGENT_PATH.write_text(json.dumps(ids, indent=2))
    logger.info("Created BimpeAI agent %s (workflow %s)", ids["agent_id"], ids["workflow_id"])
    return ids


def _call_sync(to: str, message: str, facts: str) -> dict:
    c = _client()
    ids = _ensure_agent(c)
    c.workflows.update(ids["workflow_id"], system_prompt=PROMPT.format(message=message, facts=facts or "None."))
    result = c.calls.make(ids["agent_id"], {"destination": to, "is_test_call": not LIVE},
                          idempotency_key=f"idon-{uuid.uuid4().hex[:12]}")
    return {"ok": result.status == "initiated", "status": result.status,
            "call_id": result.call_id, "detail": result.detail}


async def place_call(to_number: str, message: str, facts: str = "") -> dict:
    """Ring one number and speak `message`. Never raises."""
    if not configured():
        return {"ok": False, "reason": "Calling is not configured (BIMPEAI_API_KEY missing)."}
    to = clean_phone(to_number)
    if not to:
        return {"ok": False, "reason": "Not a valid phone number."}
    async with _lock:
        try:
            out = await asyncio.to_thread(_call_sync, to, message, facts)
        except Exception as e:
            logger.error("BimpeAI call to ••••%s failed: %s", to[-4:], e)
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    if not out["ok"]:
        out["reason"] = out.get("detail") or f"BimpeAI returned {out['status']}"
    logger.info("Call to ••••%s: %s %s", to[-4:], out["status"], out.get("call_id"))
    return out


# --- What the calls say -----------------------------------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def officer_message(lga: str, facilities: list, total: int) -> tuple[str, str]:
    """Spoken alert for an LGA officer about urgent facilities."""
    top = facilities[:2]
    names = " and ".join(f["name"] for f in top)
    message = (f"Hello, this is Idon-Lafia, the health facility alert service, calling the officer for "
               f"{lga} local government. {total} health facilit{'y needs' if total == 1 else 'ies need'} "
               f"checking now. The most urgent {'is' if len(top) == 1 else 'are'} {names}. "
               f"Please open your Idon-Lafia list today, check them, and record what you find.")
    facts = "\n".join(
        f"- {f['name']}: about {round(f['trouble_prob'] * 100)} percent chance of serious trouble. "
        f"Why: {_clean(f['reason'])} Suggested action: {_clean(f['suggested_action'])}"
        for f in facilities[:5])
    return message, facts


def facility_message(kind: str, name: str, facility: dict) -> tuple[str, str]:
    """Spoken message for a facility in-charge."""
    if kind == "reminder":
        message = (f"Hello, this is Idon-Lafia calling for the officer in charge of {name}. "
                   f"We have not received this facility's monthly report. Please send it to your "
                   f"LGA records officer this week. Thank you.")
    else:
        message = (f"Hello, this is Idon-Lafia calling for the officer in charge of {name}. "
                   f"Please check today that your vaccine fridge is working and keeping vaccines cold, "
                   f"and tell your LGA officer if it is not.")
    facts = (f"- Facility: {name}\n- Why it was flagged: {_clean(facility.get('reason'))}\n"
             f"- Suggested action: {_clean(facility.get('suggested_action'))}")
    return message, facts
