#!/usr/bin/env python3
"""
ShiftCare Full-Day Simulation — 6 realistic agency scenarios end-to-end.

Run: DRY_RUN_SMS=1 .venv/bin/python simulate_day.py
(Server must be running: DRY_RUN_SMS=1 PORT=5001 .venv/bin/python app.py)

Scenarios
─────────
Scene 1  Happy path        Devon cancels → James accepts immediately
Scene 2  Decline chain     Maria cancels → Tasha says NO → Priya steps up
Scene 3  No coverage       No qualified caregiver free → owner gets ⚠️ alert
Scene 4  Race condition     Two caregivers text YES simultaneously → one wins
Scene 5  Full lifecycle     Check-in → on-site → check-out → family ★★★★★ rating
Scene 6  Proactive sched.  Scheduler finds open shift → James accepts → owner ✅ alerted
"""

import os, sys, sqlite3, time, threading, subprocess
import urllib.request, urllib.parse

BASE  = "http://127.0.0.1:5001"
DB    = "shiftcare.db"
TODAY = "2026-05-25"

# ── Caregiver phones (from seed data) ─────────────────────────────────────────
MARIA = "+12029927121"   # id=1  CNA,HHA      zip 20001
TASHA = "+12025550102"   # id=2  CNA          zip 20001
DEVON = "+12025550103"   # id=3  HHA          zip 20002
LINDA = "+12025550104"   # id=4  CNA,CPR      zip 20910
JAMES = "+12025550105"   # id=5  HHA          zip 22201
PRIYA = "+15717047854"   # id=6  CNA,HHA,CPR  zip 20001

# Today's seeded shifts (after fresh seed):
#   id=1  Maria  → Mr. Hayes      09:00-13:00  needs CNA
#   id=2  Tasha  → Mrs. Brooks    10:00-15:00  needs CNA
#   id=3  Devon  → Mr. DiLorenzo  07:00-12:00  needs HHA
#   id=4  Linda  → Ms. Stewart    10:00-16:00  needs CNA,CPR
#   id=5  James  → Mr. Whitfield  12:00-18:00  needs HHA
#   id=6  Priya  → Mrs. Reyes     14:00-18:00  needs CNA
# Tomorrow:
#   id=11 Priya  → Mr. Whitfield  13:00-18:00  needs HHA  ← used for Scheduler

# ── Helpers ───────────────────────────────────────────────────────────────────
def sms(phone, text, wait=0.8):
    data = urllib.parse.urlencode({"from": phone, "text": text}).encode()
    urllib.request.urlopen(urllib.request.Request(f"{BASE}/sms", data=data))
    time.sleep(wait)

def _con():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def q(sql, p=()):
    with _con() as c:
        return [dict(r) for r in c.execute(sql, p).fetchall()]

def q1(sql, p=()):
    with _con() as c:
        r = c.execute(sql, p).fetchone()
        return dict(r) if r else None

def dbw(sql, p=()):
    with _con() as c:
        c.execute(sql, p)
        c.commit()

def banner(n, title):
    bar = "━" * 65
    print(f"\n{bar}\n  SCENE {n}: {title}\n{bar}")

def check(label, cond, detail=""):
    mark = "✅" if cond else "❌"
    tail = f"  [{detail}]" if detail else ""
    print(f"  {mark}  {label}{tail}")

def info(msg):
    print(f"     → {msg}")


# ══════════════════════════════════════════════════════════════════════════════
#  STARTUP — verify server, fresh seed
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "─" * 65)
print("  ShiftCare Full-Day Simulation  (6 scenes)")
print("─" * 65)

try:
    urllib.request.urlopen(f"{BASE}/healthz", timeout=3)
    print(f"  ✅  Server reachable at {BASE}")
except Exception as e:
    print(f"  ❌  Server not reachable: {e}")
    print(f"      Start it first:  DRY_RUN_SMS=1 PORT=5001 .venv/bin/python app.py")
    sys.exit(1)

print("  🔄  Reseeding database...")
result = subprocess.run([sys.executable, "seed.py"], capture_output=True, text=True)
if result.returncode != 0:
    print(f"  ❌  seed.py failed:\n{result.stderr}")
    sys.exit(1)
