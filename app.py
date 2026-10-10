"""
ComNote — WhatsApp intake for civic complaints.

A citizen sends a voice note (English or Yorùbá), a photo and their location or
NIPOST digital postcode. Gemma reads the photo and the transcript, extracts
what / where / when / category / urgency, asks one clarifying question if
something essential is missing, and files a ticket with a reference such as
CN-2026-0147. Officers work the ticket on the dashboard (dashboard.py); every
status change is pushed back to the citizen here.
"""

import asyncio
import glob
import logging
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import timedelta
from io import BytesIO
from typing import List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

load_dotenv()

import dashboard
import intake
import postcode
import store
import tickets
import vision
from anonymiser import reporter_pseudonym, strip_image_metadata
from config import (
    CATEGORIES,
    DASHBOARD_ALLOWED_ORIGINS,
    LANGUAGES,
    PUBLIC_BASE_URL,
    TICKET_PREFIX,
    language_by_button_id,
    language_button_ids,
)
from hmac_validator import validate_whatsapp_hmac
from llm import ask_text
from messages import category_label, status_label, t
from nllb_translator import numbers_to_words
from services import get_stt_service, get_tts_service, get_voice

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN")
GRAPH_API = "https://graph.facebook.com/v20.0"

if not all([WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN]):
    logger.error("Missing one or more required WhatsApp environment variables.")
    raise SystemExit(1)


# ── ffmpeg / audio joining ───────────────────────────────────────────────────

_ffmpeg_cache = None


def _find_ffmpeg():
    """Locate ffmpeg once, including a winget install on a Windows dev machine."""
    global _ffmpeg_cache
    if _ffmpeg_cache is not None:
        return _ffmpeg_cache

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        _ffmpeg_cache = (ffmpeg, shutil.which("ffprobe"))
        return _ffmpeg_cache

    appdata = os.environ.get("LOCALAPPDATA", "")
    if appdata:
        pattern = os.path.join(appdata, "Microsoft", "WinGet", "Packages", "**", "ffmpeg.exe")
        for found in glob.glob(pattern, recursive=True):
            probe = found.replace("ffmpeg.exe", "ffprobe.exe")
            os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + os.path.dirname(found)
            logger.info(f"Found ffmpeg at: {found}")
            _ffmpeg_cache = (found, probe if os.path.exists(probe) else None)
            return _ffmpeg_cache

    logger.warning("ffmpeg not found — MP3 joining will fall back to a raw concatenation")
    _ffmpeg_cache = (None, None)
    return _ffmpeg_cache


def _concat_mp3(chunks: list) -> bytes:
    """Join per-sentence MP3s into one playable file."""
    chunks = [c for c in chunks if c]
    if not chunks:
        return b""
    if len(chunks) == 1:
        return chunks[0]

    try:
        from pydub import AudioSegment

        ffmpeg_path, ffprobe_path = _find_ffmpeg()
        if ffmpeg_path:
            AudioSegment.converter = ffmpeg_path
            if ffprobe_path:
                AudioSegment.ffprobe = ffprobe_path

        combined = AudioSegment.empty()
        for chunk in chunks:
            combined += AudioSegment.from_mp3(BytesIO(chunk))
        out = BytesIO()
        combined.export(out, format="mp3")
        return out.getvalue()
    except Exception as e:
        # A raw join usually still plays; players tolerate concatenated frames.
        logger.warning(f"⚠️ pydub join failed ({e}) — using a raw concatenation")
        return b"".join(chunks)


# ── WhatsApp transport ───────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }


