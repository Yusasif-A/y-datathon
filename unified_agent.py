"""
The ComGuard conversational agent.

Two entry points, both taking the same arguments:

    get_response(...)         -> str              for written replies
    get_response_stream(...)  -> AsyncGenerator   for spoken replies

Streaming exists for voice specifically: app.py starts synthesising each
sentence as soon as it is complete, so the audio is ready almost as soon as the
text is. On a slow connection in an emergency that difference is the difference
between a useful reply and an abandoned one.

The agent runs a deliberate two-step loop rather than letting the framework
iterate: one call that may request a tool, then one final call with the tool
result and tools removed. It cannot loop, cannot call a tool twice, and cannot
stall — the worst case is one wasted call. For a service somebody is waiting on
in the rain, a bounded wrong answer beats an unbounded right one.
"""

import asyncio
import json
import logging
import re
from typing import AsyncGenerator, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from comguard_prompt import system_prompt_for
from config import GEMMA_API_KEY, GEMMA_BASE_URL, GEMMA_MODEL, get_language
from memory import get_memory
from retriever import ensemble_retriever, format_sources

logger = logging.getLogger(__name__)

memory = get_memory()


# ===================== TOOLS =====================

@tool
async def official_guidance(query: str) -> str:
    """
    Look up the official position on a levy, a checkpoint, a notice, or an emergency procedure.

    Searches indexed municipal tax codes, state gazettes, penal law and emergency
    agency (SEMA) bulletins.

    Call this when the answer depends on what the rules actually are. Examples:
    - "market stall levy amount Lagos state revenue law"
    - "who is authorised to collect road tax at a checkpoint"
    - "requirements for a valid government payment notice reference number"
    - "official flood evacuation procedure SEMA"
    - "penalty for impersonating a tax official"
    - "lawful powers to stop and search a vehicle"

    Query with the specific levy, agency or offence plus the state where possible.
    Do not call this for a pure physical emergency, a greeting, or a question about yourself.
    """
    logger.info("=" * 70)
    logger.info(f"📚 TOOL official_guidance: {query}")
    logger.info("=" * 70)

    try:
        docs = await asyncio.to_thread(ensemble_retriever.invoke, query)
        logger.info(f"Retrieved {len(docs)} documents")

        formatted = format_sources(docs)
        if not formatted:
            # Phrased as an instruction, not as data. Left as a bare "nothing
            # found", small models fill the gap by inventing a statute — which
            # in this domain means telling somebody a fake levy is lawful.
            return (
                "No matching official document was found in the indexed records. "
                "Tell the person plainly that you could not confirm the official "
                "position from the records available, and give them practical "
                "safety advice instead. Do NOT state or invent any law, section "
                "number, agency rule or official amount."
            )
        return formatted

    except Exception as e:
        logger.error(f"❌ official_guidance failed: {e}")
        return (
            "The official records could not be searched right now. Say that you "
            "could not check the official position, and do not state any law or "
            "amount from memory."
        )


TOOLS = [official_guidance]


# ===================== PROMPT-PROBE DEFENCE =====================

# A warm redirect rather than an accusation: almost everyone who trips this is
# curious, and the people who are not should learn nothing from the reply.
OFF_TOPIC_REFUSAL = {
    "english": (
        "I'm ComGuard. I help with safety problems and scams — flooding, fires, "
        "damaged roads, fake notices, people demanding money they shouldn't. "
        "What's happening where you are?"
    ),
    "yoruba": (
        "Èmi ni ComGuard. Mo ń ràn àwọn ènìyàn lọ́wọ́ lórí ewu àti ìwà ẹ̀tàn — "
        "ìkún omi, iná, ọ̀nà tí ó bàjẹ́, ìwé ìkéde èké, àti àwọn tí ń béèrè owó "
        "tí kò yẹ. Kí ni ó ń ṣẹlẹ̀ níbi tí o wà?"
    ),
    "swahili": (
        "Mimi ni ComGuard. Nasaidia kuhusu matatizo ya usalama na utapeli — "
        "mafuriko, moto, barabara zilizoharibika, notisi za kughushi, na watu "
        "wanaodai pesa isivyo halali. Ni nini kinaendelea mahali ulipo?"
    ),
}
# ⚠️ The Yoruba and Swahili lines above are best-effort and need a native speaker
# to review them before production use.


