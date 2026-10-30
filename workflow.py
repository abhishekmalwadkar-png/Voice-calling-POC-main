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
TICKET_FIELDS = ("ticket_id", "alert_id", "alarm_id", "assignment_id", "site_number", "site_name", "severity", "status", "region", "country_code",
                 "alarm_status", "operational_status", "description")
CALLED_ROLES = ("technician", "team_leader", "manager")
E164 = re.compile(r"\+[1-9]\d{7,14}")

TECH_ACTIVE = {"TECH_CALLING", "TECH_WAITING_RETRY", "EMERGENCY_TRIGGERED"}
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
    "informed": "informed of emergency assignment",
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
REJECT_REASONS = {
    "1": "Busy on another site",
    "2": "On leave / Off duty",
    "3": "Equipment or transport issue",
    "4": "Other / Personal reasons",
}


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
        recipients[role] = {"name": str(r.get("name") or "").strip(), "mobile": mobile, "language": lang,
                            "id": str(r.get("id") or r.get(f"{role}_id") or "").strip()}

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


def _is_emergency_ticket(t):
    if not t:
        return False
    sev = str(t.get("severity") or "").lower()
    uc1 = str(t.get("uc1_state") or "")
    aid = str(t.get("assignment_id") or "")
    alarm = str(t.get("alarm_id") or "")
    return (sev == "emergency" or
            uc1.startswith("EMERGENCY") or
            "-EMG" in aid or
            "EMERGENCY" in alarm.upper())


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


def _end_live_calls(tid, why, exclude_cid=None):
    for c in q("SELECT * FROM calls WHERE ticket_id=? AND processed=0 AND superseded IS NULL", (tid,)):
        if exclude_cid and c["id"] == exclude_cid:
            continue
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
    needs = {"tech_call": TECH_ACTIVE, "tl_call": {"ESCALATING", "EMERGENCY_TRIGGERED"}, "manager_call": {"ESCALATING"}}
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


def _mark_acknowledged(t, via, call_cid=None):
    tid = t["ticket_id"]
    update_ticket(tid, uc1_state="ACKNOWLEDGED", uc1_ack_via=via, uc1_ack_at=now_iso())
    _cancel_jobs(tid, ("tech_call", "tl_call", "manager_call"), "acknowledged")
    _end_live_calls(tid, "technician acknowledged", exclude_cid=call_cid)
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
    tech_name = (r.get("technician") or {}).get("name") or "Technician"

    # Send message to Team Leader and Manager upon escalation
    for role in ("team_leader", "manager"):
        rec_role = r.get(role)
        if rec_role and rec_role.get("mobile"):
            if reason == "rejected":
                rej_r = t.get("uc1_reject_reason")
                note = f"Technician: {tech_name} (Declined - Reason: {rej_r})" if rej_r else f"Technician: {tech_name} (Rejected assignment)"
            else:
                note = f"Technician: {tech_name} (Did not answer after {tech_calls} call attempts)"
            send_whatsapp_alert(t, role, rec_role, alert_type_override="Emergency Escalation" if _is_emergency_ticket(t) else "Escalation", custom_note=note)

    if r.get("team_leader", {}).get("mobile"):
        return [f"escalation started ({reason})", _schedule(tid, t["assignment_id"], "tl_call", 1, 0)]
    if not _is_emergency_ticket(t) and r.get("manager", {}).get("mobile"):
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


def _place_call(t, role, attempt, workflow, send_wa=True):
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
    is_emergency = (str(t.get("severity") or "").lower() == "emergency" or
                    str(t.get("uc1_state") or "") == "EMERGENCY_TRIGGERED")
    if send_wa and not is_emergency and role in ("team_leader", "manager") and rec.get("mobile"):
        send_whatsapp_alert(t, role, rec, call_cid=cid)

    try:
        if not rec.get("mobile"):
            raise RuntimeError(f"no mobile number for {role}")
        sid = twilio_client.create_call(rec["mobile"], cid)
        update_call(cid, call_sid=sid, status="queued")
        audit(tid, "call_placed", f"{role} call #{attempt} ({workflow}) to {rec['mobile']} sid {sid}", call_id=cid)
    except Exception as e:  # noqa: BLE001
        update_call(cid, status="failed", outcome="call_error", error_message=str(e), ended_at=now_iso(),
                    picked_up=0)
        audit(tid, "call_error", f"{role} call #{attempt} could not start: {e}", call_id=cid)
        finish_call(cid)
    return cid


def send_whatsapp_alert(t, role, rec, call_cid=None, alert_type_override=None, custom_note=None, delay_sec=None):
    """Send WhatsApp alert to Team Leader or Manager."""
    if not rec or not rec.get("mobile"):
        return
    tid = t["ticket_id"]
    role_title = role.replace("_", " ").title()
    display_name = rec.get("name") or role_title
    tech_name = (t.get("recipients", {}).get("technician") or {}).get("name") or "Technician"
    is_emergency = (alert_type_override in ("Emergency", "Emergency Escalation") or
                    _is_emergency_ticket(t))
    alert_type = alert_type_override or ("Emergency" if is_emergency else "Escalation")
    if custom_note:
        tech_line = custom_note
    elif is_emergency:
        tech_line = f"Technician: {tech_name}"
    else:
        tech_line = f"Technician: {tech_name} (Unanswered after technician calls)"
    desc = str(t.get('description') or 'N/A').replace('\\n', '\n').strip()
    msg = (
        f"FLM {alert_type} Alert ({role_title})\n\n"
        f"Ticket: {tid}\n"
        f"Site: {t.get('site_name') or 'N/A'}\n"
        f"Severity: {t.get('severity') or ('Emergency' if is_emergency else 'Critical')}\n"
        f"Issue: {desc}\n"
        f"{tech_line}\n\n"
        f"Please review and take action."
    )

    wait_time = delay_sec if delay_sec is not None else (2.0 if role == "manager" else 0.0)

    def _do_send():
        if wait_time > 0:
            time.sleep(wait_time)
        try:
            status_code, resp_body = ae_whatsapp.send_whatsapp(rec["mobile"], msg)
            print(f"[whatsapp] whatsapp message send to {display_name} ({rec['mobile']}) - Status: {status_code}", flush=True)
            audit(tid, "whatsapp_sent", f"{role_title} {alert_type.lower()} WhatsApp message sent to {display_name} ({rec['mobile']})", call_id=call_cid)
        except Exception as ex:  # noqa: BLE001
            print(f"[whatsapp] Failed to send whatsapp message to {display_name} ({rec['mobile']}): {ex}", flush=True)
            audit(tid, "whatsapp_error", f"Could not send WhatsApp message to {role_title} ({display_name}): {ex}", call_id=call_cid)

    threading.Thread(
        target=_do_send,
        daemon=True
    ).start()