async def download_media(media_id: str) -> bytes:
    """Fetch media bytes — a two-step lookup then download, as Meta requires."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        meta = await client.get(
            f"{GRAPH_API}/{media_id}",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
        )
        meta.raise_for_status()
        url = meta.json().get("url")
        if not url:
            raise HTTPException(status_code=404, detail="Media URL not found")

        media = await client.get(url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
        media.raise_for_status()
        return media.content


async def upload_media(content: bytes, mime_type: str) -> Optional[str]:
    """Upload outbound media and return its id, or None on failure."""
    suffix = ".mp3" if "audio" in mime_type else ".jpg"
    path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(content)
            path = handle.name

        with open(path, "rb") as handle:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{GRAPH_API}/{WHATSAPP_PHONE_NUMBER_ID}/media",
                    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                    files={"file": (os.path.basename(path), handle, mime_type)},
                    data={"messaging_product": "whatsapp"},
                )
        response.raise_for_status()
        return response.json().get("id")
    except Exception as e:
        logger.error(f"❌ Media upload failed: {e}")
        return None
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


def clean_for_whatsapp(text: str) -> str:
    """Convert model markdown into what WhatsApp actually renders."""
    if not text:
        return text
    text = re.sub(r"^#+\s+(.*)$", r"*\1*", text, flags=re.MULTILINE)
    text = text.replace("**", "*")
    text = re.sub(r"^[*-]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"^---+$", "────────────────", text, flags=re.MULTILINE)
    return text.strip()


async def send_message(to_number: str, text: str = None,
                       media_id: str = None, media_type: str = "text") -> bool:
    """Send one WhatsApp message. Returns False rather than raising.

    Callers are usually mid-conversation with somebody in trouble; a failed send
    should be logged and worked around, not allowed to abort the turn.
    """
    if media_type == "audio" and media_id:
        payload = {"type": "audio", "audio": {"id": media_id}}
    elif media_type == "image" and media_id:
        payload = {"type": "image", "image": {"id": media_id, "caption": text or ""}}
    else:
        payload = {"type": "text", "text": {"body": text or ""}}

    body = {"messaging_product": "whatsapp", "to": to_number, **payload}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{GRAPH_API}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
                headers=_headers(),
                json=body,
            )
        if response.status_code != 200:
            # Error 131047 (outside the 24-hour window, a template is required)
            # is the common one and is invisible without the response body.
            logger.error(f"❌ Send to {to_number} failed: {response.status_code} {response.text}")
            return False
        return True
    except Exception as e:
        logger.error(f"❌ Send to {to_number} errored: {e}")
        return False


async def send_buttons(to_number: str, body: str, buttons: List[dict],
                       header: str = None) -> bool:
    """Send up to three reply buttons.

    WhatsApp caps an interactive body at 1024 characters and rejects anything
    longer outright, so a long answer is sent as plain text first and the buttons
    follow with a short prompt — the person still gets the whole answer.
    """
    if len(body) > 1024:
        await send_message(to_number, text=body)
        body = "What would you like to do?"

    action_buttons = [
        {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
        for b in buttons[:3]
    ]

    interactive = {
        "type": "button",
        "body": {"text": body},
        "action": {"buttons": action_buttons},
    }
    if header and header.strip():
        interactive["header"] = {"type": "text", "text": header[:60]}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{GRAPH_API}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
                headers=_headers(),
                json={
                    "messaging_product": "whatsapp",
                    "to": to_number,
                    "type": "interactive",
                    "interactive": interactive,
                },
            )
        if response.status_code != 200:
            logger.error(f"❌ Buttons to {to_number} failed: {response.status_code} {response.text}")
            return False
        return True
    except Exception as e:
        logger.error(f"❌ Buttons to {to_number} errored: {e}")
        return False


async def mark_read_and_typing(message_id: str) -> bool:
    """Show the person their message arrived and something is happening."""
    if not message_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{GRAPH_API}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
                headers=_headers(),
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                    "typing_indicator": {"type": "text"},
                },
            )
        return response.status_code == 200
    except Exception as e:
        logger.warning(f"Typing indicator failed: {e}")
        return False


async def send_list(to_number: str, body: str, button: str, rows: List[dict],
                    section_title: str = "") -> bool:
    """A single-section list message (up to ten rows) — the category picker
    needs eight options, well past the three-button limit."""
    interactive = {
        "type": "list",
        "body": {"text": body[:1024]},
        "action": {
            "button": button[:20],
            "sections": [{
                "title": (section_title or button)[:24],
                "rows": [
                    {"id": r["id"][:200], "title": r["title"][:24],
                     **({"description": r["description"][:72]} if r.get("description") else {})}
                    for r in rows[:10]
                ],
            }],
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{GRAPH_API}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
                headers=_headers(),
                json={"messaging_product": "whatsapp", "to": to_number,
                      "type": "interactive", "interactive": interactive},
            )
        if response.status_code != 200:
            logger.error(f"❌ List to {to_number} failed: {response.status_code} {response.text}")
            return False
        return True
    except Exception as e:
        logger.error(f"❌ List to {to_number} errored: {e}")
        return False


def language_buttons(current: str) -> List[dict]:
    """Buttons for the OTHER languages only — offering the language someone is
    already in is a wasted button."""
    return [
        {"id": lang.button_id, "title": lang.label[:20]}
        for key, lang in LANGUAGES.items()
        if key != current
    ][:3]


def menu_buttons(language: str) -> List[dict]:
    """Report · My complaints · the other language.

    WhatsApp allows three buttons; switching language matters more than a
    "check status" button, because status is checked by just sending the
    reference (the menu text says so).
    """
    return [
        {"id": "menu_report", "title": t("btn_report", language)},
        {"id": "menu_mine", "title": t("btn_mine", language)},
    ] + language_buttons(language)[:1]


BUTTON_AGREE = "agree_terms"

# Message types worth keeping while someone reads the terms, so their first
# report is answered after they tap "I agree" instead of being dropped.
_REPLAYABLE = {"text", "audio", "image", "location"}


async def send_terms(to_number: str, language: str) -> None:
    await send_buttons(to_number, t("terms", language),
                       [{"id": BUTTON_AGREE, "title": t("btn_agree", language)}] + language_buttons(language))


# ── Speech ───────────────────────────────────────────────────────────────────

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


class NoSpeechRecogniser(Exception):
    """This language has no speech-to-text service configured."""

    def __init__(self, language: str):
        self.language = language
        super().__init__(f"No STT service for {language}")


async def transcribe(message: dict, language: str, audio_bytes: bytes = None) -> str:
    service = get_stt_service(language)
    if not service:
        # Raised before the download so we do not pull media we cannot use.
        raise NoSpeechRecogniser(language)
    if audio_bytes is None:
        audio_id = message["audio"]["id"]
        audio_bytes = await download_media(audio_id)
    text = await service.transcribe(audio_bytes)
    logger.info(f"🎤 Transcribed ({language}): {text[:120]}")
    return text


async def synthesise(text: str, language: str) -> Optional[bytes]:
    """Speak a reply. Returns None when the language has no voice configured.

    Sentence-by-sentence but SEQUENTIAL (not parallel) to avoid hitting ElevenLabs
    concurrent request limits (max 2 concurrent). One failed sentence loses only
    a few words instead of the entire answer.
    """
    service = get_tts_service(language)
    if not service:
        logger.info(f"🔇 No voice configured for {language} — replying in text only")
        return None

    sentences = [s.strip() for s in _SENTENCE_RE.split(text) if len(s.strip()) > 3] or [text]
    voice = get_voice(language)

    parts = []
    for sentence in sentences:
        try:
            audio = await asyncio.to_thread(
                service.synthesize_sync, numbers_to_words(sentence, language), voice
            )
            parts.append(audio)
        except Exception as e:
            logger.warning(f"⚠️ TTS failed for one sentence: {e}")

    if not parts:
        return None
    return await asyncio.to_thread(_concat_mp3, parts)


# ── Helpers ──────────────────────────────────────────────────────────────────

_REF_RE = re.compile(rf"\b{TICKET_PREFIX}[\s\-.]*(\d{{4}})[\s\-.,]*(\d{{3,6}})\b", re.IGNORECASE)

MENU_WORDS = {"menu", "hi", "hello", "hey", "start", "home", "help", "bawo", "pele", "ẹ n lẹ", "e n le"}
LANGUAGE_WORDS = {"language", "lang", "èdè", "ede"}
CANCEL_WORDS = {"cancel", "stop", "fagile", "fagilé"}

# A photo or pin sent this soon after filing belongs to the complaint just filed.
FOLLOW_UP_WINDOW = timedelta(hours=2)


def find_ref(text: str) -> Optional[str]:
    match = _REF_RE.search(text or "")
    if not match:
        return None
    return f"{TICKET_PREFIX}-{match.group(1)}-{int(match.group(2)):04d}".upper()


def VOICES(ticket: dict) -> int:
    """People behind an issue: the reporter, everyone who joined from WhatsApp,
    and every "Me too" on the public page. (Joining adds to `supports` too.)"""
    return 1 + int(ticket.get("supports") or 0)


def track_link(ref: str) -> str:
    return f"{PUBLIC_BASE_URL}/issue/{ref}" if PUBLIC_BASE_URL else ""


def location_summary(ticket: dict) -> str:
    parts = [ticket.get("address") or ticket.get("location_text") or "", ticket.get("landmark") or ""]
    said = " ".join(parts).lower()
    # Do not repeat an LGA or state the citizen already named in the address.
    area = ", ".join(p for p in (ticket.get("lga"), ticket.get("state")) if p and p.lower() not in said)
    if area:
        parts.append(area)
    if ticket.get("postcode"):
        parts.append(postcode.format_postcode(ticket["postcode"]))
    text = " · ".join(p for p in parts if p)
    if not text and (ticket.get("location") or {}).get("lat") is not None:
        text = "📍 pin shared"
    return text or "—"


async def speak(to_number: str, text: str, language: str) -> bool:
    """Send `text` as a voice note. True if it was delivered."""
    try:
        audio = await synthesise(text, language)
        if audio:
            media_id = await upload_media(audio, "audio/mpeg")
            if media_id:
                await asyncio.sleep(0.5)  # let WhatsApp finish processing it
                return await send_message(to_number, media_id=media_id, media_type="audio")
    except Exception as e:
        logger.error(f"❌ Voice reply failed, sending text instead: {e}")
    return False


async def reply(to_number: str, text: str, language: str, as_voice: bool,
                buttons: Optional[List[dict]] = None, keep_text: bool = False) -> None:
    """Strictly voice when they spoke to us: the written copy goes only if the
    voice note fails, or if it carries something they must keep (`keep_text`,
    e.g. a reference number) or buttons to tap."""
    if as_voice and await speak(to_number, text, language) and not (buttons or keep_text):
        return
    if buttons:
        await send_buttons(to_number, text, buttons)
    else:
        await send_message(to_number, text)


async def send_menu(to_number: str, language: str, body: str = None) -> None:
    await send_buttons(to_number, body or t("menu", language), menu_buttons(language))


async def send_category_list(to_number: str, language: str, suggested: Optional[str] = None,
                             as_voice: bool = False) -> None:
    ordered = sorted(CATEGORIES, key=lambda c: c.key != suggested)
    rows = [{
        "id": f"cat_{c.key}",
        "title": category_label(c.key, language),
        "description": (f"{t('suggested', language)} · " if c.key == suggested else "") + c.hint,
    } for c in ordered]
    body = (t("pick_category_suggested", language, label=category_label(suggested, language))
            if suggested else t("pick_category", language))
    if as_voice:
        await speak(to_number, body, language)
    await send_list(to_number, body, t("list_button", language), rows)


# ── Report pipeline ──────────────────────────────────────────────────────────

def merge_extraction(draft: dict, ex: dict) -> dict:
    """Fold one Gemma reading into the draft without losing what is known."""
    patch = {}
    for src, dst in (("title", "title"), ("summary", "summary"), ("when", "when_text"),
                     ("location_text", "location_text"), ("landmark", "landmark"),
                     ("lga", "lga"), ("state", "state"), ("urgency_reason", "urgency_reason")):
        if ex.get(src):
            patch[dst] = ex[src]

    if ex.get("category"):
        patch["ai_category"] = ex["category"]
        if draft.get("category_source") != "citizen":
            patch["category"] = ex["category"]
            patch["category_source"] = "ai"

    urgency = intake.worst_urgency(draft.get("urgency"), ex.get("urgency"))
    if urgency:
        patch["urgency"] = urgency
        patch["urgency_score"] = max(int(draft.get("urgency_score") or 0),
                                     int(ex.get("urgency_score") or intake.score_for(urgency)))
    return patch


async def apply_postcode(patch: dict, code: str, ticket: dict) -> None:
    """Resolve a NIPOST postcode to its exact address and fold it into `patch`.

    The postcode is authoritative for LGA and state — it was assigned to that
    building — so it overrides whatever was inferred from the voice note.
    """
    patch["postcode"] = code
    admin = await postcode.admin_from_code(code)
    if admin:
        patch["lga"], patch["state"] = admin["lga"], admin["state"]
    details = await postcode.lookup(code)
    if not details:
        return
    if details.get("address"):
        patch["address"] = details["address"]
    if details.get("lga"):
        patch["lga"] = details["lga"]
    if details.get("state"):
        patch["state"] = details["state"]
    if details.get("locality") and not ticket.get("location_text"):
        patch["location_text"] = details["locality"]
    if details.get("lat") is not None and (ticket.get("location") or {}).get("lat") is None:
        patch["location"] = {"lat": details["lat"], "lon": details["lon"]}
        patch["location_source"] = "postcode"


async def advance(to_number: str, user_id: str, ticket_id: str, language: str,
                  as_voice: bool, follow_up: str = "") -> None:
    """Ask for the next missing piece, or file the complaint once it is complete.

    Order: what is wrong → a photo, if they have one (and where, if not yet
    said) → where → file. The category is decided by the AI from what they
    said and showed; the citizen is told it, not asked to pick it.
    """
    draft = tickets.get(ticket_id)
    if not draft:
        await send_message(to_number, t("error", language))
        return

    has_what = bool(draft.get("summary") or draft.get("title") or (draft.get("vision") or {}).get("description"))
    pinned = (draft.get("location") or {}).get("lat") is not None
    described = bool(draft.get("location_text") or draft.get("landmark") or draft.get("address"))
    # Where it is, as the citizen gave it: a pin, a postcode, or an address
    # with its LGA. Nothing is inferred.
    has_where = bool(pinned or draft.get("postcode") or (described and draft.get("lga")))
    needs_lga_only = bool(described and not draft.get("lga") and not pinned and not draft.get("postcode"))

    if not has_what:
        if follow_up and int(draft.get("clarifications") or 0) < 2:
            tickets.update(ticket_id, {"clarifications": int(draft.get("clarifications") or 0) + 1})
            await reply(to_number, follow_up, language, as_voice)
        else:
            await reply(to_number, t("describe", language), language, as_voice)
        store.set_stage(user_id, "awaiting_details")
        return

    if not draft.get("category"):
        tickets.update(ticket_id, {"category": draft.get("ai_category") or "other", "category_source": "ai"})

    # Ask once for a photo — the strongest evidence — and, in the same breath,
    # for the location if it is still missing. "No photo" moves straight on.
    if not draft.get("photos") and not draft.get("photo_asked"):
        tickets.update(ticket_id, {"photo_asked": True})
        where = "" if has_where else t("ask_photo_where", language)
        await reply(to_number, t("ask_photo", language, where=where), language, as_voice,
                    buttons=[{"id": "no_photo", "title": t("btn_no_photo", language)}])
        store.set_stage(user_id, "awaiting_photo")
        return

    if not has_where:
        if needs_lga_only and not draft.get("lga_asked"):
            tickets.update(ticket_id, {"lga_asked": True})
            await reply(to_number, t("ask_lga", language), language, as_voice)
            store.set_stage(user_id, "awaiting_location")
            return
        if not needs_lga_only:
            await reply(to_number, t("ask_location", language), language, as_voice)
            store.set_stage(user_id, "awaiting_location")
            return
        # Asked for the LGA once and still none: file with the address they gave.

    if not draft.get("urgency"):
        tickets.update(ticket_id, {"urgency": "medium", "urgency_score": 50})

    # Same problem already on ComNote? Ask once — a wrong merge would hide a
    # real, separate problem, so the citizen decides.
    if not draft.get("match_checked"):
        tickets.update(ticket_id, {"match_checked": True})
        match = tickets.find_similar_open(draft)
        if match:
            link = track_link(match["ref"])
            if tickets.is_involved(match["ref"], user_id) or match.get("reporter_ref") == draft.get("reporter_ref"):
                tickets.discard_draft(ticket_id)
                store.set_stage(user_id, None)
                await reply(to_number, t("already_yours", language, ref=match["ref"],
                                         link=t("track_link", language, url=link) if link else ""),
                            language, as_voice, keep_text=True)
                return
            tickets.update(ticket_id, {"match_ref": match["ref"]})
            question = t("match_found", language, ref=match["ref"],
                         title=match.get("title") or category_label(match.get("category"), language),
                         place=location_summary(match), voices=VOICES(match))
            if as_voice:
                await speak(to_number, question, language)
            await send_buttons(to_number, question, [
                {"id": f"match_yes:{match['ref']}", "title": t("btn_same", language)},
                {"id": "match_no", "title": t("btn_different", language)},
            ])
            store.set_stage(user_id, "awaiting_match")
            return

    filed = tickets.submit(ticket_id, user_id) or draft
    ref = filed.get("ref") or "—"
    link = track_link(ref)
    confirmation = t(
        "filed", language,
        ref=ref,
        title=filed.get("title") or category_label(filed.get("category"), language),
        category=category_label(filed.get("category"), language),
        location=location_summary(filed),
        link=t("track_link", language, url=link) if link else "",
    )
    await reply(to_number, confirmation, language, as_voice, keep_text=True)
    if filed.get("urgency") == "critical":
        await send_message(to_number, t("danger", language))
    if not filed.get("photos") and not filed.get("photo_asked"):
        await send_message(to_number, t("photo_hint", language))

    store.set_fields(user_id, {"stage": "filed", "last_ref": ref})
    store.store_conversation(user_id=user_id, question=filed.get("transcript", ""),
                             response=confirmation, message_type="report", report_id=ref)


async def handle_complaint_input(to_number: str, user_id: str, language: str, text: str,
                                 as_voice: bool, stage: Optional[str],
                                 audio: Optional[tuple] = None) -> None:
    """A voice note or typed message: small talk, or part of a complaint."""
    draft = tickets.find_draft(user_id)
    ex = await intake.extract(text, draft, language=language)

    if not draft and ex["intent"] != "complaint" and stage not in ("awaiting_details", "awaiting_category"):
        answer = await intake.chat_reply(text, language) or t("menu", language)
        answer = clean_for_whatsapp(answer)
        await reply(to_number, answer, language, as_voice, buttons=menu_buttons(language))
        store.store_conversation(user_id=user_id, question=text, response=answer,
                                 message_type="voice" if as_voice else "text")
        return

    draft = draft or tickets.open_draft(user_id, language)
    patch = merge_extraction(draft, ex)
    patch["transcript"] = f"{draft.get('transcript') or ''}\n{text}".strip()[-6000:]
    patch["language"] = language

    code = ex.get("postcode") or postcode.find_postcode(text)
    if code and code != draft.get("postcode"):
        await apply_postcode(patch, code, draft)

    tickets.update(draft["ticket_id"], patch)
    if audio:
        # Only the words are kept — never the recording — so a voice cannot
        # identify the reporter on the dashboard.
        tickets.add_audio(draft["ticket_id"], None, audio[1], text, "")

    await advance(to_number, user_id, draft["ticket_id"], language, as_voice, ex.get("follow_up_question", ""))


async def handle_image(message: dict, to_number: str, user_id: str, language: str) -> None:
    """Download, strip metadata, analyse with Gemma vision, attach to the complaint.

    Metadata stripping happens before analysis and before storage, so the bytes
    carrying GPS coordinates exist only inside this function.
    """
    caption = (message.get("image", {}) or {}).get("caption", "") or ""
    # A photo sent on its own usually means typing is not an option, so the
    # answer is spoken. A photo with a caption is answered in text.
    as_voice = not caption.strip()
    media_id = message["image"]["id"]

    image_bytes = None
    for attempt in range(1, 4):
        try:
            image_bytes = await download_media(media_id)
            if image_bytes:
                break
        except Exception as e:
            logger.warning(f"Image download attempt {attempt} failed: {e}")
            if attempt < 3:
                await asyncio.sleep(1.5 * attempt)
    if not image_bytes:
        await send_message(to_number, t("photo_failed", language))
        return

    clean_bytes, strip_report = strip_image_metadata(image_bytes)
    # Pixels we could not clean are analysed but never stored.
    storable = clean_bytes if strip_report.get("stripped") else None
    if storable is None:
        logger.error(f"⚠️ Image metadata could not be stripped ({strip_report.get('error')}) — not storing it")

    analysis = await vision.analyse_image(clean_bytes, caption)
    draft = tickets.find_draft(user_id)

    # A photo shortly after filing is evidence for the complaint just filed.
    if not draft and not caption.strip():
        recent = tickets.latest_filed_for(user_id, FOLLOW_UP_WINDOW)
        if recent:
            if storable:
                tickets.add_photo(recent["ticket_id"], storable, analysis)
            if analysis.get("ok") and analysis.get("is_relevant"):
                worse = intake.worst_urgency(recent.get("urgency"), analysis.get("severity"))
                if worse != recent.get("urgency"):
                    tickets.update(recent["ticket_id"], {"urgency": worse, "urgency_score": intake.score_for(worse)})
                if not recent.get("vision"):
                    tickets.update(recent["ticket_id"], {"vision": analysis})
            await reply(to_number, t("photo_added", language, ref=recent["ref"]), language, as_voice)
            return

    # The photo is evidence whatever the model made of it — it is always kept
    # on the report (officers can hide it from the public page).
    draft = draft or tickets.open_draft(user_id, language)
    if storable:
        tickets.add_photo(draft["ticket_id"], storable, analysis)
    understood = bool(analysis.get("ok") and analysis.get("is_relevant"))

    # Nothing to go on yet: thank them for the photo and ask what it shows.
    if not understood and not caption.strip() and not (draft.get("summary") or draft.get("title")):
        await reply(to_number, t("photo_ask", language), language, as_voice)
        store.set_stage(user_id, "awaiting_details")
        return

    patch = {}
    reading = ""
    if analysis.get("ok") and analysis.get("is_relevant"):
        patch["vision"] = analysis
        reading = ". ".join(p for p in (analysis.get("title"), analysis.get("description")) if p)
        if analysis.get("hazards"):
            reading += f" Hazards: {', '.join(analysis['hazards'])}."

    if caption or reading:
        ex = await intake.extract(caption, draft, photo_reading=reading, language=language)
    else:
        ex = intake.empty_extraction()
    patch.update(merge_extraction(draft, ex))

    if not patch.get("category") and not draft.get("category") and analysis.get("ok") and analysis.get("category"):
        patch.update({"category": analysis["category"], "ai_category": analysis["category"], "category_source": "ai"})
    if not patch.get("title") and not draft.get("title") and analysis.get("title"):
        patch["title"] = analysis["title"]
    worse = intake.worst_urgency(patch.get("urgency") or draft.get("urgency"), analysis.get("severity"))
    if worse:
        patch["urgency"] = worse
        patch["urgency_score"] = max(int(patch.get("urgency_score") or draft.get("urgency_score") or 0),
                                     intake.score_for(worse))
    if caption:
        patch["transcript"] = f"{draft.get('transcript') or ''}\n{caption}".strip()[-6000:]
        code = ex.get("postcode") or postcode.find_postcode(caption)
        if code:
            await apply_postcode(patch, code, draft)

    tickets.update(draft["ticket_id"], patch)
    if understood:
        what = (analysis.get("title") or analysis.get("description") or "").rstrip(".")[:80].lower()
        updated = tickets.get(draft["ticket_id"]) or draft
        knows_where = bool(
            (updated.get("location") or {}).get("lat") is not None or updated.get("postcode")
            or ((updated.get("location_text") or updated.get("landmark") or updated.get("address")) and updated.get("lga"))
        )
        if not knows_where:
            # One message: say what we see, then ask where — the next thing we
            # need, and the reason we need it.
            await reply(to_number, t("photo_seen_where", language, what=what or "a problem"), language, as_voice)
            store.set_stage(user_id, "awaiting_location")
            return
    await advance(to_number, user_id, draft["ticket_id"], language, as_voice, ex.get("follow_up_question", ""))


async def handle_location(message: dict, to_number: str, user_id: str, language: str) -> None:
    """A shared pin: reverse-geocode it to the building's NIPOST postcode and address."""
    location = message.get("location", {})
    lat, lon = location.get("latitude"), location.get("longitude")
    if lat is None or lon is None:
        return
    place = location.get("name") or location.get("address") or ""
    logger.info(f"[bg] 📍 Location from {to_number}: {lat:.4f},{lon:.4f} {place}")

    target = tickets.find_draft(user_id)
    filed_target = None
    if not target:
        recent = tickets.latest_filed_for(user_id, FOLLOW_UP_WINDOW)
        if recent and (recent.get("location") or {}).get("lat") is None:
            target = filed_target = recent
    target = target or tickets.open_draft(user_id, language)

    patch = {"location": {"lat": float(lat), "lon": float(lon)}, "location_source": "pin"}
    if place and not target.get("location_text"):
        patch["location_text"] = place
    details = await postcode.reverse(float(lat), float(lon))
    if details:
        # A NIPOST postcode is authoritative for the building; an OpenStreetMap
        # description only fills in what the citizen has not already told us.
        authoritative = bool(details.get("postcode"))
        if authoritative:
            patch["postcode"] = details["postcode"]
        for key in ("address", "lga", "state"):
            if details.get(key) and (authoritative or not target.get(key)):
                patch[key] = details[key]
    tickets.update(target["ticket_id"], patch)

    shown = (details or {}).get("address") or place
    if details and details.get("postcode"):
        shown = f"{shown} ({postcode.format_postcode(details['postcode'])})".strip()
    await send_message(to_number, t("location_received", language, place=f" — {shown}" if shown else ""))

    if filed_target:
        return
    await advance(to_number, user_id, target["ticket_id"], language, as_voice=False)