def off_topic_refusal(language: str = "english") -> str:
    return OFF_TOPIC_REFUSAL.get(language, OFF_TOPIC_REFUSAL["english"])


# Context-aware, not bare substring matching. A ComGuard user legitimately says
# "the instructions on the notice", "they told me to act as if I had paid",
# "ignore the previous receipt" — all of which a naive filter would refuse,
# blocking exactly the scam reports this service exists to receive.
_INJECTION_PATTERNS = [
    r"\b(system|initial|original|previous|prior|your)\s+(prompt|instructions?|rules?)\b",
    r"\b(reveal|show|repeat|print|output|display|tell\s+me|give\s+me|what\s+is)\b.{0,40}"
    r"\b(system\s+prompt|your\s+instructions?|your\s+rules?|your\s+guidelines)\b",
    r"\bsystem\s+message\b",
    r"\bignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|your)\s+"
    r"(instructions?|rules?|prompt|message)\b",
    r"\bdisregard\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|your)\s+"
    r"(instructions?|rules?|prompt)\b",
    r"\bforget\s+(your\s+instructions?|your\s+rules?|everything\s+above)\b",
    r"\byou\s+are\s+now\s+(a|an|the)\b",
    r"\bpretend\s+(you\s+are|to\s+be|that\s+you)\b",
    r"(?:^|\b(?:you|now|please)\s+(?:should\s+|must\s+|will\s+|can\s+)?)act\s+as\s+(a|an|the)\b",
    r"\bwhat\s+were\s+you\s+(told|given|instructed|programmed|trained)\b",
    r"\b(developer|debug|god)\s+mode\b",
    r"\bjailbreak\b",
    r"\brepeat\s+(the\s+)?(text|words|everything)\s+above\b",
]


def is_prompt_injection_attempt(user_input: str) -> bool:
    clean = re.sub(r"\[.*?\]:\s*", "", (user_input or "").lower()).strip()
    return any(re.search(pattern, clean) for pattern in _INJECTION_PATTERNS)


def contains_system_prompt_leak(text: str) -> bool:
    """Catch the model naming its own instructions or the model behind it.

    People trust a safety service partly because it feels like a service rather
    than a chatbot; a reply that starts "As a Google Gemma model..." undermines
    that at the worst moment.
    """
    lowered = (text or "").lower()
    leaks = [
        "system prompt", "my instructions are", "reference document",
        "gemma", "google model", "google ai", "i am gemma", "i'm gemma",
        "powered by google", "created by google", "made by google",
    ]
    return any(term in lowered for term in leaks)


# ===================== SHORT REPLIES =====================

# The model receives the user's message untranslated and does not reliably read a
# bare "Bẹ́ẹ̀ni" as agreement, so it re-asks the question it just asked — which
# means somebody standing in a flood answers the same question three times.
# Detected deterministically instead of hoped for.
_AFFIRMATIONS = {
    "yoruba": {"beeni", "bee ni", "ooto ni", "ootoni", "eeni", "eni", "o daa",
               "odaa", "mo gba", "nitooto", "ehn", "een"},
    "swahili": {"ndiyo", "ndio", "sawa", "sawa sawa", "kweli", "hakika", "ehe",
                "naam", "haya"},
    "english": {"yes", "yeah", "yep", "yup", "correct", "right", "true", "ok",
                "okay", "sure", "exactly", "that is correct", "thats correct"},
}

_NEGATIONS = {
    "yoruba": {"rara", "beeko", "bee ko", "ko ri be", "kii se"},
    "swahili": {"hapana", "la", "sivyo", "siyo", "si kweli", "sio sahihi"},
    "english": {"no", "nope", "nah", "wrong", "incorrect", "not correct",
                "that is wrong", "thats wrong"},
}

