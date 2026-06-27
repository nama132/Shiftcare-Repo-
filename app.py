"""ShiftCare Flask app — incoming SMS webhook + live dashboard + admin panel."""
from __future__ import annotations

import logging
import os
from datetime import date as date_cls, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import generate_password_hash, check_password_hash

import re

import db
from ai_parser import parse_message, parse_message_with_context
from coverage import (
    claim_shift,
    initiate_coverage_hunt,
    remove_candidate,
    send_confirmation_to_caregiver,
    send_family_notification,
    send_owner_summary,
)
from sms import send_sms

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("shiftcare")

db.create_tables()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-shiftcare")

@app.context_processor
def inject_now():
    from datetime import datetime
    return {"now": datetime.now}

# ---------------------------------------------------------------------------
# Sentry crash alerts (no-op if SENTRY_DSN not set)
# ---------------------------------------------------------------------------
_sentry_dsn = os.getenv("SENTRY_DSN")
if _sentry_dsn:
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration
    sentry_sdk.init(dsn=_sentry_dsn, integrations=[FlaskIntegration()], traces_sample_rate=0.1)
    log.info("Sentry initialized")

# ---------------------------------------------------------------------------
# Daily owner digest (APScheduler — runs at 8am server time)
# ---------------------------------------------------------------------------

def _send_daily_digest():
    """Send the owner a morning summary of today's shifts."""
    owner = os.getenv("OWNER_PHONE")
    if not owner:
        return
    today = db.today()
    shifts = db.get_shifts_for_date(today)
    if not shifts:
        return
    total = len(shifts)
    covered = sum(1 for s in shifts if s["status"] in ("covered", "scheduled", "active"))
    uncovered = sum(1 for s in shifts if s["status"] == "uncovered")
    msg = (
        f"📋 ShiftCare daily digest for {today}: "
        f"{total} shifts — {covered} covered, {uncovered} uncovered."
    )
    from sms import send_sms
    send_sms(owner, msg)
    log.info("Daily digest sent to owner")

# Only start scheduler in production (not during tests or reloads)
if os.getenv("ENABLE_SCHEDULER") == "1":
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_send_daily_digest, "cron", hour=8, minute=0)
    _scheduler.start()
    log.info("APScheduler started — daily digest at 08:00")


# ---------------------------------------------------------------------------
# Auth — Login / Sign Up / Logout
# ---------------------------------------------------------------------------

def require_login(f):
    """Decorator: redirect to /login if user not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# Keep legacy admin decorator as alias so existing routes still work
require_admin = require_login


def _validate_phone(phone: str) -> str:
    """Normalize phone to E.164. Returns empty string if invalid."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ""


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db.get_user_by_username(username)
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"]     = user["id"]
            session["agency_name"] = user["agency_name"]
            session["username"]    = user["username"]
            session["role"]        = user["role"]
            db.update_last_login(user["id"])
            return redirect(url_for("dashboard"))
        error = "Incorrect username or password. Please try again."
    return render_template("login.html", error=error)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    errors = {}
    form = {}
    if request.method == "POST":
        form = {k: v.strip() for k, v in request.form.items()}
        agency_name = form.get("agency_name", "")
        username    = form.get("username", "").lower()
        email       = form.get("email", "").lower()
        phone_raw   = form.get("phone", "")
        password    = form.get("password", "")
        confirm     = form.get("confirm_password", "")

        # Validate
        if len(agency_name) < 2:
            errors["agency_name"] = "Agency name is required (min 2 characters)."
        if not re.match(r"^[a-z0-9_]{3,30}$", username):
            errors["username"] = "3–30 chars, lowercase letters, numbers, underscores only."
        elif db.username_exists(username):
            errors["username"] = "Username already taken. Choose another."
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            errors["email"] = "Enter a valid email address."
        elif db.email_exists(email):
            errors["email"] = "An account with this email already exists."
        phone = _validate_phone(phone_raw)
        if not phone:
            errors["phone"] = "Enter a valid US phone number (10 digits)."
        if len(password) < 8:
            errors["password"] = "Password must be at least 8 characters."
        if password != confirm:
            errors["confirm_password"] = "Passwords do not match."

        if not errors:
            # Force pbkdf2:sha256 — Python 3.9 on macOS/LibreSSL lacks scrypt
            password_hash = generate_password_hash(password, method="pbkdf2:sha256")
            db.create_user(agency_name, username, email, phone, password_hash)
            flash("Account created! Sign in to get started.", "success")
            return redirect(url_for("login"))

    return render_template("signup.html", errors=errors, form=form)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# Keep legacy admin login/logout routes pointing to new system
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    return redirect(url_for("login"))

@app.route("/admin/logout")
def admin_logout():
    return redirect(url_for("logout"))


# ---------------------------------------------------------------------------
# Admin — Caregivers
# ---------------------------------------------------------------------------

