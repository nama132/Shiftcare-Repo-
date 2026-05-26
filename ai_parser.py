"""Claude-powered SMS intent parser.

Returns a dict like:
    {"intent": "cancel_shift"|"confirm_coverage"|"decline_coverage"|"other",
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
- "intent": one of ["cancel_shift", "confirm_coverage", "decline_coverage", "other"]
  Rules:
    * "cancel_shift"      — caregiver is calling out / cancelling their OWN scheduled shift
                            (e.g. "I'm sick", "can't make it", "won't be in", "calling out")
    * "confirm_coverage"  — caregiver is accepting a COVERAGE REQUEST sent to them (replying YES)
    * "decline_coverage"  — caregiver is declining a COVERAGE REQUEST sent to them (replying NO)
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
    try:
        resp = _client().messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": _PROMPT.format(text=text)}],
        )
        raw = resp.content[0].text  # type: ignore[index]
        data = _extract_json(raw)
        intent = data.get("intent")
        if intent not in ("cancel_shift", "confirm_coverage", "decline_coverage", "other"):
            intent = "other"
        return {
            "intent": intent,
            "reason": data.get("reason") or None,
            "shift_time": data.get("shift_time") or None,
            "shift_date": data.get("shift_date") or None,
        }
    except Exception as exc:
        log.warning("parse_message fallback for %r: %s", text, exc)
        # Heuristic fallback so the app degrades gracefully without the API
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
