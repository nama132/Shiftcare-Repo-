"""SQLite (dev) + PostgreSQL (production) database helpers for ShiftCare.

When DATABASE_URL is set (Railway/Render), psycopg2 is used automatically.
All existing query helpers use ? placeholders — the PgWrapper transparently
converts them to %s so no queries need to change.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date as date_cls, datetime, time as time_cls, timedelta
from typing import Any, Iterable

DB_PATH = os.path.join(os.path.dirname(__file__), "shiftcare.db")

# Railway injects postgres:// — psycopg2 needs postgresql://
_DATABASE_URL = os.getenv("DATABASE_URL", "")
if _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql://", 1)
_USE_PG = bool(_DATABASE_URL)


# ---------------------------------------------------------------------------
# PostgreSQL thin wrapper — makes psycopg2 behave like sqlite3
# ---------------------------------------------------------------------------

class _PgCursor:
    """Cursor wrapper: converts ? → %s and returns dicts."""
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql: str, params=None):
        sql = sql.replace("?", "%s")
        if params is None:
            self._cur.execute(sql)
        else:
            self._cur.execute(sql, params)
        return self

    def executemany(self, sql: str, seq):
        sql = sql.replace("?", "%s")
        self._cur.executemany(sql, seq)

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._cur.description]
        return dict(zip(cols, row))

    def fetchall(self):
        cols = [d[0] for d in self._cur.description]
        return [dict(zip(cols, r)) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        # PostgreSQL doesn't have lastrowid — use RETURNING or fetchone
        return getattr(self._cur, "lastrowid", None)


class _PgConn:
    """Connection wrapper that returns _PgCursor and proxies commit/close."""
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _PgCursor(self._conn.cursor())

    def execute(self, sql: str, params=None):
        cur = _PgCursor(self._conn.cursor())
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


# ----------------------------------------------------------------------------
# Connection helpers
# ----------------------------------------------------------------------------

def get_conn():
    """Return a DB connection (SQLite locally, PostgreSQL in production)."""
    if _USE_PG:
        import psycopg2
        raw = psycopg2.connect(_DATABASE_URL)
        raw.autocommit = False
        return _PgConn(raw)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    return dict(row)


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------------

def create_tables() -> None:
    """Create all tables if they don't already exist."""
    # Use SERIAL for PostgreSQL auto-increment, INTEGER PRIMARY KEY AUTOINCREMENT for SQLite
    with get_conn() as conn:
        cur = conn.cursor()
        # Choose the correct auto-increment syntax based on database type
        if _USE_PG:
            # PostgreSQL syntax
            statements = [
                """
                CREATE TABLE IF NOT EXISTS caregivers (
                    id                 SERIAL PRIMARY KEY,
                    name               TEXT NOT NULL,
                    phone              TEXT NOT NULL UNIQUE,
                    zip_code           TEXT,
                    availability_json  TEXT,
                    certifications     TEXT,
                    active             INTEGER NOT NULL DEFAULT 1
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS clients (
                    id                      SERIAL PRIMARY KEY,
                    name                    TEXT NOT NULL,
                    address                 TEXT,
                    zip_code                TEXT,
                    family_phone            TEXT,
                    family_email            TEXT,
                    care_notes              TEXT,
                    required_certifications TEXT
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS shifts (
                    id            SERIAL PRIMARY KEY,
                    caregiver_id  INTEGER,
                    client_id     INTEGER NOT NULL,
                    date          TEXT NOT NULL,
                    start_time    TEXT NOT NULL,
                    end_time      TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'scheduled',
                    FOREIGN KEY (caregiver_id) REFERENCES caregivers(id),
                    FOREIGN KEY (client_id)    REFERENCES clients(id)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS pending_coverage (
                    id            SERIAL PRIMARY KEY,
                    shift_id      INTEGER NOT NULL,
                    caregiver_id  INTEGER NOT NULL,
                    phone         TEXT NOT NULL,
                    requested_at  TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'pending',
                    FOREIGN KEY (shift_id)     REFERENCES shifts(id),
                    FOREIGN KEY (caregiver_id) REFERENCES caregivers(id)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS conversation_state (
                    phone       TEXT PRIMARY KEY,
                    state       TEXT NOT NULL,
                    data_json   TEXT NOT NULL DEFAULT '{}',
                    updated_at  TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS family_tokens (
                    token       TEXT PRIMARY KEY,
                    shift_id    INTEGER NOT NULL UNIQUE,
                    created_at  TEXT NOT NULL,
                    FOREIGN KEY (shift_id) REFERENCES shifts(id)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS contact_submissions (
                    id           SERIAL PRIMARY KEY,
                    name         TEXT NOT NULL,
                    email        TEXT NOT NULL,
                    agency       TEXT,
                    message      TEXT NOT NULL,
                    created_at   TEXT NOT NULL
                )
                """,
                "CREATE INDEX IF NOT EXISTS idx_shifts_date ON shifts(date)",
                "CREATE INDEX IF NOT EXISTS idx_pending_phone ON pending_coverage(phone)",
                "CREATE INDEX IF NOT EXISTS idx_pending_shift ON pending_coverage(shift_id)",
            ]
        else:
            # SQLite syntax
            statements = [
                """
                CREATE TABLE IF NOT EXISTS caregivers (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    name               TEXT NOT NULL,
                    phone              TEXT NOT NULL UNIQUE,
                    zip_code           TEXT,
                    availability_json  TEXT,
                    certifications     TEXT,
                    active             INTEGER NOT NULL DEFAULT 1
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS clients (
                    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                    name                    TEXT NOT NULL,
                    address                 TEXT,
                    zip_code                TEXT,
                    family_phone            TEXT,
                    family_email            TEXT,
                    care_notes              TEXT,
                    required_certifications TEXT
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS shifts (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    caregiver_id  INTEGER,
                    client_id     INTEGER NOT NULL,
                    date          TEXT NOT NULL,
                    start_time    TEXT NOT NULL,
                    end_time      TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'scheduled',
                    FOREIGN KEY (caregiver_id) REFERENCES caregivers(id),
                    FOREIGN KEY (client_id)    REFERENCES clients(id)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS pending_coverage (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    shift_id      INTEGER NOT NULL,
                    caregiver_id  INTEGER NOT NULL,
                    phone         TEXT NOT NULL,
                    requested_at  TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'pending',
                    FOREIGN KEY (shift_id)     REFERENCES shifts(id),
                    FOREIGN KEY (caregiver_id) REFERENCES caregivers(id)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS conversation_state (
                    phone       TEXT PRIMARY KEY,
                    state       TEXT NOT NULL,
                    data_json   TEXT NOT NULL DEFAULT '{}',
                    updated_at  TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS family_tokens (
                    token       TEXT PRIMARY KEY,
                    shift_id    INTEGER NOT NULL UNIQUE,
                    created_at  TEXT NOT NULL,
                    FOREIGN KEY (shift_id) REFERENCES shifts(id)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS contact_submissions (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    name         TEXT NOT NULL,
                    email        TEXT NOT NULL,
                    agency       TEXT,
                    message      TEXT NOT NULL,
                    created_at   TEXT NOT NULL
                )
                """,
                "CREATE INDEX IF NOT EXISTS idx_shifts_date ON shifts(date)",
                "CREATE INDEX IF NOT EXISTS idx_pending_phone ON pending_coverage(phone)",
                "CREATE INDEX IF NOT EXISTS idx_pending_shift ON pending_coverage(shift_id)",
            ]
        if _USE_PG:
            statements.append("""
                CREATE TABLE IF NOT EXISTS users (
                    id             SERIAL PRIMARY KEY,
                    agency_name    TEXT NOT NULL,
                    username       TEXT NOT NULL UNIQUE,
                    email          TEXT NOT NULL UNIQUE,
                    phone          TEXT,
                    password_hash  TEXT NOT NULL,
                    role           TEXT NOT NULL DEFAULT 'admin',
                    created_at     TEXT,
                    last_login     TEXT
                )
            """)
        else:
            statements.append("""
                CREATE TABLE IF NOT EXISTS users (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    agency_name    TEXT NOT NULL,
                    username       TEXT NOT NULL UNIQUE,
                    email          TEXT NOT NULL UNIQUE,
                    phone          TEXT,
                    password_hash  TEXT NOT NULL,
                    role           TEXT NOT NULL DEFAULT 'admin',
                    created_at     TEXT,
                    last_login     TEXT
                )
            """)
        for stmt in statements:
            cur.execute(stmt)
        conn.commit()
    # Migration: add required_certifications to existing DBs that predate this column
    with get_conn() as conn:
        try:
            conn.execute("ALTER TABLE clients ADD COLUMN required_certifications TEXT")
            conn.commit()
        except Exception:
            # Column already exists, skip migration
            pass


