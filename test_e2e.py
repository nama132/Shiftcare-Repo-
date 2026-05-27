"""End-to-end test for all phases (1-7).

Runs in DRY_RUN_SMS mode against a fresh seeded DB. Prints PASS/FAIL per check.
Exits non-zero if any check fails.

Usage:
    DRY_RUN_SMS=1 .venv/bin/python test_e2e.py
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

# Force DRY_RUN before importing anything else.
os.environ["DRY_RUN_SMS"] = "1"
os.environ.setdefault("BASE_URL", "http://localhost:5000")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-fallback")  # forces heuristic fallback

import db
import coverage
import seed
import app as app_module
from app import app as flask_app


FAILURES: list[str] = []
PASSES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASSES.append(name)
        print(f"  ✓ {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  ✗ {name} — {detail}")


def banner(text: str) -> None:
    print(f"\n=== {text} ===")


# ---------------------------------------------------------------------------
# Phase 1 — coverage hunt + race-safe claim
# ---------------------------------------------------------------------------

def test_phase1():
    banner("Phase 1 — SMS cancel → coverage hunt → race-safe claim")

    seed.seed()  # fresh data

    maria = db.get_caregiver_by_phone(os.getenv("TELNYX_NUMBER_1", "+10000000001"))
    priya = db.get_caregiver_by_phone(os.getenv("TELNYX_NUMBER_2", "+10000000002"))
    check("seed Maria exists", maria is not None)
    check("seed Priya exists", priya is not None)

    # Find Maria's 9:00 shift today
    today = db.today()
    shifts = db.get_shifts_by_caregiver_and_date(maria["id"], today)
    check("Maria has today's shift", len(shifts) >= 1)
    target = next((s for s in shifts if s["start_time"] == "09:00"), shifts[0])

    # Simulate SMS cancel
    app_module._handle_cancel(maria, {"shift_date": "today", "shift_time": None})
    refreshed = db.get_shift_by_id(target["id"])
    check("shift marked uncovered after cancel", refreshed["status"] == "uncovered")

    # Coverage hunt should have created pending entries
    pendings = db.get_pending_candidates_for_shift(target["id"])
    check("coverage hunt contacted at least 1 candidate", len(pendings) >= 1)

    # Race condition: two simultaneous claims, only one wins.
    other_candidates = [p for p in pendings if p["caregiver_id"] != priya["id"]]
    # Priya YES
    won1 = coverage.claim_shift(target["id"], priya["id"])
    won2 = False
    if other_candidates:
        won2 = coverage.claim_shift(target["id"], other_candidates[0]["caregiver_id"])
    check("first YES wins (claim_shift True)", won1 is True)
    check("second YES loses (claim_shift False)", won2 is False or not other_candidates)

    refreshed = db.get_shift_by_id(target["id"])
    check("shift now covered by Priya", refreshed["caregiver_id"] == priya["id"]
          and refreshed["status"] == "covered")


# ---------------------------------------------------------------------------
# Phase 2 — natural-language dates
# ---------------------------------------------------------------------------

def test_phase2():
    banner("Phase 2 — date resolution")
    today = date.today()
    check("today", db.resolve_shift_date("today") == today.isoformat())
    check("tomorrow", db.resolve_shift_date("tomorrow")
          == (today + timedelta(days=1)).isoformat())
    check("ISO", db.resolve_shift_date("2030-01-15") == "2030-01-15")
    # Weekday name resolves to that weekday this/next week
    fri = db.resolve_shift_date("friday")
    check("weekday resolves to a future date or today",
          date.fromisoformat(fri).weekday() == 4)


# ---------------------------------------------------------------------------
# Phase 3 — admin panel routes
# ---------------------------------------------------------------------------

def test_phase3():
    banner("Phase 3 — admin panel auth + routes")
    client = flask_app.test_client()

    # Unauthed → redirect
    rv = client.get("/admin/caregivers", follow_redirects=False)
    check("unauthed admin route redirects", rv.status_code == 302)

    # Login
    rv = client.post("/admin/login", data={"password": os.getenv("ADMIN_PASSWORD", "shiftcare2026")},
                     follow_redirects=False)
    check("admin login succeeds (302)", rv.status_code == 302)

    rv = client.get("/admin/caregivers")
    check("authed caregivers page 200", rv.status_code == 200)
    rv = client.get("/admin/clients")
    check("authed clients page 200", rv.status_code == 200)
    rv = client.get("/admin/shifts")
    check("authed shifts page 200", rv.status_code == 200)


# ---------------------------------------------------------------------------
# Phase 4 — scoring
# ---------------------------------------------------------------------------

def test_phase4():
    banner("Phase 4 — candidate scoring")
    seed.seed()
    # Mr. Hayes (zip 20002) needs CNA today 09:00-13:00. Maria (20001, CNA+HHA)
    # has the shift, so exclude her. Top candidate should be someone with CNA + close zip.
    hayes = next(c for c in db.get_all_clients() if c["name"].startswith("Mr. Robert"))
    maria = db.get_caregiver_by_phone(os.getenv("TELNYX_NUMBER_1", "+10000000001"))
    candidates = db.get_available_caregivers(
        date=db.today(), start_time="09:00", end_time="13:00",
        exclude_id=maria["id"],
        preferred_zip=hayes["zip_code"],
        required_certifications=hayes.get("required_certifications"),
        client_id=hayes["id"],
    )
    check("at least one candidate ranked", len(candidates) > 0)
    # Top candidate should hold the required cert.
    top = candidates[0]
    has_cert = "CNA" in (top.get("certifications") or "")
    check("top candidate holds required cert", has_cert)


# ---------------------------------------------------------------------------
# Phase 5 — conversation state
# ---------------------------------------------------------------------------

def test_phase5():
    banner("Phase 5 — conversation memory")
    seed.seed()
    phone = "+19990001234"
    db.upsert_conversation(phone, "awaiting_shift_choice",
                           {"target_date": "2030-01-01", "shift_ids": [1]})
    conv = db.get_conversation(phone)
    check("conversation stored + retrieved", conv is not None
          and conv["state"] == "awaiting_shift_choice")
    db.clear_conversation(phone)
    check("conversation cleared", db.get_conversation(phone) is None)


# ---------------------------------------------------------------------------
# Phase 6 — family portal
# ---------------------------------------------------------------------------

def test_phase6():
    banner("Phase 6 — family portal token + page")
    seed.seed()
    maria = db.get_caregiver_by_phone(os.getenv("TELNYX_NUMBER_1", "+10000000001"))
    shift = db.get_shifts_by_caregiver_and_date(maria["id"], db.today())[0]
    token = db.create_family_token(shift["id"])
    token2 = db.create_family_token(shift["id"])
    check("token generated", token and len(token) >= 32)
    check("token idempotent", token == token2)

    flask_client = flask_app.test_client()
    rv = flask_client.get(f"/client/{token}")
    check("portal returns 200", rv.status_code == 200)
    check("portal includes client name",
          b"Mr. Robert Hayes" in rv.data or b"Hayes" in rv.data)

    rv = flask_client.get("/client/not-a-real-token")
    check("invalid token returns 404", rv.status_code == 404)


# ---------------------------------------------------------------------------
# Phase 7A — check-in / check-out
# ---------------------------------------------------------------------------

def test_phase7a():
    banner("Phase 7A — check-in / check-out + hours")
    seed.seed()
    maria = db.get_caregiver_by_phone(os.getenv("TELNYX_NUMBER_1", "+10000000001"))

    # Check-in
    app_module._handle_check_in(maria)
    today = db.today()
    shifts = db.get_shifts_by_caregiver_and_date(maria["id"], today)
    target = shifts[0]
    ci = db.get_checkin_for_shift(target["id"])
    check("checkin row created", ci is not None and ci["check_in_at"] is not None)
    refreshed = db.get_shift_by_id(target["id"])
    check("shift status active after check-in", refreshed["status"] == "active")

    # Idempotent check-in
    app_module._handle_check_in(maria)
    ci2 = db.get_checkin_for_shift(target["id"])
    check("second check-in is a no-op (still active)",
          ci2["check_in_at"] == ci["check_in_at"])

    # Check-out
    app_module._handle_check_out(maria)
    ci3 = db.get_checkin_for_shift(target["id"])
    check("checkout timestamp recorded", ci3["check_out_at"] is not None)
    refreshed = db.get_shift_by_id(target["id"])
    check("shift status completed after check-out",
          refreshed["status"] == "completed")

    # Hours summary
    monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    sunday = (date.fromisoformat(monday) + timedelta(days=6)).isoformat()
    summary = db.get_hours_summary(monday, sunday)
    check("hours summary returns Maria's row",
          any(r["caregiver_name"] == "Maria Johnson" for r in summary))

    # AI parser intents (heuristic fallback, since fake key)
    from ai_parser import parse_message
    check("parser detects ARRIVED", parse_message("ARRIVED").get("intent") == "check_in")
    check("parser detects DONE", parse_message("DONE").get("intent") == "check_out")


# ---------------------------------------------------------------------------
# Phase 7B — proactive scheduler
# ---------------------------------------------------------------------------

def test_phase7b():
    banner("Phase 7B — proactive scheduler")
    seed.seed()
    # Free up a future day entirely so the scheduler has candidates.
    future_date = date.today() + timedelta(days=3)
    # Pick a weekday Priya is available on (mon-sun 8am-6pm in seed).
    while future_date.weekday() == 6:  # avoid sunday only if needed
        future_date += timedelta(days=1)
    future = future_date.isoformat()
    hayes = next(c for c in db.get_all_clients() if c["name"].startswith("Mr. Robert"))
    with db.get_conn() as conn:
        # Wipe ALL shifts on that date so candidates are free
        conn.execute("DELETE FROM shifts WHERE date = ?", (future,))
        # Insert one unassigned shift in Priya's window
        conn.execute(
            """INSERT INTO shifts (caregiver_id, client_id, date, start_time, end_time, status)
               VALUES (NULL, ?, ?, '10:00', '14:00', 'scheduled')""",
            (hayes["id"], future),
        )
        conn.commit()

    summary = coverage.run_proactive_scheduler(days_ahead=7)
    check("scheduler scanned at least 1 shift", summary["shifts_scanned"] >= 1)
    check("scheduler sent at least 1 offer", summary["offers_sent"] >= 1)

    # Find the suggestion row
    suggestions = db.get_recent_suggestions(10)
    open_offer = next((s for s in suggestions if s["status"] == "offered"), None)
    check("an offered suggestion exists", open_offer is not None)

    # Have that caregiver accept
    with db.get_conn() as conn:
        row = conn.execute(
            """SELECT ss.*, cg.* FROM scheduling_suggestions ss
                 JOIN caregivers cg ON cg.id = ss.caregiver_id
                 WHERE ss.status='offered' LIMIT 1"""
        ).fetchone()
    cg = dict(row)
    # build suggestion dict for accept_offer
    suggestion = {"id": cg["id"], "shift_id": cg["shift_id"], "caregiver_id": cg["caregiver_id"]}
    caregiver = db.get_caregiver_by_id(cg["caregiver_id"])
    # Need the suggestion's own id, not caregiver's. Re-fetch:
    with db.get_conn() as conn:
        srow = conn.execute(
            "SELECT * FROM scheduling_suggestions WHERE status='offered' LIMIT 1"
        ).fetchone()
    suggestion = dict(srow)
    won = coverage.accept_offer(suggestion, caregiver)
    check("first accept wins", won is True)
    # Shift should now be assigned
    s = db.get_shift_by_id(suggestion["shift_id"])
    check("shift now has caregiver assigned",
          s["caregiver_id"] == caregiver["id"] and s["status"] == "scheduled")


# ---------------------------------------------------------------------------
# Phase 7C — family ratings
# ---------------------------------------------------------------------------

def test_phase7c():
    banner("Phase 7C — family ratings + scoring boost")
    seed.seed()
    maria = db.get_caregiver_by_phone(os.getenv("TELNYX_NUMBER_1", "+10000000001"))
    shift = db.get_shifts_by_caregiver_and_date(maria["id"], db.today())[0]
    # Simulate shift completion
    db.update_shift_status(shift["id"], "completed")
    token = db.create_family_token(shift["id"])

    flask_client = flask_app.test_client()
    rv = flask_client.get(f"/client/{token}")
    check("portal includes rating form after completion",
          b"How was today's visit" in rv.data or b"star" in rv.data.lower())

    rv = flask_client.post(f"/client/{token}/rate",
                           data={"stars": "5", "comment": "Lovely visit!"},
                           follow_redirects=True)
    check("rating submission returns 200", rv.status_code == 200)
    r = db.get_rating_for_shift(shift["id"])
    check("rating saved (5 stars)", r is not None and r["stars"] == 5)
    check("comment saved", r["comment"] == "Lovely visit!")

    # Re-render — should now show saved state, not the form
    rv = flask_client.get(f"/client/{token}")
    check("portal shows saved rating", b"Your feedback" in rv.data)

    # Scoring boost: avg rating should bump the score for this caregiver-client pair
    boost = db.get_avg_rating_for_caregiver_client(maria["id"], shift["client_id"])
    check("avg rating returns 5.0 for this pair", boost == 5.0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    tests = [test_phase1, test_phase2, test_phase3, test_phase4, test_phase5,
             test_phase6, test_phase7a, test_phase7b, test_phase7c]
    for t in tests:
        try:
            t()
        except Exception as exc:
            FAILURES.append(f"{t.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  ✗ {t.__name__} crashed: {exc}")
            import traceback; traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"PASSED: {len(PASSES)}")
    print(f"FAILED: {len(FAILURES)}")
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nALL TESTS PASSED ✓")


if __name__ == "__main__":
    main()