# ── Status and "my complaints" ───────────────────────────────────────────────

def _date(value) -> str:
    value = tickets.as_utc(value)
    return value.strftime("%d %b %Y") if value else "—"


async def send_status(to_number: str, user_id: str, ref: str, language: str) -> None:
    store.set_stage(user_id, None)
    ticket = tickets.get_by_ref(ref)
    if not ticket:
        await send_message(to_number, t("not_found", language, ref=ref))
        return
    extra = ""
    if ticket.get("assigned_agency"):
        extra += t("status_forwarded", language, agency=ticket["assigned_agency"])
    if ticket.get("public_note"):
        extra += t("status_note", language, note=ticket["public_note"])
    link = track_link(ticket["ref"])
    if link:
        extra += t("track_link", language, url=link)
    await send_buttons(to_number, t(
        "status_reply", language,
        ref=ticket["ref"], title=ticket.get("title") or category_label(ticket.get("category"), language),
        status=status_label(ticket.get("status"), language),
        category=category_label(ticket.get("category"), language),
        location=location_summary(ticket), date=_date(ticket.get("submitted_at") or ticket.get("created_at")),
        extra=extra,
    ), menu_buttons(language))


async def send_my_complaints(to_number: str, user_id: str, language: str) -> None:
    reporter = reporter_pseudonym(user_id)
    mine = tickets.tickets_for_reporter(reporter, limit=8)
    if not mine:
        await send_menu(to_number, language, t("my_reports_none", language))
        return
    rows = "\n".join(
        f"• *{r['ref']}* — {r.get('title') or category_label(r.get('category'), language)}\n"
        f"   _{status_label(r.get('status'), language)}_"
        for r in mine
    )
    link = dashboard.citizen_link(reporter)
    await send_buttons(to_number, t("my_reports", language, rows=rows,
                                    link=t("track_link", language, url=link) if link else ""),
                       menu_buttons(language))