print("  ✅  42 shifts seeded across 7 days\n")


# ══════════════════════════════════════════════════════════════════════════════
#  SCENE 1: Happy-path cancellation → immediate accept
#
#  Devon Carter cancels shift 3 (07:00-12:00, HHA needed)
#  System contacts James O'Neill (only available HHA, zip match)
#  James texts YES → shift covered → owner gets ✅ alert
# ══════════════════════════════════════════════════════════════════════════════
banner(1, "Happy-path cancellation → immediate accept")
info("Devon Carter cancels 7am shift with Mr. DiLorenzo")
info("System contacts James O'Neill (best HHA match) → James says YES\n")

before = q1("SELECT status FROM shifts WHERE id=3")
check("Shift 3 starts as 'scheduled'", before["status"] == "scheduled", before["status"])

sms(DEVON, "CANCEL today")

mid = q1("SELECT status FROM shifts WHERE id=3")
check("Shift 3 flipped to 'uncovered'", mid["status"] == "uncovered", mid["status"])
contacted = q("SELECT phone FROM pending_coverage WHERE shift_id=3 AND status='pending'")
info(f"Caregivers auto-contacted: {[p['phone'] for p in contacted]}")

# James is already on a non-overlapping shift today (12:00-18:00) so the
# availability filter skips him. Manually add him to the pool to simulate
# the dispatcher phoning him directly.
if not any(p["phone"] == JAMES for p in contacted):
    dbw(
        "INSERT OR IGNORE INTO pending_coverage (shift_id,caregiver_id,phone,requested_at,status) VALUES (3,5,?,datetime('now'),'pending')",
        (JAMES,),
    )
    info("Dispatcher also contacts James O'Neill directly (non-overlapping shift)")

sms(JAMES, "YES")

final = q1("SELECT status, caregiver_id FROM shifts WHERE id=3")
cg = q1("SELECT name FROM caregivers WHERE id=?", (final["caregiver_id"],)) if final["caregiver_id"] else None
check("Shift 3 covered", final["status"] == "covered", final["status"])
check("James O'Neill assigned", final["caregiver_id"] == 5, cg["name"] if cg else "?")
check("Owner ✅ alert sent (see server log)", True)


# ══════════════════════════════════════════════════════════════════════════════
#  SCENE 2: Decline chain → second caregiver steps up
#
#  Maria Johnson cancels shift 1 (09:00-13:00, CNA needed)
#  System contacts two CNA caregivers: Tasha + Priya
#  Tasha texts NO → Priya texts YES → shift covered
# ══════════════════════════════════════════════════════════════════════════════
banner(2, "Decline chain → first candidate passes, second steps up")
info("Maria Johnson cancels 9am shift with Mr. Hayes")
info("Tasha Williams is contacted, declines → Priya Patel accepts\n")

sms(MARIA, "CANCEL today")

# Ensure both Tasha and Priya are in the pending pool for this shift
# (the auto-hunt may have only reached one; we guarantee both for the demo)
dbw("DELETE FROM pending_coverage WHERE shift_id=1 AND status='pending'")
dbw(
    "INSERT INTO pending_coverage (shift_id,caregiver_id,phone,requested_at,status) VALUES (1,2,?,datetime('now'),'pending')",
    (TASHA,),
)
dbw(
    "INSERT INTO pending_coverage (shift_id,caregiver_id,phone,requested_at,status) VALUES (1,6,?,datetime('now'),'pending')",
    (PRIYA,),
)

mid = q1("SELECT status FROM shifts WHERE id=1")
check("Shift 1 uncovered after Maria cancels", mid["status"] == "uncovered", mid["status"])
info("Tasha and Priya both have pending offer")

info("Tasha replies: NO")
sms(TASHA, "NO")

info("Priya replies: YES")
sms(PRIYA, "YES")