@app.route("/admin/caregivers")
@require_admin
def admin_caregivers():
    caregivers = db.get_all_caregivers()
    for cg in caregivers:
        cg["portal_enabled"] = db.employee_account_exists(cg["id"]) and bool(
            (db.get_employee_account(cg["id"]) or {}).get("portal_enabled")
        )
    return render_template("admin/caregivers.html", caregivers=caregivers)


@app.route("/admin/caregivers/<int:cid>/portal", methods=["GET", "POST"])
@require_admin
def admin_caregiver_portal(cid):
    caregivers = db.get_all_caregivers()
    caregiver = next((c for c in caregivers if c["id"] == cid), None)
    if not caregiver:
        flash("Caregiver not found", "error")
        return redirect(url_for("admin_caregivers"))
    account = db.get_employee_account(cid)
    if request.method == "POST":
        action = request.form.get("action")
        if action in ("create", "reset_password"):
            raw_pw = request.form.get("password", "")
            if len(raw_pw) < 8:
                flash("Password must be at least 8 characters", "error")
            else:
                pw_hash = generate_password_hash(raw_pw, method="pbkdf2:sha256")
                if action == "create":
                    db.create_employee_account(cid, pw_hash)
                    flash(f"Portal account created for {caregiver['name']}.", "success")
                else:
                    db.update_employee_password(cid, pw_hash)
                    flash(f"Password reset for {caregiver['name']}.", "success")
                return redirect(url_for("admin_caregivers"))
        elif action == "toggle":
            new_state = not bool(account and account.get("portal_enabled"))
            db.set_employee_portal_enabled(cid, new_state)
            flash(f"Portal {'enabled' if new_state else 'disabled'} for {caregiver['name']}.", "success")
            return redirect(url_for("admin_caregivers"))
    return render_template("admin/caregiver_portal.html", caregiver=caregiver, account=account)


@app.route("/admin/debug/caregivers")
@require_admin
def admin_debug_caregivers():
    """Debug endpoint to show all caregiver phone numbers."""
    caregivers = db.get_all_caregivers()
    output = ["<h2>Caregivers in Database</h2>", "<pre>"]
    for cg in caregivers:
        output.append(f"ID: {cg['id']}")
        output.append(f"Name: {cg['name']}")
        output.append(f"Phone: {cg['phone']}")
        output.append(f"Active: {cg['active']}")
        output.append("-" * 50)
    output.append("</pre>")
    output.append(f"<p><strong>Total caregivers: {len(caregivers)}</strong></p>")
    output.append("<h2>Environment Variables</h2>")
    output.append(f"<p><strong>SMS_PROVIDER:</strong> {os.getenv('SMS_PROVIDER', 'NOT SET')}</p>")
    output.append(f"<p><strong>DRY_RUN_SMS:</strong> {os.getenv('DRY_RUN_SMS', 'NOT SET')}</p>")
    output.append(f"<p><strong>TELNYX_FROM (Agency Sender):</strong> {os.getenv('TELNYX_FROM', 'NOT SET')}</p>")
    output.append(f"<p><strong>TELNYX_API_KEY:</strong> {'SET ('+os.getenv('TELNYX_API_KEY', '')[:20]+'...)' if os.getenv('TELNYX_API_KEY') else 'NOT SET'}</p>")
    output.append(f"<p><strong>TELNYX_NUMBER_1 (Maria):</strong> {os.getenv('TELNYX_NUMBER_1', 'NOT SET')}</p>")
    output.append(f"<p><strong>TELNYX_NUMBER_2 (Priya):</strong> {os.getenv('TELNYX_NUMBER_2', 'NOT SET')}</p>")
    output.append(f"<p><strong>TELNYX_NUMBER_3 (Family):</strong> {os.getenv('TELNYX_NUMBER_3', 'NOT SET')}</p>")
    output.append(f"<p><strong>OWNER_PHONE:</strong> {os.getenv('OWNER_PHONE', 'NOT SET')}</p>")
    return "\n".join(output)


@app.route("/admin/debug/fix-coverage-data-xk9m2")
@require_admin
def admin_fix_coverage_data():
    """One-off maintenance: set backup availability + remove duplicate caregivers."""
    import json as _json
    log_lines = []

    full_week = _json.dumps({
        "mon": ["8am-8pm"], "tue": ["8am-8pm"], "wed": ["8am-8pm"],
        "thu": ["8am-8pm"], "fri": ["8am-8pm"], "sat": ["8am-8pm"], "sun": ["8am-8pm"],
    })

    # Give the CORRECT records availability so the coverage hunt can find them.
    for cg_id in (3, 4):  # Maria (703-479-8814), Priya (703-577-4626)
        db.update_caregiver_availability(cg_id, full_week)
        log_lines.append(f"Set availability for caregiver ID {cg_id}")

    # Delete the duplicate OLD records.
    for cg_id in (1, 2):  # Maria old (+15714662908), Priya old (+14436360988)
        db.delete_caregiver(cg_id)
        log_lines.append(f"Deleted duplicate caregiver ID {cg_id}")

    # Report final state
    remaining = db.get_all_caregivers()
    log_lines.append("")
    log_lines.append("Remaining caregivers:")
    for cg in remaining:
        log_lines.append(f"  ID {cg['id']} | {cg['name']} | {cg['phone']} | active={cg['active']}")

    return "<pre>" + "\n".join(log_lines) + "</pre>"