# ── Notifications from the officer dashboard ─────────────────────────────────

async def notify_citizen(ticket: dict) -> bool:
    """Tell everyone who reported the issue that it moved — the original
    reporter and everyone who joined it. Resolution asks each to confirm."""
    people = tickets.recipients_for(ticket.get("ref", ""))
    if not people:
        return False
    note_en = ticket.get("public_note") or ""
    note_yo = None
    sent_any = False
    for user_id in people:
        language = store.get_user_language(user_id)
        note = note_en
        if note and language == "yoruba":
            if note_yo is None:
                note_yo = await ask_text("Translate this short message into Yorùbá with correct tone marks. "
                                         "Return only the translation.", note_en) or note_en
            note = note_yo
        note = f"\n{note}" if note else ""
        if ticket.get("status") == tickets.STATUS_RESOLVED:
            ok = await send_buttons(user_id, t("resolved_ask", language, ref=ticket["ref"], note=note), [
                {"id": f"fb_yes:{ticket['ref']}", "title": t("btn_fixed", language)},
                {"id": f"fb_no:{ticket['ref']}", "title": t("btn_not_fixed", language)},
            ])
        else:
            ok = await send_message(user_id, t(
                "update_notice", language, ref=ticket["ref"],
                status=status_label(ticket.get("status"), language), note=note,
            ))
        sent_any = sent_any or ok
    return sent_any