def save_contact_submission(name: str, email: str, agency: str, message: str) -> None:
    """Persist a landing-page contact form submission."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO contact_submissions (name, email, agency, message, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, email, agency, message, datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_contact_submissions(limit: int = 100) -> list[dict]:
    """Return recent contact form submissions, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM contact_submissions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return _rows_to_dicts(rows)


def get_user_by_username(username: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=? LIMIT 1", (username,)).fetchone()
    return _row_to_dict(row)


def username_exists(username: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE username=? LIMIT 1", (username,)).fetchone()
    return row is not None


def email_exists(email: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE email=? LIMIT 1", (email,)).fetchone()
    return row is not None


def create_user(agency_name: str, username: str, email: str, phone: str, password_hash: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (agency_name, username, email, phone, password_hash, role, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (agency_name, username, email, phone, password_hash, "admin", datetime.utcnow().isoformat()),
        )
        conn.commit()


def update_last_login(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_login=? WHERE id=?",
                     (datetime.utcnow().isoformat(), user_id))
        conn.commit()


def get_coverage_history(caregiver_id: int, client_id: int) -> int:
    """Return how many times this caregiver has successfully covered this client."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM pending_coverage pc
            JOIN shifts s ON s.id = pc.shift_id
            WHERE pc.caregiver_id = ?
              AND s.client_id = ?
              AND pc.status = 'claimed'
            """,
            (caregiver_id, client_id),
        ).fetchone()
    return row["cnt"] if row else 0


# ----------------------------------------------------------------------------
# Phone normalization
# ----------------------------------------------------------------------------

def normalize_phone(phone: str | None) -> str:
    """Strip everything except digits and a leading +. Twilio gives us E.164."""
    if not phone:
        return ""
    phone = phone.strip()
    if phone.startswith("+"):
        return "+" + "".join(ch for ch in phone[1:] if ch.isdigit())
    digits = "".join(ch for ch in phone if ch.isdigit())
    # Assume US if 10 digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else ""


# ----------------------------------------------------------------------------
# Caregiver queries
# ----------------------------------------------------------------------------

def get_caregiver_by_phone(phone: str) -> dict | None:
    norm = normalize_phone(phone)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM caregivers WHERE phone = ? LIMIT 1", (norm,)
        ).fetchone()
    return _row_to_dict(row)


def get_caregiver_by_id(caregiver_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM caregivers WHERE id = ? LIMIT 1", (caregiver_id,)
        ).fetchone()
    return _row_to_dict(row)


def get_all_active_caregivers() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM caregivers WHERE active = 1 ORDER BY name"
        ).fetchall()
    return _rows_to_dicts(rows)


# ----------------------------------------------------------------------------
# Client queries
# ----------------------------------------------------------------------------

def get_client_by_id(client_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE id = ? LIMIT 1", (client_id,)
        ).fetchone()
    return _row_to_dict(row)


# ----------------------------------------------------------------------------
# Shift queries
# ----------------------------------------------------------------------------

def get_shift_by_id(shift_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM shifts WHERE id = ? LIMIT 1", (shift_id,)
        ).fetchone()
    return _row_to_dict(row)


def get_shift_by_caregiver_and_date(caregiver_id: int, date: str) -> dict | None:
    """Find a caregiver's shift on a given date (YYYY-MM-DD)."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM shifts
            WHERE caregiver_id = ? AND date = ?
              AND status IN ('scheduled', 'active')
            ORDER BY start_time ASC
            LIMIT 1
            """,
            (caregiver_id, date),
        ).fetchone()
    return _row_to_dict(row)


