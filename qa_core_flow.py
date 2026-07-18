"""Stage-1 QA driver — validates the 5 BUILT core coverage scenarios (Path A).

Runs against a live server on :5001 in DRY-RUN. Reseeds between scenarios for
clean state. Only tests what the product actually implements.
"""
import os, sqlite3, time, threading, urllib.request, urllib.parse

os.environ["DRY_RUN_SMS"] = "1"
BASE = "http://127.0.0.1:5001"
DB = "shiftcare.db"

import seed  # noqa: E402

# Seed phones
MARIA = "+12029927121"   # id1 CNA,HHA
TASHA = "+12025550102"   # id2 CNA
DEVON = "+12025550103"   # id3 HHA
LINDA = "+12025550104"   # id4 CNA,CPR
JAMES = "+12025550105"   # id5 HHA
PRIYA = "+15717047854"   # id6 CNA,HHA,CPR

PASS, FAIL = [], []


def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print(f"    {'✅' if ok else '❌'} {label}" + (f"  [{detail}]" if detail else ""))
    return ok


def con():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    return c


def q1(sql, p=()):
    with con() as c:
        r = c.execute(sql, p).fetchone()
        return dict(r) if r else None


def q(sql, p=()):
    with con() as c:
        return [dict(r) for r in c.execute(sql, p).fetchall()]


def dbw(sql, p=()):
    with con() as c:
        c.execute(sql, p); c.commit()


def sms(phone, text, wait=1.1):
    data = urllib.parse.urlencode({"from": phone, "text": text}).encode()
    urllib.request.urlopen(urllib.request.Request(f"{BASE}/sms", data=data))
    time.sleep(wait)


def reseed():
    seed.seed()


def banner(t):
    print(f"\n  ── {t} ──")


# ══════════════════════════════════════════════════════════════════
print("========== STEP 4: CORE COVERAGE FLOW (a–e) ==========")

# ---- (a) Happy path -------------------------------------------------
banner("(a) Happy path: Maria cancels → Priya YES → covered")
reseed()
before = q1("SELECT status FROM shifts WHERE id=1")
check("shift 1 starts 'scheduled'", before["status"] == "scheduled", before["status"])
sms(MARIA, "CANCEL today")
mid = q1("SELECT status FROM shifts WHERE id=1")
check("shift 1 → 'uncovered' after cancel", mid["status"] == "uncovered", mid["status"])
contacted = q("SELECT caregiver_id, phone FROM pending_coverage WHERE shift_id=1 AND status='pending'")
check("coverage hunt contacted ≥1 backup", len(contacted) >= 1, f"{len(contacted)} contacted")
priya_contacted = any(p["phone"] == PRIYA for p in contacted)
check("eligible CNA backup (Priya) was contacted", priya_contacted)
sms(PRIYA, "YES")
final = q1("SELECT status, caregiver_id FROM shifts WHERE id=1")
check("shift 1 → 'covered'", final["status"] == "covered", final["status"])
check("Priya (id6) assigned", final["caregiver_id"] == 6, str(final["caregiver_id"]))
claimed = q1("SELECT status FROM pending_coverage WHERE shift_id=1 AND caregiver_id=6")
check("Priya's pending row → 'claimed'", claimed and claimed["status"] == "claimed",
      claimed["status"] if claimed else "none")

# ---- (b) Decline chain ---------------------------------------------
banner("(b) Decline chain: Tasha NO → Priya YES")
reseed()
sms(MARIA, "CANCEL today")
# Guarantee both Tasha + Priya are in the pool for a deterministic decline→accept
dbw("DELETE FROM pending_coverage WHERE shift_id=1")
dbw("INSERT INTO pending_coverage (shift_id,caregiver_id,phone,requested_at,status) VALUES (1,2,?,datetime('now'),'pending')", (TASHA,))
dbw("INSERT INTO pending_coverage (shift_id,caregiver_id,phone,requested_at,status) VALUES (1,6,?,datetime('now'),'pending')", (PRIYA,))
check("shift 1 uncovered, Tasha+Priya pending", q1("SELECT status FROM shifts WHERE id=1")["status"] == "uncovered")
sms(TASHA, "NO")
tasha = q1("SELECT status FROM pending_coverage WHERE shift_id=1 AND caregiver_id=2")
check("Tasha's row resolved (declined)", tasha and tasha["status"] == "declined", tasha["status"] if tasha else "none")
still_uncovered = q1("SELECT status FROM shifts WHERE id=1")
check("shift still uncovered after decline", still_uncovered["status"] == "uncovered", still_uncovered["status"])
sms(PRIYA, "YES")
final = q1("SELECT status, caregiver_id FROM shifts WHERE id=1")
check("shift 1 → 'covered' by Priya", final["status"] == "covered" and final["caregiver_id"] == 6,
      f"{final['status']}/{final['caregiver_id']}")

