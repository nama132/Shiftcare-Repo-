"""ShiftCare Flask app — incoming SMS webhook + live dashboard + admin panel."""
from __future__ import annotations

import logging
import os
from datetime import date as date_cls, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, flash, redirect, render_template,
                   request, session, url_for)

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
# Admin auth
# ---------------------------------------------------------------------------

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == os.getenv("ADMIN_PASSWORD", "shiftcare2026"):
            session["admin"] = True
            return redirect(url_for("admin_caregivers"))
        flash("Wrong password", "error")
    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


# ---------------------------------------------------------------------------
# Admin — Caregivers
# ---------------------------------------------------------------------------

@app.route("/admin/caregivers")
@require_admin
def admin_caregivers():
    caregivers = db.get_all_caregivers()
    return render_template("admin/caregivers.html", caregivers=caregivers)


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
            "Thanks! We didn't have an open coverage request for you — already filled.",
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

@app.route("/dashboard")
@app.route("/")
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
    return render_template(
        "dashboard.html",
        shifts=shifts,
        today=today,
        selected=selected,
        prev_date=prev_date,
        next_date=next_date,
    )


# ---------------------------------------------------------------------------
# Family portal
# ---------------------------------------------------------------------------

@app.route("/client/<token>")
def family_portal(token: str):
    data = db.get_shift_by_token(token)
    if not data:
        return render_template("client_portal.html", error="This link is not valid or has expired."), 404
    from coverage import _format_time
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

@app.route("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)