# ── Webhook ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("🗣️  ComNote starting")
    _find_ffmpeg()
    logger.info(f"🌍 Languages: {', '.join(l.label for l in LANGUAGES.values())}")
    logger.info(f"📮 NIPOST postcode API: {'on' if postcode.enabled() else 'off (set NIPOST_API_KEY)'}")
    logger.info("✅ ComNote ready")
    logger.info("=" * 60)
    yield
    logger.info("👋 ComNote shutting down")


app = FastAPI(title="ComNote", lifespan=lifespan)

# The dashboard may be hosted on Firebase (com-note.web.app) and call this API
# cross-origin with a bearer token.
app.add_middleware(
    CORSMiddleware,
    allow_origins=DASHBOARD_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Citizen-Token"],
    max_age=86400,
)

dashboard.configure(notify=notify_citizen, send_message=send_message)
app.include_router(dashboard.router)
dashboard.mount_static(app)


@app.get("/health", include_in_schema=False)
async def health():
    return {"ok": True, "service": "ComNote", "nipost": postcode.enabled()}


@app.api_route("/whatsapp", methods=["GET", "POST"])
async def whatsapp_handler(request: Request) -> Response:
    """Verify the webhook, then acknowledge fast and work in the background."""
    if request.method == "GET":
        params = request.query_params
        if params.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN:
            return Response(content=params.get("hub.challenge"), status_code=200)
        return Response(content="Verification token mismatch", status_code=403)

    await validate_whatsapp_hmac(request)

    try:
        data = await request.json()
    except Exception:
        logger.warning("Malformed JSON on webhook")
        return Response(status_code=400)

    try:
        value = data["entry"][0]["changes"][0]["value"]
        messages_in = value.get("messages", [])
        if not messages_in:
            return Response(status_code=200)  # delivery receipt, nothing to do
        message = messages_in[0]
        from_number = message["from"]
    except (KeyError, IndexError):
        logger.warning("Unexpected webhook payload shape")
        return Response(status_code=200)

    task = asyncio.create_task(process_message(message, from_number))

    def _log_failure(finished):
        try:
            finished.result()
        except Exception as e:
            logger.error(f"❌ Background task failed for {from_number}: {e}", exc_info=True)

    task.add_done_callback(_log_failure)
    logger.info(f"⚡ Acknowledged {message.get('id')} from {from_number}")
    return Response(status_code=200)


