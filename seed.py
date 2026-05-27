"""Seed the database with fake DMV-area caregivers, clients, and today's shifts.

⚠️  Before running, edit the phone numbers below to use phones YOU control
    (or Twilio test numbers). The first caregiver phone should be your real
    phone, since you'll text the Twilio number from it to simulate a callout.

Usage:
    python seed.py            # wipes seed-y data and reseeds
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from db import create_tables, get_conn, normalize_phone

# ---------------------------------------------------------------------------
# Edit these to real phones you control before running an end-to-end test.
# For on-net Telnyx demo: set TELNYX_NUMBER_1 as Maria's phone and
# TELNYX_NUMBER_2 as Priya's phone. Both must be purchased in the Telnyx portal.
# TELNYX_FROM (agency sender) should be a THIRD Telnyx number, OR reuse
# TELNYX_NUMBER_1 as the agency number and test inbound via the Telnyx portal.
# ---------------------------------------------------------------------------
import os as _os
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(dotenv_path=_os.path.join(_os.path.dirname(__file__), ".env"), override=True)
_TELNYX_NUMBER_1 = _os.getenv("TELNYX_NUMBER_1", "+10000000001")  # Maria / canceling caregiver
_TELNYX_NUMBER_2 = _os.getenv("TELNYX_NUMBER_2", "+10000000002")  # Priya / coverage candidate

CAREGIVERS = [
    {
        "name": "Maria Johnson",
        "phone": _TELNYX_NUMBER_1,   # ← Telnyx Number 1 (texts agency to cancel)
        "zip_code": "20001",
        "availability_json": json.dumps({
            "mon": ["8am-5pm"], "tue": ["8am-5pm"], "wed": ["8am-5pm"],
            "thu": ["8am-5pm"], "fri": ["8am-5pm"],
        }),
        "certifications": "CNA,HHA",
        "active": 1,
    },
    {
        "name": "Tasha Williams",
        "phone": "+12025550102",   # ← placeholder (not used in demo)
        "zip_code": "20001",
        "availability_json": json.dumps({
            "mon": ["7am-7pm"], "tue": ["7am-7pm"], "wed": ["7am-7pm"],
            "thu": ["7am-7pm"], "fri": ["7am-7pm"], "sat": ["8am-2pm"],
        }),
        "certifications": "CNA",
        "active": 1,
    },
    {
        "name": "Devon Carter",
        "phone": "+12025550103",
        "zip_code": "20002",
        "availability_json": json.dumps({
            "mon": ["6am-2pm"], "tue": ["6am-2pm"], "wed": ["6am-2pm"],
            "thu": ["6am-2pm"], "fri": ["6am-2pm"],
        }),
        "certifications": "HHA",
        "active": 1,
    },
    {
        "name": "Linda Martinez",
        "phone": "+12025550104",
        "zip_code": "20910",      # Silver Spring, MD
        "availability_json": json.dumps({
            "mon": ["9am-6pm"], "wed": ["9am-6pm"], "fri": ["9am-6pm"],
            "sat": ["9am-6pm"], "sun": ["9am-6pm"],
        }),
        "certifications": "CNA,CPR",
        "active": 1,
    },
    {
        "name": "James O'Neill",
        "phone": "+12025550105",
        "zip_code": "22201",      # Arlington, VA
        "availability_json": json.dumps({
            "tue": ["8am-8pm"], "thu": ["8am-8pm"], "sat": ["8am-8pm"],
            "sun": ["8am-8pm"],
        }),
        "certifications": "HHA",
        "active": 1,
    },
    {
        "name": "Priya Patel",
        "phone": _TELNYX_NUMBER_2,  # ← Telnyx Number 2 (receives coverage request, replies YES)
        "zip_code": "20001",
        "availability_json": json.dumps({
            "mon": ["8am-6pm"], "tue": ["8am-6pm"], "wed": ["8am-6pm"],
            "thu": ["8am-6pm"], "fri": ["8am-6pm"],
            "sat": ["8am-6pm"], "sun": ["8am-6pm"],
        }),
        "certifications": "CNA,HHA,CPR",
        "active": 1,
    },
]

CLIENTS = [
    {
        "name": "Mr. Robert Hayes",
        "address": "1420 H St NE, Washington, DC",
        "zip_code": "20002",
        "family_phone": "+12025550201",
        "family_email": "hayes.family@example.com",
        "care_notes": "Mobility assist; meds at 10am; gentle reminder for hydration.",
        "required_certifications": "CNA",
    },
    {
        "name": "Mrs. Eleanor Brooks",
        "address": "88 Rhode Island Ave NW, Washington, DC",
        "zip_code": "20001",
        "family_phone": "+12025550202",
        "family_email": "brooks.daughter@example.com",
        "care_notes": "Early-stage dementia. Calm tone. Loves crosswords.",
        "required_certifications": "CNA",
    },
    {
        "name": "Mr. Frank DiLorenzo",
        "address": "212 12th St SE, Washington, DC",
        "zip_code": "20003",
        "family_phone": "+12025550203",
        "family_email": "dilorenzo.son@example.com",
        "care_notes": "Post-surgery recovery. No lifting >10 lbs. PT exercises 2pm.",
        "required_certifications": "HHA",
    },
    {
        "name": "Ms. Gloria Stewart",
        "address": "501 Wayne Ave, Silver Spring, MD",
        "zip_code": "20910",
        "family_phone": "+12025550204",
        "family_email": "stewart.family@example.com",
        "care_notes": "Diabetic. Glucose check at 11am and 3pm.",
        "required_certifications": "CNA,CPR",
    },
    {
        "name": "Mr. Charles Whitfield",
        "address": "2100 N Highland St, Arlington, VA",
        "zip_code": "22201",
        "family_phone": "+12025550205",
        "family_email": "whitfield.daughter@example.com",
        "care_notes": "Hard of hearing. Speak slowly. Cat named Biscuit, do not let out.",
        "required_certifications": "HHA",
    },
    {
        "name": "Mrs. Yolanda Reyes",
        "address": "915 Florida Ave NW, Washington, DC",
        "zip_code": "20001",
        "family_phone": "+12025550206",
        "family_email": "reyes.son@example.com",
        "care_notes": "Spanish/English bilingual. Loves music. Dinner prep at 5pm.",
        "required_certifications": "CNA",
    },
]


def _today_iso() -> str:
    return date.today().isoformat()


def wipe() -> None:
    with get_conn() as conn:
        for stmt in [
            "DELETE FROM shift_ratings",
            "DELETE FROM scheduling_suggestions",
            "DELETE FROM checkins",
            "DELETE FROM family_tokens",
            "DELETE FROM conversation_state",
            "DELETE FROM pending_coverage",
            "DELETE FROM shifts",
            "DELETE FROM clients",
            "DELETE FROM caregivers",
        ]:
            conn.execute(stmt)
        # Reset SQLite autoincrement counters (no-op on PG)
        try:
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('caregivers','clients','shifts','pending_coverage')"
            )
        except Exception:
            pass
        conn.commit()


def seed() -> None:
    create_tables()
    wipe()
    with get_conn() as conn:
        cur = conn.cursor()

        # Caregivers
        cg_ids: list[int] = []
        for cg in CAREGIVERS:
            cur.execute(
                """INSERT INTO caregivers
                   (name, phone, zip_code, availability_json, certifications, active)
                   VALUES (?,?,?,?,?,?)""",
                (
                    cg["name"],
                    normalize_phone(cg["phone"]),
                    cg["zip_code"],
                    cg["availability_json"],
                    cg["certifications"],
                    cg["active"],
                ),
            )
            cg_ids.append(cur.lastrowid)

        # Clients
        cl_ids: list[int] = []
        for cl in CLIENTS:
            cur.execute(
                """INSERT INTO clients
                   (name, address, zip_code, family_phone, family_email, care_notes,
                    required_certifications)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    cl["name"],
                    cl["address"],
                    cl["zip_code"],
                    normalize_phone(cl["family_phone"]),
                    cl["family_email"],
                    cl["care_notes"],
                    cl.get("required_certifications", ""),
                ),
            )
            cl_ids.append(cur.lastrowid)

        # Shifts — seed 7 days (today + 6 future days).
        # Each day rotates through all 6 (caregiver, client) pairs with
        # slightly different times so the schedule looks realistic.
        # Day 0 (today) keeps Maria → Mr. Hayes at 09:00 as the canonical
        # cancel-test shift.
        base_date = date.today()
        total_shifts = 0

        # Per-day templates: list of (cg_idx, cl_idx, start, end)
        # We cycle all 6 caregivers every day (availability permitting —
        # for seed purposes we ignore availability constraints).
        daily_templates = [
            # Day 0 — today (canonical layout for Phase 1 tests)
            [
                (0, 0, "09:00", "13:00"),
                (1, 1, "10:00", "15:00"),
                (2, 2, "07:00", "12:00"),
                (3, 3, "10:00", "16:00"),
                (4, 4, "12:00", "18:00"),
                (5, 5, "14:00", "18:00"),
            ],
            # Day 1
            [
                (1, 0, "08:00", "14:00"),
                (2, 1, "09:00", "13:00"),
                (3, 2, "11:00", "17:00"),
                (4, 3, "08:00", "12:00"),
                (5, 4, "13:00", "18:00"),
                (0, 5, "09:00", "15:00"),
            ],
            # Day 2
            [
                (2, 0, "10:00", "16:00"),
                (3, 1, "08:00", "12:00"),
                (4, 2, "09:00", "14:00"),
                (5, 3, "10:00", "15:00"),
                (0, 4, "07:00", "13:00"),
                (1, 5, "14:00", "18:00"),
            ],
            # Day 3
            [
                (3, 0, "09:00", "14:00"),
                (4, 1, "10:00", "16:00"),
                (5, 2, "08:00", "12:00"),
                (0, 3, "11:00", "17:00"),
                (1, 4, "09:00", "13:00"),
                (2, 5, "13:00", "18:00"),
            ],
            # Day 4
            [
                (4, 0, "08:00", "13:00"),
                (5, 1, "09:00", "15:00"),
                (0, 2, "10:00", "16:00"),
                (1, 3, "08:00", "12:00"),
                (2, 4, "11:00", "17:00"),
                (3, 5, "14:00", "18:00"),
            ],
            # Day 5
            [
                (5, 0, "09:00", "13:00"),
                (0, 1, "10:00", "15:00"),
                (1, 2, "08:00", "12:00"),
                (2, 3, "09:00", "14:00"),
                (3, 4, "11:00", "17:00"),
                (4, 5, "13:00", "18:00"),
            ],
            # Day 6
            [
                (0, 0, "10:00", "14:00"),
                (1, 1, "09:00", "13:00"),
                (2, 2, "08:00", "12:00"),
                (3, 3, "11:00", "16:00"),
                (4, 4, "13:00", "18:00"),
                (5, 5, "09:00", "15:00"),
            ],
        ]

        for day_offset, plan in enumerate(daily_templates):
            shift_date = (base_date + timedelta(days=day_offset)).isoformat()
            for cg_idx, cl_idx, start, end in plan:
                cur.execute(
                    """INSERT INTO shifts
                       (caregiver_id, client_id, date, start_time, end_time, status)
                       VALUES (?,?,?,?,?, 'scheduled')""",
                    (cg_ids[cg_idx], cl_ids[cl_idx], shift_date, start, end),
                )
                total_shifts += 1

        conn.commit()

    print("✅ Seeded:")
    print(f"   {len(CAREGIVERS)} caregivers")
    print(f"   {len(CLIENTS)} clients")
    print(f"   {total_shifts} shifts across 7 days ({_today_iso()} to {(date.today() + timedelta(days=6)).isoformat()})")


if __name__ == "__main__":
    seed()
