"""Call orchestration (UC1 of the FLM workflow design):
NexTask event intake -> current-state projection -> timers -> technician calls, escalation calls, reminder call.

Every scheduled contact re-checks the latest ticket state first (contact guard), so a closed ticket,
a reassignment or a cleared alarm cancels pending work, and stale state holds it.
"""
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.request

import ae_whatsapp
import texts
import twilio_client
from config import CFG
from store import (audit, epoch, get_call, get_ticket, iso_at, now_iso, parse_time, q, ticket_lock,
                   update_call, update_ticket)

EVENT_TYPES = {
    "ASSIGNMENT_CREATED", "REASSIGNED", "ASSIGNMENT_ACCEPTED", "ASSIGNMENT_REJECTED",
    "OPERATIONAL_STATUS", "ALARM_STATUS", "TICKET_CREATED", "TICKET_UPDATED", "SEVERITY_CHANGED", "TICKET_CLOSED",
}
TICKET_FIELDS = ("ticket_id", "alarm_id", "assignment_id", "site_number", "site_name", "severity", "status",
                 "alarm_status", "operational_status", "description")
CALLED_ROLES = ("technician", "team_leader", "manager")
E164 = re.compile(r"\+[1-9]\d{7,14}")

TECH_ACTIVE = {"TECH_CALLING", "TECH_WAITING_RETRY"}
ACTIVE = TECH_ACTIVE | {"ESCALATING"}
FINAL = {"ACKNOWLEDGED", "TL_REACHED", "MANAGER_REACHED", "ESCALATION_UNRESOLVED", "ESCALATION_NO_CONTACTS",
         "CANCELLED_REASSIGNED", "CANCELLED_CLOSED", "CANCELLED_ALARM_CLEARED"}

# Twilio final CallStatus -> outcome when the callee never got to respond
UNANSWERED_OUTCOME = {"no-answer": "not_picked_up", "busy": "busy_or_declined",
                      "failed": "call_failed", "canceled": "canceled"}
NOT_PICKED_UP = {"not_picked_up", "busy_or_declined", "call_failed", "canceled", "call_error"}
RESPONSE_TEXT = {
    "acknowledged": "acknowledged the assignment",
    "rejected": "rejected the assignment",
    "reached": "picked up but did not press 1 or 9 (counted as no response)",
    "reassign_yes": "answered: yes, assign the alert to another technician",
    "reassign_no": "answered: no, do not assign the alert to another technician",
    "reminder_delivered": "answered the reminder call",
    "not_picked_up": "did not pick up the call",
    "busy_or_declined": "line was busy or the call was declined",
    "call_failed": "call could not be connected",
    "canceled": "call was canceled",
    "call_error": "call could not be started",
    "no_input": "picked up but did not press any key",
    "invalid_input": "picked up but pressed an invalid key",
    "hung_up_without_response": "picked up but hung up before responding",
}
WORKFLOW_PURPOSE = {"uc1_technician": "technician", "uc1_escalation": "escalation", "uc1_reminder": "reminder"}
WEBHOOK_DELAYS = (0, 5, 15, 30)


class EventError(ValueError):
    pass


# =========================================================================== event intake
def _digit(value, field):
    if value in (None, ""):
        return None
    value = str(value).strip()
    if not re.fullmatch(r"[0-9]", value):
        raise EventError(f"{field} must be a single digit 0-9")
    return value


def normalize_event(p):
    """Validate a NexTask event. Returns (event, ticket_fields, recipients)."""
    if not isinstance(p, dict):
        raise EventError("body must be a JSON object")
    ev = {"event_id": str(p.get("event_id") or "").strip(),
          "event_type": str(p.get("event_type") or "").strip().upper()}
    if not ev["event_id"]:
        raise EventError("event_id is required")
    if ev["event_type"] not in EVENT_TYPES:
        raise EventError(f"event_type must be one of: {', '.join(sorted(EVENT_TYPES))}")
    ev["event_time"] = p.get("event_time")
    if ev["event_time"] and parse_time(ev["event_time"]) is None:
        raise EventError("event_time must be ISO-8601, e.g. 2026-10-14T10:05:00Z")
    try:
        ev["version"] = int(p["version"]) if p.get("version") not in (None, "") else None
    except (TypeError, ValueError):
        raise EventError("version must be an integer") from None
    ev["ack_digit"] = _digit(p.get("ack_digit"), "ack_digit")
    ev["reject_digit"] = _digit(p.get("reject_digit"), "reject_digit")
    ev["callback_url"] = str(p.get("callback_url") or "").strip() or None
    if ev["callback_url"] and not re.match(r"https?://[^\s/]+", ev["callback_url"]):
        raise EventError("callback_url must start with http:// or https://")

    nested = p.get("ticket") if isinstance(p.get("ticket"), dict) else {}
    fields = {k: nested.get(k, p.get(k)) for k in TICKET_FIELDS}
    fields = {k: str(v).strip() for k, v in fields.items() if v not in (None, "")}
    if not fields.get("ticket_id"):
        raise EventError("ticket.ticket_id is required")
    fields["ticket_id"] = fields["ticket_id"].upper()

    raw = p.get("recipients") or []
    if isinstance(raw, dict):
        raw = [dict(v, role=k) for k, v in raw.items()]
    recipients = {}
    for r in raw:
        role = str(r.get("role") or "").strip().lower()
        if not role:
            raise EventError("every recipient needs a role")
        mobile = str(r.get("mobile") or "").strip()
        if mobile and not E164.fullmatch(mobile):
            raise EventError(f"recipient '{role}' mobile must be E.164, e.g. +966500000000 (got '{mobile}')")
        lang = str(r.get("language") or "").strip().lower()
        if lang and lang != "ask" and lang not in texts.LANGS:
            raise EventError(f"recipient '{role}' language must be one of {', '.join(texts.LANGS)} or 'ask'")
        recipients[role] = {"name": str(r.get("name") or "").strip(), "mobile": mobile, "language": lang}

    if ev["event_type"] in ("ASSIGNMENT_CREATED", "REASSIGNED"):
        if not fields.get("assignment_id"):
            raise EventError(f"{ev['event_type']} needs ticket.assignment_id")
        if not recipients.get("technician", {}).get("mobile"):
            raise EventError(f"{ev['event_type']} needs a recipient with role 'technician' and a mobile")
    return ev, fields, recipients