@app.route("/admin/caregivers/new", methods=["GET", "POST"])
@require_admin
def admin_caregiver_new():
    if request.method == "POST":
        db.upsert_caregiver(request.form)
        flash("Caregiver added ✓", "success")
        return redirect(url_for("admin_caregivers"))
    return render_template("admin/caregiver_form.html", caregiver=None)


@app.route("/admin/caregivers/<int:cid>/edit", methods=["GET", "POST"])
@require_admin
def admin_caregiver_edit(cid):
    caregivers = db.get_all_caregivers()
    caregiver = next((c for c in caregivers if c["id"] == cid), None)
    if not caregiver:
        flash("Not found", "error")
        return redirect(url_for("admin_caregivers"))
    if request.method == "POST":
        data = dict(request.form)
        data["id"] = cid
        db.upsert_caregiver(data)
        flash("Caregiver updated ✓", "success")
        return redirect(url_for("admin_caregivers"))
    return render_template("admin/caregiver_form.html", caregiver=caregiver)


@app.route("/admin/caregivers/<int:cid>/delete", methods=["POST"])
@require_admin
def admin_caregiver_delete(cid):
    db.delete_caregiver(cid)
    flash("Caregiver deleted", "success")
    return redirect(url_for("admin_caregivers"))


# ---------------------------------------------------------------------------
# Admin — Clients
# ---------------------------------------------------------------------------

@app.route("/admin/clients")
@require_admin
def admin_clients():
    clients = db.get_all_clients()
    return render_template("admin/clients.html", clients=clients)


@app.route("/admin/clients/new", methods=["GET", "POST"])
@require_admin
def admin_client_new():
    if request.method == "POST":
        db.upsert_client(request.form)
        flash("Client added ✓", "success")
        return redirect(url_for("admin_clients"))
    return render_template("admin/client_form.html", client=None)


@app.route("/admin/clients/<int:cid>/edit", methods=["GET", "POST"])
@require_admin
def admin_client_edit(cid):
    clients = db.get_all_clients()
    client = next((c for c in clients if c["id"] == cid), None)
    if not client:
        flash("Not found", "error")
        return redirect(url_for("admin_clients"))
    if request.method == "POST":
        data = dict(request.form)
        data["id"] = cid
        db.upsert_client(data)
        flash("Client updated ✓", "success")
        return redirect(url_for("admin_clients"))
    return render_template("admin/client_form.html", client=client)


@app.route("/admin/clients/<int:cid>/delete", methods=["POST"])
@require_admin
def admin_client_delete(cid):
    db.delete_client(cid)
    flash("Client deleted", "success")
    return redirect(url_for("admin_clients"))


# ---------------------------------------------------------------------------
# Admin — Shifts
# ---------------------------------------------------------------------------

@app.route("/admin/shifts")
@require_admin
def admin_shifts():
    selected = request.args.get("date", str(date_cls.today()))
    prev_date = str(date_cls.fromisoformat(selected) - timedelta(days=1))
    next_date = str(date_cls.fromisoformat(selected) + timedelta(days=1))
    shifts = db.get_shifts_for_date_range(selected, selected)
    caregivers = db.get_all_caregivers()
    clients = db.get_all_clients()
    return render_template("admin/shifts.html",
                           shifts=shifts, selected=selected,
                           prev_date=prev_date, next_date=next_date,
                           caregivers=caregivers, clients=clients)


@app.route("/admin/shifts/new", methods=["GET", "POST"])
@require_admin
def admin_shift_new():
    caregivers = db.get_all_caregivers()
    clients = db.get_all_clients()
    if request.method == "POST":
        db.create_shift(request.form)
        flash("Shift created ✓", "success")
        return redirect(url_for("admin_shifts", date=request.form.get("date")))
    return render_template("admin/shift_form.html",
                           shift=None, caregivers=caregivers, clients=clients)


@app.route("/admin/shifts/<int:sid>/edit", methods=["GET", "POST"])
@require_admin
def admin_shift_edit(sid):
    caregivers = db.get_all_caregivers()
    clients = db.get_all_clients()
    con = db._con()
    shift = con.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone()
    con.close()
    if not shift:
        flash("Shift not found", "error")
        return redirect(url_for("admin_shifts"))
    shift = dict(shift)
    if request.method == "POST":
        data = dict(request.form)
        data["id"] = sid
        db.update_shift(data)
        flash("Shift updated ✓", "success")
        return redirect(url_for("admin_shifts", date=data.get("date")))
    return render_template("admin/shift_form.html",
                           shift=shift, caregivers=caregivers, clients=clients)