def get_shifts_by_caregiver_and_date(caregiver_id: int, date: str) -> list[dict]:
    """Return ALL scheduled/active shifts for a caregiver on a given date."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM shifts
            WHERE caregiver_id = ? AND date = ?
              AND status IN ('scheduled', 'active')
            ORDER BY start_time ASC
            """,
            (caregiver_id, date),
        ).fetchall()
    return _rows_to_dicts(rows)


def get_shifts_for_date_range(start_date: str, end_date: str) -> list[dict]:
    """Return all shifts joined with names between start_date and end_date (inclusive)."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.date, s.start_time, s.end_time, s.status,
                   s.caregiver_id, s.client_id,
                   COALESCE(cg.name, '— unassigned —') AS caregiver_name,
                   cl.name AS client_name
            FROM shifts s
            LEFT JOIN caregivers cg ON cg.id = s.caregiver_id
            JOIN clients cl ON cl.id = s.client_id
            WHERE s.date BETWEEN ? AND ?
            ORDER BY s.date ASC, s.start_time ASC
            """,
            (start_date, end_date),
        ).fetchall()
    return _rows_to_dicts(rows)


def get_shifts_for_date(date: str) -> list[dict]:
    """Return all shifts on a date joined with caregiver + client names."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.date, s.start_time, s.end_time, s.status,
                   s.caregiver_id, s.client_id,
                   COALESCE(cg.name, '— unassigned —') AS caregiver_name,
                   cl.name AS client_name
            FROM shifts s
            LEFT JOIN caregivers cg ON cg.id = s.caregiver_id
            JOIN clients cl ON cl.id = s.client_id
            WHERE s.date = ?
            ORDER BY s.start_time ASC
            """,
            (date,),
        ).fetchall()
    return _rows_to_dicts(rows)