def _stale_reason(t, ev):
    if not t:
        return None
    if ev["version"] is not None and t["version"] is not None and ev["version"] < t["version"]:
        return f"version {ev['version']} is older than current version {t['version']}"
    if ev["version"] is None and ev["event_time"] and t["last_event_time"]:
        if parse_time(ev["event_time"]) < parse_time(t["last_event_time"]):
            return f"event_time {ev['event_time']} is older than last event {t['last_event_time']}"
    return None


def ingest_event(payload):
    """POST /api/events. Returns (http_status, response_dict)."""
    ev, fields, recipients = normalize_event(payload)
    tid = fields["ticket_id"]
    with ticket_lock(tid):
        if q("SELECT 1 FROM events WHERE event_id=?", (ev["event_id"],), one=True):
            audit(tid, "event_duplicate", f"Duplicate event {ev['event_id']} suppressed", event_id=ev["event_id"])
            return 200, {"event_id": ev["event_id"], "result": "duplicate", "ticket_id": tid, "actions": []}
        t = get_ticket(tid)
        stale = _stale_reason(t, ev)
        q("INSERT INTO events (event_id, ticket_id, event_type, version, event_time, received_at, result, note, "
          "payload) VALUES (?,?,?,?,?,?,?,?,?)",
          (ev["event_id"], tid, ev["event_type"], ev["version"], ev["event_time"], now_iso(),
           "stale" if stale else "accepted", stale, json.dumps(payload, ensure_ascii=False)))
        if stale:
            audit(tid, "event_stale", f"{ev['event_type']} {ev['event_id']} ignored: {stale}", event_id=ev["event_id"])
            return 200, {"event_id": ev["event_id"], "result": "stale", "ticket_id": tid, "note": stale,
                         "actions": []}
        audit(tid, "event_accepted", f"{ev['event_type']} {ev['event_id']}", event_id=ev["event_id"])
        previous = t
        t = _apply_state(tid, t, ev, fields, recipients)
        actions = _release_held_jobs(tid)
        actions += _dispatch(t, previous, ev)
        q("UPDATE events SET note=? WHERE event_id=?", ("; ".join(actions) or None, ev["event_id"]))
        return 202, {"event_id": ev["event_id"], "result": "accepted", "ticket_id": tid, "actions": actions,
                     "ticket": ticket_view(tid)}


def _apply_state(tid, t, ev, fields, recipients):
    if not t:
        q("INSERT INTO tickets (ticket_id, recipients, created_at, updated_at) VALUES (?,?,?,?)",
          (tid, "{}", now_iso(), now_iso()))
        t = get_ticket(tid)
    merged = dict(t["recipients"])
    merged.update(recipients)
    changes = {k: v for k, v in fields.items() if k != "ticket_id"}
    if ev["event_type"] == "TICKET_CLOSED" and (changes.get("status") or "").lower() not in CFG["closed_statuses"]:
        changes["status"] = "Closed"  # the event type alone means closed
    changes.update(recipients=merged, last_event_id=ev["event_id"],
                   last_event_time=ev["event_time"] or now_iso(), last_event_received_at=epoch())
    if ev["version"] is not None:
        changes["version"] = max(ev["version"], t["version"] or ev["version"])
    for key in ("ack_digit", "reject_digit", "callback_url"):
        if ev[key]:
            changes[key] = ev[key]
    ack = changes.get("ack_digit") or t["ack_digit"] or CFG["default_ack_digit"]
    rej = changes.get("reject_digit") or t["reject_digit"] or CFG["default_reject_digit"]
    if ack == rej:  # never let accept and reject share a key
        rej = next(d for d in "9876543210" if d != ack)
    changes.update(ack_digit=ack, reject_digit=rej)
    update_ticket(tid, **changes)
    return get_ticket(tid)


def _is_closed(t):
    return (t["status"] or "").lower() in CFG["closed_statuses"]


def _alarm_cleared(t):
    return (t["alarm_status"] or "").lower() in CFG["alarm_cleared_values"]