@app.route("/admin/shifts/<int:sid>/delete", methods=["POST"])
@require_admin
def admin_shift_delete(sid):
    db.delete_shift(sid)
    flash("Shift deleted", "success")
    return redirect(url_for("admin_shifts"))


# ---------------------------------------------------------------------------
# Admin — Coverage Log
# ---------------------------------------------------------------------------

@app.route("/admin/coverage-log")
@require_admin
def admin_coverage_log():
    selected = request.args.get("date", "")
    log_entries = db.get_coverage_log(selected if selected else None)
    return render_template("admin/coverage_log.html",
                           log=log_entries, selected=selected)


@app.route("/admin/inquiries")
@require_admin
def admin_contacts():
    submissions = db.get_contact_submissions()
    return render_template("admin/contacts.html", submissions=submissions)


# ---------------------------------------------------------------------------
# Incoming SMS webhook
# ---------------------------------------------------------------------------

@app.route("/sms", methods=["GET", "POST"])
def sms_incoming():
    sender = ""
    body = ""

    if request.is_json:
        data = request.get_json(force=True) or {}
        event_type = data.get("data", {}).get("event_type", "")
        if event_type and event_type != "message.received":
            log.info("Ignoring non-inbound event: %s", event_type)
            return "", 200
        payload = data.get("data", {}).get("payload", {})
        if payload:
            from_obj = payload.get("from", {})
            sender = (from_obj.get("phone_number") if isinstance(from_obj, dict) else from_obj) or ""
            body = payload.get("text", "")
        if not sender:
            sender = (data.get("from") or data.get("msisdn") or "").strip()
            body = (data.get("text") or "").strip()
    else:
        sender = (request.values.get("from") or request.values.get("msisdn") or "").strip()
        body = (request.values.get("text") or "").strip()

    log.info("Incoming SMS from %s: %r", sender, body)

    # Ignore empty/whitespace-only messages (avoids wasting AI API calls)
    if not body or not body.strip():
        log.info("Ignoring empty message from %s", sender)
        return "", 200

    try:
        handle_incoming(sender, body)
    except Exception:
        log.exception("handle_incoming crashed for sender=%s body=%r", sender, body)
    return "", 200


def handle_incoming(sender: str, body: str) -> None:
    caregiver = db.get_caregiver_by_phone(sender)
    if not caregiver:
        log.info("Ignoring SMS from unknown number %s", sender)
        return

    # Check for an active conversation session first.
    conv = db.get_conversation(sender)
    if conv and conv["state"] == "awaiting_shift_choice":
        _handle_shift_choice(caregiver, sender, body, conv["data"])
        return

    parsed = parse_message(body)
    intent = parsed.get("intent")
    log.info("Parsed intent=%s for %s", intent, caregiver["name"])

    if intent == "cancel_shift":
        _handle_cancel(caregiver, parsed)
    elif intent == "confirm_coverage":
        _handle_confirm(caregiver, sender)
    elif intent == "decline_coverage":
        _handle_decline(caregiver, sender)
    else:
        log.info("No-op intent for %s: %r", caregiver["name"], body)
        # Send a helpful reply so caregivers know their message was received
        owner_phone = os.getenv("OWNER_PHONE", "the office")
        send_sms(
            caregiver["phone"],
            f"Hi {caregiver['name'].split()[0]}! Your message was received but we couldn't process it automatically. "
            f"For schedule questions or changes, please call the office.",
        )


def _handle_cancel(caregiver: dict, parsed: dict) -> None:
    raw_date = parsed.get("shift_date")
    target_date = db.resolve_shift_date(raw_date)
    shift_time = parsed.get("shift_time")

    shifts = db.get_shifts_by_caregiver_and_date(caregiver["id"], target_date)

    if not shifts:
        date_label = "today" if target_date == db.today() else target_date
        send_sms(
            caregiver["phone"],
            f"We didn't find a shift on your schedule for {date_label} — please call the office.",
        )
        return

    # If multiple shifts and no time specified — ask which one.
    if len(shifts) > 1 and not shift_time:
        from coverage import _format_time
        options = " or ".join(
            f"{_format_time(s['start_time'])} ({db.get_client_by_id(s['client_id'])['name'] if db.get_client_by_id(s['client_id']) else '?'})"
            for s in shifts
        )
        send_sms(
            caregiver["phone"],
            f"You have {len(shifts)} shifts on {target_date}. Which one are you cancelling? ({options})",
        )
        db.upsert_conversation(
            caregiver["phone"],
            state="awaiting_shift_choice",
            data={"target_date": target_date, "shift_ids": [s["id"] for s in shifts]},
        )
        log.info("Awaiting shift choice from %s for %s", caregiver["name"], target_date)
        return

    # Disambiguate by time if provided.
    shift = shifts[0]
    if len(shifts) > 1 and shift_time:
        from coverage import _format_time
        lowered_time = shift_time.lower().replace(" ", "")
        for s in shifts:
            if _format_time(s["start_time"]).replace(" ", "") == lowered_time:
                shift = s
                break

    _do_cancel_shift(caregiver, shift, target_date)