def send_whatsapp_closure(t, role, rec, resolution=None, closed_at=None, delay_sec=None):
    """Send Emergency Closure WhatsApp notification to Team Leader, Manager, or Technician."""
    if not rec or not rec.get("mobile"):
        return
    tid = t["ticket_id"]
    role_title = role.replace("_", " ").title()
    display_name = rec.get("name") or role_title
    res = str(resolution or "Emergency issue resolved and service restored.").replace('\\n', '\n').strip()
    close_time = closed_at or now_iso()
    msg = (
        f"FLM Emergency Closure Alert ({role_title})\n\n"
        f"Ticket: {tid}\n"
        f"Site: {t.get('site_name') or 'N/A'}\n"
        f"Severity: Emergency\n"
        f"Status: Closed\n"
        f"Closed At: {close_time}\n"
        f"Resolution: {res}\n\n"
        f"The emergency ticket has been closed. No further action is required."
    )
    wait_time = delay_sec if delay_sec is not None else (2.0 if role == "manager" else 0.0)

    def _do_send():
        if wait_time > 0:
            time.sleep(wait_time)
        try:
            status_code, resp_body = ae_whatsapp.send_whatsapp(rec["mobile"], msg)
            print(f"[whatsapp] whatsapp closure message send to {display_name} ({rec['mobile']}) - Status: {status_code}", flush=True)
            audit(tid, "whatsapp_sent", f"{role_title} emergency closure WhatsApp message sent to {display_name} ({rec['mobile']})")
        except Exception as ex:  # noqa: BLE001
            print(f"[whatsapp] Failed to send whatsapp closure message to {display_name} ({rec['mobile']}): {ex}", flush=True)
            audit(tid, "whatsapp_error", f"Could not send closure WhatsApp to {role_title} ({display_name}): {ex}")

    threading.Thread(
        target=_do_send,
        daemon=True
    ).start()


def trigger_emergency(ticket_id, alarm_id, assignment_id, site_name, description, severity,
                      recipients, callback_url=None, ack_digit="1", reject_digit="9"):
    """Emergency workflow:
    1. Technician receives a voice call (1 attempt only, no retries).
    2. Team Leader receives a voice call (1 attempt only, no retries).
    3. Team Leader and Manager receive WhatsApp alert messages (Manager receives NO voice calls).
    """
    with ticket_lock(ticket_id):
        t = get_ticket(ticket_id)
        if not t:
            q("INSERT INTO tickets (ticket_id, recipients, created_at, updated_at) VALUES (?,?,?,?)",
              (ticket_id, "{}", now_iso(), now_iso()))
            t = get_ticket(ticket_id)

        merged = dict(t.get("recipients") or {})
        for rk, rv in recipients.items():
            if rv.get("mobile"):
                merged[rk] = rv

        # Ensure technician asks for language first if not explicitly set
        if "technician" in merged and not merged["technician"].get("language"):
            merged["technician"]["language"] = "ask"

        changes = {
            "alarm_id": alarm_id,
            "assignment_id": assignment_id,
            "site_name": site_name,
            "description": description,
            "severity": severity or "Emergency",
            "status": "Assigned",
            "recipients": merged,
            "uc1_assignment_id": assignment_id,
            "uc1_state": "EMERGENCY_TRIGGERED",
            "ack_digit": ack_digit,
            "reject_digit": reject_digit,
            "last_event_time": now_iso(),
            "last_event_received_at": epoch(),
        }
        if callback_url:
            changes["callback_url"] = callback_url
        update_ticket(ticket_id, **changes)
        t = get_ticket(ticket_id)

        audit(ticket_id, "emergency_triggered",
              f"Emergency workflow triggered for {ticket_id} at {site_name}. Severity: {severity}")

        calls_placed = {}
        # 1. Voice call ONLY to technician (attempt 1; attempt 2 happens on retry if not picked up)
        if (merged.get("technician") or {}).get("mobile"):
            cid = _place_call(t, "technician", 1, "uc1_technician")
            calls_placed["technician"] = cid

        # 2. WhatsApp alerts to Team Leader and Manager (1 message each)
        wa_recipients = []
        for role in ("team_leader", "manager"):
            rec_role = merged.get(role)
            if rec_role and rec_role.get("mobile"):
                send_whatsapp_alert(t, role, rec_role, alert_type_override="Emergency")
                wa_recipients.append({
                    "role": role,
                    "name": rec_role.get("name") or role.replace("_", " ").title(),
                    "mobile": rec_role["mobile"],
                })

        # Optional initial webhook delivery to callback_url
        if t.get("callback_url"):
            notify(ticket_id, "emergency.triggered", {
                "ticket_id": ticket_id,
                "alarm_id": alarm_id,
                "assignment_id": assignment_id,
                "site_name": site_name,
                "severity": severity or "Emergency",
                "status": "EMERGENCY_TRIGGERED",
                "calls_placed": calls_placed,
                "whatsapp_notifications": wa_recipients,
                "recipients": merged,
            })

        return 202, {
            "ticket_id": ticket_id,
            "result": "emergency_triggered",
            "severity": severity or "Emergency",
            "calls_placed": calls_placed,
            "whatsapp_notifications": wa_recipients,
            "callback_url": t.get("callback_url"),
            "status_url": f"/api/tickets/{ticket_id}"
        }