async def process_message(message: dict, from_number: str):
    """The full pipeline for one inbound message."""
    user_id = from_number.replace("+", "")
    message_type = message.get("type", "unknown")
    settings = store.get_user_settings(user_id) or {}
    language = store.get_user_language(user_id)
    logger.info(f"[bg] {message_type} from {from_number} ({language})")

    try:
        if message_type == "interactive":
            if await handle_interactive(message, from_number, user_id, language, settings):
                return

        # ── Onboarding: one privacy notice, English by default ───────────
        # Nobody is asked to pick a language first; the notice carries a button
        # for the other language. Whatever they sent is kept and answered as
        # soon as they agree.
        if not settings.get("terms_accepted"):
            if message_type in _REPLAYABLE:
                store.set_fields(user_id, {"pending_message": message})
            await send_terms(from_number, language)
            return

        await mark_read_and_typing(message.get("id"))
        stage = settings.get("stage")

        # ── Text ─────────────────────────────────────────────────────────
        if message_type == "text":
            raw = message["text"]["body"].strip()
            key = raw.lower().strip(" .!?")

            ref = find_ref(raw)
            if ref:
                await send_status(from_number, user_id, ref, language)
                return
            if stage == "awaiting_ref":
                await send_status(from_number, user_id, raw[:30].upper(), language)
                return
            if key in LANGUAGE_WORDS:
                await send_buttons(from_number, t("switch_language", language), language_buttons(language))
                return
            if key in CANCEL_WORDS:
                draft = tickets.find_draft(user_id)
                if draft:
                    tickets.discard_draft(draft["ticket_id"])
                store.set_stage(user_id, None)
                await send_menu(from_number, language, t("cancelled", language))
                return
            if key in MENU_WORDS and not tickets.find_draft(user_id):
                await send_menu(from_number, language)
                return
            await handle_complaint_input(from_number, user_id, language, raw, False, stage)

        # ── Voice note ───────────────────────────────────────────────────
        elif message_type == "audio":
            try:
                if not get_stt_service(language):
                    raise NoSpeechRecogniser(language)
                audio_bytes = await download_media(message["audio"]["id"])
                transcript = await transcribe(message, language, audio_bytes)
            except NoSpeechRecogniser:
                await send_message(from_number, t("no_voice_input", language))
                return
            except Exception as e:
                logger.error(f"[bg] Transcription failed: {e}")
                await send_message(from_number, t("voice_failed", language))
                return

            ref = find_ref(transcript)
            if ref and len(transcript) < 80:
                await send_status(from_number, user_id, ref, language)
                return
            mime = (message.get("audio") or {}).get("mime_type", "audio/ogg")
            await handle_complaint_input(from_number, user_id, language, transcript, True, stage,
                                         audio=(audio_bytes, mime))

        # ── Photo ────────────────────────────────────────────────────────
        elif message_type == "image":
            await handle_image(message, from_number, user_id, language)

        # ── Location pin ─────────────────────────────────────────────────
        elif message_type == "location":
            await handle_location(message, from_number, user_id, language)

        else:
            await send_menu(from_number, language, t("unsupported", language))

    except Exception:
        logger.exception(f"[bg] Unhandled error processing message from {from_number}")
        try:
            await send_message(from_number, t("error", language))
        except Exception:
            pass