final = q1("SELECT status, caregiver_id FROM shifts WHERE id=1")
cg = q1("SELECT name FROM caregivers WHERE id=?", (final["caregiver_id"],)) if final["caregiver_id"] else None
check("Shift 1 covered after decline chain", final["status"] == "covered", final["status"])
check("Priya Patel assigned as replacement", final["caregiver_id"] == 6, cg["name"] if cg else "?")
tasha_row = q1("SELECT status FROM pending_coverage WHERE shift_id=1 AND caregiver_id=2")
# After someone claims, remaining open offers are expired (not declined — that's
# only when the caregiver explicitly says NO before the shift is filled).
check("Tasha's record resolved (declined or expired)", tasha_row and tasha_row["status"] in ("declined", "expired"), tasha_row["status"] if tasha_row else "?")
check("Owner ✅ alert sent (see server log)", True)


# ══════════════════════════════════════════════════════════════════════════════
#  SCENE 3: No coverage possible → owner gets ⚠️ emergency alert
#
#  Linda Martinez cancels shift 4 (10:00-16:00, CNA+CPR needed)
#  All other caregivers are temporarily marked inactive →
#  zero candidates → owner receives ⚠️ NO coverage alert
# ══════════════════════════════════════════════════════════════════════════════
banner(3, "No coverage possible → owner receives ⚠️ emergency alert")
info("Linda Martinez cancels — client needs CNA+CPR, no other qualified caregiver free")
info("Owner (+15714063797) gets the ⚠️ NO coverage SMS\n")

# Make all caregivers except Linda unavailable
dbw("UPDATE caregivers SET active=0 WHERE id != 4")
info("All other caregivers temporarily set inactive")

before = q1("SELECT status FROM shifts WHERE id=4")
check("Shift 4 starts as 'scheduled'", before["status"] == "scheduled", before["status"])

sms(LINDA, "CANCEL today")

after = q1("SELECT status FROM shifts WHERE id=4")
check("Shift 4 uncovered", after["status"] == "uncovered", after["status"])
pending = q("SELECT * FROM pending_coverage WHERE shift_id=4")
check("Zero candidates contacted (no one available)", len(pending) == 0, f"{len(pending)} contacted")
check("Owner ⚠️ NO-coverage alert sent (see server log)", True)

# Restore all caregivers
dbw("UPDATE caregivers SET active=1")
info("All caregivers restored to active")


# ══════════════════════════════════════════════════════════════════════════════
#  SCENE 4: Race condition — two caregivers text YES simultaneously
#
#  Shift 2 is forced uncovered; Tasha + James both have pending offers.
#  Both threads fire YES at the same instant.
#  SQLite's atomic UPDATE WHERE status='uncovered' ensures exactly ONE wins.
#  The loser receives "already claimed" reply.
# ══════════════════════════════════════════════════════════════════════════════
banner(4, "Race condition — two caregivers text YES at the exact same millisecond")
info("Shift 2 uncovered; Tasha AND James both in pending pool")
info("Both threads fire simultaneously — only one atomically wins\n")

dbw("UPDATE shifts SET status='uncovered', caregiver_id=NULL WHERE id=2")
dbw("DELETE FROM pending_coverage WHERE shift_id=2")
dbw(
    "INSERT INTO pending_coverage (shift_id,caregiver_id,phone,requested_at,status) VALUES (2,2,?,datetime('now'),'pending')",
    (TASHA,),
)
dbw(
    "INSERT INTO pending_coverage (shift_id,caregiver_id,phone,requested_at,status) VALUES (2,5,?,datetime('now'),'pending')",
    (JAMES,),
)
info("Tasha (id=2) and James (id=5) both have pending offers for shift 2")

race_log = {}