# Words that mean "I am in trouble right now". A one-word plea must never be
# mistaken for small talk, so these are recognised before anything else runs.
_DISTRESS_WORDS = {
    "english": {"help", "help me", "emergency", "danger", "sos", "please help"},
    "yoruba": {"egba mi", "e gba mi", "iranlowo", "ewu", "e ran mi lowo"},
    "swahili": {"msaada", "nisaidie", "saidia", "hatari", "dharura", "okoa"},
}


def _normalise_reply(text: str) -> str:
    """Lowercase, strip accents and punctuation so "Bẹ́ẹ̀ni!" matches "beeni"."""
    import unicodedata
    decomposed = unicodedata.normalize("NFD", (text or "").strip().lower())
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    stripped = re.sub(r"[^\w\s]", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def is_distress_signal(text: str, language: str) -> bool:
    key = _normalise_reply(text)
    if not key or len(key) > 24:
        return False
    return key in (_DISTRESS_WORDS.get(language, set()) | _DISTRESS_WORDS["english"])


def _annotate_short_reply(user_message: str, language: str) -> str:
    """Gloss a bare yes/no so the model does not re-ask what it just asked.

    Only fires on short messages, so a real sentence containing "ko" or "no" is
    never rewritten.
    """
    match = re.match(r"^(\[[^\]]*\]\s*)(.*)$", user_message, re.DOTALL)
    prefix, body = (match.group(1), match.group(2)) if match else ("", user_message)

    key = _normalise_reply(body)
    if not key or len(key) > 24:
        return user_message

    yes = _AFFIRMATIONS.get(language, set()) | _AFFIRMATIONS["english"]
    no = _NEGATIONS.get(language, set()) | _NEGATIONS["english"]

    if key in yes:
        return prefix + (
            f'[The user replied "{body.strip()}". This means YES — they are '
            f"confirming what you just said. Do NOT repeat or re-ask that "
            f"question. Move on to the next step.]\n\n"
        ) + body
    if key in no:
        return prefix + (
            f'[The user replied "{body.strip()}". This means NO — they are '
            f"disagreeing. Ask them what is correct instead of repeating your "
            f"previous message.]\n\n"
        ) + body
    return user_message


# ===================== HISTORY =====================

def _strip_images_from_history(history: list) -> list:
    """Drop image payloads from past turns, keeping a note of what they showed.

    Two reasons. Tokens: a base64 photo re-sent on every turn exhausts the
    context within a few messages. Behaviour: the text accompanying an image is
    an instruction ("analyse this photo and confirm what you see"), and leaving
    it in history means the model reads that instruction again every turn and
    dutifully re-runs it — asking "is that correct?" long after the person
    answered.
    """
    cleaned = []
    for message in history:
        if isinstance(message, HumanMessage) and isinstance(message.content, list):
            had_image = any(
                isinstance(part, dict) and part.get("type") == "image_url"
                for part in message.content
            )
            if not had_image:
                cleaned.append(message)
                continue
            cleaned.append(HumanMessage(content=[{
                "type": "text",
                "text": "[Earlier in this conversation the person sent a photo, "
                        "which has already been discussed. Do not ask them to "
                        "describe or confirm it again.]",
            }]))
        else:
            cleaned.append(message)
    return cleaned


def _trim_history(history: list) -> list:
    """Keep the last two complete turns; the current one makes three.

    Short on purpose. A report is a focused exchange, and a long history makes
    the model drift back to an earlier incident when somebody reports a second
    one from the same number.
    """
    human_indices = [i for i, m in enumerate(history) if isinstance(m, HumanMessage)]
    if len(human_indices) > 2:
        history = history[human_indices[-2]:]
        logger.info(f"✂️ Trimmed history to the last 2 turns ({len(history)} messages)")
    return _strip_images_from_history(history)


def _clean_text(text: str) -> str:
    text = re.sub(r"<\|im_end\|>|<\|im_start\|>\w*", "", text or "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"^(thought|Thought)\s*\n", "", text.lstrip())
    return text.lstrip()


def _parse_raw_tool_call(text: str) -> Optional[dict]:
    """Recover a tool call the model wrote as plain text instead of calling.

    Small models do this often enough that ignoring it means the person gets a
    literal "call:official_guidance{...}" in their reply.
    """
    match = re.search(r"call\s*:\s*(\w+)\s*\{([^}]*)\}", text or "", re.IGNORECASE)
    if not match:
        return None

    name, raw_args = match.group(1), match.group(2).strip()
    try:
        args = json.loads("{" + raw_args + "}")
    except Exception:
        args = {}
        for pair in re.split(r",(?![^{]*})", raw_args):
            parts = pair.strip().split(":", 1)
            if len(parts) == 2:
                key = parts[0].strip().strip('"')
                value = parts[1].strip().strip('"')
                try:
                    value = json.loads(value)
                except Exception:
                    pass
                args[key] = value
    return {"name": name, "args": args, "id": "raw_tool_call"}


# ===================== AGENT =====================

class ComGuardAgent:
    def __init__(self):
        self.llm: Optional[ChatOpenAI] = None
        self.llm_with_tools = None
        self.agent = None
        self.memory = memory
        self.tools = TOOLS
        self._initialised = False

    async def initialize(self):
        logger.info("🛡️ Initialising ComGuard agent")

        self.llm = ChatOpenAI(
            base_url=GEMMA_BASE_URL,
            api_key=GEMMA_API_KEY,
            model=GEMMA_MODEL,
            temperature=0.1,   # low: this is guidance, not creative writing
            streaming=True,
            tags=["comguard"],
            stop=["<|eot_id|>"],
        )
        self.llm_with_tools = self.llm.bind_tools(self.tools)

        # The graph is built for its checkpointed state, which is what gives a
        # conversation continuity across messages. The two-step tool loop below
        # drives the model directly rather than invoking the graph, so a report
        # can never get stuck in an agent loop.
        self.agent = create_agent(
            model=self.llm,
            tools=self.tools,
            checkpointer=self.memory,
        )
        self._initialised = True

        logger.info(f"✅ ComGuard agent ready — {GEMMA_MODEL} at {GEMMA_BASE_URL}")

    # ── checkpoint ──────────────────────────────────────────────────────────
    #
    # History is read and written through the LangGraph agent's own state rather
    # than by talking to the checkpointer directly: the stored format is the
    # graph's, and hand-writing it is how you end up with checkpoints that load
    # in one library version and not the next.

    def _config(self, thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    def _load_history(self, thread_id: str) -> list:
        if self.agent is None:
            return []
        try:
            state = self.agent.get_state(self._config(thread_id))
            messages = state.values.get("messages", []) if state and state.values else []
            return _trim_history(list(messages))
        except Exception as e:
            logger.warning(f"⚠️ Could not load history for {thread_id}: {e}")
            return []

    def _save_turn(self, thread_id: str, new_messages: list):
        if self.agent is None or not new_messages:
            return
        try:
            self.agent.update_state(self._config(thread_id), {"messages": new_messages})
            logger.info(f"💾 Saved {len(new_messages)} message(s) for {thread_id}")
        except Exception as e:
            logger.error(f"❌ Could not save turn for {thread_id}: {e}")

    # ── tools ───────────────────────────────────────────────────────────────

    async def _execute_tools(self, tool_calls: list) -> list:
        results = []
        for call in tool_calls:
            name = call.get("name", "")
            args = call.get("args", {}) or {}
            call_id = call.get("id", "unknown")
            logger.info(f"🔧 {name}({args})")
            try:
                if name == "official_guidance":
                    result = await official_guidance.ainvoke(args)
                else:
                    result = f"Unknown tool: {name}"
            except Exception as e:
                logger.error(f"❌ Tool {name} failed: {e}")
                result = (
                    "That lookup failed. Tell the person you could not check the "
                    "official records, and do not state any law or amount from memory."
                )
            results.append(ToolMessage(content=result, tool_call_id=call_id))
        return results

    # ── message assembly ────────────────────────────────────────────────────

    def _prepare(self, content, message_type: str, language: str, original_content: str):
        """Work out the prompt, the user message, and whether an image is attached."""
        is_image = isinstance(content, list) and any(
            isinstance(p, dict) and p.get("type") == "image_url" for p in content
        )

        if isinstance(content, list):
            text_content = next(
                (p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text"),
                "[photo]",
            )
        else:
            text_content = str(content)

        is_voice = message_type == "voice"
        prompt = system_prompt_for(language, is_voice)

        if is_image:
            user_message = content
        else:
            raw = original_content if original_content else text_content
            raw = _annotate_short_reply(raw, language)
            user_message = f"[Voice message]: {raw}" if is_voice else raw

        logger.info(
            "🧭 %s | %s | %s",
            "VOICE" if is_voice else "TEXT",
            get_language(language).label,
            "IMAGE" if is_image else "no image",
        )
        return is_image, text_content, prompt, user_message

    def _final_turn_instruction(self) -> SystemMessage:
        return SystemMessage(content=(
            "You now have the official records above. Write your final reply to "
            "the person. Do NOT call any tool. If the records did not answer the "
            "question, say you could not confirm it from official sources rather "
            "than stating a law or amount you are not sure of."
        ))

    # ── public API ──────────────────────────────────────────────────────────

    async def get_response(
        self, content, thread_id: str, message_type: str = "text",
        language: str = "english", original_content: str = None,
    ) -> str:
        if not self._initialised:
            await self.initialize()

        is_image, text_content, prompt, user_message = self._prepare(
            content, message_type, language, original_content
        )

        if not is_image and is_prompt_injection_attempt(text_content):
            logger.info("🚫 Prompt-probe refused")
            return off_topic_refusal(language)

        history = self._load_history(thread_id)
        human = HumanMessage(content=user_message)
        messages = [SystemMessage(content=prompt)] + history + [human]

        try:
            first = await self.llm_with_tools.ainvoke(messages)
        except Exception as e:
            logger.error(f"❌ First LLM call failed: {e}", exc_info=True)
            return self._unavailable(language)

        turn = [human]
        tool_calls = first.tool_calls or []
        if not tool_calls:
            raw = _parse_raw_tool_call(first.content or "")
            tool_calls = [raw] if raw else []

        if tool_calls:
            logger.info(f"✓ Tool requested: {[c['name'] for c in tool_calls]}")
            turn.append(first)
            tool_results = await self._execute_tools(tool_calls)
            turn.extend(tool_results)
            final_messages = messages + [first] + tool_results + [self._final_turn_instruction()]
        else:
            final_messages = None
            response = _clean_text(first.content or "")

        if final_messages is not None:
            try:
                final = await self.llm.ainvoke(final_messages)
                response = _clean_text(
                    final.content if hasattr(final, "content") else str(final)
                )
            except Exception as e:
                logger.error(f"❌ Final LLM call failed: {e}", exc_info=True)
                return self._unavailable(language)

        response = (response or "").replace("###", "").strip()
        if not response:
            return "I did not catch that. Can you tell me again what is happening?"

        if contains_system_prompt_leak(response):
            logger.warning("🚫 Blocked a reply that leaked instructions or the model name")
            return off_topic_refusal(language)

        # The model sometimes emits a tool call in the FINAL reply, after tools
        # were removed. Run it and retry once rather than showing the person raw
        # call syntax.
        if re.search(r"call\s*:\s*\w+\s*\{|<tool_call>", response, re.IGNORECASE):
            recovered = _parse_raw_tool_call(response)
            if recovered:
                logger.info(f"⚠️ Tool call leaked into the reply — running {recovered['name']}")
                extra = await self._execute_tools([recovered])
                try:
                    retry = await self.llm.ainvoke(
                        (final_messages or messages) + [AIMessage(content=response)]
                        + extra + [self._final_turn_instruction()]
                    )
                    response = _clean_text(getattr(retry, "content", str(retry)))
                except Exception:
                    return self._unavailable(language)
            else:
                logger.error(f"❌ Unparseable tool call in reply: {response[:120]}")
                return self._unavailable(language)

        turn.append(AIMessage(content=response))
        self._save_turn(thread_id, turn)
        return response

    async def get_response_stream(
        self, content, thread_id: str, message_type: str = "text",
        language: str = "english", original_content: str = None,
    ) -> AsyncGenerator[str, None]:
        if not self._initialised:
            await self.initialize()

        is_image, text_content, prompt, user_message = self._prepare(
            content, message_type, language, original_content
        )

        if not is_image and is_prompt_injection_attempt(text_content):
            yield off_topic_refusal(language)
            return

        history = self._load_history(thread_id)
        human = HumanMessage(content=user_message)
        messages = [SystemMessage(content=prompt)] + history + [human]

        try:
            first = await self.llm_with_tools.ainvoke(messages)
        except Exception as e:
            logger.error(f"❌ First LLM call failed: {e}", exc_info=True)
            yield self._unavailable(language)
            return

        turn = [human]
        tool_calls = first.tool_calls or []
        if not tool_calls:
            raw = _parse_raw_tool_call(first.content or "")
            tool_calls = [raw] if raw else []

        if tool_calls:
            turn.append(first)
            tool_results = await self._execute_tools(tool_calls)
            turn.extend(tool_results)
            stream_source = self.llm.astream(
                messages + [first] + tool_results + [self._final_turn_instruction()]
            )
            direct = None
        else:
            stream_source = None
            direct = _clean_text(first.content or "")

        full_response = ""
        buffer = ""
        boundaries = [" ", "\n", ".", ",", "!", "?", ";", ":"]

        try:
            if stream_source is not None:
                async for chunk in stream_source:
                    raw = chunk.content
                    if not raw:
                        continue
                    text = (
                        "".join(
                            p.get("text", "") if isinstance(p, dict) else str(p)
                            for p in raw
                        ) if isinstance(raw, list) else str(raw)
                    )
                    if not text:
                        continue

                    text = _clean_text(text)
                    full_response += text
                    buffer += text

                    last = max((buffer.rfind(b) for b in boundaries), default=-1)
                    if last > 0:
                        ready = buffer[:last + 1]
                        if contains_system_prompt_leak(ready):
                            yield off_topic_refusal(language)
                            return
                        yield ready.replace("###", "")
                        buffer = buffer[last + 1:]
            else:
                if contains_system_prompt_leak(direct):
                    yield off_topic_refusal(language)
                    return
                direct = (direct or "").replace("###", "")
                full_response = direct
                words = direct.split(" ")
                for i, word in enumerate(words):
                    yield word + (" " if i < len(words) - 1 else "")
                    await asyncio.sleep(0)

            if buffer:
                buffer = _clean_text(buffer)
                if not contains_system_prompt_leak(buffer):
                    yield buffer.replace("###", "")

        except Exception as e:
            logger.error(f"❌ Streaming failed: {e}", exc_info=True)
            yield self._unavailable(language)
            return

        if full_response.strip():
            turn.append(AIMessage(content=full_response))
            self._save_turn(thread_id, turn)

    @staticmethod
    def _unavailable(language: str) -> str:
        """Shown when the model is unreachable.

        It names the emergency number, because the one thing that must still
        work when ComGuard does not is somebody knowing who else to call.
        """
        from config import EMERGENCY_NUMBERS
        return (
            "I can't reach my system right now, so I can't look into this properly. "
            f"If anyone is in danger, call {EMERGENCY_NUMBERS} now. "
            "Please send your message again in a few minutes."
        )


unified_agent = ComGuardAgent()