def _dispatch(t, previous, ev):
    kind, tid = ev["event_type"], t["ticket_id"]
    # Closure and alarm clearance override everything else (PDF p.4)
    if _is_closed(t):
        return _stop_all(t, "CANCELLED_CLOSED", "STOPPED_CLOSED", "ticket closed")
    if kind == "ALARM_STATUS" or (kind in ("TICKET_UPDATED", "TICKET_CREATED") and _alarm_cleared(t)):
        if _alarm_cleared(t):
            return _stop_all(t, "CANCELLED_ALARM_CLEARED", "STOPPED_ALARM_CLEARED", "alarm cleared")
        return [f"alarm status {t['alarm_status']} recorded"]

    if kind == "ASSIGNMENT_CREATED":
        if t["uc1_assignment_id"] == t["assignment_id"] and t["uc1_state"] and \
                not t["uc1_state"].startswith("CANCELLED"):
            audit(tid, "assignment_duplicate", f"Assignment {t['assignment_id']} already handled - suppressed")
            return ["assignment already being handled - suppressed"]
        actions = []
        if t["uc1_assignment_id"] and t["uc1_assignment_id"] != t["assignment_id"]:
            actions += _stop_all(t, "CANCELLED_REASSIGNED", "STOPPED_REASSIGNED", "new assignment")
        return actions + _start_uc1(get_ticket(tid))

    if kind == "REASSIGNED":
        actions = _stop_all(t, "CANCELLED_REASSIGNED", "STOPPED_REASSIGNED",
                            f"reassigned from {previous['assignment_id'] if previous else '-'}")
        return actions + _start_uc1(get_ticket(tid))

    if kind == "ASSIGNMENT_ACCEPTED":
        if t["uc1_state"] in ACTIVE:
            _cancel_jobs(tid, ("tech_call", "tl_call", "manager_call"), "accepted in NexTask")
            _end_live_calls(tid, "accepted in NexTask")
            return _mark_acknowledged(get_ticket(tid), "nextask")
        return [f"accepted in NexTask (call workflow state {t['uc1_state']} unchanged)"]

    if kind == "ASSIGNMENT_REJECTED":
        if t["uc1_state"] in TECH_ACTIVE:
            _cancel_jobs(tid, ("tech_call",), "rejected in NexTask")
            _end_live_calls(tid, "rejected in NexTask")
            return _escalate(get_ticket(tid), "rejected", 0)
        return [f"rejection recorded (call workflow state {t['uc1_state']} unchanged)"]

    if kind == "OPERATIONAL_STATUS":
        status = (t["operational_status"] or "").lower()
        if status in CFG["operational_done_statuses"] and t["followup_state"] == "SCHEDULED":
            _cancel_jobs(tid, ("followup",), f"operational status {t['operational_status']} recorded")
            update_ticket(tid, followup_state="STOPPED_STATUS_RECORDED")
            return [f"follow-up reminder cancelled: {t['operational_status']} recorded"]
        return [f"operational status {t['operational_status']} recorded"]

    if kind == "SEVERITY_CHANGED":
        return [f"severity {previous['severity'] if previous else '-'} -> {t['severity']}"]
    return ["ticket state updated"]


# =========================================================================== timers
def _schedule(tid, assignment_id, kind, attempt, delay):
    run_at = epoch() + delay
    q("INSERT INTO jobs (ticket_id, assignment_id, kind, attempt_no, run_at, status, created_at, updated_at) "
      "VALUES (?,?,?,?,?,'pending',?,?)", (tid, assignment_id, kind, attempt, run_at, now_iso(), now_iso()))
    if kind == "followup":
        update_ticket(tid, followup_state="SCHEDULED", followup_due_at=iso_at(run_at))
    audit(tid, "timer_scheduled", f"{kind} #{attempt} due {iso_at(run_at)}")
    return f"{kind} #{attempt} scheduled for {iso_at(run_at)}"


def _cancel_jobs(tid, kinds, why):
    marks = ",".join("?" * len(kinds))
    n = q(f"UPDATE jobs SET status='cancelled', note=?, updated_at=? WHERE ticket_id=? "
          f"AND status IN ('pending','held') AND kind IN ({marks})", (why, now_iso(), tid, *kinds))
    if n:
        audit(tid, "timer_cancelled", f"{n} timer(s) cancelled: {why}")
    return n


def _release_held_jobs(tid):
    n = q("UPDATE jobs SET status='pending', run_at=?, note='released by new event', updated_at=? "
          "WHERE ticket_id=? AND status='held'", (epoch(), now_iso(), tid))
    if n:
        audit(tid, "timer_released", f"{n} held contact(s) released after fresh event")
        return [f"{n} held contact(s) released"]
    return []


def _end_live_calls(tid, why):
    for c in q("SELECT * FROM calls WHERE ticket_id=? AND processed=0 AND superseded IS NULL", (tid,)):
        update_call(c["id"], superseded=why)
        try:
            twilio_client.end_call(c["call_sid"], c["status"])
        except Exception as e:  # noqa: BLE001 - call may already be over
            audit(tid, "call_end_failed", f"Could not end call {c['call_sid']}: {e}", call_id=c["id"])
        audit(tid, "call_superseded", f"{c['role']} call #{c['attempt_no']} ended: {why}", call_id=c["id"])


def _stop_all(t, uc1_state, followup_state, why):
    tid = t["ticket_id"]
    n = _cancel_jobs(tid, ("tech_call", "tl_call", "manager_call", "followup"), why)
    _end_live_calls(tid, why)
    changes, actions = {}, [f"{n} pending contact(s) cancelled: {why}"] if n else []
    if t["uc1_state"] in ACTIVE:
        changes["uc1_state"] = uc1_state
        actions.append(f"call workflow -> {uc1_state}")
    if t["followup_state"] == "SCHEDULED":
        changes["followup_state"] = followup_state
        actions.append(f"follow-up -> {followup_state}")
    update_ticket(tid, **changes)
    if "uc1_state" in changes:
        notify(tid, "uc1.cancelled", {"reason": why})
    return actions or [why]


def contact_guard(t, job):
    """None = go ahead; ('cancel'|'hold', reason) otherwise (PDF p.5 CONTACT GUARD)."""
    if _is_closed(t):
        return "cancel", "ticket closed"
    if job["assignment_id"] != t["assignment_id"]:
        return "cancel", f"assignment changed to {t['assignment_id']}"
    if _alarm_cleared(t):
        return "cancel", "alarm cleared"
    age = epoch() - (t["last_event_received_at"] or 0)
    if CFG["state_max_age_sec"] and age > CFG["state_max_age_sec"]:
        return "hold", f"ticket state is {int(age)} s old (limit {CFG['state_max_age_sec']} s)"
    needs = {"tech_call": TECH_ACTIVE, "tl_call": {"ESCALATING"}, "manager_call": {"ESCALATING"}}
    if job["kind"] in needs and t["uc1_state"] not in needs[job["kind"]]:
        return "cancel", f"no longer needed (call workflow is {t['uc1_state']})"
    if job["kind"] == "followup" and t["followup_state"] != "SCHEDULED":
        return "cancel", f"no longer needed (follow-up is {t['followup_state']})"
    return None