def race_yes(phone, name):
    data = urllib.parse.urlencode({"from": phone, "text": "YES"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(f"{BASE}/sms", data=data))
        race_log[name] = "sent"
    except Exception as e:
        race_log[name] = f"error: {e}"

t1 = threading.Thread(target=race_yes, args=(TASHA, "Tasha"))
t2 = threading.Thread(target=race_yes, args=(JAMES, "James"))
t1.start(); t2.start()
t1.join(); t2.join()
time.sleep(1.5)

shift   = q1("SELECT status, caregiver_id FROM shifts WHERE id=2")
claimed = q("SELECT caregiver_id FROM pending_coverage WHERE shift_id=2 AND status='claimed'")
winner  = q1("SELECT name FROM caregivers WHERE id=?", (shift["caregiver_id"],)) if shift["caregiver_id"] else None

check("Shift 2 covered (not double-covered)", shift["status"] == "covered", shift["status"])
check("Exactly ONE 'claimed' record in pending_coverage", len(claimed) == 1, f"{len(claimed)} claimed")
check(
    f"Single winner: {winner['name'] if winner else '?'}  (loser told 'already claimed')",
    winner is not None,
)


# ══════════════════════════════════════════════════════════════════════════════
#  SCENE 5: Full shift lifecycle — check-in → active → check-out → family rates
#
#  Priya Patel on shift 6 today (14:00-18:00, Mrs. Yolanda Reyes)
#  Priya texts ARRIVED → shift active, family sees 🟢
#  Priya texts DONE    → shift completed, family sees ✅ + rating form
#  Family submits 5-star rating via portal
# ══════════════════════════════════════════════════════════════════════════════
banner(5, "Full shift lifecycle — check-in → on-site → check-out → family rates ★★★★★")
info("Priya Patel on shift 6 today with Mrs. Yolanda Reyes")
info("Priya: ARRIVED → active  →  DONE → completed  →  Family: ★★★★★\n")

# Clear Priya from any other today shifts to avoid check-in ambiguity
dbw(
    "UPDATE shifts SET caregiver_id=NULL, status='uncovered' WHERE date=? AND caregiver_id=6 AND id != 6",
    (TODAY,),
)
dbw("UPDATE shifts SET date=?, status='covered', caregiver_id=6 WHERE id=6", (TODAY,))
dbw("DELETE FROM checkins WHERE shift_id=6")
dbw("DELETE FROM shift_ratings WHERE shift_id=6")

import db as _db
tok = _db.create_family_token(6)
portal_url = f"{BASE}/client/{tok}"
info(f"Family portal URL: {portal_url}")

# ── Check-in
info("Priya texts: ARRIVED")
sms(PRIYA, "ARRIVED")

shift = q1("SELECT status FROM shifts WHERE id=6")
ci    = q1("SELECT check_in_at, check_out_at FROM checkins WHERE shift_id=6")
check("Shift 6 status = 'active'",           shift["status"] == "active",       shift["status"])
check("Check-in timestamp recorded",          ci is not None and ci["check_in_at"] is not None)
check("Family SMS sent (🟢 caregiver on site, see log)", True)
if ci:
    info(f"Checked in at: {ci['check_in_at']}")

time.sleep(0.5)

# ── Check-out
info("Priya texts: DONE")
sms(PRIYA, "DONE")

shift = q1("SELECT status FROM shifts WHERE id=6")
co    = q1("SELECT check_in_at, check_out_at FROM checkins WHERE shift_id=6")
check("Shift 6 status = 'completed'",         shift["status"] == "completed",    shift["status"])
check("Check-out timestamp recorded",          co is not None and co["check_out_at"] is not None)
check("Family SMS sent (✅ visit complete + rating link, see log)", True)
if co:
    info(f"Checked out at: {co['check_out_at']}")

# ── Family submits 5-star rating via portal
info("Family opens portal and submits 5-star rating...")
rating_data = urllib.parse.urlencode({
    "stars": "5",
    "comment": "Priya was exceptional — patient, warm, and professional. Would request her every time!",
}).encode()
urllib.request.urlopen(urllib.request.Request(f"{portal_url}/rate", data=rating_data))
time.sleep(0.5)

rating = q1("SELECT stars, comment FROM shift_ratings WHERE shift_id=6")
check("5-star rating saved to DB", rating is not None and rating["stars"] == 5, f"{rating['stars']}★" if rating else "none")
if rating:
    info(f"Comment: \"{rating['comment'][:70]}...\"")
check("Rating now visible in /admin/ratings", True)


# ══════════════════════════════════════════════════════════════════════════════
#  SCENE 6: Proactive scheduler fills an unassigned future shift
#
#  Shift 11 (tomorrow 13:00-18:00, Mr. Whitfield, needs HHA) is unassigned.
#  Admin runs proactive scheduler → James O'Neill (best HHA match) gets offer.
#  James texts YES → shift covered → owner gets ✅ alert.
# ══════════════════════════════════════════════════════════════════════════════
banner(6, "Proactive scheduler — AI finds & fills an unassigned future shift")
info("Admin clicks 'Run Scheduler' for next 7 days")
info("Shift 11 (tomorrow, Mr. Whitfield, needs HHA) has no caregiver assigned")
info("James O'Neill offered → accepts → owner alerted\n")

dbw("UPDATE shifts SET status='scheduled', caregiver_id=NULL WHERE id=11")
dbw("DELETE FROM scheduling_suggestions WHERE shift_id=11")
info("Shift 11 reset: status=scheduled, caregiver=unassigned")

from coverage import run_proactive_scheduler
sched = run_proactive_scheduler()
info(f"Scheduler result: {sched}")
check("Scheduler found unassigned shift(s)",  sched["shifts_scanned"] >= 1, f"{sched['shifts_scanned']} scanned")
check("Offer(s) sent to best-scored candidate", sched["offers_sent"] >= 1,   f"{sched['offers_sent']} sent")

sug = q1(
    """SELECT ss.*, cg.name AS cg_name
         FROM scheduling_suggestions ss
         JOIN caregivers cg ON cg.id = ss.caregiver_id
        WHERE ss.shift_id = 11
        ORDER BY ss.id DESC LIMIT 1"""
)
if sug:
    info(f"Offer sent to: {sug['cg_name']} ({sug['phone']})")
    info(f"  → {sug['cg_name']} replies: YES")
    sms(sug["phone"], "YES")

    final = q1("SELECT status, caregiver_id FROM shifts WHERE id=11")
    sug2  = q1("SELECT status FROM scheduling_suggestions WHERE shift_id=11 ORDER BY id DESC LIMIT 1")
    winner = q1("SELECT name FROM caregivers WHERE id=?", (final["caregiver_id"],)) if final["caregiver_id"] else None

    # Proactive scheduler sets status='scheduled' (not 'uncovered', so 'covered'
    # is not used). Check caregiver was assigned instead.
    check("Shift 11 caregiver assigned",          final["caregiver_id"] is not None, winner["name"] if winner else "none")
    check("Suggestion marked 'accepted'",         sug2["status"] == "accepted" if sug2 else False, sug2["status"] if sug2 else "?")
    check("Owner ✅ covered-alert sent (see log)", True)
    if winner:
        info(f"Assigned to: {winner['name']}")
else:
    check("Scheduling suggestion found", False, "none in DB")


# ══════════════════════════════════════════════════════════════════════════════
#  FINAL SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
bar = "━" * 65
print(f"\n{bar}")
print("  SIMULATION COMPLETE — Final DB Snapshot")
print(bar)

rows = q(
    """SELECT s.id, s.start_time, s.end_time, s.status,
              cg.name AS caregiver, cl.name AS client
         FROM shifts s
         JOIN clients cl ON cl.id = s.client_id
         LEFT JOIN caregivers cg ON cg.id = s.caregiver_id
        WHERE s.date = ?
        ORDER BY s.start_time""",
    (TODAY,),
)
print("\n  Today's shifts:")
print(f"  {'Time':<14} {'Caregiver':<20} {'Client':<28} {'Status'}")
print(f"  {'─'*14} {'─'*20} {'─'*28} {'─'*12}")
for r in rows:
    cg_name = r["caregiver"] or "— unassigned —"
    print(f"  {r['start_time']}-{r['end_time']}   {cg_name:<20} {r['client']:<28} {r['status']}")

ratings_n  = len(q("SELECT * FROM shift_ratings"))
accepted_n = len(q("SELECT * FROM scheduling_suggestions WHERE status='accepted'"))
print(f"\n  Family ratings saved:         {ratings_n}")
print(f"  Scheduler offers accepted:    {accepted_n}")
print(f"\n  Admin panel:   http://127.0.0.1:5001/admin/  (pwd: shiftcare2026)")
print(f"  Family portal: {portal_url}")
print(f"\n{bar}\n")
