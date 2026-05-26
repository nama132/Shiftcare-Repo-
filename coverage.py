"""Shift-coverage search, candidate notification, and claim logic."""
from __future__ import annotations

import logging
import os
import time as _time
from datetime import datetime

from dotenv import load_dotenv

import db
from sms import send_sms

load_dotenv(override=True)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def find_available_caregivers(
    date: str,
    start_time: str,
    end_time: str,
    exclude_id: int | None = None,
    preferred_zip: str | None = None,
    required_certifications: str | None = None,
    client_id: int | None = None,
) -> list[dict]:
    """Wrapper around db.get_available_caregivers."""
    return db.get_available_caregivers(
        date=date,
        start_time=start_time,
        end_time=end_time,
        exclude_id=exclude_id,
        preferred_zip=preferred_zip,
        required_certifications=required_certifications,
        client_id=client_id,
    )


# ---------------------------------------------------------------------------
# Outreach
# ---------------------------------------------------------------------------

def _format_time(t: str) -> str:
    """Turn '09:00' into '9:00am' for friendlier SMS copy."""
    try:
        dt = datetime.strptime(t, "%H:%M")
        return dt.strftime("%-I:%M%p").lower()
    except Exception:
        return t


def _coverage_request_body(client: dict, shift: dict) -> str:
    return (
        f"Coverage needed today {_format_time(shift['start_time'])}"
        f"-{_format_time(shift['end_time'])} for {client['name']}. "
        f"Text YES to take the shift or NO to decline."
    )


def initiate_coverage_hunt(shift_id: int) -> list[dict]:
    """Find replacements for an uncovered shift and text them all.

    Returns the list of caregivers contacted (so callers/tests can inspect).
    """
    shift = db.get_shift_by_id(shift_id)
    if not shift:
        log.warning("initiate_coverage_hunt: shift %s not found", shift_id)
        return []

    client = db.get_client_by_id(shift["client_id"])
    if not client:
        log.warning("initiate_coverage_hunt: client %s missing", shift["client_id"])
        return []

    candidates = find_available_caregivers(
        date=shift["date"],
        start_time=shift["start_time"],
        end_time=shift["end_time"],
        exclude_id=shift["caregiver_id"],
        preferred_zip=client.get("zip_code"),
        required_certifications=client.get("required_certifications"),
        client_id=client.get("id"),
    )

    if not candidates:
        log.warning("No available caregivers for shift %s", shift_id)
        _alert_owner_no_coverage(shift, client)
        return []

    body = _coverage_request_body(client, shift)
    for cg in candidates:
        db.add_pending_candidate(shift_id, cg["id"], cg["phone"])
        send_sms(cg["phone"], body)

    log.info("Coverage hunt: contacted %d caregivers for shift %s", len(candidates), shift_id)
    return candidates


def _alert_owner_no_coverage(shift: dict, client: dict) -> None:
    owner = os.getenv("OWNER_PHONE")
    if not owner:
        return
    send_sms(
        owner,
        f"⚠️ ShiftCare: NO coverage found for {client['name']} "
        f"({_format_time(shift['start_time'])}–{_format_time(shift['end_time'])}). "
        f"Manual intervention needed.",
    )


# ---------------------------------------------------------------------------
# Claim / decline
# ---------------------------------------------------------------------------

_CLAIM_TIMESTAMPS: dict[int, float] = {}  # shift_id → monotonic ts of winning claim


def claim_shift(shift_id: int, caregiver_id: int) -> bool:
    """Assign the shift to caregiver_id IF it's still open. Returns True on win.

    Race-safe-ish: uses a single UPDATE ... WHERE status='uncovered' and checks
    rowcount. Two simultaneous YES replies → only one update succeeds.
    """
    with db.get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE shifts
               SET status = 'covered', caregiver_id = ?
             WHERE id = ? AND status = 'uncovered'
            """,
            (caregiver_id, shift_id),
        )
        won = cur.rowcount == 1
        conn.commit()

    if won:
        _CLAIM_TIMESTAMPS[shift_id] = _time.monotonic()
        db.expire_pending_for_shift(shift_id)
        # Mark the winner's pending row as claimed (for audit)
        with db.get_conn() as conn:
            conn.execute(
                """UPDATE pending_coverage SET status = 'claimed'
                     WHERE shift_id = ? AND caregiver_id = ?""",
                (shift_id, caregiver_id),
            )
            conn.commit()
    return won


def remove_candidate(phone: str) -> None:
    """A caregiver declined → mark their pending row(s) declined."""
    norm = db.normalize_phone(phone)
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE pending_coverage SET status = 'declined'
                 WHERE phone = ? AND status = 'pending'""",
            (norm,),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Notifications after a successful claim
# ---------------------------------------------------------------------------

def send_confirmation_to_caregiver(caregiver: dict, shift_id: int) -> None:
    shift = db.get_shift_by_id(shift_id)
    if not shift:
        return
    client = db.get_client_by_id(shift["client_id"])
    if not client:
        return
    body = (
        f"You're confirmed to cover {client['name']} today, "
        f"{_format_time(shift['start_time'])}–{_format_time(shift['end_time'])}. "
        f"Address: {client['address']}. Notes: {client.get('care_notes') or 'n/a'}. "
        f"Thank you!"
    )
    send_sms(caregiver["phone"], body)


def send_family_notification(shift_id: int) -> None:
    shift = db.get_shift_by_id(shift_id)
    if not shift:
        return
    client = db.get_client_by_id(shift["client_id"])
    caregiver = db.get_caregiver_by_id(shift["caregiver_id"]) if shift["caregiver_id"] else None
    if not client or not caregiver or not client.get("family_phone"):
        return

    # Generate (or retrieve) the family portal token and build the link.
    token = db.create_family_token(shift_id)
    base_url = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")
    portal_link = f"{base_url}/client/{token}"

    body = (
        f"Update for {client['name']}: today's caregiver is {caregiver['name']}, "
        f"arriving by {_format_time(shift['start_time'])}. "
        f"See details: {portal_link}"
    )
    send_sms(client["family_phone"], body)


def send_owner_summary(shift_id: int, original_caregiver_name: str | None = None) -> None:
    owner = os.getenv("OWNER_PHONE")
    if not owner:
        return
    shift = db.get_shift_by_id(shift_id)
    if not shift:
        return
    client = db.get_client_by_id(shift["client_id"])
    caregiver = db.get_caregiver_by_id(shift["caregiver_id"]) if shift["caregiver_id"] else None
    if not client or not caregiver:
        return

    elapsed = ""
    started = _CLAIM_TIMESTAMPS.get(shift_id)
    if started:
        # The dict is filled at claim-time, so "elapsed since claim" is ~0.
        # We keep a placeholder string for the guide's "how long it took" line.
        elapsed = " (covered live)"

    out = (
        f"ShiftCare summary: {original_caregiver_name or 'caregiver'} called out for "
        f"{client['name']} ({_format_time(shift['start_time'])}). "
        f"Covered by {caregiver['name']}{elapsed}."
    )
    send_sms(owner, out)