async def handle_interactive(message: dict, from_number: str, user_id: str,
                             language: str, settings: dict) -> bool:
    """Handle a button tap or list selection. True when fully dealt with."""
    interactive = message.get("interactive", {})
    kind = interactive.get("type")
    if kind == "button_reply":
        choice = interactive.get("button_reply", {}).get("id", "")
    elif kind == "list_reply":
        choice = interactive.get("list_reply", {}).get("id", "")
    else:
        return False

    if choice in language_button_ids():
        chosen = language_by_button_id(choice)
        if chosen:
            store.set_user_language(user_id, chosen.key)
            if not settings.get("terms_accepted"):
                await send_terms(from_number, chosen.key)
            else:
                await send_menu(from_number, chosen.key)
        return True

    if choice == BUTTON_AGREE:
        store.set_terms_accepted(user_id)
        pending = settings.get("pending_message")
        if pending:
            # Answer what they sent before agreeing — their report, not a menu.
            store.set_fields(user_id, {"pending_message": None})
            await process_message(pending, from_number)
        else:
            await send_menu(from_number, language)
        return True

    if not settings.get("terms_accepted"):
        return False

    if choice == "menu_report":
        await send_message(from_number, t("describe", language))
        store.set_stage(user_id, "awaiting_details")
        return True

    if choice == "menu_status":
        store.set_stage(user_id, "awaiting_ref")
        await send_message(from_number, t("status_prompt", language))
        return True

    if choice == "menu_mine":
        await send_my_complaints(from_number, user_id, language)
        return True

    if choice.startswith("cat_"):
        key = choice[4:]
        draft = tickets.open_draft(user_id, language)
        tickets.update(draft["ticket_id"], {"category": key, "category_source": "citizen"})
        await advance(from_number, user_id, draft["ticket_id"], language, as_voice=False)
        return True

    if choice == "no_photo":
        draft = tickets.find_draft(user_id)
        if draft:
            await advance(from_number, user_id, draft["ticket_id"], language, as_voice=False)
        else:
            await send_menu(from_number, language)
        return True

    if choice.startswith("match_yes:") or choice == "match_no":
        draft = tickets.find_draft(user_id)
        store.set_stage(user_id, None)
        if not draft:
            await send_menu(from_number, language)
            return True
        if choice == "match_no":
            tickets.update(draft["ticket_id"], {"match_declined": True})
            await advance(from_number, user_id, draft["ticket_id"], language, as_voice=False)
            return True
        target = tickets.get_by_ref(choice.split(":", 1)[1])
        if not target or target.get("status") not in tickets.OPEN_STATUSES:
            await advance(from_number, user_id, draft["ticket_id"], language, as_voice=False)
            return True
        joined = tickets.merge_into(draft, target, user_id) or target
        link = track_link(joined["ref"])
        await send_message(from_number, t("merged", language, ref=joined["ref"], voices=VOICES(joined),
                                          link=t("track_link", language, url=link) if link else ""))
        store.set_fields(user_id, {"stage": "filed", "last_ref": joined["ref"]})
        return True

    if choice.startswith("fb_yes:") or choice.startswith("fb_no:"):
        answer, ref = choice.split(":", 1)
        # Only people who reported it (filed or joined) can confirm or reopen it.
        if tickets.is_involved(ref, user_id):
            tickets.record_feedback(ref, answer == "fb_yes")
            key = "feedback_yes" if answer == "fb_yes" else "feedback_no"
            await send_message(from_number, t(key, language, ref=ref))
        return True

    return False


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "5005")))