# ---- (c) No coverage ------------------------------------------------
banner("(c) No coverage: only Linda active → owner alert")
reseed()
dbw("UPDATE caregivers SET active=0 WHERE id != 4")  # only Linda active
before = q1("SELECT status FROM shifts WHERE id=4")
check("shift 4 starts 'scheduled'", before["status"] == "scheduled", before["status"])
sms(LINDA, "CANCEL today")
after = q1("SELECT status FROM shifts WHERE id=4")
check("shift 4 → 'uncovered'", after["status"] == "uncovered", after["status"])
pend = q("SELECT * FROM pending_coverage WHERE shift_id=4")
check("zero candidates contacted", len(pend) == 0, f"{len(pend)} contacted")
# owner alert is a DRY_RUN send — verified via server log grep after run
print("    ℹ owner no-coverage alert is a DRY_RUN SMS — verified in server log below")
dbw("UPDATE caregivers SET active=1")  # restore

# ---- (d) Race condition --------------------------------------------
banner("(d) Race: two YES simultaneously → one wins")
reseed()
dbw("UPDATE shifts SET status='uncovered', caregiver_id=NULL WHERE id=2")
dbw("DELETE FROM pending_coverage WHERE shift_id=2")
dbw("INSERT INTO pending_coverage (shift_id,caregiver_id,phone,requested_at,status) VALUES (2,2,?,datetime('now'),'pending')", (TASHA,))
dbw("INSERT INTO pending_coverage (shift_id,caregiver_id,phone,requested_at,status) VALUES (2,6,?,datetime('now'),'pending')", (PRIYA,))
def race_yes(phone):
    data = urllib.parse.urlencode({"from": phone, "text": "YES"}).encode()
    urllib.request.urlopen(urllib.request.Request(f"{BASE}/sms", data=data))
t1 = threading.Thread(target=race_yes, args=(TASHA,))
t2 = threading.Thread(target=race_yes, args=(PRIYA,))
t1.start(); t2.start(); t1.join(); t2.join()
time.sleep(1.5)
shift = q1("SELECT status, caregiver_id FROM shifts WHERE id=2")
claimed = q("SELECT caregiver_id FROM pending_coverage WHERE shift_id=2 AND status='claimed'")
check("shift 2 covered (not double)", shift["status"] == "covered", shift["status"])
check("exactly ONE claimed row", len(claimed) == 1, f"{len(claimed)} claimed")
check("winner assigned to shift", shift["caregiver_id"] in (2, 6), str(shift["caregiver_id"]))

# ---- (e) Conversation memory ---------------------------------------
banner("(e) Conversation memory: 2 shifts → bot asks which → resolve")
reseed()
today = q1("SELECT date FROM shifts ORDER BY date LIMIT 1")["date"]
# Give Maria a 2nd shift today: reassign the unassigned-free client Reyes(6) slot?
# Simpler: assign shift 6 (14:00-18:00) to Maria so she has shift 1 (09:00) + shift 6 (14:00)
dbw("UPDATE shifts SET caregiver_id=1 WHERE id=6")
maria_today = q("SELECT id,start_time FROM shifts WHERE caregiver_id=1 AND date=? ORDER BY start_time", (today,))
check("Maria has 2 shifts today", len(maria_today) == 2, f"ids={[s['id'] for s in maria_today]}")
sms(MARIA, "I need to cancel today")  # no time specified → should ask which
conv = q1("SELECT state FROM conversation_state WHERE phone=?", (MARIA,))
check("bot saved 'awaiting_shift_choice'", conv and conv["state"] == "awaiting_shift_choice",
      conv["state"] if conv else "none")
sms(MARIA, "the 9am one with Mr. Hayes")  # resolve to shift 1
s1 = q1("SELECT status FROM shifts WHERE id=1")
s6 = q1("SELECT status FROM shifts WHERE id=6")
check("correct shift (9am, id1) cancelled", s1["status"] == "uncovered", s1["status"])
check("other shift (2pm, id6) untouched", s6["status"] == "scheduled", s6["status"])
conv2 = q1("SELECT state FROM conversation_state WHERE phone=?", (MARIA,))
check("conversation cleared after resolve", conv2 is None, conv2["state"] if conv2 else "cleared")

# ---- summary --------------------------------------------------------
print(f"\n  STEP 4 RESULT: {len(PASS)} passed / {len(FAIL)} failed")
if FAIL:
    print("  FAILURES:", ", ".join(FAIL))
