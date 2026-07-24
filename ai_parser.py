"""Claude-powered SMS intent parser.

Returns a dict like:
    {"intent": "cancel_shift"|"confirm_coverage"|"decline_coverage"
              |"check_in"|"check_out"|"other",
     "reason": str|None,
     "shift_time": str|None}
"""
from __future__ import annotations

import json
import logging
import os
import re

from dotenv import load_dotenv

load_dotenv(override=True)

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

_ANTHROPIC = None


# ---------------------------------------------------------------------------
# Deterministic pre-classifier — guarantees common phrasings always work,
# independent of the (non-deterministic) LLM. Also saves API calls for
# unambiguous yes/no replies.
# ---------------------------------------------------------------------------

# Callout / cancellation signals (substring match on lowercased text).
_CANCEL_KEYWORDS = (
    "cancel", "calling out", "call out", "call off", "callout",
    "won't make", "wont make", "can't make", "cant make", "cannot make",
    "won't be in", "wont be in", "not coming", "can't come", "cant come",
    "won't come", "wont come", "not gonna make", "not going to make",
    "can't come in", "cant come in", "won't be able to make",
    "sick", "not feeling well", "under the weather", "fever", "throwing up",
    "vomiting", "food poisoning", "family emergency", "emergency",
    "car broke", "car broke down", "staying home", "out sick", "running a fever",
)

# Affirmative replies (accept a coverage request).
_AFFIRM_WORDS = frozenset({
    "yes", "y", "yep", "yeah", "yup", "ya", "yea", "sure", "ok", "okay",
    "k", "kk", "absolutely", "definitely", "yessir", "affirmative", "👍",
})
_AFFIRM_PHRASES = (
    "i can cover", "i'll take it", "ill take it", "i can take", "i will take",
    "count me in", "i'm in", "im in", "i got it", "i've got it", "on it",
    "will do", "sounds good", "happy to", "i can do it",
)

# Negative replies (decline a coverage request).
_DECLINE_WORDS = frozenset({
    "no", "n", "nope", "nah", "naw", "pass", "decline", "cannot", "unable", "👎",
})
_DECLINE_PHRASES = (
    "can't cover", "cant cover", "cannot cover", "can't take", "cant take",
    "not able", "no thanks", "no can do", "can't do it", "cant do it",
    "sorry can't", "sorry i cant", "won't be able to cover",
)

# Check-in signals (caregiver arrived on site).
_CHECKIN_WORDS = frozenset({
    "arrived", "arrive", "here", "onsite", "on-site", "checkin", "checkedin",
})
_CHECKIN_PHRASES = (
    "i'm here", "im here", "i have arrived", "i've arrived", "ive arrived",
    "just arrived", "on site", "at the house", "at the client", "checking in",
    "checked in", "clocking in", "clock in", "clock me in", "starting my shift",
    "starting shift", "made it",
)

# Check-out signals (caregiver finished the visit).
_CHECKOUT_WORDS = frozenset({
    "done", "finished", "complete", "completed", "checkout", "checkedout",
    "leaving", "departing",
})
_CHECKOUT_PHRASES = (
    "all done", "i'm done", "im done", "visit complete", "visit done",
    "shift done", "shift complete", "finished up", "wrapping up", "heading out",
    "checking out", "checked out", "clocking out", "clock out", "clock me out",
    "leaving now", "all set here", "ending my shift",
)