def run_job(job):
    tid = job["ticket_id"]
    with ticket_lock(tid):
        t = get_ticket(tid)
        verdict = contact_guard(t, job)
        if verdict:
            action, why = verdict
            q("UPDATE jobs SET status=?, note=?, updated_at=? WHERE id=?",
              ("held" if action == "hold" else "cancelled", why, now_iso(), job["id"]))
            audit(tid, "contact_held" if action == "hold" else "contact_cancelled",
                  f"{job['kind']} #{job['attempt_no']}: {why}")
            return
        q("UPDATE jobs SET status='done', updated_at=? WHERE id=?", (now_iso(), job["id"]))
        kind, n = job["kind"], job["attempt_no"]
        if kind == "tech_call":
            update_ticket(tid, uc1_state="TECH_CALLING")
            _place_call(t, "technician", n, "uc1_technician")
        elif kind == "tl_call":
            _place_call(t, "team_leader", n, "uc1_escalation")
        elif kind == "manager_call":
            _place_call(t, "manager", n, "uc1_escalation")
        elif kind == "followup":
            _followup_check(t)


class Scheduler(threading.Thread):
    """Runs due timers. Timers live in the database, so they survive a restart."""

    def __init__(self, tick=0.5):
        super().__init__(daemon=True)
        self.tick, self.stopped = tick, threading.Event()

    def run(self):
        while not self.stopped.is_set():
            try:
                for job in q("SELECT * FROM jobs WHERE status='pending' AND run_at<=? ORDER BY run_at", (epoch(),)):
                    if q("UPDATE jobs SET status='running' WHERE id=? AND status='pending'", (job["id"],)):
                        threading.Thread(target=run_job, args=(job,), daemon=True).start()
            except Exception as e:  # noqa: BLE001 - keep the scheduler alive
                print(f"scheduler error: {e}", flush=True)
            self.stopped.wait(self.tick)


# =========================================================================== UC1 steps
def _start_uc1(t):
    tid = t["ticket_id"]
    update_ticket(tid, uc1_assignment_id=t["assignment_id"], uc1_state="TECH_CALLING", uc1_reason=None,
                  uc1_ack_via=None, uc1_ack_at=None, esc_reason=None, esc_detail=None,
                  followup_state=None, followup_due_at=None)
    audit(tid, "workflow_started", f"Technician notification for assignment {t['assignment_id']}")
    return [_schedule(tid, t["assignment_id"], "tech_call", 1, 0)]


def _mark_acknowledged(t, via):
    tid = t["ticket_id"]
    update_ticket(tid, uc1_state="ACKNOWLEDGED", uc1_ack_via=via, uc1_ack_at=now_iso())
    _cancel_jobs(tid, ("tech_call", "tl_call", "manager_call"), "acknowledged")
    audit(tid, "acknowledged", f"Technician acknowledged via {via}")
    if CFG["followup_sec"] > 0:
        action = _schedule(tid, t["assignment_id"], "followup", 1, CFG["followup_sec"])
    else:
        update_ticket(tid, followup_state="AWAITING_TRIGGER")
        action = "follow-up awaits POST /api/tickets/{ticket_id}/followup"
    notify(tid, "uc1.acknowledged", {"via": via})
    notify(tid, "uc1.completed", {"state": "ACKNOWLEDGED"})
    return [f"acknowledged via {via}", action]


def _escalate(t, reason, tech_calls):
    """PDF p.5: stop technician retries, call Team Leader (1 attempt) then Manager (up to 2)."""
    tid = t["ticket_id"]
    _cancel_jobs(tid, ("tech_call",), f"escalation: {reason}")
    update_ticket(tid, uc1_state="ESCALATING", esc_reason=reason, esc_detail=str(tech_calls))
    audit(tid, "escalation_started", f"Escalation started: {reason} after {tech_calls} technician call(s)")
    notify(tid, "uc1.escalation_started", {"reason": reason, "technician_calls": tech_calls})
    r = t["recipients"]
    if r.get("team_leader", {}).get("mobile"):
        return [f"escalation started ({reason})", _schedule(tid, t["assignment_id"], "tl_call", 1, 0)]
    if r.get("manager", {}).get("mobile"):
        return [f"escalation started ({reason}), no team leader number",
                _schedule(tid, t["assignment_id"], "manager_call", 1, 0)]
    return [f"escalation started ({reason})"] + _finish_uc1(tid, "ESCALATION_NO_CONTACTS")


def _finish_uc1(tid, state):
    update_ticket(tid, uc1_state=state)
    audit(tid, "workflow_finished", f"Call workflow finished: {state}")
    notify(tid, "uc1.completed", {"state": state})
    return [f"call workflow -> {state}"]


def _followup_check(t):
    """PDF p.4, at +15 min: closed / reassigned / alarm cleared are already handled by the contact guard."""
    tid = t["ticket_id"]
    status = (t["operational_status"] or "").lower()
    if status in CFG["operational_done_statuses"]:
        update_ticket(tid, followup_state="STOPPED_STATUS_RECORDED")
        audit(tid, "followup_stopped", f"Reminder not needed: {t['operational_status']} recorded")
        return
    update_ticket(tid, followup_state="REMINDER_CALLING")
    _place_call(t, "technician", 1, "uc1_reminder")


