"""Read-only audit queries (PDF p.7 "Platform tool delivery"): timeline, search, escalations, exceptions, export."""
import csv
import io
import json

import workflow
from config import CFG
from store import iso_at, q

CALL_COLUMNS = ["id", "ticket_id", "assignment_id", "workflow", "role", "attempt_no", "recipient_name", "to_number",
                "language", "language_source", "template", "template_version", "call_sid", "status", "outcome",
                "dtmf", "ack_result", "picked_up", "cause_code", "error_message", "duration_sec", "created_at",
                "initiated_at", "answered_at", "ended_at", "superseded"]
EVENT_COLUMNS = ["event_id", "ticket_id", "event_type", "version", "event_time", "received_at", "result", "note"]
TIMELINE_COLUMNS = ["ts", "ticket_id", "kind", "message", "call_id", "event_id"]


def _limit(params, default=200):
    try:
        return max(1, min(int(params.get("limit", default)), 5000))
    except ValueError:
        return default


def list_tickets(params):
    where, args = [], []
    for key, col in (("state", "uc1_state"), ("status", "status"), ("severity", "severity")):
        if params.get(key):
            where.append(f"LOWER({col})=LOWER(?)")
            args.append(params[key])
    sql = "SELECT ticket_id FROM tickets" + (" WHERE " + " AND ".join(where) if where else "")
    rows = q(sql + " ORDER BY updated_at DESC LIMIT ?", (*args, _limit(params, 100)))
    return [workflow.ticket_view(r["ticket_id"]) for r in rows]


def search_calls(params):
    """GET /api/calls?ticket_id=&role=&workflow=&status=&outcome=&language=&from=&to=&limit="""
    where, args = [], []
    for key in ("ticket_id", "role", "workflow", "status", "outcome", "language"):
        if params.get(key):
            where.append(f"LOWER({key})=LOWER(?)")
            args.append(params[key])
    if params.get("from"):
        where.append("created_at >= ?")
        args.append(params["from"])
    if params.get("to"):
        where.append("created_at <= ?")
        args.append(params["to"])
    sql = "SELECT * FROM calls" + (" WHERE " + " AND ".join(where) if where else "")
    rows = q(sql + " ORDER BY id DESC LIMIT ?", (*args, _limit(params)))
    out = []
    for c in rows:
        view = workflow._call_view(dict(c, processed=c["processed"]))
        view.update(ticket_id=c["ticket_id"], assignment_id=c["assignment_id"])
        out.append(view)
    return out


def timeline(ticket_id):
    """Chronological timeline of received events, attempts, DTMF results, messages, timers, state checks and escalations."""
    items = []
    # 1. Audit logs
    rows = q("SELECT ts, kind, message, call_id, event_id, data FROM audit_log WHERE ticket_id=? ORDER BY id", (ticket_id,))
    for r in rows:
        items.append({
            "timestamp": r["ts"],
            "category": r["kind"],
            "description": r["message"],
            "call_id": r["call_id"],
            "event_id": r["event_id"],
            "data": json.loads(r["data"]) if r["data"] else None,
            "source": "audit"
        })
    # 2. Voice Calls
    calls = q("SELECT * FROM calls WHERE ticket_id=? ORDER BY id", (ticket_id,))
    for c in calls:
        if c.get("created_at") or c.get("initiated_at"):
            items.append({
                "timestamp": c.get("initiated_at") or c.get("created_at") or "",
                "category": "call_initiated",
                "description": f"Voice call initiated to {c.get('role', 'Technician').capitalize()} ({c.get('to_number')}) - Attempt #{c.get('attempt_no', 1)}",
                "call_sid": c.get("call_sid"),
                "event_id": None,
                "source": "voice"
            })
        if c.get("answered_at"):
            items.append({
                "timestamp": c.get("answered_at"),
                "category": "call_answered",
                "description": f"Call answered by {c.get('recipient_name') or c.get('role', 'Technician').capitalize()} ({c.get('to_number')})",
                "call_sid": c.get("call_sid"),
                "source": "voice"
            })
        if c.get("dtmf"):
            items.append({
                "timestamp": c.get("ended_at") or c.get("answered_at") or "",
                "category": "dtmf_captured",
                "description": f"DTMF Key pressed: '{c.get('dtmf')}' - Result: {c.get('ack_result') or 'recorded'} ({c.get('response') or 'completed'})",
                "call_sid": c.get("call_sid"),
                "source": "voice"
            })
    # 3. WhatsApp messages
    try:
        wams = q("SELECT * FROM whatsapp_messages WHERE ticket_id=? ORDER BY id", (ticket_id,))
        for w in wams:
            items.append({
                "timestamp": w.get("sent_at") or w.get("submitted_at") or "",
                "category": "sms_dispatched",
                "description": f"SMS {w.get('message_type', 'Notification')} dispatched to {w.get('recipient_role', 'Recipient').capitalize()} ({w.get('mobile_number')}) via template '{w.get('template_name')}'",
                "provider_message_id": w.get("provider_message_id"),
                "status": w.get("delivery_status"),
                "source": "sms"
            })
    except Exception:
        pass
    
    items.sort(key=lambda x: str(x.get("timestamp") or ""))
    return items