def _deterministic_intent(text: str) -> str | None:
    """Return a confident intent for unambiguous messages, else None.

    Order matters: callout signals first, then affirmative (so "yep no problem"
    resolves to confirm), then negative.
    """
    t = " ".join(text.lower().split())
    if not t:
        return None

    # 1) Callout / cancellation signals.
    for kw in _CANCEL_KEYWORDS:
        if kw in t:
            return "cancel_shift"

    # Word/phrase-level checks on a punctuation-stripped copy (keep apostrophes).
    stripped = re.sub(r"[^a-z0-9' ]", " ", t)
    stripped = " ".join(stripped.split())
    words = stripped.split()
    first = words[0] if words else ""

    # 2) Check-in / check-out. Exact-word match only (a bare "ARRIVED" or
    #    "DONE") plus explicit phrases — kept strict so ordinary replies
    #    ("will do", "no thanks") never collide with these.
    if stripped in _CHECKIN_WORDS or any(p in stripped for p in _CHECKIN_PHRASES):
        return "check_in"
    if stripped in _CHECKOUT_WORDS or any(p in stripped for p in _CHECKOUT_PHRASES):
        return "check_out"

    # 3) Affirmative → confirm coverage.
    if (stripped in _AFFIRM_WORDS or first in _AFFIRM_WORDS
            or any(p in stripped for p in _AFFIRM_PHRASES) or "👍" in t):
        return "confirm_coverage"

    # 4) Negative → decline coverage.
    if (stripped in _DECLINE_WORDS or first in _DECLINE_WORDS
            or any(p in stripped for p in _DECLINE_PHRASES) or "👎" in t):
        return "decline_coverage"

    return None


def _client():
    global _ANTHROPIC
    if _ANTHROPIC is not None:
        return _ANTHROPIC
    import anthropic  # local import → app boots without the key

    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")
    _ANTHROPIC = anthropic.Anthropic(api_key=key)
    return _ANTHROPIC


_PROMPT = """You are parsing a text from a home care agency caregiver.
Return ONLY valid JSON (no prose, no markdown fences) with these exact fields:
- "intent": one of ["cancel_shift", "confirm_coverage", "decline_coverage", "check_in", "check_out", "other"]
  Rules:
    * "cancel_shift"      — caregiver is calling out / cancelling their OWN scheduled shift
                            (e.g. "I'm sick", "can't make it", "won't be in", "calling out")
    * "confirm_coverage"  — caregiver is accepting a COVERAGE REQUEST sent to them (replying YES)
                            (e.g. "yes", "ok", "sure", "yep", "i'll take it", "count me in")
    * "decline_coverage"  — caregiver is declining a COVERAGE REQUEST sent to them (replying NO)
                            (e.g. "no", "nope", "nah", "pass", "can't cover", "no thanks")
    * "check_in"          — caregiver announcing they ARRIVED at the client's home / starting the visit
                            (e.g. "ARRIVED", "I'm here", "on site", "clocking in")
    * "check_out"         — caregiver announcing they FINISHED the visit / leaving
                            (e.g. "DONE", "all done", "finished up", "clocking out", "heading out")
    * "other"             — anything else
- "reason": a short string describing the reason, or null
- "shift_time": a time mentioned in the message (e.g. "9am", "2:30pm"), or null
- "shift_date": the date the caregiver is referring to — use one of:
    "today", "tomorrow", a weekday name like "friday", or an ISO date "YYYY-MM-DD".
    Use null if no date is mentioned (assume today).

Message: "{text}"
"""


def _extract_json(raw: str) -> dict:
    """Pull the first {...} block out of a model response and parse it."""
    raw = raw.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in model output: {raw!r}")
    return json.loads(match.group(0))