def _handle_shift_choice(caregiver: dict, sender: str, body: str, conv_data: dict) -> None:
    """Resolve which shift the caregiver means after being asked."""
    target_date = conv_data.get("target_date", db.today())
    shift_ids = conv_data.get("shift_ids", [])
    shifts = [s for s in [db.get_shift_by_id(sid) for sid in shift_ids] if s]

    # Build context string for Claude
    from coverage import _format_time
    options_str = ", ".join(
        f"{_format_time(s['start_time'])}-{_format_time(s['end_time'])} for {(db.get_client_by_id(s['client_id']) or {}).get('name', '?')}"
        for s in shifts
    )
    context = f"The caregiver has shifts on {target_date}: {options_str}. They were asked which one to cancel."

    result = parse_message_with_context(body, context)
    resolved_time = result.get("shift_time")  # HH:MM format from Claude

    shift = None
    if resolved_time:
        for s in shifts:
            if s["start_time"] == resolved_time:
                shift = s
                break
        # Fallback: try matching display format
        if not shift:
            lowered = body.lower().replace(" ", "")
            for s in shifts:
                if _format_time(s["start_time"]).replace(" ", "") in lowered:
                    shift = s
                    break

    if not shift:
        # Still can't resolve — ask again
        options = " or ".join(
            f"{_format_time(s['start_time'])} ({(db.get_client_by_id(s['client_id']) or {}).get('name', '?')})"
            for s in shifts
        )
        send_sms(
            caregiver["phone"],
            f"Sorry, I didn't catch that. Which shift? ({options})",
        )
        log.info("Could not resolve shift choice from %s: %r", caregiver["name"], body)
        return

    # Got it — clear conversation and cancel
    db.clear_conversation(sender)
    _do_cancel_shift(caregiver, shift, target_date)


def _do_cancel_shift(caregiver: dict, shift: dict, target_date: str) -> None:
    """Mark a shift uncovered and fire the coverage hunt."""
    db.update_shift_status(shift["id"], "uncovered")
    send_sms(caregiver["phone"], "Got it — we'll find coverage. Feel better!")
    contacted = initiate_coverage_hunt(shift["id"])
    log.info(
        "Cancellation by %s: shift %s on %s, contacted %d candidates",
        caregiver["name"], shift["id"], target_date, len(contacted),
    )
    _ORIGINAL_CAREGIVER_NAMES[shift["id"]] = caregiver["name"]


_ORIGINAL_CAREGIVER_NAMES: dict[int, str] = {}


def _handle_confirm(caregiver: dict, sender: str) -> None:
    shift_id = db.get_pending_shift_for_phone(sender)
    if not shift_id:
        send_sms(
            caregiver["phone"],
            f"Hi {caregiver['name'].split()[0]}! There's no open coverage request for you right now. "
            f"If you received a coverage text, the shift may already be filled. "
            f"Reply to the original coverage request text or call the office.",
        )
        return

    if claim_shift(shift_id, caregiver["id"]):
        send_confirmation_to_caregiver(caregiver, shift_id)
        send_family_notification(shift_id)
        send_owner_summary(
            shift_id,
            original_caregiver_name=_ORIGINAL_CAREGIVER_NAMES.pop(shift_id, None),
        )
    else:
        send_sms(
            caregiver["phone"],
            "Thanks for stepping up! Another caregiver already claimed that shift.",
        )


def _handle_decline(caregiver: dict, sender: str) -> None:
    remove_candidate(sender)
    send_sms(caregiver["phone"], "Thanks for letting us know. We'll keep looking.")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def landing():
    # Logged-in staff go straight to their dashboard; visitors see the landing page.
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/dashboard")
@require_login
def dashboard():
    today = db.today()
    raw_date = request.args.get("date", today)
    try:
        selected = date_cls.fromisoformat(raw_date).isoformat()
    except ValueError:
        selected = today
    prev_date = (date_cls.fromisoformat(selected) - timedelta(days=1)).isoformat()
    next_date = (date_cls.fromisoformat(selected) + timedelta(days=1)).isoformat()
    shifts = db.get_shifts_for_date(selected)

    # Stat card counts
    total    = len(shifts)
    covered  = sum(1 for s in shifts if s["status"] in ("covered", "active"))
    scheduled = sum(1 for s in shifts if s["status"] == "scheduled")
    uncovered = sum(1 for s in shifts if s["status"] == "uncovered")

    # Recent activity feed — last 10 coverage events for selected date
    activity = db.get_coverage_log(selected)[:10]

    return render_template(
        "dashboard.html",
        shifts=shifts,
        today=today,
        selected=selected,
        prev_date=prev_date,
        next_date=next_date,
        agency_name=session.get("agency_name", "ShiftCare"),
        username=session.get("username", ""),
        total=total,
        covered=covered,
        scheduled=scheduled,
        uncovered=uncovered,
        activity=activity,
    )