def escalations(params):
    status = (params.get("status") or "all").lower()
    states = {"unresolved": ("ESCALATION_UNRESOLVED", "ESCALATION_NO_CONTACTS"),
              "open": ("ESCALATING",), "resolved": ("TL_REACHED", "MANAGER_REACHED")}.get(status)
    sql = "SELECT ticket_id FROM tickets WHERE esc_reason IS NOT NULL"
    args = []
    if states:
        sql += f" AND uc1_state IN ({','.join('?' * len(states))})"
        args = list(states)
    out = []
    for r in q(sql + " ORDER BY updated_at DESC LIMIT ?", (*args, _limit(params))):
        v = workflow.ticket_view(r["ticket_id"])
        out.append({"ticket_id": v["ticket_id"], "assignment_id": v["assignment_id"], "site_number": v["site_number"],
                    "state": v["call_workflow"]["state"], **v["call_workflow"]["escalation"],
                    "response_summary": v["call_workflow"]["response_summary"], "updated_at": v["updated_at"]})
    return out


def exceptions(params):
    """Things a person should look at: unreachable escalations, call errors, held contacts, stale events."""
    items = []
    for e in escalations({"status": "unresolved"}):
        items.append({"type": "unresolved_escalation", "ticket_id": e["ticket_id"], "at": e["updated_at"],
                      "detail": e["response_summary"]})
    for c in q("SELECT * FROM calls WHERE outcome IN ('call_error','call_failed') ORDER BY id DESC LIMIT 500"):
        items.append({"type": "call_failure", "ticket_id": c["ticket_id"], "at": c["ended_at"] or c["created_at"],
                      "detail": f"{c['role']} call #{c['attempt_no']} to {c['to_number']}: "
                                f"{c['error_message'] or c['outcome']} (cause {c['cause_code'] or '-'})",
                      "call_id": c["id"]})
    for j in q("SELECT * FROM jobs WHERE status='held' ORDER BY id DESC"):
        items.append({"type": "contact_held", "ticket_id": j["ticket_id"], "at": j["updated_at"],
                      "detail": f"{j['kind']} #{j['attempt_no']}: {j['note']}"})
    for e in q("SELECT * FROM events WHERE result='stale' ORDER BY received_at DESC LIMIT 500"):
        items.append({"type": "stale_event", "ticket_id": e["ticket_id"], "at": e["received_at"],
                      "detail": f"{e['event_type']} {e['event_id']}: {e['note']}"})
    if params.get("type"):
        items = [i for i in items if i["type"] == params["type"]]
    return sorted(items, key=lambda i: i["at"] or "", reverse=True)[:_limit(params)]


def events(params):
    where, args = [], []
    for key in ("ticket_id", "event_type", "result"):
        if params.get(key):
            where.append(f"LOWER({key})=LOWER(?)")
            args.append(params[key])
    sql = f"SELECT {', '.join(EVENT_COLUMNS)} FROM events" + (" WHERE " + " AND ".join(where) if where else "")
    return q(sql + " ORDER BY received_at DESC LIMIT ?", (*args, _limit(params)))


def _log_line(kind, r):
    """One readable text line per row, for the .log downloads."""
    def g(key):
        v = r.get(key)
        return "-" if v in (None, "") else str(v).replace("\r", " ").replace("\n", " ")

    if kind in ("audit", "audit_log", "tb_audit"):
        line = f"[{g('created_at')}] {g('ticket_id')} | {g('activity_type')} | {g('activity_status')} | {g('activity_description')}"
        extra = [f"{k}={r[k]}" for k in ("assignment_id", "workflow_id", "reference_id", "event_id") if r.get(k)]
        return line + (" | " + " ".join(extra) if extra else "")
    if kind == "timeline":
        extra = [f"{k}={r[k]}" for k in ("call_id", "event_id") if r.get(k)]
        line = f"[{g('ts')}] {g('ticket_id')} | {g('kind')} | {g('message')}"
        return line + (" | " + " ".join(extra) if extra else "")
    if kind == "events":
        return (f"[{g('received_at')}] {g('ticket_id')} | {g('event_type')} | {g('result')} | {g('note')} "
                f"| event_id={g('event_id')} version={g('version')}")
    if kind == "calls":
        return (f"[{g('created_at')}] {g('ticket_id')} | {g('role')} call #{g('attempt_no')} to {g('to_number')} | "
                f"status={g('status')} outcome={g('outcome')} key={g('dtmf')} duration={g('duration_sec')}s | "
                f"assignment={g('assignment_id')} sid={g('call_sid')}")
    return " | ".join(f"{k}={g(k)}" for k in r)