def parse_message(text: str) -> dict:
    """Classify an incoming SMS. Always returns a dict; never raises."""
    fallback = {"intent": "other", "reason": None, "shift_time": None, "shift_date": None}
    if not text or not text.strip():
        return fallback

    # Deterministic fast path — guarantees common phrasings always work and
    # skips the API for unambiguous accept/decline/check-in/out replies.
    det = _deterministic_intent(text)
    if det in ("confirm_coverage", "decline_coverage", "check_in", "check_out"):
        return {"intent": det, "reason": None, "shift_time": None, "shift_date": None}

    try:
        resp = _client().messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": _PROMPT.format(text=text)}],
        )
        raw = resp.content[0].text  # type: ignore[index]
        data = _extract_json(raw)
        intent = data.get("intent")
        if intent not in ("cancel_shift", "confirm_coverage", "decline_coverage",
                          "check_in", "check_out", "other"):
            intent = "other"
        # Safety net: if the model missed a clear intent, trust the
        # deterministic classifier instead of returning "other".
        if intent == "other" and det is not None:
            intent = det
        return {
            "intent": intent,
            "reason": data.get("reason") or None,
            "shift_time": data.get("shift_time") or None,
            "shift_date": data.get("shift_date") or None,
        }
    except Exception as exc:
        log.warning("parse_message fallback for %r: %s", text, exc)
        # Heuristic fallback so the app degrades gracefully without the API.
        if det is not None:
            return {"intent": det, "reason": "heuristic", "shift_time": None, "shift_date": None}
        lowered = text.lower().strip()
        if any(k in lowered for k in ("can't make", "cant make", "cancel", "sick", "calling out", "call out")):
            return {"intent": "cancel_shift", "reason": "heuristic", "shift_time": None, "shift_date": None}
        if lowered in ("y", "yes", "yes!", "yep", "yeah", "yup") or "i can cover" in lowered or lowered.startswith("yes"):
            return {"intent": "confirm_coverage", "reason": None, "shift_time": None, "shift_date": None}
        if (
            lowered in ("n", "no", "nope")
            or lowered.startswith("no ")
            or lowered.startswith("no,")
            or "no thanks" in lowered
            or "can't cover" in lowered
            or "cant cover" in lowered
            or "sorry" in lowered and ("no" in lowered or "can't" in lowered or "cant" in lowered)
        ):
            return {"intent": "decline_coverage", "reason": None, "shift_time": None, "shift_date": None}
        return fallback


_CONTEXT_PROMPT = """You are helping resolve an ambiguous follow-up message from a home care agency caregiver.

Prior context: {context}

The caregiver just replied: "{text}"

Based on the context, identify which shift time they are referring to.
Return ONLY valid JSON with these fields:
- "shift_time": the time they are referring to in HH:MM 24-hour format (e.g. "09:00", "14:00"), or null if unclear
- "intent": one of ["cancel_shift", "other"] — "cancel_shift" if they are confirming the cancellation, "other" if unclear

Message: "{text}"
"""


def parse_message_with_context(text: str, context: str) -> dict:
    """Parse an ambiguous follow-up using prior conversation context."""
    fallback = {"intent": "other", "shift_time": None}
    if not text or not text.strip():
        return fallback
    try:
        resp = _client().messages.create(
            model=MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": _CONTEXT_PROMPT.format(
                text=text, context=context
            )}],
        )
        raw = resp.content[0].text  # type: ignore[index]
        data = _extract_json(raw)
        intent = data.get("intent")
        if intent not in ("cancel_shift", "other"):
            intent = "other"
        return {
            "intent": intent,
            "shift_time": data.get("shift_time") or None,
        }
    except Exception as exc:
        log.warning("parse_message_with_context fallback: %s", exc)
        # Heuristic: look for time patterns like "9am", "2pm", "09:00"
        import re as _re
        m = _re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text.lower())
        if m:
            h = int(m.group(1))
            mins = int(m.group(2) or 0)
            suffix = m.group(3)
            if suffix == "pm" and h != 12:
                h += 12
            if suffix == "am" and h == 12:
                h = 0
            return {"intent": "cancel_shift", "shift_time": f"{h:02d}:{mins:02d}"}
        return fallback


if __name__ == "__main__":
    # Quick smoke test
    samples = [
        "I'm sick, can't make my 9am shift",
        "Hey, I can't make Friday's 2pm shift",
        "I won't be in tomorrow morning",
        "YES I can cover it",
        "Sorry no, I have another commitment",
        "What's the weather today?",
    ]
    for s in samples:
        print(s, "→", parse_message(s))