# ---------------------------------------------------------------------------
# Employee portal — Phase 2: Auth (password + SMS OTP)
# ---------------------------------------------------------------------------

import random as _random
from datetime import datetime as _dt

def _require_employee(f):
    """Decorator: redirect to /employee/login if employee not authenticated."""
    @wraps(f)
    def _decorated(*args, **kwargs):
        if not session.get("employee_id"):
            return redirect(url_for("employee_login"))
        return f(*args, **kwargs)
    return _decorated


def _send_otp(phone: str, code: str) -> None:
    """Send the 6-digit verification code via SMS."""
    from sms import send_sms
    send_sms(phone, f"ShiftCare: Your login code is {code}. It expires in 10 minutes. Do not share this code.")


@app.route("/employee/login", methods=["GET", "POST"])
def employee_login():
    if session.get("employee_id"):
        return redirect(url_for("employee_dashboard"))
    error = None
    if request.method == "POST":
        phone_raw = request.form.get("phone", "").strip()
        password  = request.form.get("password", "")
        phone = db.normalize_phone(phone_raw)
        user = db.get_employee_by_phone(phone)
        if user and check_password_hash(user["password_hash"], password):
            # Credentials correct — generate and send OTP
            code = str(_random.randint(100000, 999999))
            expires = (_dt.utcnow() + __import__("datetime").timedelta(minutes=10)).isoformat()
            db.save_otp(phone, code, expires)
            _send_otp(phone, code)
            session["employee_pending_phone"] = phone  # temporary until OTP verified
            return redirect(url_for("employee_verify_otp"))
        error = "Incorrect phone number or password. Please try again."
    return render_template("employee/login.html", error=error)


@app.route("/employee/verify", methods=["GET", "POST"])
def employee_verify_otp():
    phone = session.get("employee_pending_phone")
    if not phone:
        return redirect(url_for("employee_login"))
    error = None
    if request.method == "POST":
        entered = request.form.get("otp", "").strip()
        otp_row = db.get_otp(phone)
        if otp_row and otp_row["attempts"] >= 5:
            error = "Too many attempts. Please start over."
            session.pop("employee_pending_phone", None)
        elif otp_row and _dt.utcnow().isoformat() > otp_row["expires_at"]:
            error = "Verification code expired. Please log in again."
            session.pop("employee_pending_phone", None)
            db.delete_otp(phone)
        elif otp_row and entered == otp_row["otp_code"]:
            # OTP correct — complete login
            db.delete_otp(phone)
            session.pop("employee_pending_phone", None)
            user = db.get_employee_by_phone(phone)
            session["employee_id"]    = user["id"]
            session["employee_name"]  = user["name"]
            session["employee_phone"] = phone
            db.update_employee_last_login(user["id"])
            return redirect(url_for("employee_dashboard"))
        else:
            db.increment_otp_attempts(phone)
            error = "Incorrect code. Please try again."
    return render_template("employee/verify.html", error=error)


@app.route("/employee/logout", methods=["GET", "POST"])
def employee_logout():
    session.pop("employee_id", None)
    session.pop("employee_name", None)
    session.pop("employee_phone", None)
    session.pop("employee_pending_phone", None)
    return redirect(url_for("employee_login"))


@app.route("/employee/resend-otp", methods=["POST"])
def employee_resend_otp():
    phone = session.get("employee_pending_phone")
    if not phone:
        return redirect(url_for("employee_login"))
    code = str(_random.randint(100000, 999999))
    expires = (_dt.utcnow() + __import__("datetime").timedelta(minutes=10)).isoformat()
    db.save_otp(phone, code, expires)
    _send_otp(phone, code)
    flash("A new code has been sent to your phone.", "success")
    return redirect(url_for("employee_verify_otp"))


@app.route("/employee/")
@app.route("/employee/dashboard")
@_require_employee
def employee_dashboard():
    cid = session["employee_id"]
    today_shift = db.get_today_shift_for_employee(cid)
    upcoming    = db.get_upcoming_shifts_for_employee(cid, limit=4)
    week_stats  = db.get_week_stats(cid)
    clock_state = None
    if today_shift:
        clock_state = "in" if db.is_clocked_in(today_shift["id"], cid) else "out"
    return render_template(
        "employee/dashboard.html",
        today_shift=today_shift,
        upcoming=upcoming,
        week_stats=week_stats,
        clock_state=clock_state,
    )


@app.route("/employee/clock-in/<int:shift_id>", methods=["POST"])
@_require_employee
def employee_clock_in(shift_id: int):
    cid = session["employee_id"]
    if not db.is_clocked_in(shift_id, cid):
        db.clock_in(shift_id, cid)
        db.update_shift_status(shift_id, "active")
        flash("Clocked in successfully!", "success")
    else:
        flash("You are already clocked in.", "error")
    return redirect(url_for("employee_dashboard"))