def export(params):
    """GET /api/audit/export?type=calls|events|timeline&format=csv|json&from=&to=  -> (content_type, body, filename)"""
    kind = (params.get("type") or "calls").lower()
    frm, to = params.get("from") or "", params.get("to") or "9999"
    if kind == "events":
        cols, rows = EVENT_COLUMNS, q(f"SELECT {', '.join(EVENT_COLUMNS)} FROM events WHERE received_at BETWEEN ? AND ? "
                                      "ORDER BY received_at", (frm, to))
    elif kind == "timeline":
        cols = TIMELINE_COLUMNS
        rows = q(f"SELECT {', '.join(TIMELINE_COLUMNS)} FROM audit_log WHERE ts BETWEEN ? AND ? "
                 + ("AND ticket_id=? " if params.get("ticket_id") else "") + "ORDER BY id",
                 (frm, to, *([params["ticket_id"]] if params.get("ticket_id") else [])))
    elif kind == "calls":
        cols, rows = CALL_COLUMNS, q(f"SELECT {', '.join(CALL_COLUMNS)} FROM calls WHERE created_at BETWEEN ? AND ? "
                                     "ORDER BY id", (frm, to))
    elif kind in ("audit", "audit_log", "tb_audit"):
        import pg_store
        cols = pg_store.AUDIT_PG_COLUMNS
        rows = pg_store.audit_rows_pg(params.get("from") or "", params.get("to") or "",
                                      params.get("ticket_id") or "")
    elif kind in ("evidence", "audit_evidence"):
        cols = [
            "ticket_id", "alert_id", "assignment_id", "site_number", "site_name", "severity",
            "operational_status", "channel", "recipient_role", "recipient_name", "to_number",
            "provider_identifier", "status", "dtmf_key", "outcome_response", "duration_sec",
            "event_timestamp", "completed_delivered_at"
        ]
        call_rows = q("""
            SELECT 
                c.ticket_id, t.alarm_id as alert_id, c.assignment_id, t.site_number, t.site_name, t.severity,
                t.operational_status, 'voice_call' as channel, c.role as recipient_role, c.recipient_name, c.to_number,
                c.call_sid as provider_identifier, c.status, c.dtmf as dtmf_key, c.response as outcome_response,
                c.duration_sec, c.initiated_at as event_timestamp, c.ended_at as completed_delivered_at
            FROM calls c
            LEFT JOIN tickets t ON c.ticket_id = t.ticket_id
            ORDER BY c.id DESC LIMIT 5000
        """)
        try:
            wa_rows = q("""
                SELECT 
                    w.ticket_id, t.alarm_id as alert_id, w.assignment_id, t.site_number, t.site_name, t.severity,
                    t.operational_status, 'whatsapp' as channel, w.recipient_role, w.recipient_name, w.mobile_number as to_number,
                    w.provider_message_id as provider_identifier, w.delivery_status as status, '' as dtmf_key,
                    w.template_name as outcome_response, 0 as duration_sec, w.submitted_at as event_timestamp,
                    w.sent_at as completed_delivered_at
                FROM whatsapp_messages w
                LEFT JOIN tickets t ON w.ticket_id = t.ticket_id
                ORDER BY w.id DESC LIMIT 5000
            """)
        except Exception:
            wa_rows = []
        rows = list(call_rows) + list(wa_rows)
    else:
        raise ValueError("type must be audit, calls, events, timeline or evidence")
    stamp = (params.get("from") or "all")[:10]
    if (params.get("format") or "csv").lower() == "log":
        text = "\n".join(_log_line(kind, r) for r in rows)
        return "text/plain; charset=utf-8", text + ("\n" if text else ""), f"flm_{kind}_{stamp}.log"
    if (params.get("format") or "csv").lower() == "json":
        return "application/json; charset=utf-8", json.dumps(rows, indent=2, ensure_ascii=False), \
            f"flm_{kind}_{stamp}.json"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return "text/csv; charset=utf-8", "﻿" + buf.getvalue(), f"flm_{kind}_{stamp}.csv"


def config_view():
    """Effective settings - no secrets."""
    keys = ("from", "dry_run", "base_url", "tech_max_attempts", "tech_retry_sec", "ring_timeout", "followup_sec",
            "tl_max_attempts", "manager_max_attempts", "manager_retry_sec", "default_language", "menu_languages",
            "default_ack_digit", "default_reject_digit", "state_max_age_sec", "validate_twilio_signature")
    out = {k: CFG[k] for k in keys}
    for k in ("closed_statuses", "alarm_cleared_values", "operational_done_statuses"):
        out[k] = sorted(CFG[k])
    out["webhook_url_set"] = bool(CFG["webhook_url"])
    out["event_types"] = sorted(workflow.EVENT_TYPES)
    return out


def pending_timers():
    rows = q("SELECT id, ticket_id, kind, attempt_no, status, note, run_at FROM jobs "
             "WHERE status IN ('pending','held','running') ORDER BY run_at")
    for r in rows:
        r["due_at"] = iso_at(r.pop("run_at"))
    return rows