def trigger_followup(tid, assignment_id=None):
    """POST /api/tickets/{ticket_id}/followup - the caller's 15-minute timer fired (PDF p.4).
    Returns (http_status, response). Places the reminder call only when it is still needed."""
    with ticket_lock(tid):
        t = get_ticket(tid)
        if not t:
            return 404, {"error": "ticket not found"}

        def not_needed(reason, state=None):
            if state:
                update_ticket(tid, followup_state=state)
            audit(tid, "followup_not_needed", f"Reminder not placed: {reason}")
            return 200, {"ticket_id": tid, "result": "not_needed", "reason": reason}

        if _is_closed(t):
            return not_needed("ticket closed", "STOPPED_CLOSED")
        if assignment_id and assignment_id != t["assignment_id"]:
            return not_needed(f"ticket reassigned to {t['assignment_id']}", "STOPPED_REASSIGNED")
        if t["uc1_state"] != "ACKNOWLEDGED" or t["uc1_assignment_id"] != t["assignment_id"]:
            return 409, {"ticket_id": tid, "result": "rejected",
                         "reason": "technician has not acknowledged the current assignment "
                                   f"(call workflow is {t['uc1_state']})"}
        if (t["operational_status"] or "").lower() in CFG["operational_done_statuses"]:
            return not_needed(f"operational status {t['operational_status']} already recorded",
                              "STOPPED_STATUS_RECORDED")
        if _alarm_cleared(t):
            return not_needed("alarm cleared", "STOPPED_ALARM_CLEARED")
        if t["followup_state"] == "REMINDER_CALLING":
            return 409, {"ticket_id": tid, "result": "rejected", "reason": "a reminder call is already in progress"}
        _cancel_jobs(tid, ("followup",), "triggered by API")
        update_ticket(tid, followup_state="REMINDER_CALLING")
        cid = _place_call(t, "technician", 1, "uc1_reminder")
        return 202, {"ticket_id": tid, "result": "reminder_call_placed", "call_id": cid}