@app.route("/employee/clock-out/<int:shift_id>", methods=["POST"])
@_require_employee
def employee_clock_out(shift_id: int):
    cid = session["employee_id"]
    if db.is_clocked_in(shift_id, cid):
        db.clock_out(shift_id, cid)
        db.update_shift_status(shift_id, "covered")
        flash("Clocked out. Great work today!", "success")
    else:
        flash("You haven't clocked in yet.", "error")
    return redirect(url_for("employee_dashboard"))


@app.route("/employee/shifts")
@_require_employee
def employee_shifts():
    cid = session["employee_id"]
    today = date_cls.today()
    start_month = today.replace(day=1).isoformat()
    upcoming = db.get_upcoming_shifts_for_employee(cid, limit=20)
    past = db.get_shifts_for_caregiver_range(cid, start_month, today.isoformat())
    past = [s for s in past if s["date"] < today.isoformat()]
    rate = db.get_caregiver_pay_rate(cid)
    for s in past:
        s["hours"] = db._calc_shift_hours(s)
        s["earned"] = round(s["hours"] * rate, 2)
    return render_template("employee/shifts.html", upcoming=upcoming, past=past)


@app.route("/employee/shifts/<int:shift_id>")
@_require_employee
def employee_shift_detail(shift_id: int):
    cid = session["employee_id"]
    # Verify this shift belongs to the logged-in employee
    with db.get_conn() as conn:
        row = conn.execute(
            """SELECT s.*, cl.name AS client_name, cl.address AS client_address,
                      cl.care_notes, cl.family_phone
               FROM shifts s JOIN clients cl ON cl.id = s.client_id
               WHERE s.id = ? AND s.caregiver_id = ?""",
            (shift_id, cid),
        ).fetchone()
    if not row:
        flash("Shift not found.", "error")
        return redirect(url_for("employee_shifts"))
    shift = db._row_to_dict(row)
    rate = db.get_caregiver_pay_rate(cid)
    # Get clock events for this shift
    events = db.get_clock_events_for_shift(shift_id)
    clock_in_evt  = next((e for e in events if e["event_type"] == "clock_in"),  None)
    clock_out_evt = next((e for e in events if e["event_type"] == "clock_out"), None)
    hours  = db._calc_shift_hours(shift)
    earned = round(hours * rate, 2)
    return render_template(
        "employee/shift_detail.html",
        shift=shift,
        rate=rate,
        hours=hours,
        earned=earned,
        clock_in_evt=clock_in_evt,
        clock_out_evt=clock_out_evt,
    )


@app.route("/employee/pay")
@_require_employee
def employee_pay():
    cid = session["employee_id"]
    week  = db.get_week_stats(cid)
    month = db.get_month_stats(cid)
    ytd   = db.get_ytd_stats(cid)
    db.get_or_create_current_pay_period()
    periods = db.get_pay_periods(limit=12)
    rate = db.get_caregiver_pay_rate(cid)
    period_summaries = []
    for p in periods:
        h = db.get_hours_worked(cid, p["period_start"], p["period_end"])
        e = round(h * rate, 2)
        period_summaries.append({**p, "hours": h, "earnings": e})
    return render_template("employee/pay.html",
                           week=week, month=month, ytd=ytd,
                           periods=period_summaries, rate=rate)


@app.route("/employee/pay-stub/<int:period_id>")
@_require_employee
def employee_pay_stub(period_id: int):
    cid = session["employee_id"]
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM pay_periods WHERE id = ? LIMIT 1", (period_id,)).fetchone()
    if not row:
        flash("Pay period not found.", "error")
        return redirect(url_for("employee_pay"))
    period = db._row_to_dict(row)
    rate = db.get_caregiver_pay_rate(cid)
    shifts = db.get_shifts_for_caregiver_range(cid, period["period_start"], period["period_end"])
    shift_rows = []
    for s in shifts:
        if s["status"] == "cancelled":
            continue
        hours  = db._calc_shift_hours(s)
        earned = round(hours * rate, 2)
        shift_rows.append({**s, "hours": hours, "earned": earned})
    total_hours  = round(sum(s["hours"] for s in shift_rows), 2)
    total_earned = round(sum(s["earned"] for s in shift_rows), 2)
    caregivers = db.get_all_caregivers()
    caregiver  = next((c for c in caregivers if c["id"] == cid), None)
    agency_name = session.get("agency_name", "ShiftCare Agency")
    return render_template(
        "employee/pay_stub.html",
        period=period,
        shift_rows=shift_rows,
        total_hours=total_hours,
        total_earned=total_earned,
        rate=rate,
        caregiver=caregiver,
        agency_name=agency_name,
    )