def update_shift_status(
    shift_id: int, status: str, new_caregiver_id: int | None = None
) -> None:
    """Update status and optionally reassign the caregiver."""
    with get_conn() as conn:
        if new_caregiver_id is not None:
            conn.execute(
                "UPDATE shifts SET status = ?, caregiver_id = ? WHERE id = ?",
                (status, new_caregiver_id, shift_id),
            )
        else:
            conn.execute(
                "UPDATE shifts SET status = ? WHERE id = ?",
                (status, shift_id),
            )
        conn.commit()


# ----------------------------------------------------------------------------
# Availability + conflict checks
# ----------------------------------------------------------------------------

_DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _day_key(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return _DAY_KEYS[d.weekday()]


def _parse_window(window: str) -> tuple[time_cls, time_cls] | None:
    """Parse '8am-5pm' / '08:00-17:00' into (start_time, end_time)."""
    try:
        start_str, end_str = [w.strip() for w in window.split("-")]
        return _parse_time(start_str), _parse_time(end_str)
    except Exception:
        return None


def _parse_time(t: str) -> time_cls:
    t = t.strip().lower().replace(" ", "")
    if t.endswith("am") or t.endswith("pm"):
        suffix = t[-2:]
        body = t[:-2]
        if ":" in body:
            h, m = body.split(":")
        else:
            h, m = body, "0"
        h, m = int(h), int(m)
        if suffix == "pm" and h != 12:
            h += 12
        if suffix == "am" and h == 12:
            h = 0
        return time_cls(h, m)
    # 24h "HH:MM"
    h, m = t.split(":")
    return time_cls(int(h), int(m))


def _shift_overlaps_window(
    shift_start: str, shift_end: str, win_start: time_cls, win_end: time_cls
) -> bool:
    s = _parse_time(shift_start)
    e = _parse_time(shift_end)
    # Caregiver must be available for the ENTIRE shift window.
    return win_start <= s and win_end >= e


def _times_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    a_s, a_e = _parse_time(a_start), _parse_time(a_end)
    b_s, b_e = _parse_time(b_start), _parse_time(b_end)
    return a_s < b_e and b_s < a_e


def get_available_caregivers(
    date: str,
    start_time: str,
    end_time: str,
    exclude_id: int | None = None,
    preferred_zip: str | None = None,
    required_certifications: str | None = None,
    client_id: int | None = None,
    max_shifts_per_day: int = 2,
) -> list[dict]:
    """Return active caregivers free for [start_time, end_time] on `date`.

    Filters:
      - active = 1
      - excludes `exclude_id`
      - availability_json must cover the shift window on that weekday
      - no overlapping shift that day
      - fewer than max_shifts_per_day shifts already assigned that day

    Scored (descending) by:
      +30  zip_code matches preferred_zip
      +20  per required certification the caregiver holds
      + 5  per past covered shift for this client
      + 2  caregiver holds extra certs beyond what's required (generalist bonus)
    """
    day = _day_key(date)
    candidates = get_all_active_caregivers()
    required_certs: list[str] = [
        c.strip().upper()
        for c in (required_certifications or "").split(",")
        if c.strip()
    ]
    scored: list[tuple[int, dict]] = []

    with get_conn() as conn:
        for cg in candidates:
            if exclude_id is not None and cg["id"] == exclude_id:
                continue

            # 1. Availability window check
            try:
                avail = json.loads(cg["availability_json"] or "{}")
            except json.JSONDecodeError:
                avail = {}
            windows = avail.get(day, [])
            covers = False
            for w in windows:
                parsed = _parse_window(w)
                if parsed and _shift_overlaps_window(
                    start_time, end_time, parsed[0], parsed[1]
                ):
                    covers = True
                    break
            if not covers:
                continue

            # 2. Fetch all of the caregiver's shifts that day
            day_shifts = conn.execute(
                """
                SELECT start_time, end_time FROM shifts
                WHERE caregiver_id = ? AND date = ?
                  AND status IN ('scheduled', 'active', 'covered')
                """,
                (cg["id"], date),
            ).fetchall()

            # 3. Max shifts per day guard
            if len(day_shifts) >= max_shifts_per_day:
                continue

            # 4. No time conflict
            if any(
                _times_overlap(start_time, end_time, c["start_time"], c["end_time"])
                for c in day_shifts
            ):
                continue

            # 5. Compute score
            score = 0
            cg_certs = [
                c.strip().upper()
                for c in (cg.get("certifications") or "").split(",")
                if c.strip()
            ]

            # Zip proximity
            if preferred_zip and cg.get("zip_code") == preferred_zip:
                score += 30

            # Certification match
            matched = sum(1 for rc in required_certs if rc in cg_certs)
            score += matched * 20

            # Generalist bonus (holds certs beyond what's required)
            extra = len([c for c in cg_certs if c not in required_certs])
            score += extra * 2

            # Past coverage history for this client
            if client_id:
                score += get_coverage_history(cg["id"], client_id) * 5

            scored.append((score, cg))

    # Sort by score descending, then name alphabetically as tiebreaker
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    return [cg for _, cg in scored]


# ----------------------------------------------------------------------------
# Pending coverage tracker
# ----------------------------------------------------------------------------

def add_pending_candidate(shift_id: int, caregiver_id: int, phone: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO pending_coverage (shift_id, caregiver_id, phone, requested_at, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (shift_id, caregiver_id, normalize_phone(phone), datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_pending_shift_for_phone(phone: str) -> int | None:
    """Return the shift_id this phone was most recently asked to cover, if still open."""
    norm = normalize_phone(phone)
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT pc.shift_id
            FROM pending_coverage pc
            JOIN shifts s ON s.id = pc.shift_id
            WHERE pc.phone = ?
              AND pc.status = 'pending'
              AND s.status = 'uncovered'
            ORDER BY pc.requested_at DESC
            LIMIT 1
            """,
            (norm,),
        ).fetchone()
    return row["shift_id"] if row else None


def get_pending_candidates_for_shift(shift_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_coverage WHERE shift_id = ?",
            (shift_id,),
        ).fetchall()
    return _rows_to_dicts(rows)


def mark_candidate_status(phone: str, shift_id: int, status: str) -> None:
    norm = normalize_phone(phone)
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE pending_coverage SET status = ?
            WHERE phone = ? AND shift_id = ? AND status = 'pending'
            """,
            (status, norm, shift_id),
        )
        conn.commit()


def expire_pending_for_shift(shift_id: int) -> None:
    """Mark every still-pending candidate for this shift as expired (someone else won)."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE pending_coverage SET status = 'expired'
            WHERE shift_id = ? AND status = 'pending'
            """,
            (shift_id,),
        )
        conn.commit()


# ----------------------------------------------------------------------------
# Date helpers
# ----------------------------------------------------------------------------

def today() -> str:
    return date_cls.today().isoformat()


_WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def resolve_shift_date(raw: str | None) -> str:
    """Convert a natural-language date string from the AI parser into YYYY-MM-DD.

    Handles:
      - None / "today"          → today
      - "tomorrow"              → tomorrow
      - weekday name ("friday") → the *next* occurrence of that weekday
                                   (if today is already that day, returns today)
      - ISO date "YYYY-MM-DD"  → pass-through after validation
      - anything else           → today (safe fallback)
    """
    base = date_cls.today()
    if not raw:
        return base.isoformat()
    raw = raw.strip().lower()
    if raw in ("today", ""):
        return base.isoformat()
    if raw == "tomorrow":
        return (base + timedelta(days=1)).isoformat()
    # Weekday name
    for idx, name in enumerate(_WEEKDAY_NAMES):
        if raw == name or raw == name[:3]:  # "fri" or "friday"
            days_ahead = (idx - base.weekday()) % 7
            # If days_ahead == 0, it's today — keep today
            target = base + timedelta(days=days_ahead)
            return target.isoformat()
    # Try ISO parse
    try:
        parsed = date_cls.fromisoformat(raw)
        return parsed.isoformat()
    except ValueError:
        pass
    # Safe fallback
    return base.isoformat()


# ----------------------------------------------------------------------------
# Conversation state (Phase 5 — two-way memory)
# ----------------------------------------------------------------------------

CONV_TTL_MINUTES = 30


def upsert_conversation(phone: str, state: str, data: dict) -> None:
    """Create or overwrite a conversation session for this phone number."""
    norm = normalize_phone(phone)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO conversation_state (phone, state, data_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE
               SET state=excluded.state,
                   data_json=excluded.data_json,
                   updated_at=excluded.updated_at
            """,
            (norm, state, json.dumps(data), datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_conversation(phone: str) -> dict | None:
    """Return the active conversation for this phone if it's within the TTL."""
    norm = normalize_phone(phone)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM conversation_state WHERE phone = ?", (norm,)
        ).fetchone()
    if not row:
        return None
    # Check TTL
    try:
        updated = datetime.fromisoformat(row["updated_at"])
        age_minutes = (datetime.utcnow() - updated).total_seconds() / 60
        if age_minutes > CONV_TTL_MINUTES:
            clear_conversation(phone)
            return None
    except Exception:
        return None
    return {
        "phone": row["phone"],
        "state": row["state"],
        "data": json.loads(row["data_json"] or "{}"),
    }


def clear_conversation(phone: str) -> None:
    """Delete the conversation session for this phone."""
    norm = normalize_phone(phone)
    with get_conn() as conn:
        conn.execute("DELETE FROM conversation_state WHERE phone = ?", (norm,))
        conn.commit()


# ----------------------------------------------------------------------------
# Family portal tokens (Phase 6)
# ----------------------------------------------------------------------------

import uuid as _uuid


def create_family_token(shift_id: int) -> str:
    """Generate a unique token for the family portal for this shift.
    If a token already exists for this shift, return it (idempotent)."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT token FROM family_tokens WHERE shift_id = ?", (shift_id,)
        ).fetchone()
        if existing:
            return existing["token"]
        token = str(_uuid.uuid4())
        conn.execute(
            "INSERT INTO family_tokens (token, shift_id, created_at) VALUES (?, ?, ?)",
            (token, shift_id, datetime.utcnow().isoformat()),
        )
        conn.commit()
    return token


def get_shift_by_token(token: str) -> dict | None:
    """Return shift + caregiver + client info for a family portal token."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                s.id AS shift_id, s.date, s.start_time, s.end_time, s.status,
                cg.name   AS caregiver_name, cg.phone AS caregiver_phone,
                cl.name   AS client_name,    cl.address, cl.care_notes,
                cl.zip_code
            FROM family_tokens ft
            JOIN shifts      s  ON s.id  = ft.shift_id
            LEFT JOIN caregivers cg ON cg.id = s.caregiver_id
            JOIN clients     cl ON cl.id = s.client_id
            WHERE ft.token = ?
            """,
            (token,),
        ).fetchone()
    return _row_to_dict(row)


if __name__ == "__main__":
    create_tables()
    print(f"Tables created at {DB_PATH}")


# ----------------------------------------------------------------------------
# Admin helper — raw connection
# ----------------------------------------------------------------------------

def _con() -> sqlite3.Connection:
    return get_conn()


# ----------------------------------------------------------------------------
# Admin — Caregivers CRUD
# ----------------------------------------------------------------------------

def get_all_caregivers() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM caregivers ORDER BY name").fetchall()
    return _rows_to_dicts(rows)


def upsert_caregiver(data) -> None:
    cid = data.get("id")
    name = data.get("name", "").strip()
    phone = normalize_phone(data.get("phone", ""))
    zip_code = data.get("zip_code", data.get("zip", "")).strip()
    active = 1 if data.get("available") else 0
    with get_conn() as conn:
        if cid:
            conn.execute(
                "UPDATE caregivers SET name=?, phone=?, zip_code=?, active=? WHERE id=?",
                (name, phone, zip_code, active, int(cid)),
            )
        else:
            conn.execute(
                "INSERT INTO caregivers (name, phone, zip_code, active) VALUES (?,?,?,?)",
                (name, phone, zip_code, active),
            )
        conn.commit()


def delete_caregiver(cid: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM caregivers WHERE id=?", (cid,))
        conn.commit()


# ----------------------------------------------------------------------------
# Admin — Clients CRUD
# ----------------------------------------------------------------------------

def get_all_clients() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM clients ORDER BY name").fetchall()
    return _rows_to_dicts(rows)


def upsert_client(data) -> None:
    cid = data.get("id")
    name = data.get("name", "").strip()
    phone = normalize_phone(data.get("phone", data.get("family_phone", "")))
    address = data.get("address", "").strip()
    zip_code = data.get("zip_code", data.get("zip", "")).strip()
    with get_conn() as conn:
        if cid:
            conn.execute(
                "UPDATE clients SET name=?, family_phone=?, address=?, zip_code=? WHERE id=?",
                (name, phone, address, zip_code, int(cid)),
            )
        else:
            conn.execute(
                "INSERT INTO clients (name, family_phone, address, zip_code) VALUES (?,?,?,?)",
                (name, phone, address, zip_code),
            )
        conn.commit()


def delete_client(cid: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM clients WHERE id=?", (cid,))
        conn.commit()


# ----------------------------------------------------------------------------
# Admin — Shifts CRUD
# ----------------------------------------------------------------------------

def create_shift(data) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO shifts
               (date, start_time, end_time, caregiver_id, client_id, status)
               VALUES (?,?,?,?,?,?)""",
            (
                data.get("date"),
                data.get("time"),
                data.get("end_time"),
                int(data.get("caregiver_id")),
                int(data.get("client_id")),
                data.get("status", "scheduled"),
            ),
        )
        conn.commit()


def update_shift(data) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE shifts
               SET date=?, start_time=?, end_time=?, caregiver_id=?, client_id=?, status=?
               WHERE id=?""",
            (
                data.get("date"),
                data.get("time"),
                data.get("end_time"),
                int(data.get("caregiver_id")),
                int(data.get("client_id")),
                data.get("status", "scheduled"),
                int(data.get("id")),
            ),
        )
        conn.commit()


def delete_shift(sid: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM shifts WHERE id=?", (sid,))
        conn.commit()


# ----------------------------------------------------------------------------
# Admin — Coverage Log
# ----------------------------------------------------------------------------

def get_coverage_log(date: str | None = None) -> list[dict]:
    query = """
        SELECT
            s.date,
            s.start_time || ' - ' || s.end_time AS time,
            cg_orig.name  AS original_caregiver,
            cl.name       AS client_name,
            cg_cand.name  AS candidate_name,
            pc.status
        FROM pending_coverage pc
        JOIN shifts      s        ON s.id         = pc.shift_id
        LEFT JOIN caregivers cg_orig ON cg_orig.id = s.caregiver_id
        LEFT JOIN clients    cl      ON cl.id       = s.client_id
        LEFT JOIN caregivers cg_cand ON cg_cand.id  = pc.caregiver_id
    """
    with get_conn() as conn:
        if date:
            rows = conn.execute(
                query + " WHERE s.date=? ORDER BY s.date DESC, s.start_time", (date,)
            ).fetchall()
        else:
            rows = conn.execute(
                query + " ORDER BY s.date DESC, s.start_time"
            ).fetchall()
    return _rows_to_dicts(rows)

# ----------------------------------------------------------------------------
# Employee portal — accounts, OTP, clock-in/out, pay, availability
# ----------------------------------------------------------------------------

def _ensure_employee_tables() -> None:
    pg = _USE_PG
    id_col = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    stmts = [
        f"""CREATE TABLE IF NOT EXISTS employee_accounts (
            id              {id_col},
            caregiver_id    INTEGER NOT NULL UNIQUE,
            password_hash   TEXT NOT NULL,
            portal_enabled  INTEGER NOT NULL DEFAULT 1,
            last_login      TEXT,
            FOREIGN KEY (caregiver_id) REFERENCES caregivers(id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS otp_codes (
            id          {id_col},
            phone       TEXT NOT NULL,
            otp_code    TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            attempts    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS clock_events (
            id          {id_col},
            shift_id    INTEGER NOT NULL,
            caregiver_id INTEGER NOT NULL,
            event_type  TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS pay_periods (
            id           {id_col},
            period_start TEXT NOT NULL,
            period_end   TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'open'
        )""",
    ]
    with get_conn() as conn:
        cur = conn.cursor()
        for s in stmts:
            cur.execute(s)
        conn.commit()

_ensure_employee_tables()


def employee_account_exists(caregiver_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM employee_accounts WHERE caregiver_id=? LIMIT 1", (caregiver_id,)).fetchone()
    return row is not None


def get_employee_account(caregiver_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM employee_accounts WHERE caregiver_id=? LIMIT 1", (caregiver_id,)).fetchone()
    return _row_to_dict(row)


def create_employee_account(caregiver_id: int, password_hash: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO employee_accounts (caregiver_id, password_hash, portal_enabled) VALUES (?,?,1)", (caregiver_id, password_hash))
        conn.commit()


def update_employee_password(caregiver_id: int, password_hash: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE employee_accounts SET password_hash=? WHERE caregiver_id=?", (password_hash, caregiver_id))
        conn.commit()


def set_employee_portal_enabled(caregiver_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE employee_accounts SET portal_enabled=? WHERE caregiver_id=?", (1 if enabled else 0, caregiver_id))
        conn.commit()


def update_employee_last_login(caregiver_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE employee_accounts SET last_login=? WHERE caregiver_id=?", (datetime.utcnow().isoformat(), caregiver_id))
        conn.commit()


def get_employee_by_phone(phone: str) -> dict | None:
    cg = get_caregiver_by_phone(phone)
    if not cg:
        return None
    acct = get_employee_account(cg["id"])
    if not acct:
        return None
    return {**cg, **acct}


def save_otp(phone: str, otp_code: str, expires_at: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM otp_codes WHERE phone=?", (phone,))
        conn.execute("INSERT INTO otp_codes (phone, otp_code, expires_at, attempts, created_at) VALUES (?,?,?,0,?)", (phone, otp_code, expires_at, datetime.utcnow().isoformat()))
        conn.commit()


def get_otp(phone: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM otp_codes WHERE phone=? ORDER BY created_at DESC LIMIT 1", (phone,)).fetchone()
    return _row_to_dict(row)


def delete_otp(phone: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM otp_codes WHERE phone=?", (phone,))
        conn.commit()


def increment_otp_attempts(phone: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE otp_codes SET attempts=attempts+1 WHERE phone=?", (phone,))
        conn.commit()


def clock_in(shift_id: int, caregiver_id: int) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO clock_events (shift_id, caregiver_id, event_type, recorded_at) VALUES (?,?,?,?)", (shift_id, caregiver_id, "clock_in", datetime.utcnow().isoformat()))
        conn.commit()


def clock_out(shift_id: int, caregiver_id: int) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO clock_events (shift_id, caregiver_id, event_type, recorded_at) VALUES (?,?,?,?)", (shift_id, caregiver_id, "clock_out", datetime.utcnow().isoformat()))
        conn.commit()


def is_clocked_in(shift_id: int, caregiver_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT event_type FROM clock_events WHERE shift_id=? AND caregiver_id=? ORDER BY recorded_at DESC LIMIT 1", (shift_id, caregiver_id)).fetchone()
    return row is not None and row["event_type"] == "clock_in"


def get_clock_events_for_shift(shift_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM clock_events WHERE shift_id=? ORDER BY recorded_at", (shift_id,)).fetchall()
    return _rows_to_dicts(rows)


def _calc_shift_hours(shift: dict) -> float:
    try:
        from datetime import datetime as _dt
        start = _dt.strptime(shift["start_time"], "%H:%M")
        end = _dt.strptime(shift["end_time"], "%H:%M")
        return round(max(0, (end - start).total_seconds() / 3600), 2)
    except Exception:
        return 0.0


def get_caregiver_pay_rate(caregiver_id: int) -> float:
    return 20.0


def get_today_shift_for_employee(caregiver_id: int) -> dict | None:
    td = today()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM shifts WHERE caregiver_id=? AND date=? AND status NOT IN ('cancelled') LIMIT 1", (caregiver_id, td)).fetchone()
    return _row_to_dict(row)


def get_upcoming_shifts_for_employee(caregiver_id: int, limit: int = 10) -> list[dict]:
    td = today()
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM shifts WHERE caregiver_id=? AND date>=? ORDER BY date, start_time LIMIT ?", (caregiver_id, td, limit)).fetchall()
    return _rows_to_dicts(rows)


def get_shifts_for_caregiver_range(caregiver_id: int, start: str, end: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM shifts WHERE caregiver_id=? AND date>=? AND date<=? ORDER BY date, start_time", (caregiver_id, start, end)).fetchall()
    return _rows_to_dicts(rows)


def get_week_stats(caregiver_id: int) -> dict:
    import datetime as _d
    td = _d.date.today()
    week_start = (td - _d.timedelta(days=td.weekday())).isoformat()
    shifts = get_shifts_for_caregiver_range(caregiver_id, week_start, td.isoformat())
    rate = get_caregiver_pay_rate(caregiver_id)
    hours = sum(_calc_shift_hours(s) for s in shifts)
    return {"hours": round(hours, 2), "earnings": round(hours * rate, 2), "shifts": len(shifts)}


def get_month_stats(caregiver_id: int) -> dict:
    import datetime as _d
    td = _d.date.today()
    shifts = get_shifts_for_caregiver_range(caregiver_id, td.replace(day=1).isoformat(), td.isoformat())
    rate = get_caregiver_pay_rate(caregiver_id)
    hours = sum(_calc_shift_hours(s) for s in shifts)
    return {"hours": round(hours, 2), "earnings": round(hours * rate, 2), "shifts": len(shifts)}


def get_ytd_stats(caregiver_id: int) -> dict:
    import datetime as _d
    td = _d.date.today()
    shifts = get_shifts_for_caregiver_range(caregiver_id, td.replace(month=1, day=1).isoformat(), td.isoformat())
    rate = get_caregiver_pay_rate(caregiver_id)
    hours = sum(_calc_shift_hours(s) for s in shifts)
    return {"hours": round(hours, 2), "earnings": round(hours * rate, 2), "shifts": len(shifts)}


def get_or_create_current_pay_period() -> dict:
    import datetime as _d
    td = _d.date.today()
    period_start = td.replace(day=1).isoformat()
    nm = td.replace(day=28) + _d.timedelta(days=4)
    period_end = (nm - _d.timedelta(days=nm.day)).isoformat()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pay_periods WHERE period_start=? LIMIT 1", (period_start,)).fetchone()
        if not row:
            conn.execute("INSERT INTO pay_periods (period_start, period_end, status) VALUES (?,?,?)", (period_start, period_end, "open"))
            conn.commit()
            row = conn.execute("SELECT * FROM pay_periods WHERE period_start=? LIMIT 1", (period_start,)).fetchone()
    return _row_to_dict(row)


def get_pay_periods(limit: int = 12) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM pay_periods ORDER BY period_start DESC LIMIT ?", (limit,)).fetchall()
    return _rows_to_dicts(rows)


def get_hours_worked(caregiver_id: int, start: str, end: str) -> float:
    shifts = get_shifts_for_caregiver_range(caregiver_id, start, end)
    return round(sum(_calc_shift_hours(s) for s in shifts if s.get("status") != "cancelled"), 2)


def update_caregiver_availability(caregiver_id: int, availability_json: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE caregivers SET availability_json=? WHERE id=?", (availability_json, caregiver_id))
        conn.commit()