def _place_call(t, role, attempt, workflow):
    tid = t["ticket_id"]
    rec = t["recipients"].get(role) or {}
    lang, source = texts.resolve_language(rec.get("language"))
    if lang is None:  # menu: reuse what this person already chose on an earlier call of this ticket
        earlier = q("SELECT language FROM calls WHERE ticket_id=? AND role=? AND to_number=? "
                    "AND language_source='menu' AND language IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (tid, role, rec.get("mobile")), one=True)
        if earlier:
            lang, source = earlier["language"], "menu_earlier_call"
    template, version = texts.TEMPLATES[WORKFLOW_PURPOSE[workflow]]
    cid = q("INSERT INTO calls (ticket_id, assignment_id, workflow, role, attempt_no, recipient_name, to_number, "
            "language, language_source, template, template_version, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'initiating',?)",
            (tid, t["assignment_id"], workflow, role, attempt, rec.get("name"), rec.get("mobile"), lang, source,
             template, version, now_iso()))
    try:
        if not rec.get("mobile"):
            raise RuntimeError(f"no mobile number for {role}")
        sid = twilio_client.create_call(rec["mobile"], cid)
        update_call(cid, call_sid=sid, status="queued")
        audit(tid, "call_placed", f"{role} call #{attempt} ({workflow}) to {rec['mobile']} sid {sid}", call_id=cid)
        if role in ("team_leader", "manager") and rec.get("mobile"):
            def _send_escalation_wa(target_mobile, target_name, target_role, call_cid):
                role_title = target_role.replace("_", " ").title()
                display_name = target_name or role_title
                tech_name = (t.get("recipients", {}).get("technician") or {}).get("name") or "Technician"
                msg = (
                    f"FLM Escalation Alert ({role_title})\n\n"
                    f"Ticket: {tid}\n"
                    f"Site: {t.get('site_name') or 'N/A'}\n"
                    f"Severity: {t.get('severity') or 'Critical'}\n"
                    f"Issue: {t.get('description') or 'N/A'}\n"
                    f"Technician: {tech_name} (Unanswered after technician calls)\n\n"
                    f"Please review and take action."
                )
                try:
                    status_code, resp_body = ae_whatsapp.send_whatsapp(target_mobile, msg)
                    print(f"[whatsapp] whatsapp message send to {display_name} ({target_mobile}) - Status: {status_code}", flush=True)
                    audit(tid, "whatsapp_sent", f"{role_title} escalation WhatsApp message sent to {display_name} ({target_mobile})", call_id=call_cid)
                except Exception as ex:  # noqa: BLE001
                    print(f"[whatsapp] Failed to send whatsapp message to {display_name} ({target_mobile}): {ex}", flush=True)
                    audit(tid, "whatsapp_error", f"Could not send WhatsApp message to {role_title} ({display_name}): {ex}", call_id=call_cid)

            threading.Thread(
                target=_send_escalation_wa,
                args=(rec["mobile"], rec.get("name"), role, cid),
                daemon=True
            ).start()
    except Exception as e:  # noqa: BLE001
        update_call(cid, status="failed", outcome="call_error", error_message=str(e), ended_at=now_iso(),
                    picked_up=0)
        audit(tid, "call_error", f"{role} call #{attempt} could not start: {e}", call_id=cid)
        finish_call(cid)
    return cid


# =========================================================================== Twilio webhooks (TwiML)
def _twiml(body):
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


def _menu(cid, retry):
    prompts = "".join(texts.say(c, texts.LANGS[c]["menu"].format(n=i + 1)) for i, c in enumerate(texts.MENU))
    return _twiml(f'<Gather input="dtmf" numDigits="1" timeout="6" method="POST" '
                  f'action="/twilio/lang?cid={cid}&amp;r={retry}">{prompts}</Gather>'
                  f'<Redirect method="POST">/twilio/lang?cid={cid}&amp;r={retry}</Redirect>')


def twiml_voice(cid, retry):
    c = get_call(cid)
    if not c:
        return _twiml("<Hangup/>")
    if c["superseded"]:
        return _twiml(texts.say(c["language"] or "en", texts.LANGS[c["language"] or "en"]["goodbye"]) + "<Hangup/>")
    if not c["answered_at"]:
        update_call(cid, answered_at=now_iso(), picked_up=1)
    lang = c["language"]
    if not lang:
        return _menu(cid, 0)
    t = get_ticket(c["ticket_id"])
    purpose = WORKFLOW_PURPOSE[c["workflow"]]
    if purpose == "technician":
        prompt = texts.voice_text(lang, "technician", t, ack=t["ack_digit"], rej=t["reject_digit"],
                                  greet=texts.greeting(lang, c["recipient_name"]))
        gather = (f'<Gather input="dtmf" numDigits="1" timeout="8" method="POST" '
                  f'action="/twilio/gather?cid={cid}&amp;r={retry}">{texts.say(lang, prompt)}</Gather>')
        if retry == 0:
            return _twiml(gather + f'<Redirect method="POST">/twilio/voice?cid={cid}&amp;r=1</Redirect>')
        update_call(cid, outcome=c["outcome"] or "no_input")
        return _twiml(gather + texts.say(lang, texts.LANGS[lang]["noinput"]) + "<Hangup/>")
    if purpose == "escalation":
        # Answering counts as reached; the reassign question records a yes/no decision on top
        tech = t["recipients"].get("technician", {})
        reason_key = "reason_rejected" if t["esc_reason"] == "rejected" else "reason_no_ack"
        reason = texts.LANGS[lang][reason_key].format(n=t["esc_detail"] or "")
        text = (texts.voice_text(lang, "escalation", t, name=tech.get("name", ""), reason=reason,
                                 greet=texts.greeting(lang, c["recipient_name"])) + " "
                + texts.LANGS[lang]["escalation_question"].format(ack=t["ack_digit"], rej=t["reject_digit"]))
        if c["outcome"] not in ("reassign_yes", "reassign_no"):
            update_call(cid, outcome="reached")
        gather = (f'<Gather input="dtmf" numDigits="1" timeout="8" method="POST" '
                  f'action="/twilio/gather?cid={cid}&amp;r={retry}">{texts.say(lang, text)}</Gather>')
        if retry == 0:
            return _twiml(gather + f'<Redirect method="POST">/twilio/voice?cid={cid}&amp;r=1</Redirect>')
        return _twiml(gather + texts.say(lang, texts.LANGS[lang]["goodbye"]) + "<Hangup/>")
    else:
        text = texts.voice_text(lang, "reminder", t)
        update_call(cid, outcome="reminder_delivered")
    goodbye = texts.LANGS[lang]["goodbye"]
    return _twiml(texts.say(lang, text) + '<Pause length="1"/>' + texts.say(lang, text) + texts.say(lang, goodbye)
                  + "<Hangup/>")


def twiml_lang(cid, retry, digits):
    c = get_call(cid)
    if not c:
        return _twiml("<Hangup/>")
    if digits.isdigit() and 1 <= int(digits) <= len(texts.MENU):
        lang = texts.MENU[int(digits) - 1]
    elif retry == 0:
        menu = _menu(cid, 1)
        if digits:
            menu = menu.replace("<Response>", "<Response>" + texts.say(texts.MENU[0],
                                texts.LANGS[texts.MENU[0]]["invalid"]), 1)
        return menu
    else:
        lang = texts.resolve_language(None)[0]
    update_call(cid, language=lang, language_source="menu")
    audit(c["ticket_id"], "language_selected", f"{c['role']} selected {texts.LANGS[lang]['name']}", call_id=cid)
    return _twiml(f'<Redirect method="POST">/twilio/voice?cid={cid}&amp;r=0</Redirect>')


def twiml_gather(cid, retry, digits):
    c = get_call(cid)
    if not c:
        return _twiml("<Hangup/>")
    t = get_ticket(c["ticket_id"])
    lang = c["language"] or "en"
    with ticket_lock(c["ticket_id"]):
        update_call(cid, dtmf=",".join(x for x in (c["dtmf"], digits) if x))
        audit(c["ticket_id"], "dtmf", f"{c['role']} pressed '{digits}'", call_id=cid)
        if c["workflow"] == "uc1_escalation":
            if digits in (t["ack_digit"], t["reject_digit"]):
                decision = "reassign_yes" if digits == t["ack_digit"] else "reassign_no"
                update_call(cid, outcome=decision, ack_result="valid")
                return _twiml(texts.say(lang, texts.LANGS[lang][decision]) + "<Hangup/>")
            update_call(cid, ack_result="invalid")
            if retry == 0:
                return _twiml(texts.say(lang, texts.LANGS[lang]["invalid"]) +
                              f'<Redirect method="POST">/twilio/voice?cid={cid}&amp;r=1</Redirect>')
            return _twiml(texts.say(lang, texts.LANGS[lang]["goodbye"]) + "<Hangup/>")
        if digits == t["ack_digit"]:
            update_call(cid, outcome="acknowledged", ack_result="valid")
            return _twiml(texts.say(lang, texts.LANGS[lang]["ack"]) + "<Hangup/>")
        if digits == t["reject_digit"]:
            update_call(cid, outcome="rejected", ack_result="rejected")
            return _twiml(texts.say(lang, texts.LANGS[lang]["reject"]) + "<Hangup/>")
        update_call(cid, ack_result="invalid")
        if retry == 0:
            return _twiml(texts.say(lang, texts.LANGS[lang]["invalid"]) +
                          f'<Redirect method="POST">/twilio/voice?cid={cid}&amp;r=1</Redirect>')
        update_call(cid, outcome="invalid_input")
        return _twiml(texts.say(lang, texts.LANGS[lang]["invalid"]) + "<Hangup/>")


def call_status(cid, form):
    """Twilio StatusCallback: initiated / ringing / in-progress (answered) / completed / busy / no-answer / ..."""
    c = get_call(cid)
    if not c:
        return
    status = form.get("CallStatus", "")
    changes = {"status": status}
    if status == "initiated":
        changes["initiated_at"] = now_iso()
    if status == "in-progress" and not c["answered_at"]:
        changes.update(answered_at=now_iso(), picked_up=1)
    cause = form.get("SipResponseCode") or form.get("ErrorCode")
    if cause:
        changes["cause_code"] = cause
    if status in ("completed", "busy", "no-answer", "failed", "canceled"):
        changes["ended_at"] = now_iso()
        if form.get("CallDuration"):
            changes["duration_sec"] = int(form["CallDuration"])
        if status in UNANSWERED_OUTCOME and not c["outcome"]:
            changes["outcome"] = UNANSWERED_OUTCOME[status]
    update_call(cid, **changes)
    audit(c["ticket_id"], "call_status", f"{c['role']} call #{c['attempt_no']}: {status}"
          + (f" (cause {cause})" if cause else ""), call_id=cid)
    if "ended_at" in changes:
        finish_call(cid)


def finish_call(cid):
    """A call has ended: work out its outcome, then decide the next step."""
    c = get_call(cid)
    with ticket_lock(c["ticket_id"]):
        if not q("UPDATE calls SET processed=1 WHERE id=? AND processed=0", (cid,)):
            return
        c = get_call(cid)
        outcome = c["outcome"] or ("hung_up_without_response" if c["picked_up"] else "not_picked_up")
        picked = 0 if outcome in NOT_PICKED_UP else 1
        update_call(cid, outcome=outcome, picked_up=picked)
        tid = c["ticket_id"]
        audit(tid, "call_finished", f"{c['role']} call #{c['attempt_no']}: {RESPONSE_TEXT.get(outcome, outcome)}",
              call_id=cid)
        t = get_ticket(tid)
        if c["superseded"] or c["assignment_id"] != t["assignment_id"]:
            audit(tid, "call_ignored", f"Result of superseded call #{c['attempt_no']} not used", call_id=cid)
            return
        if c["workflow"] == "uc1_technician":
            _after_technician_call(t, c, outcome)
        elif c["workflow"] == "uc1_escalation":
            _after_escalation_call(t, c, outcome)
        elif c["workflow"] == "uc1_reminder":
            update_ticket(tid, followup_state="REMINDER_CALLED")
            notify(tid, "uc1.reminder_called", {"outcome": outcome})


def _after_technician_call(t, c, outcome):
    tid, n = t["ticket_id"], c["attempt_no"]
    if t["uc1_state"] not in TECH_ACTIVE:
        return
    if outcome == "acknowledged":
        _mark_acknowledged(t, "voice")
    elif outcome == "rejected":
        _escalate(t, "rejected", n)
    elif n < CFG["tech_max_attempts"]:
        update_ticket(tid, uc1_state="TECH_WAITING_RETRY")
        _schedule(tid, t["assignment_id"], "tech_call", n + 1, CFG["tech_retry_sec"])
    else:
        _escalate(t, "no_acknowledgement", n)


def _after_escalation_call(t, c, outcome):
    """Only an answer to the reassign question (1 or 9) ends the escalation. Picking up without a key
    counts as no response: the next attempt or the next person is called."""
    tid, n, role = t["ticket_id"], c["attempt_no"], c["role"]
    if t["uc1_state"] != "ESCALATING":
        return
    if outcome in ("reassign_yes", "reassign_no"):
        _finish_uc1(tid, "TL_REACHED" if role == "team_leader" else "MANAGER_REACHED")
        return
    has_manager = bool(t["recipients"].get("manager", {}).get("mobile"))
    if role == "team_leader" and n < CFG["tl_max_attempts"]:
        _schedule(tid, t["assignment_id"], "tl_call", n + 1, CFG["manager_retry_sec"])
    elif role == "team_leader" and has_manager:
        _schedule(tid, t["assignment_id"], "manager_call", 1, 0)
    elif role == "manager" and n < CFG["manager_max_attempts"]:
        _schedule(tid, t["assignment_id"], "manager_call", n + 1, CFG["manager_retry_sec"])
    else:
        _finish_uc1(tid, "ESCALATION_UNRESOLVED")


# =========================================================================== views
def _call_view(c):
    out = {k: c[k] for k in ("id", "role", "workflow", "attempt_no", "recipient_name", "to_number", "language",
                             "language_source", "template", "template_version", "call_sid", "status", "outcome",
                             "dtmf", "ack_result", "cause_code", "error_message", "duration_sec", "created_at",
                             "initiated_at", "answered_at", "ended_at", "superseded")}
    out["picked_up"] = None if c["picked_up"] is None or not c["processed"] else bool(c["picked_up"])
    out["response"] = RESPONSE_TEXT.get(c["outcome"], c["outcome"] or "call in progress")
    return out


def _summary(calls):
    label = {"uc1_technician": "Technician", "uc1_escalation": None, "uc1_reminder": "Reminder"}
    parts = []
    for key, name in (("technician", "Technician"), ("team_leader", "Team leader"), ("manager", "Manager")):
        done = [c for c in calls if c["role"] == key and c["outcome"] and c["workflow"] != "uc1_reminder"]
        if done:
            parts.append(f"{name}: " + "; ".join(f"call {c['attempt_no']} {c['response']}" for c in done) + ".")
    reminder = [c for c in calls if c["workflow"] == "uc1_reminder" and c["outcome"]]
    if reminder:
        parts.append(f"{label['uc1_reminder']}: {reminder[-1]['response']}.")
    return " ".join(parts) or "No call has finished yet."


def ticket_view(tid):
    t = get_ticket(tid)
    if not t:
        return None
    calls = [_call_view(c) for c in q("SELECT * FROM calls WHERE ticket_id=? ORDER BY id", (tid,))]
    current = [c for c in calls if not c["superseded"]]
    tech = [c for c in current if c["workflow"] == "uc1_technician" and c["outcome"]]
    esc = None
    if t["esc_reason"]:
        esc_calls = [c for c in current if c["workflow"] == "uc1_escalation" and c["outcome"]]
        reached = lambda role: (any(c["picked_up"] for c in esc_calls if c["role"] == role)  # noqa: E731
                                if any(c["role"] == role for c in esc_calls) else None)
        decisions = {"reassign_yes": "assign_to_another_technician", "reassign_no": "do_not_reassign"}

        def decision(role):
            answered = [c["outcome"] for c in esc_calls if c["role"] == role and c["outcome"] in decisions]
            return decisions[answered[-1]] if answered else None

        esc = {"reason": t["esc_reason"], "technician_calls": int(t["esc_detail"] or 0),
               "team_leader_reached": reached("team_leader"), "team_leader_decision": decision("team_leader"),
               "manager_reached": reached("manager"), "manager_decision": decision("manager")}
        esc["reassign"] = esc["team_leader_decision"] or esc["manager_decision"]
    timers = q("SELECT kind, attempt_no, status, note, run_at FROM jobs WHERE ticket_id=? "
               "AND status IN ('pending','held','running') ORDER BY run_at", (tid,))
    for j in timers:
        j["due_at"] = iso_at(j.pop("run_at"))
    return {
        "ticket_id": t["ticket_id"], "alarm_id": t["alarm_id"], "assignment_id": t["assignment_id"],
        "site_number": t["site_number"], "site_name": t["site_name"], "severity": t["severity"],
        "status": t["status"], "alarm_status": t["alarm_status"], "operational_status": t["operational_status"],
        "description": t["description"], "ack_digit": t["ack_digit"], "reject_digit": t["reject_digit"],
        "version": t["version"], "last_event_id": t["last_event_id"], "last_event_time": t["last_event_time"],
        "recipients": t["recipients"], "callback_url": t["callback_url"],
        "call_workflow": {
            "state": t["uc1_state"], "final": t["uc1_state"] in FINAL, "assignment_id": t["uc1_assignment_id"],
            "acknowledged": t["uc1_state"] == "ACKNOWLEDGED", "acknowledged_via": t["uc1_ack_via"],
            "acknowledged_at": t["uc1_ack_at"],
            "technician_response": tech[-1]["outcome"] if tech else "pending",
            "technician_picked_up": any(c["picked_up"] for c in tech) if tech else None,
            "escalation": esc,
            "response_summary": _summary(current),
        },
        "followup": {"state": t["followup_state"], "due_at": t["followup_due_at"]},
        "pending_timers": timers,
        "calls": calls,
        "created_at": t["created_at"], "updated_at": t["updated_at"],
    }


# =========================================================================== synchronous wait (?wait=true)
MAX_WAIT_SEC = 900


def technician_decision(t):
    """accepted / rejected / no_response / cancelled once the technician part is decided, else None."""
    state = t["uc1_state"]
    if state == "ACKNOWLEDGED":
        return "accepted"
    if t["esc_reason"] == "rejected":
        return "rejected"
    if t["esc_reason"] == "no_acknowledgement":
        return "no_response"
    if state and state.startswith("CANCELLED"):
        return "cancelled"
    return None


def wait_for_result(tid, assignment_id, until="technician", timeout=300):
    """Block until the technician has answered (until=technician) or the whole call workflow, escalation
    included, is finished (until=final) - or until timeout seconds. Returns a summary for the API response."""
    timeout = max(1, min(int(timeout), MAX_WAIT_SEC))
    start = time.time()
    while True:
        t = get_ticket(tid)
        if t["uc1_assignment_id"] != assignment_id:
            decision, done = "reassigned", True
        else:
            decision = technician_decision(t)
            done = decision is not None and (until != "final" or t["uc1_state"] in FINAL)
        if done or time.time() - start >= timeout:
            break
        time.sleep(0.5)
    view = ticket_view(tid)
    return {
        "call_result": decision if done else "pending",
        "timed_out": not done,
        "waited_sec": round(time.time() - start, 1),
        "wait_until": until,
        "technician_response": view["call_workflow"]["technician_response"],
        "response_summary": view["call_workflow"]["response_summary"],
        "ticket": view,
    }


def wait_for_call(cid, timeout=120):
    """Block until one call has ended (used by the follow-up API's ?wait=true)."""
    timeout = max(1, min(int(timeout), MAX_WAIT_SEC))
    start = time.time()
    while not get_call(cid)["processed"] and time.time() - start < timeout:
        time.sleep(0.5)
    c = get_call(cid)
    done = bool(c["processed"])
    view = _call_view(c)
    t = get_ticket(c["ticket_id"])
    return {
        "reminder_result": c["outcome"] if done else "pending",
        "answered": view["picked_up"] if done else None,
        "response": view["response"],
        "timed_out": not done,
        "waited_sec": round(time.time() - start, 1),
        "followup": {"state": t["followup_state"], "due_at": t["followup_due_at"]},
        "call": view,
    }


# =========================================================================== outbound webhook
def notify(tid, event, data):
    """POST a signed notification to the ticket's callback_url (or RESULT_WEBHOOK_URL). Fire-and-forget."""
    t = get_ticket(tid)
    url = (t and t["callback_url"]) or CFG["webhook_url"]
    if not url:
        return
    body = json.dumps({"event": event, "ticket_id": tid, "sent_at": now_iso(), "data": data,
                       "ticket": ticket_view(tid)}, ensure_ascii=False).encode()
    threading.Thread(target=_deliver, args=(tid, event, url, body), daemon=True).start()


def _deliver(tid, event, url, body):
    headers = {"Content-Type": "application/json", "User-Agent": "flm-voice-notify/2.0", "X-Event": event}
    if CFG["api_key"]:
        headers["X-Signature"] = hmac.new(CFG["api_key"].encode(), body, hashlib.sha256).hexdigest()
    result = "not sent"
    for n, delay in enumerate(WEBHOOK_DELAYS, 1):
        time.sleep(delay)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method="POST"),
                                        timeout=15) as r:
                result = f"delivered (HTTP {r.status}, attempt {n})"
                break
        except urllib.error.HTTPError as e:
            result = f"failed (HTTP {e.code}, attempt {n})"
            if 400 <= e.code < 500 and e.code not in (408, 429):
                break
        except Exception as e:  # noqa: BLE001
            result = f"failed ({type(e).__name__}, attempt {n})"
    audit(tid, "webhook_delivered" if result.startswith("delivered") else "webhook_failed",
          f"{event} -> {url}: {result}")