def trigger_emergency_close(ticket_id, resolution=None, closed_at=None, recipients_override=None, site_name=None):
    """Emergency Closure workflow:
    1. Cancels active/pending calls and timers for this ticket.
    2. Marks ticket Closed in the database with closure timestamp.
    3. Sends Emergency Closure WhatsApp notification to Team Leader and Manager.
    4. Records audit trail and notifies callback_url.
    """
    with ticket_lock(ticket_id):
        t = get_ticket(ticket_id)
        close_time = closed_at or now_iso()
        res = resolution or "Emergency issue resolved and service restored."

        if not t:
            q("INSERT INTO tickets (ticket_id, recipients, created_at, updated_at) VALUES (?,?,?,?)",
              (ticket_id, "{}", now_iso(), now_iso()))
            t = get_ticket(ticket_id)

        merged = dict(t.get("recipients") or {})
        if recipients_override and isinstance(recipients_override, dict):
            for rk, rv in recipients_override.items():
                if isinstance(rv, dict) and rv.get("mobile"):
                    merged[rk] = rv

        changes = {
            "status": "Closed",
            "uc1_state": "CANCELLED_CLOSED",
            "followup_state": "STOPPED_CLOSED",
            "recipients": merged,
            "updated_at": now_iso()
        }
        if site_name:
            changes["site_name"] = site_name
        if resolution:
            orig_desc = t.get("description") or ""
            changes["description"] = f"{orig_desc} [Closed: {res}]".strip() if orig_desc else res

        update_ticket(ticket_id, **changes)
        _cancel_jobs(ticket_id, ("tech_call", "tl_call", "manager_call", "followup"), "emergency ticket closed")
        _end_live_calls(ticket_id, "emergency ticket closed")
        t = get_ticket(ticket_id)

        audit(ticket_id, "emergency_closed",
              f"Emergency ticket {ticket_id} closed at {close_time}. Resolution: {res}")

        # Send WhatsApp closure alerts to Technician, Team Leader, and Manager
        wa_recipients = []
        target_roles = [r for r in ("technician", "team_leader", "manager") if r in merged and merged[r].get("mobile")]
        for idx, role in enumerate(target_roles):
            rec_role = merged[role]
            send_whatsapp_closure(t, role, rec_role, resolution=res, closed_at=close_time, delay_sec=idx * 2.0)
            wa_recipients.append({
                "role": role,
                "name": rec_role.get("name") or role.replace("_", " ").title(),
                "mobile": rec_role["mobile"],
            })

        if t.get("callback_url"):
            notify(ticket_id, "emergency.closed", {
                "ticket_id": ticket_id,
                "status": "Closed",
                "closed_at": close_time,
                "resolution": res,
                "whatsapp_notifications": wa_recipients
            })

        return 200, {
            "ticket_id": ticket_id,
            "result": "emergency_closed",
            "status": "Closed",
            "closed_at": close_time,
            "resolution": res,
            "whatsapp_notifications": wa_recipients,
            "status_url": f"/api/tickets/{ticket_id}"
        }


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
    t = get_ticket(c["ticket_id"])
    if c["superseded"] or (c["workflow"] == "uc1_escalation" and t and t["uc1_state"] == "ACKNOWLEDGED"):
        return _twiml(texts.say(c["language"] or "en", texts.LANGS[c["language"] or "en"]["goodbye"]) + "<Hangup/>")
    if not c["answered_at"]:
        update_call(cid, answered_at=now_iso(), picked_up=1)
    lang = c["language"]
    if not lang:
        return _menu(cid, 0)
    t = get_ticket(c["ticket_id"])
    purpose = WORKFLOW_PURPOSE[c["workflow"]]
    is_emergency = _is_emergency_ticket(t)
    if purpose == "technician":
        if is_emergency:
            prompt = texts.voice_text(lang, "emergency_technician", t,
                                      greet=texts.greeting(lang, c["recipient_name"]))
            update_call(cid, outcome="informed")
            goodbye = texts.LANGS[lang]["goodbye"]
            return _twiml(texts.say(lang, prompt) + '<Pause length="1"/>' + texts.say(lang, prompt) + texts.say(lang, goodbye) + "<Hangup/>")
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
        tech_name = tech.get("name") or "the technician"
        if is_emergency:
            reason_key = "emergency_reason_rejected" if t.get("esc_reason") == "rejected" else "emergency_reason_no_answer"
            reason = texts.LANGS[lang].get(reason_key, "did not answer the call")
            text = (texts.voice_text(lang, "emergency_escalation", t, name=tech_name, reason=reason,
                                     greet=texts.greeting(lang, c["recipient_name"])) + " "
                    + texts.LANGS[lang]["escalation_question"].format(ack=t["ack_digit"], rej=t["reject_digit"]))
        else:
            reason_key = "reason_rejected" if t.get("esc_reason") == "rejected" else "reason_no_ack"
            reason = texts.LANGS[lang][reason_key].format(n=t.get("esc_detail") or "")
            text = (texts.voice_text(lang, "escalation", t, name=tech_name, reason=reason,
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
            if c["superseded"] or t["uc1_state"] == "ACKNOWLEDGED":
                update_call(cid, outcome="superseded", ack_result="ignored")
                return _twiml(texts.say(lang, texts.LANGS[lang]["goodbye"]) + "<Hangup/>")
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
            prompt = texts.LANGS[lang].get("reject_reason_prompt") or texts.LANGS["en"]["reject_reason_prompt"]
            gather = (f'<Gather input="dtmf" numDigits="1" timeout="7" method="POST" '
                      f'action="/twilio/reject_reason?cid={cid}">{texts.say(lang, prompt)}</Gather>'
                      f'<Redirect method="POST">/twilio/reject_reason?cid={cid}</Redirect>')
            return _twiml(gather)
        update_call(cid, ack_result="invalid")
        if retry == 0:
            return _twiml(texts.say(lang, texts.LANGS[lang]["invalid"]) +
                          f'<Redirect method="POST">/twilio/voice?cid={cid}&amp;r=1</Redirect>')
        update_call(cid, outcome="invalid_input")
        return _twiml(texts.say(lang, texts.LANGS[lang]["invalid"]) + "<Hangup/>")


def twiml_reject_reason(cid, digits):
    c = get_call(cid)
    if not c:
        return _twiml("<Hangup/>")
    lang = c["language"] or "en"
    digits = str(digits or "").strip()
    reason_text = REJECT_REASONS.get(digits, "Other / unspecified")
    with ticket_lock(c["ticket_id"]):
        update_call(cid, reject_reason=reason_text, dtmf=",".join(x for x in (c["dtmf"], f"reason:{digits}") if x))
        update_ticket(c["ticket_id"], uc1_reject_reason=reason_text)
        audit(c["ticket_id"], "reject_reason_selected",
              f"{c['role']} selected decline reason: '{reason_text}' (key {digits or 'none'})", call_id=cid)
    farewell = texts.LANGS[lang].get("reject_reason_recorded") or texts.LANGS["en"]["reject_reason_recorded"]
    return _twiml(texts.say(lang, farewell) + "<Hangup/>")


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
        tid = c["ticket_id"]
        t = get_ticket(tid)
        if c["superseded"] or (c["workflow"] == "uc1_escalation" and t and t["uc1_state"] == "ACKNOWLEDGED"):
            reason = c["superseded"] or "technician already acknowledged"
            update_call(cid, superseded=reason)
            audit(tid, "call_finished", f"{c['role']} call #{c['attempt_no']}: ended ({reason})", call_id=cid)
            audit(tid, "call_ignored", f"Result of superseded call #{c['attempt_no']} not used", call_id=cid)
            return

        outcome = c["outcome"] or ("hung_up_without_response" if c["picked_up"] else "not_picked_up")
        picked = 0 if outcome in NOT_PICKED_UP else 1
        update_call(cid, outcome=outcome, picked_up=picked)
        audit(tid, "call_finished", f"{c['role']} call #{c['attempt_no']}: {RESPONSE_TEXT.get(outcome, outcome)}",
              call_id=cid)
        if c["assignment_id"] != t["assignment_id"]:
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
    if t["uc1_state"] not in TECH_ACTIVE and not _is_emergency_ticket(t):
        return
    if outcome in ("acknowledged", "informed"):
        _mark_acknowledged(t, "voice", call_cid=c["id"])
        return

    is_emergency = _is_emergency_ticket(t)
    if is_emergency:
        if outcome == "rejected":
            _escalate(t, "rejected", n)
            return
        if n < 2:
            update_ticket(tid, uc1_state="TECH_WAITING_RETRY")
            audit(tid, "emergency_tech_retry", f"Technician did not answer emergency call #{n} ({outcome}). Retrying attempt #2.")
            _schedule(tid, t["assignment_id"], "tech_call", n + 1, CFG["tech_retry_sec"])
            return
        else:
            audit(tid, "emergency_tech_escalate", f"Technician did not answer emergency call after {n} attempt(s). Escalating to Team Leader.")
            _escalate(t, "no_acknowledgement", n)
            return

    if outcome == "rejected":
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
    if t["uc1_state"] not in ("ESCALATING", "EMERGENCY_TRIGGERED"):
        return
    if outcome in ("reassign_yes", "reassign_no"):
        _finish_uc1(tid, "TL_REACHED" if role == "team_leader" else "MANAGER_REACHED")
        return
    is_emergency = _is_emergency_ticket(t)
    if is_emergency:
        # Emergency: Team Leader gets a 2nd call attempt if they do not pick up attempt 1. Never call Manager.
        if role == "team_leader" and n < 2:
            _schedule(tid, t["assignment_id"], "tl_call", n + 1, CFG["manager_retry_sec"])
            return

        tech_active = q("SELECT COUNT(*) AS c FROM calls WHERE ticket_id=? AND role='technician' AND processed=0", (tid,), one=True)["c"]
        if tech_active == 0:
            _finish_uc1(tid, "ESCALATION_UNRESOLVED")
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
                             "dtmf", "ack_result", "reject_reason", "cause_code", "error_message", "duration_sec", "created_at",
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
        "ticket_id": t["ticket_id"], "alert_id": t.get("alert_id") or t["alarm_id"], "alarm_id": t["alarm_id"], "assignment_id": t["assignment_id"],
        "site_number": t["site_number"], "site_name": t["site_name"], "severity": t["severity"],
        "status": t["status"], "alarm_status": t["alarm_status"], "operational_status": t["operational_status"],
        "description": t["description"], "ack_digit": t["ack_digit"], "reject_digit": t["reject_digit"], "dtmf_pressed": (tech[-1].get("dtmf") if tech else None), "ack_number_pressed": (tech[-1].get("dtmf") if tech else None), "acknowledged": (t["uc1_state"] == "ACKNOWLEDGED"),
        "version": t["version"], "last_event_id": t["last_event_id"], "last_event_time": t["last_event_time"],
        "recipients": t["recipients"], "callback_url": t["callback_url"],
        "call_workflow": {
            "state": t["uc1_state"], "final": t["uc1_state"] in FINAL, "assignment_id": t["uc1_assignment_id"],
            "acknowledged": t["uc1_state"] == "ACKNOWLEDGED", "acknowledged_via": t["uc1_ack_via"],
            "acknowledged_at": t["uc1_ack_at"],
            "technician_response": tech[-1]["outcome"] if tech else "pending",
            "technician_reject_reason": t["uc1_reject_reason"],
            "technician_picked_up": any(c["picked_up"] for c in tech) if tech else None,
            "escalation": esc,
            "response_summary": _summary(current),
        },
        "rejection_reason": t["uc1_reject_reason"],
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
        "rejection_reason": view["rejection_reason"],
        "technician_reject_reason": view["rejection_reason"],
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
    if not url or not (str(url).startswith("http://") or str(url).startswith("https://")):
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


# =========================================================================== Alert Intake API (Single & Batch)
def _format_e164(phone, default_country="+966"):
    """Ensure phone number conforms to E.164, applying country_code if missing prefix."""
    if not phone:
        return ""
    p = "".join(str(phone).split())
    if not p:
        return ""
    if p.startswith("+"):
        return p
    c = str(default_country or "+966").strip()
    if not c.startswith("+"):
        c = "+" + c
    digits_only = re.sub(r"\D", "", p)
    if digits_only.startswith("0"):
        digits_only = digits_only[1:]
    return f"{c}{digits_only}"


def _normalize_lang(l, default="en"):
    if not l:
        return default
    s = str(l).strip().lower()
    if s in ("arabic", "ar"):
        return "ar"
    if s in ("english", "en"):
        return "en"
    if s in ("urdu", "ur"):
        return "ur"
    if s in ("hindi", "hi"):
        return "hi"
    if s in ("ask", "menu"):
        return "ask"
    return s[:2] if len(s) >= 2 else default


def normalize_intake_alert(item):
    """Normalize a single alert object (flat, human-readable, or nested NexTask event)
    supporting all architectural variations:
    - 14 fields architecture
    - Nested ticket: {ticket_id, alarm_id, issue_description, severity, ticket_status, alarm_status}
    - Nested branch: {branch_id, branch_name, site_number}
    - Nested assignment: {assignment_id, assignment_type, assigned_at}
    - Nested acknowledgement: {dtmf_digit}
    - Nested technician, team_leader, manager, emergency_contacts
    - Preserves BOTH ticket_id and alert_id (and alarm_id)
    Returns: (event_dict, normalized_alert_dict) or raises EventError.
    """
    if not isinstance(item, dict):
        raise EventError("alert item must be a JSON object")

    ticket_dict = item.get("ticket") if isinstance(item.get("ticket"), dict) else {}
    assignment_dict = item.get("assignment") if isinstance(item.get("assignment"), dict) else (item.get("assignment_details") if isinstance(item.get("assignment_details"), dict) else {})
    branch_dict = item.get("branch") if isinstance(item.get("branch"), dict) else (item.get("site") if isinstance(item.get("site"), dict) else {})
    ack_dict = item.get("acknowledgement") if isinstance(item.get("acknowledgement"), dict) else {}

    # Build clean-key map to match keys regardless of case, underscores, spaces, or parentheses
    key_map = {}
    for source in (branch_dict, assignment_dict, ack_dict, ticket_dict, item):
        for k, val in source.items():
            if not isinstance(val, dict):
                clean = re.sub(r"[^a-zA-Z0-9]", "", str(k)).lower()
                key_map[clean] = val

    def _get(*keys, default=""):
        for k in keys:
            clean = re.sub(r"[^a-zA-Z0-9]", "", str(k)).lower()
            if clean in key_map:
                val = key_map[clean]
                if val is not None and str(val).strip() != "":
                    return val
        return default

    # 4. Alert ID, Ticket ID & Alarm ID - preserve BOTH
    raw_ticket_id = str(_get("ticket_id", "incident_id", "incident_number", "ticket", default="")).strip().upper()
    raw_alert_id = str(_get("alert_id", "alertid", "alarm_id", "alarmid", default="")).strip()
    if not raw_ticket_id and not raw_alert_id:
        raise EventError("missing required field: ticket_id or alert_id")
    ticket_id = raw_ticket_id or raw_alert_id.upper()
    alert_id = raw_alert_id or ticket_id
    alarm_id = str(_get("alarm_id", "alarmid", default=alert_id)).strip()

    # 1. Region
    region = str(_get("region", "region_name", "region_id", default="")).strip()

    # 2. Site Name & Site Number
    site_name = str(_get("site_name", "branch_name", "site_details", "site", "site_number_name", default="")).strip()
    site_number = str(_get("site_number", "branch_id", "site_id", default="")).strip()
    if not site_name and not site_number:
        raise EventError(f"[{alert_id}] missing required field: site_name or site_number")
    if not site_name:
        site_name = f"Site {site_number}"

    # 3. Description
    description = str(_get("issue_description", "description", "issue", "alarm_type", "short_description", "fault_type", default="")).strip()
    if not description:
        raise EventError(f"[{alert_id}] missing required field: description (or issue / alarm_type)")

    # 11. Severity
    severity = str(_get("severity", "priority", default="Major")).strip()

    # 13. Country Code
    country_code = str(_get("country_code", default="+966")).strip()
    if country_code and not country_code.startswith("+"):
        country_code = "+" + country_code

    # 14. Alert Status & Ticket Status
    alert_status = str(_get("alert_status", "alarm_status", "status", "ticket_status", default="Active")).strip()
    ticket_status = str(_get("ticket_status", "status", "alert_status", default="Assigned")).strip()

    # 12. Date & Time
    date_time = str(_get("date_time", "event_timestamp", "timestamp", "assigned_at", "date_and_time", "event_time", "created_at",
                         default=now_iso())).strip()

    # 5 & 6. Technician Name, Number & Language
    raw_recipients = item.get("recipients") or []
    tech_info = {}
    tl_info = {}
    mgr_info = {}

    if isinstance(raw_recipients, list):
        for r in raw_recipients:
            if isinstance(r, dict) and r.get("role"):
                role = str(r["role"]).strip().lower()
                if role in ("technician", "engineer"):
                    tech_info = r
                elif role in ("team_leader", "tl"):
                    tl_info = r
                elif role in ("manager", "mgr"):
                    mgr_info = r
    elif isinstance(raw_recipients, dict):
        tech_info = raw_recipients.get("technician") or raw_recipients.get("engineer") or {}
        tl_info = raw_recipients.get("team_leader") or raw_recipients.get("tl") or {}
        mgr_info = raw_recipients.get("manager") or raw_recipients.get("mgr") or {}

    tech_dict = item.get("technician") if isinstance(item.get("technician"), dict) else {}
    tech_id = str(tech_dict.get("technician_id") or tech_dict.get("eng_id") or tech_dict.get("id") or tech_info.get("id") or _get("technician_id", "engineer_id", "tech_id", default="")).strip()
    tech_name = str(tech_dict.get("name") or tech_info.get("name") or _get("technician_name", "engineer_name", "tech_name", "technician", default="")).strip()
    tech_mob = str(tech_dict.get("mobile_number") or tech_dict.get("mobile") or tech_dict.get("phone") or
                   tech_info.get("mobile_number") or tech_info.get("mobile") or
                   _get("technician_number", "technician_mobile", "phone_number", "engineer_mobile", "tech_mobile", "mobile", default="")).strip()
    raw_tech_lang = tech_dict.get("preferred_language") or tech_dict.get("language") or tech_info.get("preferred_language") or tech_info.get("language") or _get("technician_language", "language", default="ask")
    tech_lang = _normalize_lang(raw_tech_lang, default="ask")

    if not tech_mob:
        raise EventError(f"[{alert_id}] missing required field: technician_number (or technician_mobile)")
    tech_mob = _format_e164(tech_mob, country_code)

    recipients = [{
        "role": "technician",
        "id": tech_id,
        "name": tech_name,
        "mobile": tech_mob,
        "language": tech_lang
    }]

    # 7 & 8. Team Leader (TL) Name & Number
    tl_dict = item.get("team_leader") if isinstance(item.get("team_leader"), dict) else {}
    tl_id = str(tl_dict.get("team_leader_id") or tl_dict.get("tl_id") or tl_dict.get("id") or tl_info.get("id") or _get("team_leader_id", "tl_id", default="")).strip()
    tl_name = str(tl_dict.get("name") or tl_info.get("name") or _get("team_leader_name", "team_leader_tl_name", "tl_name", "team_leader", default="")).strip()
    tl_mob = str(tl_dict.get("mobile_number") or tl_dict.get("mobile") or tl_dict.get("phone") or
                 tl_info.get("mobile_number") or tl_info.get("mobile") or
                 _get("team_leader_number", "team_leader_tl_number", "team_leader_mobile", "tl_mobile", "tl_number", default="")).strip()
    raw_tl_lang = tl_dict.get("preferred_language") or tl_dict.get("language") or tl_info.get("preferred_language") or tl_info.get("language") or _get("team_leader_language", default="en")
    tl_lang = _normalize_lang(raw_tl_lang, default="en")

    if tl_mob:
        recipients.append({
            "role": "team_leader",
            "id": tl_id,
            "name": tl_name,
            "mobile": _format_e164(tl_mob, country_code),
            "language": tl_lang
        })

    # 9 & 10. Manager Name & Number
    mgr_dict = item.get("manager") if isinstance(item.get("manager"), dict) else {}
    mgr_id = str(mgr_dict.get("manager_id") or mgr_dict.get("mgr_id") or mgr_dict.get("id") or mgr_info.get("id") or _get("manager_id", "mgr_id", default="")).strip()
    mgr_name = str(mgr_dict.get("name") or mgr_info.get("name") or _get("manager_name", "mgr_name", "manager", default="")).strip()
    mgr_mob = str(mgr_dict.get("mobile_number") or mgr_dict.get("mobile") or mgr_dict.get("phone") or
                  mgr_info.get("mobile_number") or mgr_info.get("mobile") or
                  _get("manager_number", "manager_mobile", "mgr_mobile", "mgr_number", default="")).strip()
    raw_mgr_lang = mgr_dict.get("preferred_language") or mgr_dict.get("language") or mgr_info.get("preferred_language") or mgr_info.get("language") or _get("manager_language", default="en")
    mgr_lang = _normalize_lang(raw_mgr_lang, default="en")

    if mgr_mob:
        recipients.append({
            "role": "manager",
            "id": mgr_id,
            "name": mgr_name,
            "mobile": _format_e164(mgr_mob, country_code),
            "language": mgr_lang
        })

    # Optional Emergency Contacts / SPOC
    em_contacts = item.get("emergency_contacts") if isinstance(item.get("emergency_contacts"), dict) else {}
    spoc_name = str(_get("spoc_name", default="SPOC")).strip()
    spoc_mob = str(em_contacts.get("spoc") or _get("spoc_number", "spoc_mobile", "spoc", default="")).strip()
    if spoc_mob:
        recipients.append({
            "role": "spoc",
            "id": "SPOC",
            "name": spoc_name or "SPOC",
            "mobile": _format_e164(spoc_mob, country_code),
            "language": "en"
        })

    stamp = int(time.time() * 1000)
    assignment_id = str(_get("assignment_id", default=f"{ticket_id}-A{stamp}")).strip()

    # Event version
    raw_ver = _get("event_version", "version", default="")
    version = int(raw_ver) if str(raw_ver).strip().isdigit() else None

    # Acknowledgement digit
    ack_digit = str(_get("dtmf_digit", "dtmf_acknowledgement_digit", "ack_digit", default="1")).strip()

    event = {
        "event_id": str(_get("event_id", default=f"INTAKE-{ticket_id}-{stamp}")).strip(),
        "event_type": ("REASSIGNED" if str(_get("event_type", default="ASSIGNMENT_CREATED")).strip().upper() in ("ASSIGNMENT_REASSIGNED", "REASSIGN", "REASSIGNMENT") else str(_get("event_type", default="ASSIGNMENT_CREATED")).strip().upper()),
        "event_time": date_time,
        "version": version,
        "ack_digit": ack_digit,
        "reject_digit": str(_get("reject_digit", default="9")).strip(),
        "callback_url": _get("callback_url", default=None),
        "ticket": {
            "ticket_id": ticket_id,
            "alert_id": alert_id,
            "alarm_id": alarm_id,
            "assignment_id": assignment_id,
            "site_number": site_number,
            "site_name": site_name,
            "region": region,
            "country_code": country_code,
            "severity": severity,
            "status": ticket_status,
            "alarm_status": alert_status,
            "description": description
        },
        "recipients": recipients
    }

    event_id = str(_get("event_id", default=f"INTAKE-{ticket_id}-{stamp}")).strip()
    # Normalized dictionary containing all fields + ticket_id + alert_id + event_timestamp
    normalized = {
        "ticket_id": ticket_id,
        "alert_id": alert_id,
        "alarm_id": alarm_id,
        "event_id": event_id,
        "event_timestamp": date_time,
        "timestamp": date_time,
        "region": region,
        "site_name": site_name,
        "site_number": site_number,
        "description": description,
        "technician_name": tech_name,
        "technician_number": tech_mob,
        "team_leader_name": tl_name,
        "team_leader_number": _format_e164(tl_mob, country_code) if tl_mob else "",
        "manager_name": mgr_name,
        "manager_number": _format_e164(mgr_mob, country_code) if mgr_mob else "",
        "severity": severity,
        "date_time": date_time,
        "country_code": country_code,
        "alert_status": alert_status,
        "ack_digit": ack_digit,
        "recipients": {r["role"]: r for r in recipients}
    }

    return event, normalized


def handle_alert_intake(payload):
    """Handles alert intake for both single alert (dict) or multiple alerts (list / dict with 'alerts').
    Returns: (http_code, response_json)
    """
    if isinstance(payload, dict):
        if "alerts" in payload and isinstance(payload["alerts"], list):
            items = payload["alerts"]
            is_batch = True
            # Propagate parent context (branch, timestamp, batch_id) to each alert if missing
            parent_branch = payload.get("branch") if isinstance(payload.get("branch"), dict) else None
            parent_time = payload.get("event_timestamp") or payload.get("timestamp") or payload.get("date_time")
            parent_batch_id = payload.get("batch_id")
            for it in items:
                if isinstance(it, dict):
                    if parent_branch and "branch" not in it:
                        it["branch"] = parent_branch
                    if parent_time and "event_timestamp" not in it and "date_time" not in it:
                        it["event_timestamp"] = parent_time
                    if parent_batch_id and "batch_id" not in it:
                        it["batch_id"] = parent_batch_id
        else:
            items = [payload]
            is_batch = False
    elif isinstance(payload, list):
        items = payload
        is_batch = True
    else:
        return 400, {
            "status": "error",
            "error": "body must be a JSON object or array of alert objects"
        }

    if not items:
        return 400, {
            "status": "error",
            "error": "no alerts provided in request body"
        }

    # Persist alert intake payload into PostgreSQL production database
    try:
        import pg_store
        pg_store.save_alert_intake(payload)
    except Exception as e:
        logger.warning(f"PostgreSQL persistence notice: {e}")

    results = []
    success_count = 0
    fail_count = 0

    for idx, item in enumerate(items):
        try:
            event, normalized = normalize_intake_alert(item)
            code, body = ingest_event(event)
            t = get_ticket(normalized["ticket_id"])
            ack_digit = t["ack_digit"] if t else str(event.get("ack_digit") or "1")
            reject_digit = t["reject_digit"] if t else str(event.get("reject_digit") or "9")
            latest_call = q("SELECT dtmf, ack_result, outcome FROM calls WHERE ticket_id=? AND role='technician' ORDER BY id DESC LIMIT 1", (normalized["ticket_id"],), one=True) if t else None
            dtmf_pressed = latest_call["dtmf"] if (latest_call and latest_call["dtmf"]) else None
            acknowledged = (t["uc1_state"] == "ACKNOWLEDGED") if t else False
            event_id = str(event.get("event_id") or normalized.get("event_id") or "").strip()
            event_timestamp = str(event.get("event_time") or normalized.get("date_time") or "").strip()
            alert_entry = {
                **normalized,
                "event_id": event_id,
                "event_timestamp": event_timestamp,
                "timestamp": event_timestamp,
                "ack_digit": ack_digit,
                "reject_digit": reject_digit,
                "dtmf_pressed": dtmf_pressed,
                "ack_number_pressed": dtmf_pressed,
                "acknowledged": acknowledged
            }
            event_id = str(event.get("event_id") or normalized.get("event_id") or "").strip()
            event_timestamp = str(event.get("event_time") or normalized.get("date_time") or "").strip()
            results.append({
                "index": idx,
                "ticket_id": normalized["ticket_id"],
                "alert_id": normalized["alert_id"],
                "event_id": event_id,
                "event_timestamp": event_timestamp,
                "event_time": event_timestamp,
                "timestamp": event_timestamp,
                "ack_digit": ack_digit,
                "reject_digit": reject_digit,
                "dtmf_pressed": dtmf_pressed,
                "ack_number_pressed": dtmf_pressed,
                "acknowledged": acknowledged,
                "status": body.get("result") or ("accepted" if code in (200, 202) else "error"),
                "http_code": code,
                "actions": body.get("actions", []),
                "alert": alert_entry
            })
            if code in (200, 202):
                success_count += 1
            else:
                fail_count += 1
        except EventError as e:
            fail_count += 1
            tid = ""
            if isinstance(item, dict):
                td = item.get("ticket") if isinstance(item.get("ticket"), dict) else {}
                tid = item.get("ticket_id") or td.get("ticket_id") or item.get("alert_id") or ""
            results.append({
                "index": idx,
                "ticket_id": tid or f"index_{idx}",
                "status": "failed",
                "http_code": 400,
                "error": str(e),
                "alert": item if isinstance(item, dict) else {}
            })
        except Exception as e:
            fail_count += 1
            tid = ""
            if isinstance(item, dict):
                td = item.get("ticket") if isinstance(item.get("ticket"), dict) else {}
                tid = item.get("ticket_id") or td.get("ticket_id") or item.get("alert_id") or ""
            results.append({
                "index": idx,
                "ticket_id": tid or f"index_{idx}",
                "status": "error",
                "http_code": 500,
                "error": f"Internal error: {e}",
                "alert": item if isinstance(item, dict) else {}
            })

    if not is_batch:
        res = results[0]
        if res["status"] in ("failed", "error"):
            return res["http_code"], {
                "status": "error",
                "mode": "single",
                "ticket_id": res["ticket_id"],
                "error": res.get("error", "Failed to process alert"),
                "alert": res.get("alert")
            }
        return res["http_code"], {
            "status": "success",
            "message": "Alert accepted and processed",
            "mode": "single",
            "ticket_id": res["ticket_id"],
            "alert_id": res.get("alert_id") or (res.get("alert") or {}).get("alert_id") or res["ticket_id"],
            "event_id": res.get("event_id") or (res.get("alert") or {}).get("event_id") or "",
            "event_timestamp": res.get("event_timestamp") or (res.get("alert") or {}).get("date_time") or "",
            "event_time": res.get("event_timestamp") or (res.get("alert") or {}).get("date_time") or "",
            "timestamp": res.get("event_timestamp") or (res.get("alert") or {}).get("date_time") or "",
            "ack_digit": res.get("ack_digit"),
            "reject_digit": res.get("reject_digit"),
            "dtmf_pressed": res.get("dtmf_pressed"),
            "ack_number_pressed": res.get("ack_number_pressed"),
            "acknowledged": res.get("acknowledged"),
            "result": res["status"],
            "actions": res.get("actions", []),
            "alert": res.get("alert")
        }

    overall_status = "success" if fail_count == 0 else ("partial_success" if success_count > 0 else "failed")
    http_code = 200 if fail_count == 0 else (207 if success_count > 0 else 400)
    return http_code, {
        "status": overall_status,
        "mode": "batch",
        "total": len(items),
        "successful": success_count,
        "failed": fail_count,
        "results": results
    }


def get_alerts_view(payload=None):
    """Retrieve alerts view for GET /api/getalerts.
    If payload with alerts is passed, returns status for those alerts.
    Otherwise returns recent tickets from the database.
    """
    items = []
    if isinstance(payload, dict):
        if "alerts" in payload and isinstance(payload["alerts"], list):
            items = payload["alerts"]
            parent_branch = payload.get("branch") if isinstance(payload.get("branch"), dict) else None
            parent_time = payload.get("event_timestamp") or payload.get("timestamp") or payload.get("date_time")
            for it in items:
                if isinstance(it, dict):
                    if parent_branch and "branch" not in it:
                        it["branch"] = parent_branch
                    if parent_time and "event_timestamp" not in it and "date_time" not in it:
                        it["event_timestamp"] = parent_time
        elif "ticket_id" in payload or "alert_id" in payload:
            items = [payload]
    elif isinstance(payload, list):
        items = payload

    results = []
    if items:
        for idx, item in enumerate(items):
            try:
                ev, norm = normalize_intake_alert(item)
                tid = norm["ticket_id"]
                t = get_ticket(tid)
                if not t:
                    code, body = ingest_event(ev)
                    t = get_ticket(tid)
                    status_val = body.get("result") or "accepted"
                    http_code = code
                    actions_val = body.get("actions", [])
                else:
                    status_val = "accepted" if t["uc1_state"] in ("TECH_CALLING", "ACKNOWLEDGED") else "duplicate"
                    http_code = 200
                    actions_val = []

                ack_digit = t["ack_digit"] if t else norm.get("ack_digit", "1")
                reject_digit = t["reject_digit"] if t else "9"
                latest_call = q("SELECT dtmf, ack_result, outcome FROM calls WHERE ticket_id=? AND role='technician' ORDER BY id DESC LIMIT 1", (tid,), one=True) if t else None
                dtmf_pressed = latest_call["dtmf"] if (latest_call and latest_call["dtmf"]) else None
                acknowledged = (t["uc1_state"] == "ACKNOWLEDGED") if t else False

                evt_id = str(item.get("event_id") or norm.get("event_id") or (t.get("last_event_id") if t else "") or "").strip()
                evt_time = str(item.get("event_timestamp") or item.get("timestamp") or norm.get("date_time") or (t.get("last_event_time") if t else "") or "").strip()
                alert_dict = {
                    **norm,
                    "event_id": evt_id,
                    "event_timestamp": evt_time,
                    "timestamp": evt_time,
                    "ack_digit": ack_digit,
                    "reject_digit": reject_digit,
                    "dtmf_pressed": dtmf_pressed,
                    "ack_number_pressed": dtmf_pressed,
                    "acknowledged": acknowledged
                }

                evt_id = str(item.get("event_id") or norm.get("event_id") or (t.get("last_event_id") if t else "") or "").strip()
                evt_time = str(item.get("event_timestamp") or item.get("timestamp") or norm.get("date_time") or (t.get("last_event_time") if t else "") or "").strip()
                results.append({
                    "index": idx,
                    "ticket_id": tid,
                    "alert_id": norm["alert_id"],
                    "event_id": evt_id,
                    "event_timestamp": evt_time,
                    "event_time": evt_time,
                    "timestamp": evt_time,
                    "ack_digit": ack_digit,
                    "reject_digit": reject_digit,
                    "dtmf_pressed": dtmf_pressed,
                    "ack_number_pressed": dtmf_pressed,
                    "acknowledged": acknowledged,
                    "status": status_val,
                    "http_code": http_code,
                    "actions": actions_val,
                    "alert": alert_dict
                })
            except Exception as e:
                tid = ""
                if isinstance(item, dict):
                    td = item.get("ticket") if isinstance(item.get("ticket"), dict) else {}
                    tid = item.get("ticket_id") or td.get("ticket_id") or item.get("alert_id") or f"index_{idx}"
                results.append({
                    "index": idx,
                    "ticket_id": tid or f"index_{idx}",
                    "status": "failed",
                    "http_code": 400,
                    "error": str(e),
                    "alert": item if isinstance(item, dict) else {}
                })
    else:
        rows = q("SELECT ticket_id FROM tickets ORDER BY rowid DESC LIMIT 50")
        for idx, r in enumerate(rows):
            tid = r["ticket_id"]
            t = get_ticket(tid)
            if not t:
                continue
            ack_digit = t["ack_digit"] or "1"
            reject_digit = t["reject_digit"] or "9"
            latest_call = q("SELECT dtmf, ack_result, outcome FROM calls WHERE ticket_id=? AND role='technician' ORDER BY id DESC LIMIT 1", (tid,), one=True)
            dtmf_pressed = latest_call["dtmf"] if (latest_call and latest_call["dtmf"]) else None
            acknowledged = (t["uc1_state"] == "ACKNOWLEDGED")
            alert_id = t.get("alert_id") or t.get("alarm_id") or tid
            recipients = t["recipients"] if isinstance(t["recipients"], dict) else json.loads(t["recipients"] or "{}")

            tech_r = recipients.get("technician") or {}
            tl_r = recipients.get("team_leader") or {}
            mgr_r = recipients.get("manager") or {}

            evt_id_db = t.get("last_event_id") or f"EVT-{tid}"
            evt_time_db = t.get("last_event_time") or t.get("created_at") or ""
            alert_dict = {
                "ticket_id": tid,
                "alert_id": alert_id,
                "alarm_id": t["alarm_id"],
                "event_id": evt_id_db,
                "event_timestamp": evt_time_db,
                "timestamp": evt_time_db,
                "region": t.get("region") or "",
                "site_name": t["site_name"] or "",
                "site_number": t["site_number"] or "",
                "description": t["description"] or "",
                "technician_name": tech_r.get("name", ""),
                "technician_number": tech_r.get("mobile", ""),
                "team_leader_name": tl_r.get("name", ""),
                "team_leader_number": tl_r.get("mobile", ""),
                "manager_name": mgr_r.get("name", ""),
                "manager_number": mgr_r.get("mobile", ""),
                "severity": t["severity"] or "Major",
                "date_time": evt_time_db,
                "country_code": t.get("country_code") or "+966",
                "alert_status": t["status"] or "Active",
                "ack_digit": ack_digit,
                "recipients": recipients,
                "reject_digit": reject_digit,
                "dtmf_pressed": dtmf_pressed,
                "ack_number_pressed": dtmf_pressed,
                "acknowledged": acknowledged
            }

            results.append({
                "index": idx,
                "ticket_id": tid,
                "alert_id": alert_id,
                "event_id": t.get("last_event_id") or f"EVT-{tid}",
                "event_timestamp": t.get("last_event_time") or t.get("created_at") or "",
                "event_time": t.get("last_event_time") or t.get("created_at") or "",
                "timestamp": t.get("last_event_time") or t.get("created_at") or "",
                "ack_digit": ack_digit,
                "reject_digit": reject_digit,
                "dtmf_pressed": dtmf_pressed,
                "ack_number_pressed": dtmf_pressed,
                "acknowledged": acknowledged,
                "status": "duplicate" if t["uc1_state"] in FINAL else "accepted",
                "http_code": 200,
                "actions": [],
                "alert": alert_dict
            })

    return {
        "status": "success",
        "mode": "batch",
        "total": len(results),
        "successful": sum(1 for r in results if r["http_code"] in (200, 202)),
        "failed": sum(1 for r in results if r["http_code"] not in (200, 202)),
        "results": results
    }

