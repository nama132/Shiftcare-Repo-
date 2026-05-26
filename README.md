# ShiftCare

Flask + Claude app that automates shift coverage for a home-care agency.
A caregiver texts "I'm sick, can't make my 9am" → ShiftCare parses the intent
with Claude, finds the best available replacement (scored by certifications,
proximity, and history), texts them, claims the shift for whoever replies YES
first, and notifies the family + owner.

---

## Phases completed

| Phase | What was built |
|---|---|
| **Phase 1 — Core MVP** | SMS webhook, Claude intent parser, coverage hunt, race-safe claim, live dashboard, Telnyx + Twilio + Vonage support |
| **Phase 2 — Multi-day Scheduling** | AI extracts `shift_date` from messages ("can't make Friday's 10am"), `resolve_shift_date` converts natural language to ISO dates, dashboard date navigation, 7-day seeded schedule |
| **Phase 3 — Admin Dashboard (CRUD)** | Password-protected `/admin` panel — add/edit/delete caregivers, clients, shifts; coverage log with date filter |
| **Phase 4 — Smarter Coverage Matching** | Candidate scoring: +30 zip match, +20 per required cert, +5 per past coverage for that client, +2 generalist bonus; `max_shifts_per_day` guard (default 2); `required_certifications` column on clients |
| **Phase 5 — Two-way Conversation Memory** | Stateful multi-turn SMS: if a caregiver has multiple shifts and doesn't specify which one to cancel, the bot asks and remembers the answer using a `conversation_state` table with a 30-minute TTL; context-aware Claude parsing via `parse_message_with_context` |
| **Phase 6 — Family & Client Portal** | Token-based `/client/<token>` page texted to the family when a shift is confirmed — shows caregiver name, arrival time, care notes, live status; no login required; `BASE_URL` env var controls the link domain |

---

## 1. Local setup

```bash
cd shiftcare
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env with your real values
```

Edit `seed.py` and change the caregiver/client phone numbers to phones **you
control** (or Twilio test numbers). The first caregiver's phone should be your
own — that's the one you'll text from to simulate a callout.

Initialize the DB and seed it:

```bash
python db.py        # creates shiftcare.db
python seed.py      # inserts 6 caregivers + 6 clients + today's shifts
```

Confirm the data looks right by opening `shiftcare.db` in
[DB Browser for SQLite](https://sqlitebrowser.org/) (free).

---

## 2. Run locally

```bash
python app.py                       # Flask on :5000
# in a second terminal:
ngrok http 5000                     # expose to the internet
```

Copy the ngrok HTTPS URL and paste it (with `/sms` appended) into the Twilio
console → Phone Numbers → your number → **Messaging webhook**, e.g.:

```
https://abc123.ngrok.io/sms
```

Open the dashboard at `http://localhost:5000/dashboard`.

Open the admin panel at `http://localhost:5000/admin/login` (password: `shiftcare2026`, or set `ADMIN_PASSWORD` in `.env`).

### Dry-run mode

If you want to test the full flow without burning Twilio SMS credits, set:

```bash
export DRY_RUN_SMS=1
```

Outbound messages will print to the terminal instead of being sent.

---

## 3. End-to-end test

1. Text your Twilio number from **your real phone** (the one matching the
   first caregiver in `seed.py`):
   `"Hey it's Maria, I'm sick, can't make my 9am"`
2. The terminal should log the incoming SMS, the parsed intent
   (`cancel_shift`), and the coverage hunt firing off.
3. The dashboard refreshes — Maria's shift flips to red (`uncovered`).
4. The other caregivers' phones (or your DRY_RUN logs) get a coverage
   request.
5. From the **second** test phone, reply `YES`.
6. That caregiver gets a confirmation; the family phone for Mr. Hayes gets
   a notification; the owner gets a one-line summary.
7. Refresh `/dashboard` — the shift is now green (`covered`) and reassigned
   to the new caregiver.

### Edge cases to verify

- **Race:** two phones reply YES at almost the same time → only the first
  update succeeds (rowcount guard in `claim_shift`). The loser gets a
  "already claimed" text.
- **No replies:** the shift simply stays `uncovered`; the owner is alerted
  immediately when no candidates are found.
- **Unknown number:** texting from a phone that isn't in the `caregivers`
  table is silently ignored — no crash, no reply.

---

## 4. Deploy

1. Push to GitHub (`.env` is gitignored — never commit secrets).
2. Create a Railway or Render account, connect the repo.
3. Add the env vars from `.env.example` in the platform dashboard.
4. The included `Procfile` runs `gunicorn app:app`.
5. Update the Twilio webhook to your live URL, e.g.
   `https://shiftcare.up.railway.app/sms`.
6. Re-run the end-to-end test against the deployed URL.

---

## File map

| File | Purpose |
|---|---|
| `app.py`          | Flask entry: `/sms` webhook + `/dashboard` |
| `db.py`           | SQLite schema + query helpers |
| `ai_parser.py`    | Claude Haiku intent classifier (with heuristic fallback) |
| `coverage.py`     | Find replacements, text candidates, claim shifts, send notifications |
| `sms.py`          | Twilio outbound SMS helper (supports DRY_RUN_SMS) |
| `seed.py`         | Fake DMV-area caregivers, clients, and today's shifts |
| `templates/dashboard.html` | Color-coded live schedule, 30s auto-refresh |
| `Procfile`        | Production entrypoint for Railway/Render |