@app.route("/employee/profile")
@_require_employee
def employee_profile():
    import json as _json
    cid = session["employee_id"]
    caregivers = db.get_all_caregivers()
    caregiver  = next((c for c in caregivers if c["id"] == cid), None)
    avail = {}
    if caregiver and caregiver.get("availability_json"):
        try:
            avail = _json.loads(caregiver["availability_json"])
        except (ValueError, TypeError):
            avail = {}
    return render_template("employee/profile.html", caregiver=caregiver, avail=avail)


@app.route("/employee/update-availability", methods=["POST"])
@_require_employee
def employee_update_availability():
    import json as _json
    cid = session["employee_id"]
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    availability = {}
    for day in days:
        if request.form.get(f"day_{day}"):
            start = request.form.get(f"start_{day}", "8am").strip()
            end   = request.form.get(f"end_{day}",   "5pm").strip()
            if start and end:
                availability[day] = [f"{start}-{end}"]
    db.update_caregiver_availability(cid, _json.dumps(availability))
    flash("Availability updated successfully!", "success")
    return redirect(url_for("employee_profile"))


@app.route("/employee/change-password", methods=["GET", "POST"])
@_require_employee
def employee_change_password():
    cid = session["employee_id"]
    error = None
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw     = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")
        user = db.get_employee_account(cid)
        if not user or not check_password_hash(user["password_hash"], current_pw):
            error = "Current password is incorrect."
        elif len(new_pw) < 8:
            error = "New password must be at least 8 characters."
        elif new_pw != confirm_pw:
            error = "Passwords do not match."
        else:
            db.update_employee_password(cid, generate_password_hash(new_pw, method="pbkdf2:sha256"))
            flash("Password updated successfully.", "success")
            return redirect(url_for("employee_profile"))
    return render_template("employee/change_password.html", error=error)


# ---------------------------------------------------------------------------
# Family portal
# ---------------------------------------------------------------------------

@app.route("/client/<token>")
def family_portal(token: str):
    data = db.get_shift_by_token(token)
    if not data:
        return render_template("client_portal.html", error="This link is not valid or has expired."), 404
    from coverage import _format_time
    data["agency_phone"] = os.getenv("OWNER_PHONE", "")
    return render_template(
        "client_portal.html",
        error=None,
        shift=data,
        start=_format_time(data["start_time"]),
        end=_format_time(data["end_time"]),
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
# Public legal pages (required for 10DLC compliance)
# ---------------------------------------------------------------------------

@app.route("/about")
def about():
    return render_template("about.html")



def _send_contact_email(name: str, email: str, agency: str, message: str) -> None:
    """Email a contact form submission to the owner via SMTP (no-op if unconfigured)."""
    host = os.getenv("SMTP_HOST")
    if not host:
        log.info("SMTP not configured — contact email skipped (submission still saved to DB)")
        return
    import smtplib
    from email.message import EmailMessage

    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    to_addr = os.getenv("CONTACT_TO_EMAIL", "amanabbas@shiftcare.com")
    from_addr = os.getenv("SMTP_FROM", user or to_addr)

    msg = EmailMessage()
    msg["Subject"] = f"New ShiftCare inquiry from {name}"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Reply-To"] = email
    msg.set_content(
        f"New contact form submission:\n\n"
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"Agency: {agency or '(not provided)'}\n\n"
        f"Message:\n{message}\n"
    )
    try:
        with smtplib.SMTP(host, port, timeout=10) as server:
            server.starttls()
            if user:
                server.login(user, password)
            server.send_message(msg)
        log.info("Contact email sent to %s", to_addr)
    except Exception:
        log.exception("Failed to send contact email")


@app.route("/contact", methods=["POST"])
def contact():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    agency = request.form.get("agency", "").strip()
    message = request.form.get("message", "").strip()

    if not name or not email or not message:
        flash("Please fill in your name, email, and message.", "error")
        return redirect(url_for("landing") + "#contact")

    db.save_contact_submission(name, email, agency, message)
    _send_contact_email(name, email, agency, message)
    flash("Thanks! We received your message and will be in touch shortly.", "success")
    return redirect(url_for("landing") + "#contact")


@app.route("/privacy")
def privacy_policy():
    return render_template("privacy.html")


@app.route("/terms")
def terms_of_service():
    return render_template("terms.html")


@app.route("/intake-form")
def intake_form():
    return render_template("intake_form.html")


# ---------------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    return {"ok": True}


@app.route("/admin/seed-database", methods=["POST"])
@require_admin
def admin_seed_database():
    """Seed the database with test data. Protected admin endpoint."""
    import subprocess
    import sys
    try:
        # Run the seed script
        result = subprocess.run(
            [sys.executable, "seed.py"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            flash("Database seeded successfully!", "success")
            return redirect(url_for("admin_shifts"))
        else:
            flash(f"Seed failed: {result.stderr}", "error")
            return redirect(url_for("admin_shifts"))
    except Exception as e:
        flash(f"Seed error: {str(e)}", "error")
        return redirect(url_for("admin_shifts"))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)