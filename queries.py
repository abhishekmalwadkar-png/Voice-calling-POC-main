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
    """Every event, timer, call status, key press and decision for one ticket, oldest first."""
    rows = q("SELECT ts, kind, message, call_id, event_id, data FROM audit_log WHERE ticket_id=? ORDER BY id",
             (ticket_id,))
    for r in rows:
        r["data"] = json.loads(r["data"]) if r["data"] else None
    return rows


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
    else:
        raise ValueError("type must be calls, events or timeline")
    stamp = (params.get("from") or "all")[:10]
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
