"""Outbound SMS helper — supports Vonage and Twilio.

Set SMS_PROVIDER=twilio in .env to use Twilio, otherwise defaults to Vonage.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv

load_dotenv(override=True)

log = logging.getLogger(__name__)

_VONAGE_CLIENT: Any = None
_TWILIO_CLIENT: Any = None


def _vonage_client():
    global _VONAGE_CLIENT
    if _VONAGE_CLIENT is not None:
        return _VONAGE_CLIENT
    from vonage import Auth, Vonage
    api_key = os.getenv("VONAGE_API_KEY")
    api_secret = os.getenv("VONAGE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("VONAGE_API_KEY and VONAGE_API_SECRET must be set in .env")
    _VONAGE_CLIENT = Vonage(Auth(api_key=api_key, api_secret=api_secret))
    return _VONAGE_CLIENT


def _twilio_client():
    global _TWILIO_CLIENT
    if _TWILIO_CLIENT is not None:
        return _TWILIO_CLIENT
    from twilio.rest import Client
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set in .env")
    _TWILIO_CLIENT = Client(account_sid, auth_token)
    return _TWILIO_CLIENT


def send_sms(to: str, body: str) -> bool:
    """Send an SMS via Telnyx, Twilio, or Vonage (controlled by SMS_PROVIDER env var).

    If DRY_RUN_SMS=1 is set, logs the message instead of sending.
    """
    if os.getenv("DRY_RUN_SMS") == "1":
        log.warning("[DRY_RUN_SMS] → %s: %s", to, body)
        return True

    provider = os.getenv("SMS_PROVIDER", "vonage").lower()

    if provider == "telnyx":
        return _send_telnyx(to, body)
    elif provider == "twilio":
        return _send_twilio(to, body)
    else:
        return _send_vonage(to, body)


def _send_telnyx(to: str, body: str) -> bool:
    api_key = os.getenv("TELNYX_API_KEY")
    from_number = os.getenv("TELNYX_FROM")
    if not api_key or not from_number:
        log.error("TELNYX_API_KEY and TELNYX_FROM must be set in .env")
        return False
    to_e164 = to if to.startswith("+") else f"+{to}"
    try:
        import telnyx
        client = telnyx.Telnyx(api_key=api_key)
        msg = client.messages.send_long_code(from_=from_number, to=to_e164, text=body)
        msg_id = getattr(msg, 'id', None) or getattr(msg, 'record_type', 'sent')
        log.info("Sent SMS via Telnyx to %s (id: %s)", to, msg_id)
        return True
    except Exception as exc:
        log.exception("Telnyx send failed to %s: %s", to, exc)
        return False


def _send_twilio(to: str, body: str) -> bool:
    from_number = os.getenv("TWILIO_FROM")
    if not from_number:
        log.error("TWILIO_FROM not set in .env")
        return False
    # Ensure E.164 format
    to_e164 = to if to.startswith("+") else f"+{to}"
    try:
        msg = _twilio_client().messages.create(body=body, from_=from_number, to=to_e164)
        log.info("Sent SMS via Twilio to %s (sid: %s, status: %s)", to, msg.sid, msg.status)
        return True
    except Exception as exc:
        log.exception("Twilio send failed to %s: %s", to, exc)
        return False


def _send_vonage(to: str, body: str) -> bool:
    from_name = os.getenv("VONAGE_FROM", "ShiftCare")
    try:
        from vonage_sms import SmsMessage
        response = _vonage_client().sms.send(
            SmsMessage(to=to.lstrip("+"), from_=from_name, text=body)
        )
        result = response.messages[0]
        if result.status == "0":
            log.info("Sent SMS via Vonage to %s (message-id: %s)", to, result.message_id)
            return True
        else:
            log.error("Vonage error sending to %s: %s", to, result.error_text)
            return False
    except Exception as exc:
        log.exception("Vonage send failed to %s: %s", to, exc)
        return False
