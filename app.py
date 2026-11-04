
def docs_html():
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NexTask Gateway - API Documentation</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.5.0/github-markdown.min.css">
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    body {
      background-color: #0d1117;
      color: #c9d1d9;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      margin: 0;
      padding: 20px;
    }
    .markdown-body {
      box-sizing: border-box;
      min-width: 200px;
      max-width: 980px;
      margin: 0 auto;
      padding: 45px;
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 8px;
    }
    .badge {
      display: inline-block;
      padding: 4px 8px;
      border-radius: 4px;
      font-weight: bold;
      font-size: 12px;
      margin-right: 6px;
    }
    .get { background: #238636; color: white; }
    .post { background: #1f6feb; color: white; }
    @media (max-width: 767px) {
      .markdown-body { padding: 15px; }
    }
  </style>
</head>
<body>
  <article class="markdown-body" id="content">Loading documentation...</article>
  <script>
    fetch('/api/docs/raw')
      .then(res => res.text())
      .then(md => {
        document.getElementById('content').innerHTML = marked.parse(md);
      })
      .catch(err => {
        document.getElementById('content').innerHTML = '<p>Error loading documentation: ' + err + '</p>';
      });
  </script>
</body>
</html>"""

"""
FLM Helpdesk - voice call platform (Twilio). Implements the call side of "FLM Helpdesk Final PoC Workflows":
UC1 technician notification + acknowledgement, 2nd attempt, Team Leader / Manager escalation calls,
15-minute follow-up reminder (triggered by the caller), contact guard, deduplication and a call audit trail.
WhatsApp is out of scope (handled by another system - use the outbound webhook events to drive it).

Run:  python app.py      (standard library only)
API:  see "FLM Voice Call API Guide"; every /api/* call needs header X-API-Key.
"""
import base64
import html
import importlib
import json
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

import ae_whatsapp
import queries
import store
import texts
import twilio_client
import config
import workflow
from config import CFG
import os
import sys

LOG_FILE_PATH = os.path.join(config.BASE_DIR, "server.log")

class _TeeStream:
    def __init__(self, original, file_path):
        self.original = original
        self.file_path = file_path
        self._lock = threading.Lock()

    def write(self, data):
        self.original.write(data)
        if not data:
            return
        with self._lock:
            try:
                with open(self.file_path, "a", encoding="utf-8") as f:
                    f.write(data)
            except Exception:
                pass

    def flush(self):
        self.original.flush()

if not isinstance(sys.stdout, _TeeStream):
    sys.stdout = _TeeStream(sys.stdout, LOG_FILE_PATH)
if not isinstance(sys.stderr, _TeeStream):
    sys.stderr = _TeeStream(sys.stderr, LOG_FILE_PATH)


def _reload_modules():
    """Hot-reload modified modules into memory without restarting the server or tunnel."""
    config.load_env(override=True)
    import pg_store
    modules = [store, config, texts, ae_whatsapp, queries, workflow, pg_store]
    reloaded = []
    for mod in modules:
        try:
            importlib.reload(mod)
            reloaded.append(mod.__name__)
        except Exception as e:
            reloaded.append(f"{mod.__name__} (error: {e})")
    msg = f"Hot-reloaded modules: {', '.join(reloaded)}"
    print(f"[{store.now_iso()}] - modules_reloaded: {msg}", flush=True)
    store.audit(None, "modules_reloaded", msg)
    return reloaded



# --------------------------------------------------------------------------- legacy single-call API (/api/call)
def legacy_call(form):
    """POST /api/call: supports both flat format {alert_id, ticket_id, phone_number, site_details...}
    and nested event format {event_id, event_type, ticket, recipients...}.
    """
    if not isinstance(form, dict):
        raise workflow.EventError("body must be a JSON object")

    if form.get("event_type") or (isinstance(form.get("ticket"), dict) and "recipients" in form):
        code, body = workflow.ingest_event(form)
        tid = form.get("ticket_id") or (form.get("ticket") or {}).get("ticket_id")
        if tid and code == 202:
            return code, dict(legacy_view(str(tid).upper()) or body, status_url=f"/api/call/{tid}")
        return code, body

    missing = [k for k in ("alert_id", "ticket_id", "phone_number", "site_details") if not str(form.get(k) or "").strip()]
    if missing:
        raise workflow.EventError(f"missing required field(s): {', '.join(missing)}")
    tid = str(form["ticket_id"]).strip().upper()
    t = store.get_ticket(tid)
    if t and t["uc1_state"] in workflow.ACTIVE:
        return 409, {"error": f"{tid} is already in progress ({t['uc1_state']}) - duplicate ignored"}
    stamp = int(time.time() * 1000)
    default_language = form.get("language") or "ask"  # per-person *_language overrides it
    recipients = [{"role": "technician", "mobile": str(form["phone_number"]).strip(),
                   "name": form.get("technician_name") or "",
                   "language": form.get("technician_language") or default_language}]
    for role, key in (("team_leader", "team_leader_number"), ("manager", "manager_number")):
        if str(form.get(key) or "").strip():
            recipients.append({"role": role, "mobile": str(form[key]).strip(), "name": form.get(f"{role}_name") or "",
                               "language": form.get(f"{role}_language") or default_language})
    event = {
        "event_id": f"CALL-{tid}-{stamp}", "event_type": "ASSIGNMENT_CREATED",
        "ticket": {"ticket_id": tid, "alarm_id": form["alert_id"], "assignment_id": f"{tid}-A{stamp}",
                   "site_name": form["site_details"], "description": form.get("description"),
                   "severity": form.get("severity") or "Major", "status": "Assigned",
                   "region": form.get("region"), "country_code": form.get("country_code")},
        "ack_digit": form.get("ack_digit") or "1", "reject_digit": form.get("reject_digit"),
        "callback_url": form.get("callback_url"), "recipients": recipients,
    }
    workflow.ingest_event(event)
    return 202, dict(legacy_view(tid), status_url=f"/api/call/{tid}")


def emergency_call(form):
    """POST /api/emergency_api or /api/emergency
    Triggers emergency alert: sends WhatsApp alert messages to technician and team leader
    (no voice calls).
    Accepts flat fields or nested event format.
    """
    if not isinstance(form, dict):
        raise workflow.EventError("body must be a JSON object")

    ticket_data = form.get("ticket") if isinstance(form.get("ticket"), dict) else {}
    tid = str(ticket_data.get("ticket_id") or form.get("ticket_id") or "").strip().upper()
    if not tid:
        raise workflow.EventError("missing required field: ticket_id")

    site = ticket_data.get("site_name") or form.get("site_details") or form.get("site_name") or "Site N/A"
    desc = ticket_data.get("description") or form.get("description") or "Emergency Alert"
    alarm_id = ticket_data.get("alarm_id") or form.get("alert_id") or form.get("alarm_id") or "ALM-EMERGENCY"
    severity = ticket_data.get("severity") or form.get("severity") or "Emergency"
    callback_url = form.get("callback_url") or ticket_data.get("callback_url")
    ack_digit = form.get("ack_digit") or "1"
    reject_digit = form.get("reject_digit") or "9"
    default_lang = form.get("language") or "ask"

    stamp = int(time.time() * 1000)
    aid = ticket_data.get("assignment_id") or form.get("assignment_id") or f"{tid}-EMG{stamp}"

    recipients = {}
    raw_recipients = form.get("recipients")
    if isinstance(raw_recipients, list):
        for r in raw_recipients:
            if isinstance(r, dict) and r.get("role"):
                recipients[r["role"]] = {
                    "mobile": str(r.get("mobile") or "").strip(),
                    "name": r.get("name") or "",
                    "language": r.get("language") or default_lang,
                }
    elif isinstance(raw_recipients, dict):
        for role, r in raw_recipients.items():
            if isinstance(r, dict):
                recipients[role] = {
                    "mobile": str(r.get("mobile") or "").strip(),
                    "name": r.get("name") or "",
                    "language": r.get("language") or default_lang,
                }

    tech_mob = str(form.get("phone_number") or form.get("technician_number") or "").strip()
    if tech_mob or "technician" not in recipients:
        recipients["technician"] = {
            "mobile": tech_mob or (recipients.get("technician") or {}).get("mobile", ""),
            "name": form.get("technician_name") or (recipients.get("technician") or {}).get("name", ""),
            "language": form.get("technician_language") or (recipients.get("technician") or {}).get("language") or "ask",
        }

    tl_mob = str(form.get("team_leader_number") or "").strip()
    if tl_mob or "team_leader" not in recipients:
        recipients["team_leader"] = {
            "mobile": tl_mob or (recipients.get("team_leader") or {}).get("mobile", ""),
            "name": form.get("team_leader_name") or (recipients.get("team_leader") or {}).get("name", ""),
            "language": form.get("team_leader_language") or (recipients.get("team_leader") or {}).get("language", default_lang),
        }

    mgr_mob = str(form.get("manager_number") or "").strip()
    if mgr_mob or "manager" not in recipients:
        recipients["manager"] = {
            "mobile": mgr_mob or (recipients.get("manager") or {}).get("mobile", ""),
            "name": form.get("manager_name") or (recipients.get("manager") or {}).get("name", ""),
            "language": form.get("manager_language") or (recipients.get("manager") or {}).get("language", default_lang),
        }

    return workflow.trigger_emergency(
        ticket_id=tid,
        alarm_id=alarm_id,
        assignment_id=aid,
        site_name=site,
        description=desc,
        severity=severity,
        recipients=recipients,
        callback_url=callback_url,
        ack_digit=ack_digit,
        reject_digit=reject_digit,
    )


def emergency_close_call(form):
    """POST /api/emergency_close, /api/emergency/close, or /api/emergency_closure
    Closes an emergency ticket and sends closure WhatsApp alert messages to Team Leader and Manager.
    """
    if not isinstance(form, dict):
        raise workflow.EventError("body must be a JSON object")

    ticket_data = form.get("ticket") if isinstance(form.get("ticket"), dict) else {}
    tid = str(ticket_data.get("ticket_id") or form.get("ticket_id") or form.get("incident_number") or "").strip().upper()
    if not tid:
        raise workflow.EventError("missing required field: ticket_id or incident_number")

    site = ticket_data.get("site_name") or form.get("site_details") or form.get("site_name")
    res = (form.get("resolution") or form.get("resolution_notes") or ticket_data.get("resolution")
           or form.get("close_notes") or form.get("fault_type") or form.get("short_description")
           or "Emergency issue resolved and service restored.")
    closed_at = form.get("closed_at") or form.get("closure_time") or store.now_iso()

    recipients = {}
    raw_recipients = form.get("recipients")
    if isinstance(raw_recipients, list):
        for r in raw_recipients:
            if isinstance(r, dict) and r.get("role") and r.get("mobile"):
                recipients[r["role"]] = {
                    "mobile": str(r.get("mobile")).strip(),
                    "name": r.get("name") or "",
                }
    elif isinstance(raw_recipients, dict):
        for role, r in raw_recipients.items():
            if isinstance(r, dict) and r.get("mobile"):
                recipients[role] = {
                    "mobile": str(r.get("mobile")).strip(),
                    "name": r.get("name") or "",
                }

    for role, key in (("team_leader", "team_leader_number"), ("manager", "manager_number"), ("technician", "technician_number")):
        mob = str(form.get(key) or form.get(f"{role}_mobile") or "").strip()
        if role == "technician" and not mob:
            mob = str(form.get("engineer_mobile") or form.get("phone_number") or "").strip()
        if mob:
            name = form.get(f"{role}_name")
            if role == "technician" and not name:
                name = form.get("engineer_name")
            recipients[role] = {
                "mobile": mob,
                "name": name or (recipients.get(role) or {}).get("name", ""),
            }

    return workflow.trigger_emergency_close(
        ticket_id=tid,
        resolution=res,
        closed_at=closed_at,
        recipients_override=recipients,
        site_name=site
    )


def legacy_view(tid):
    v = workflow.ticket_view(tid)
    if not v:
        return None
    state, reason = v["call_workflow"]["state"], (v["call_workflow"]["escalation"] or {}).get("reason")
    if state == "ACKNOWLEDGED":
        visit = "accepted"
    elif state in ("TL_REACHED", "MANAGER_REACHED", "ESCALATION_UNRESOLVED", "ESCALATION_NO_CONTACTS"):
        visit = "rejected" if reason == "rejected" else "no_response"
    elif state and state.startswith("CANCELLED"):
        visit = "cancelled"
    else:
        visit = "pending"
    return dict(v, visit_status=visit, final=v["call_workflow"]["final"])


# --------------------------------------------------------------------------- dashboard

def login_page_html(error_msg=None):
    err_html = f'<div class="error-banner">{error_msg}</div>' if error_msg else ''
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Login · NexTask Voice Platform</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #090d16;
      color: #f8fafc;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
      background-image: 
        radial-gradient(circle at 20% 20%, rgba(56, 189, 248, 0.08) 0%, transparent 40%),
        radial-gradient(circle at 80% 80%, rgba(168, 85, 247, 0.06) 0%, transparent 45%);
    }}
    .login-card {{
      background: rgba(16, 23, 38, 0.9);
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 16px;
      padding: 40px;
      width: 100%;
      max-width: 420px;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5);
      backdrop-filter: blur(12px);
    }}
    .logo-badge {{
      width: 48px;
      height: 48px;
      border-radius: 12px;
      background: linear-gradient(135deg, #38bdf8, #3b82f6);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 24px;
      margin: 0 auto 20px;
      box-shadow: 0 0 20px rgba(56, 189, 248, 0.4);
    }}
    h1 {{
      font-size: 22px;
      font-weight: 700;
      text-align: center;
      color: #fff;
      margin-bottom: 6px;
      letter-spacing: -0.02em;
    }}
    .subtitle {{
      font-size: 13px;
      color: #94a3b8;
      text-align: center;
      margin-bottom: 28px;
    }}
    .error-banner {{
      background: rgba(239, 68, 68, 0.15);
      border: 1px solid rgba(239, 68, 68, 0.3);
      color: #fca5a5;
      padding: 10px 14px;
      border-radius: 8px;
      font-size: 13px;
      margin-bottom: 20px;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .form-group {{
      margin-bottom: 20px;
    }}
    label {{
      display: block;
      font-size: 12.5px;
      font-weight: 600;
      color: #cbd5e1;
      margin-bottom: 8px;
    }}
    input {{
      width: 100%;
      padding: 12px 14px;
      background: #070a10;
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 8px;
      color: #fff;
      font-size: 14px;
      font-family: inherit;
      transition: all 0.2s ease;
    }}
    input:focus {{
      outline: none;
      border-color: #38bdf8;
      box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.2);
    }}
    .btn-login {{
      width: 100%;
      padding: 12px;
      background: linear-gradient(135deg, #38bdf8, #3b82f6);
      color: #fff;
      border: none;
      border-radius: 8px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      box-shadow: 0 4px 14px rgba(56, 189, 248, 0.3);
      transition: transform 0.15s ease, box-shadow 0.15s ease;
      margin-top: 8px;
    }}
    .btn-login:hover {{
      transform: translateY(-1px);
      box-shadow: 0 6px 20px rgba(56, 189, 248, 0.45);
    }}
    .footer-note {{
      text-align: center;
      margin-top: 24px;
      font-size: 12px;
      color: #64748b;
    }}
  </style>
</head>
<body>
  <div class="login-card">
    <div class="logo-badge">⚡</div>
    <h1>NexTask Voice Platform</h1>
    <p class="subtitle">Enter administrative credentials to access the portal</p>

    {err_html}

    <form method="POST" action="/login">
      <div class="form-group">
        <label for="username">Username</label>
        <input type="text" id="username" name="username" placeholder="admin" required autofocus>
      </div>

      <div class="form-group">
        <label for="password">Password</label>
        <input type="password" id="password" name="password" placeholder="••••••••" required>
      </div>

      <button type="submit" class="btn-login">Sign In &rarr;</button>
    </form>

    <div class="footer-note">
      AutomationEdge FLM Helpdesk · Secure Gateway
    </div>
  </div>
</body>
</html>"""



def safe_esc(val):
    if val is None:
        return ""
    return escape(str(val))


def safe_esc(val):
    if val is None:
        return ""
    return html.escape(str(val), quote=True)

def dashboard_html():
    tickets = queries.list_tickets({"limit": 1000})
    total_tickets = len(tickets)
    ack_count = 0
    assigned_count = 0
    reassigned_count = 0
    pending_count = 0
    escalated_count = 0
    emergency_count = 0
    critical_count = 0
    major_count = 0
    minor_count = 0
    unresolved_count = 0

    try:
        wa_row = store.q("SELECT COUNT(*) as cnt FROM whatsapp_messages", one=True)
        wa_total = wa_row["cnt"] if wa_row else 0
        wa_deliv_row = store.q("SELECT COUNT(*) as cnt FROM whatsapp_messages WHERE delivery_status IN ('delivered', 'read')", one=True)
        wa_delivered = wa_deliv_row["cnt"] if wa_deliv_row else 0
    except Exception:
        wa_total = 0
        wa_delivered = 0

    try:
        call_total_row = store.q("SELECT COUNT(*) as cnt FROM calls", one=True)
        calls_total = call_total_row["cnt"] if call_total_row else 0
        call_comp_row = store.q("SELECT COUNT(*) as cnt FROM calls WHERE status='completed'", one=True)
        calls_completed = call_comp_row["cnt"] if call_comp_row else 0
        call_fail_row = store.q("SELECT COUNT(*) as cnt FROM calls WHERE status IN ('failed', 'busy', 'no-answer') OR error_message IS NOT NULL", one=True)
        calls_failed = call_fail_row["cnt"] if call_fail_row else 0
    except Exception:
        calls_total = 0
        calls_completed = 0
        calls_failed = 0

    cards = ""
    for v in tickets:
        cw = v.get("call_workflow") or {}
        is_ack = v.get("acknowledged") or cw.get("acknowledged")
        state = (cw.get("state") or "").upper()
        task_status_raw = str(v.get("status") or "").strip()

        # Resolve technician assignment & reassignment details
        recips = v.get("recipients") or {}
        curr_tech = recips.get("technician") or {}
        curr_name = curr_tech.get("name") or "-"
        curr_mob = curr_tech.get("mobile") or "-"
        curr_lang = curr_tech.get("language") or "English"
        curr_aid = v.get("assignment_id") or "-"

        is_reassigned = (
            task_status_raw.lower() in ("reassigned", "assignment_reassigned")
            or state == "CANCELLED_REASSIGNED"
            or bool(recips.get("previous_technician"))
        )

        prev_tech = recips.get("previous_technician")
        prev_name, prev_mob, prev_lang, prev_aid = "-", "-", "-", "-"
        if is_reassigned and not prev_tech:
            tid_lookup = v.get("ticket_id")
            ev = store.q(
                "SELECT payload FROM events WHERE ticket_id=? AND event_type IN ('ALERT_INGESTED', 'ASSIGNMENT_CREATED', 'ASSIGNMENT_INITIAL') ORDER BY rowid ASC LIMIT 1",
                (tid_lookup,),
                one=True
            )
            if ev and ev.get("payload"):
                try:
                    p = json.loads(ev["payload"])
                    p_name = p.get("technician_name")
                    if p_name and p_name != curr_name:
                        prev_tech = {
                            "name": p_name,
                            "mobile": p.get("technician_mobile") or p.get("phone_number") or "-",
                            "language": p.get("technician_preferred_lang") or p.get("technician_language") or "English",
                            "assignment_id": f"{tid_lookup}-ASN1"
                        }
                except Exception:
                    pass

        if prev_tech:
            is_reassigned = True
            prev_name = prev_tech.get("name") or "-"
            prev_mob = prev_tech.get("mobile") or "-"
            prev_lang = prev_tech.get("language") or "English"
            prev_aid = prev_tech.get("assignment_id") or f"{v.get('ticket_id')}-ASN1"

        if is_ack:
            ack_count += 1
            filter_cat = "ack"
            cls = "status-ack"
            status_text = "ACKNOWLEDGED"
            is_unresolved = False
        elif is_reassigned:
            reassigned_count += 1
            filter_cat = "reassigned"
            cls = "status-reassigned"
            status_text = "REASSIGNED"
            is_unresolved = False
        elif "ESCALAT" in state or "MANAGER" in state or "LEADER" in state:
            escalated_count += 1
            unresolved_count += 1
            filter_cat = "escalated"
            cls = "status-escalated"
            status_text = "ESCALATED"
            is_unresolved = True
        elif "CALL" in state or "PENDING" in state or (v.get("calls") and not is_ack):
            pending_count += 1
            filter_cat = "pending"
            cls = "status-calling"
            status_text = "PENDING / DIALING"
            is_unresolved = False
        else:
            assigned_count += 1
            filter_cat = "assigned"
            cls = "status-assigned"
            status_text = "ASSIGNED"
            is_unresolved = False

        sev_raw = str(v.get("severity") or "").strip()
        sev_lower = sev_raw.lower()
        tid_str = str(v.get("ticket_id") or "").upper()
        desc_str = str(v.get("description") or "").lower()

        if sev_lower == "emergency" or tid_str.startswith("EMG") or "emergency" in desc_str:
            emergency_count += 1
            severity = "Emergency"
            sev_cls = "sev-emergency"
        elif sev_lower == "critical":
            critical_count += 1
            severity = "Critical"
            sev_cls = "sev-critical"
        elif sev_lower == "major":
            major_count += 1
            severity = "Major"
            sev_cls = "sev-major"
        else:
            minor_count += 1
            severity = "Minor" if sev_lower in ("minor", "normal", "low", "") else sev_raw.capitalize()
            sev_cls = "sev-minor"

        op_status = v.get("operational_status")
        op_badge = f'<span class="sub-badge op-badge">Movement: <b>{safe_esc(op_status)}</b></span>' if op_status else ""

        all_call_sids = []
        all_languages = []
        all_outcomes = []
        all_workflows = []
        calls_html = ""

        for c in v.get("calls", []):
            role_title = f"{safe_esc(c.get('role', 'Technician')).capitalize()} #{c.get('attempt_no', 1)}"
            c_status = safe_esc(c.get("status") or "unknown")
            c_lang = safe_esc(c.get("language") or "-")
            c_dtmf = safe_esc(str(c.get("dtmf") or "-"))
            c_resp = safe_esc(c.get("response") or "-")
            c_phone = safe_esc(c.get("to_number") or "")
            c_sid = safe_esc(c.get("call_sid") or "")
            c_outcome = safe_esc(c.get("outcome") or "")
            c_wf = safe_esc(c.get("workflow") or "")
            superseded = " <span class='badge-superseded'>superseded</span>" if c.get("superseded") else ""

            if c_sid:
                all_call_sids.append(c_sid)
            if c_lang and c_lang != "-":
                all_languages.append(c_lang.lower())
            if c_outcome:
                all_outcomes.append(c_outcome.lower())
            if c_wf:
                all_workflows.append(c_wf.lower())

            sid_pill = f"""<span class="corr-id" onclick="copyText('{c_sid}')" title="Click to copy Call SID">SID: {c_sid[:12]}...</span>""" if c_sid else ""

            calls_html += f"""
            <div class="call-row">
              <div class="call-role">
                <span class="role-badge">{role_title}</span>
                <span class="phone-number">{c_phone}</span>
                {sid_pill}
              </div>
              <div class="call-meta">
                <span class="meta-tag">Status: <b>{c_status}</b></span>
                <span class="meta-tag">Lang: <b>{c_lang}</b></span>
                <span class="meta-tag">Key: <b>{c_dtmf}</b></span>
                <span class="meta-tag response-tag">Resp: <b>{c_resp}</b></span>
                {superseded}
              </div>
            </div>"""

        wa_msgs = v.get("sms_messages") or v.get("whatsapp_messages") or []
        wa_html = ""
        all_wamids = []
        all_wa_statuses = []
        if wa_msgs:
            for w in wa_msgs:
                w_role = safe_esc(w.get("recipient_role", "Recipient")).capitalize()
                w_name = safe_esc(w.get("recipient_name") or "-")
                w_tpl = safe_esc(w.get("template_name") or "-")
                w_stat = safe_esc(w.get("delivery_status") or "sent")
                w_time = safe_esc(w.get("sent_at") or w.get("submitted_at") or "-")
                w_mid = safe_esc(w.get("provider_message_id") or "")

                if w_mid:
                    all_wamids.append(w_mid)
                if w_stat:
                    all_wa_statuses.append(w_stat.lower())

                wamid_pill = f"""<span class="corr-id corr-id-wa" onclick="copyText('{w_mid}')" title="Click to copy WhatsApp ID">WAMID: {w_mid[:14]}...</span>""" if w_mid else ""

                wa_html += f"""
                <div class="wa-row">
                  <div class="call-role">
                    <span class="wa-badge" style="background:#e0e7ff;color:#3730a3;">SMS: {w_role}</span>
                    <span class="phone-number">{safe_esc(w.get('mobile_number') or '')}</span>
                    {wamid_pill}
                  </div>
                  <div class="call-meta">
                    <span class="meta-tag">To: <b>{w_name}</b></span>
                    <span class="meta-tag">Template: <b>{w_tpl}</b></span>
                    <span class="meta-tag">Status: <b class="wa-status">{w_stat}</b></span>
                    <span class="meta-tag">Time: <b>{w_time}</b></span>
                  </div>
                </div>"""

        time_str = safe_esc(v.get("last_event_time") or v.get("created_at") or "-")

        recips = v.get("recipients") or {}
        recip_names = " ".join(r.get("name", "") for r in recips.values() if isinstance(r, dict))
        recip_phones = " ".join(r.get("mobile", "") for r in recips.values() if isinstance(r, dict))

        if is_reassigned:
            tech_html = f"""
            <div class="reassign-container">
              <div class="reassign-title-row">
                <div class="reassign-title">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="8.5" cy="7" r="4"></circle><polyline points="17 11 19 13 23 9"></polyline></svg>
                  Task Reassignment Details
                </div>
                <span class="status-pill status-reassigned"><span class="status-dot"></span> Reassigned</span>
              </div>
              <div class="reassign-cards-grid">
                <div class="tech-card-sub previous">
                  <div class="tech-sub-header">Previously Assigned To</div>
                  <div class="tech-sub-name">{safe_esc(prev_name)}</div>
                  <div class="tech-sub-meta">
                    <span class="meta-tag">Mobile: <b class="mono-val">{safe_esc(prev_mob)}</b></span>
                    <span class="meta-tag">Language: <b>{safe_esc(prev_lang)}</b></span>
                    <span class="meta-tag">Prev Assignment ID: <b class="mono-val">{safe_esc(prev_aid)}</b></span>
                  </div>
                </div>
                <div class="reassign-arrow-divider" title="Reassigned to new technician">
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="5" y1="12" x2="19" y2="12"></line><polyline points="12 5 19 12 12 19"></polyline></svg>
                </div>
                <div class="tech-card-sub current">
                  <div class="tech-sub-header">Reassigned To (Current Technician)</div>
                  <div class="tech-sub-name">{safe_esc(curr_name)}</div>
                  <div class="tech-sub-meta">
                    <span class="meta-tag">Mobile: <b class="mono-val">{safe_esc(curr_mob)}</b></span>
                    <span class="meta-tag">Language: <b>{safe_esc(curr_lang)}</b></span>
                    <span class="meta-tag">New Assignment ID: <b class="mono-val">{safe_esc(curr_aid)}</b></span>
                  </div>
                </div>
              </div>
            </div>"""
        else:
            tech_html = f"""
            <div class="tech-details-section">
              <div class="tech-sub-header" style="color:#475569;">Assigned Technician</div>
              <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
                <div class="tech-sub-name" style="margin-bottom:0;">{safe_esc(curr_name)}</div>
                <div class="tech-sub-meta">
                  <span class="meta-tag">Mobile: <b class="mono-val">{safe_esc(curr_mob)}</b></span>
                  <span class="meta-tag">Language: <b>{safe_esc(curr_lang)}</b></span>
                  <span class="meta-tag">Assignment ID: <b class="mono-val">{safe_esc(curr_aid)}</b></span>
                </div>
              </div>
            </div>"""

        search_blob = (
            f"{safe_esc(v.get('ticket_id')).lower()} "
            f"{safe_esc(v.get('alarm_id')).lower()} "
            f"{safe_esc(v.get('alert_id')).lower()} "
            f"{safe_esc(v.get('assignment_id')).lower()} "
            f"{safe_esc(v.get('last_event_id')).lower()} "
            f"{safe_esc(v.get('site_name')).lower()} "
            f"{safe_esc(v.get('site_number')).lower()} "
            f"{recip_names.lower()} {recip_phones} "
            f"{safe_esc(prev_name).lower()} {safe_esc(prev_mob)} "
            f"{' '.join(all_call_sids).lower()} "
            f"{' '.join(all_wamids).lower()} "
            f"{safe_esc(filter_cat)}"
        )

        lang_attr = " ".join(set(all_languages)) or "en"
        outcome_attr = " ".join(set(all_outcomes)) or "none"
        tid_str = str(v.get('ticket_id') or '').upper()
        sev_str = str(v.get('severity') or '').lower()
        desc_str = str(v.get('description') or '').lower()
        if tid_str.startswith('EMG') or sev_str == 'emergency' or 'emergency' in desc_str:
            all_workflows.append('emergency')
        elif not all_workflows:
            all_workflows.append('uc1_technician')

        created_raw = str(v.get('created_at') or v.get('last_event_time') or '')
        date_match = re.search(r'\d{4}[-/]\d{2}[-/]\d{2}', created_raw)
        date_iso = date_match.group(0).replace('/', '-') if date_match else ''
        wf_attr = " ".join(set(all_workflows))
        wa_status_attr = " ".join(set(all_wa_statuses)) or "none"

        task_details_obj = {
            "task_id": v.get("ticket_id"),
            "alarm_id": v.get("alarm_id") or v.get("alert_id") or "-",
            "site_number": v.get("site_number") or "-",
            "site_name": v.get("site_name") or "Main Site",
            "severity": severity,
            "task_status": v.get("status") or "Active",
            "alarm_status": v.get("alarm_status") or "Active",
            "operational_status": op_status or "-",
            "description": v.get("description") or "-",
            "is_reassigned": is_reassigned,
            "initial_assignment": {
                "name": (prev_name if is_reassigned else curr_name),
                "mobile": (prev_mob if is_reassigned else curr_mob),
                "language": (prev_lang if is_reassigned else curr_lang),
                "assignment_id": (prev_aid if is_reassigned else curr_aid)
            },
            "reassignment": {
                "name": curr_name,
                "mobile": curr_mob,
                "language": curr_lang,
                "assignment_id": curr_aid,
                "reassigned_at": time_str
            } if is_reassigned else None,
            "team_leader": {
                "name": (recips.get("team_leader") or {}).get("name") or "-",
                "mobile": (recips.get("team_leader") or {}).get("mobile") or "-",
                "language": (recips.get("team_leader") or {}).get("language") or "-"
            },
            "manager": {
                "name": (recips.get("manager") or {}).get("name") or "-",
                "mobile": (recips.get("manager") or {}).get("mobile") or "-",
                "language": (recips.get("manager") or {}).get("language") or "-"
            },
            "event_timestamp": time_str,
            "last_event_id": v.get("last_event_id") or "-"
        }
        task_json_str = html.escape(json.dumps(task_details_obj), quote=True)
        sev_rank = {"Emergency": 4, "Critical": 3, "Major": 2}.get(severity, 1)

        cards += f"""
        <div class="ticket-card"
             data-tid="{safe_esc(v.get('ticket_id'))}"
             data-severity="{safe_esc(severity.lower())}"
             data-sevrank="{sev_rank}"
             data-ts="{time_str}"
             data-category="{filter_cat}"
             data-date="{date_iso}"
             data-lang="{lang_attr}"
             data-outcome="{outcome_attr}"
             data-wf="{wf_attr}"
             data-ack="{'true' if is_ack else 'false'}"
             data-unresolved="{'true' if is_unresolved else 'false'}"
             data-wastatus="{wa_status_attr}"
             data-search="{search_blob}">
          <div class="card-header">
            <div class="ticket-primary">
              <span class="ticket-id"><span style="font-size:10px;font-weight:600;color:#64748b;text-transform:uppercase;margin-right:3px;">Task:</span>{safe_esc(v.get('ticket_id') or 'INC000')}</span>
              <span class="site-badge">Site {safe_esc(v.get('site_number') or '')} - {safe_esc(v.get('site_name') or 'Main Site')}</span>
              <span class="sev-badge {sev_cls}">{severity}</span>
              <span class="sub-badge">Alarm: <b>{safe_esc(v.get('alarm_id') or v.get('alert_id') or '-')}</b></span>
              <span class="sub-badge">Assign: <b>{safe_esc(v.get('assignment_id') or '-')}</b></span>
              {op_badge}
            </div>
            <div class="card-header-right">
              <div class="status-pill {cls}">
                <span class="status-dot"></span> {status_text}
              </div>
              <button type="button" class="btn-view-details" onclick="openTaskDetails(this)" data-task="{task_json_str}" title="View complete task assignment, reassignment, and technician details">
                View Details
              </button>
              <button type="button" class="btn-inspect" onclick="openTimeline('{safe_esc(v.get('ticket_id'))}')" title="Inspect complete chronological communication timeline">
                Inspect Timeline
              </button>
            </div>
          </div>
          <div class="card-body">
            {tech_html}
            <div class="card-summary">
              <div><span class="summary-label">Summary:</span> {safe_esc(cw.get('response_summary') or 'No call response yet.')}</div>
              <div class="followup-info">Follow-up: <b>{safe_esc(v.get('followup', {}).get('state') or 'None')}</b></div>
            </div>
            <div class="calls-timeline">
              {calls_html or '<div class="no-calls">No voice call attempts recorded.</div>'}
            </div>
            {f'<div class="wa-container">{wa_html}</div>' if wa_html else ''}
          </div>
          <div class="card-footer">
            <span>Event Timestamp: <b>{time_str}</b></span>
            <span>Last Event ID: <b>{safe_esc(v.get('last_event_id') or '-')}</b></span>
            <span>Task Status: <b>{safe_esc(v.get('status') or 'Active')}</b></span>
          </div>
        </div>"""

    ack_rate = f"{int((ack_count / total_tickets) * 100)}%" if total_tickets > 0 else "0%"
    mode = "DRY RUN (Simulator)" if CFG["dry_run"] else f"LIVE GATEWAY - {safe_esc(CFG.get('base_url') or 'Connected')}"
    dash_stats_json = json.dumps({
        "status": [assigned_count, reassigned_count, pending_count, escalated_count, ack_count],
        "severity": [minor_count, major_count, critical_count, emergency_count],
        "activity": [calls_total, calls_completed, wa_total],
    })

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>NexTask Voice Platform - Operations & Audit Center</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {{
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --border: #e2e8f0;
      --text: #0f172a;
      --text-muted: #64748b;
      --primary: #0969da;
      --primary-hover: #0856b3;
      --sidebar-bg: #0f172a;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }}

    /* Global Header */
    header {{
      background: #0f172a;
      color: #fff;
      padding: 12px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      box-shadow: 0 1px 3px rgba(0,0,0,0.12);
      position: sticky;
      top: 0;
      z-index: 100;
    }}
    .brand-group {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .brand-icon {{
      width: 32px;
      height: 32px;
      background: linear-space;
      background: #0969da;
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #fff;
    }}
    .brand-title {{
      font-size: 15px;
      font-weight: 700;
      letter-spacing: -0.01em;
      color: #f8fafc;
    }}
    .brand-mode {{
      font-size: 11px;
      color: #94a3b8;
      font-family: 'JetBrains Mono', monospace;
    }}
    .header-actions {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .btn-header {{
      background: rgba(255,255,255,0.08);
      color: #e2e8f0;
      border: 1px solid rgba(255,255,255,0.15);
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 500;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: background 0.15s ease, border-color 0.15s ease;
    }}
    .btn-header:hover {{
      background: rgba(255,255,255,0.15);
      color: #ffffff;
      border-color: rgba(255,255,255,0.3);
    }}

    /* Main Container */
    main {{
      max-width: 1360px;
      width: 100%;
      margin: 0 auto;
      padding: 24px 28px 60px 28px;
      flex: 1;
    }}

    /* Metrics Grid */
    .metrics-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 14px;
      margin-bottom: 24px;
    }}
    .metric-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px 16px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.03);
      transition: transform 0.15s ease, box-shadow 0.15s ease;
      cursor: pointer;
    }}
    .metric-card:hover {{
      transform: translateY(-2px);
      box-shadow: 0 4px 12px rgba(0,0,0,0.06);
    }}
    .metric-label {{
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--text-muted);
      margin-bottom: 6px;
    }}
    .metric-value-row {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
    }}
    .metric-number {{
      font-size: 22px;
      font-weight: 800;
      color: var(--text);
      font-feature-settings: "tnum";
    }}
    .metric-badge {{
      font-size: 11px;
      padding: 2px 7px;
      border-radius: 12px;
      font-weight: 600;
    }}
    .badge-blue {{ background: #eff6ff; color: #1d4ed8; }}
    .badge-cyan {{ background: #ecfeff; color: #0e7490; }}
    .badge-amber {{ background: #fffbeb; color: #b45309; }}
    .badge-red {{ background: #fef2f2; color: #b91c1c; }}
    .badge-green {{ background: #f0fdf4; color: #15803d; }}
    .badge-purple {{ background: #faf5ff; color: #7e22ce; }}
    .badge-slate {{ background: #f8fafc; color: #475569; }}

    /* Analytics Charts Grid */
    .analytics-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 16px;
      margin-bottom: 24px;
    }}
    @media (max-width: 1024px) {{
      .analytics-grid {{ grid-template-columns: 1fr; }}
    }}
    .chart-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px 18px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.03);
    }}
    .chart-header {{
      margin-bottom: 12px;
      border-bottom: 1px solid #f1f5f9;
      padding-bottom: 8px;
    }}
    .chart-title {{
      font-size: 13px;
      font-weight: 700;
      color: #1e293b;
    }}
    .chart-subtitle {{
      font-size: 11px;
      color: #64748b;
      margin-top: 2px;
    }}
    .chart-container {{
      position: relative;
      height: 200px;
      width: 100%;
    }}

    /* Filter Console */
    .filter-console {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px 20px;
      margin-bottom: 20px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.03);
    }}
    .filter-console-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .console-title {{
      font-size: 13px;
      font-weight: 700;
      color: #1e293b;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .console-actions {{
      display: flex;
      gap: 8px;
      align-items: center;
    }}
    .btn-reset {{
      background: #f1f5f9;
      color: #475569;
      border: 1px solid #cbd5e1;
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 11.5px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s ease;
    }}
    .btn-reset:hover {{
      background: #e2e8f0;
    }}
    .search-row {{
      display: flex;
      gap: 10px;
      align-items: center;
      margin-bottom: 14px;
    }}
    .search-box-wrap {{
      position: relative;
      flex: 1;
    }}
    .search-input-main {{
      width: 100%;
      box-sizing: border-box;
      padding: 10px 14px 10px 38px;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      font-size: 13px;
      font-family: inherit;
      background: #f8fafc;
      transition: border-color 0.15s ease, background 0.15s ease;
    }}
    .search-input-main:focus {{
      outline: none;
      background: #fff;
      border-color: #0969da;
      box-shadow: 0 0 0 3px rgba(9, 105, 218, 0.1);
    }}
    .btn-search-main {{
      background: #0969da;
      color: #ffffff;
      border: 1px solid #0969da;
      padding: 0 22px;
      height: 40px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s ease;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      box-sizing: border-box;
      flex-shrink: 0;
    }}
    .btn-search-main:hover {{
      background: #0856b3;
      border-color: #0856b3;
    }}
    .btn-apply-filters {{
      background: #0969da;
      color: #ffffff;
      border: 1px solid #0969da;
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 11.5px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s ease;
    }}
    .btn-apply-filters:hover {{
      background: #0856b3;
      border-color: #0856b3;
    }}
    .search-icon-main {{
      position: absolute;
      left: 12px;
      top: 50%;
      transform: translateY(-50%);
      color: #94a3b8;
      display: flex;
      align-items: center;
    }}
    .filter-controls-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }}
    .filter-field {{
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .filter-field label {{
      font-size: 11px;
      font-weight: 600;
      color: #64748b;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }}
    .filter-field select, .filter-field input {{
      padding: 7px 10px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      font-size: 12px;
      font-family: inherit;
      background: #fff;
    }}
    .filter-field select:focus, .filter-field input:focus {{
      outline: none;
      border-color: #0969da;
    }}
    .filter-pills-row {{
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      border-top: 1px solid #f1f5f9;
      padding-top: 12px;
    }}
    .pill-label {{
      font-size: 11.5px;
      font-weight: 600;
      color: #64748b;
      margin-right: 4px;
    }}
    .filter-btn {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      padding: 5px 12px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      color: #475569;
      transition: all 0.15s ease;
    }}
    .filter-btn:hover {{
      background: #f1f5f9;
      border-color: #cbd5e1;
    }}
    .filter-btn.active {{
      background: #0969da;
      border-color: #0969da;
      color: #ffffff;
      font-weight: 600;
    }}

    .filter-feedback-banner {{
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      color: #1e40af;
      padding: 9px 14px;
      border-radius: 8px;
      font-size: 12.5px;
      margin-bottom: 14px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}

    /* Ticket Cards */
    .ticket-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      margin-bottom: 12px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.03);
      transition: box-shadow 0.15s ease, border-color 0.15s ease;
    }}
    .ticket-card:hover {{
      box-shadow: 0 4px 12px rgba(0,0,0,0.06);
      border-color: #cbd5e1;
    }}
    .card-header {{
      padding: 12px 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      border-bottom: 1px solid #f1f5f9;
    }}
    .ticket-primary {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .ticket-id {{
      font-size: 14px;
      font-weight: 700;
      font-family: 'JetBrains Mono', monospace;
      color: #0f172a;
    }}
    .site-badge {{
      background: #f1f5f9;
      color: #334155;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 11.5px;
      font-weight: 500;
    }}
    .sev-badge {{
      font-size: 11px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 4px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .sev-emergency {{ background: #fdf2f8; color: #991b1b; border: 1px solid #fecdd3; }}
    .sev-critical {{ background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }}
    .sev-major {{ background: #fffbeb; color: #d97706; border: 1px solid #fde68a; }}
    .sev-minor {{ background: #eff6ff; color: #2563eb; border: 1px solid #bfdbfe; }}
    .sev-normal {{ background: #eff6ff; color: #2563eb; border: 1px solid #bfdbfe; }}
    .sub-badge {{
      font-size: 11px;
      color: #64748b;
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      padding: 2px 6px;
      border-radius: 4px;
    }}
    .sub-badge b {{ color: #1e293b; }}
    .op-badge {{ background: #f0fdfa; border-color: #ccfbf1; color: #0d9488; }}
    .op-badge b {{ color: #0f766e; }}

    .card-header-right {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .status-pill {{
      font-size: 11.5px;
      font-weight: 600;
      padding: 3px 10px;
      border-radius: 20px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    .status-dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
    }}
    .status-ack {{ background: #f0fdf4; color: #166534; }}
    .status-ack .status-dot {{ background: #16a34a; }}
    .status-calling {{ background: #eff6ff; color: #1e40af; }}
    .status-calling .status-dot {{ background: #2563eb; }}
    .status-escalated {{ background: #fef2f2; color: #991b1b; }}
    .status-escalated .status-dot {{ background: #dc2626; }}
    .status-assigned {{ background: #f8fafc; color: #475569; }}
    .status-assigned .status-dot {{ background: #94a3b8; }}
    .status-reassigned {{ background: #faf5ff; color: #7e22ce; border: 1px solid #e9d5ff; }}
    .status-reassigned .status-dot {{ background: #a855f7; }}
    .mono-val {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; }}

    .tech-details-section {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      padding: 10px 14px;
      margin-bottom: 12px;
    }}
    .reassign-container {{
      background: #faf5ff;
      border: 1px solid #e9d5ff;
      border-radius: 8px;
      padding: 12px 14px;
      margin-bottom: 12px;
    }}
    .reassign-title-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
    }}
    .reassign-title {{
      font-size: 11.5px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: #7e22ce;
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .reassign-cards-grid {{
      display: grid;
      grid-template-columns: 1fr auto 1fr;
      align-items: center;
      gap: 12px;
    }}
    @media (max-width: 768px) {{
      .reassign-cards-grid {{
        grid-template-columns: 1fr;
      }}
    }}
    .tech-card-sub {{
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      padding: 10px 12px;
    }}
    .tech-card-sub.current {{
      border-color: #c084fc;
      background: #ffffff;
      box-shadow: 0 1px 3px rgba(168,85,247,0.08);
    }}
    .tech-card-sub.previous {{
      border-color: #cbd5e1;
      background: #f8fafc;
    }}
    .tech-sub-header {{
      font-size: 10.5px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.4px;
      margin-bottom: 4px;
    }}
    .tech-card-sub.current .tech-sub-header {{
      color: #9333ea;
    }}
    .tech-card-sub.previous .tech-sub-header {{
      color: #64748b;
    }}
    .tech-sub-name {{
      font-size: 13.5px;
      font-weight: 600;
      color: #0f172a;
      margin-bottom: 6px;
    }}
    .tech-sub-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px 12px;
      font-size: 11.5px;
    }}
    .reassign-arrow-divider {{
      display: flex;
      align-items: center;
      justify-content: center;
      color: #a855f7;
      padding: 4px;
    }}

    .btn-view-details {{
      background: #2563eb;
      color: #ffffff;
      border: 1px solid #1d4ed8;
      padding: 4px 11px;
      border-radius: 6px;
      font-size: 11.5px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      transition: all 0.15s ease;
    }}
    .btn-view-details:hover {{
      background: #1d4ed8;
      box-shadow: 0 1px 4px rgba(37,99,235,0.3);
    }}
    .btn-inspect {{
      background: #ffffff;
      color: #0969da;
      border: 1px solid #0969da;
      padding: 4px 10px;
      border-radius: 6px;
      font-size: 11.5px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s ease, color 0.15s ease;
    }}
    .btn-inspect:hover {{
      background: #0969da;
      color: #ffffff;
    }}

    .card-body {{
      padding: 14px 18px;
    }}
    .card-summary {{
      font-size: 13px;
      color: #334155;
      margin-bottom: 12px;
      display: flex;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .summary-label {{
      font-weight: 600;
      color: #1e293b;
    }}
    .followup-info {{
      font-size: 12px;
      color: #64748b;
    }}

    .call-row, .wa-row {{
      background: #f8fafc;
      border: 1px solid #f1f5f9;
      border-radius: 6px;
      padding: 8px 12px;
      margin-bottom: 6px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      font-size: 12px;
    }}
    .call-role {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .role-badge {{
      font-weight: 600;
      color: #1e293b;
    }}
    .phone-number {{
      font-family: 'JetBrains Mono', monospace;
      color: #64748b;
      font-size: 11.5px;
    }}
    .wa-badge {{
      background: #dcfce7;
      color: #15803d;
      font-weight: 600;
      padding: 1px 6px;
      border-radius: 4px;
      font-size: 11px;
    }}
    .corr-id {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 10.5px;
      color: #0969da;
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      padding: 1px 6px;
      border-radius: 4px;
      cursor: pointer;
    }}
    .corr-id:hover {{
      background: #dbeafe;
    }}
    .corr-id-wa {{
      color: #047857;
      background: #ecfdf5;
      border-color: #a7f3d0;
    }}
    .call-meta {{
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .meta-tag {{
      color: #64748b;
    }}
    .meta-tag b {{
      color: #1e293b;
    }}
    .wa-status {{
      color: #059669;
    }}
    .badge-superseded {{
      background: #f1f5f9;
      color: #94a3b8;
      padding: 1px 5px;
      border-radius: 4px;
      font-size: 10px;
    }}
    .no-calls {{
      font-size: 12.5px;
      color: #94a3b8;
      font-style: italic;
      padding: 4px 0;
    }}

    .card-footer {{
      padding: 10px 18px;
      background: #f8fafc;
      border-top: 1px solid #f1f5f9;
      border-bottom-left-radius: 10px;
      border-bottom-right-radius: 10px;
      display: flex;
      justify-content: space-between;
      font-size: 11px;
      color: #64748b;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .card-footer b {{
      color: #334155;
    }}

    /* Pagination Bar */
    .pagination-bar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 14px 4px;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .pagination-info {{
      font-size: 12.5px;
      color: #64748b;
    }}
    .pagination-controls {{
      display: flex;
      gap: 4px;
      align-items: center;
    }}
    .page-btn {{
      background: #ffffff;
      border: 1px solid #cbd5e1;
      color: #334155;
      padding: 5px 11px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.15s ease, border-color 0.15s ease;
    }}
    .page-btn:hover:not(:disabled) {{
      background: #f1f5f9;
      border-color: #94a3b8;
    }}
    .page-btn.active {{
      background: #0969da;
      border-color: #0969da;
      color: #ffffff;
      font-weight: 700;
    }}
    .page-btn:disabled {{
      opacity: 0.4;
      cursor: not-allowed;
    }}
    .page-ellipsis {{
      padding: 0 4px;
      color: #94a3b8;
    }}

    /* Modal Backdrop & Timeline Dialog */
    .modal-backdrop {{
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.65);
      backdrop-filter: blur(4px);
      z-index: 1000;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }}
    .modal-card {{
      background: #ffffff;
      border-radius: 14px;
      max-width: 720px;
      width: 100%;
      max-height: 85vh;
      display: flex;
      flex-direction: column;
      box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
      border: 1px solid #e2e8f0;
      overflow: hidden;
    }}
    .modal-header {{
      padding: 16px 20px;
      border-bottom: 1px solid #e2e8f0;
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #f8fafc;
    }}
    .modal-title {{
      font-size: 15px;
      font-weight: 700;
      color: #0f172a;
    }}
    .btn-modal-close {{
      background: transparent;
      border: none;
      font-size: 20px;
      line-height: 1;
      color: #64748b;
      cursor: pointer;
      padding: 4px;
    }}
    .btn-modal-close:hover {{
      color: #0f172a;
    }}
    .modal-body {{
      padding: 20px;
      overflow-y: auto;
      flex: 1;
    }}

    .timeline-stream {{
      position: relative;
      padding-left: 28px;
    }}
    .timeline-stream::before {{
      content: "";
      position: absolute;
      left: 8px;
      top: 6px;
      bottom: 6px;
      width: 2px;
      background: #e2e8f0;
    }}
    .timeline-item {{
      position: relative;
      margin-bottom: 18px;
    }}
    .timeline-item:last-child {{
      margin-bottom: 0;
    }}
    .timeline-node {{
      position: absolute;
      left: -28px;
      top: 3px;
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: #ffffff;
      border: 3px solid #0969da;
    }}
    .timeline-node.node-voice {{ border-color: #2563eb; }}
    .timeline-node.node-whatsapp {{ border-color: #059669; }}
    .timeline-node.node-escalation {{ border-color: #dc2626; }}
    .timeline-node.node-ack {{ border-color: #10b981; }}
    .timeline-content {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      padding: 10px 14px;
    }}
    .timeline-header-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 4px;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .timeline-cat {{
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: #1e293b;
    }}
    .timeline-time {{
      font-size: 11px;
      font-family: 'JetBrains Mono', monospace;
      color: #64748b;
    }}
    .timeline-desc {{
      font-size: 12.5px;
      color: #334155;
      line-height: 1.45;
    }}
    .timeline-corr {{
      margin-top: 6px;
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }}

    .empty-state {{
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 40px 20px;
      text-align: center;
      color: #64748b;
      font-size: 13.5px;
      display: none;
    }}

    /* Live Refresh Bar */
    .live-bar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 16px;
    }}
    .live-status {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: #64748b;
    }}
    .live-status b {{ color: #1e293b; font-family: 'JetBrains Mono', monospace; font-weight: 600; }}
    .live-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #94a3b8;
    }}
    .live-dot.on {{
      background: #16a34a;
      box-shadow: 0 0 0 0 rgba(22, 163, 74, 0.5);
      animation: livePulse 2s infinite;
    }}
    .live-dot.err {{ background: #dc2626; }}
    @keyframes livePulse {{
      0% {{ box-shadow: 0 0 0 0 rgba(22, 163, 74, 0.45); }}
      70% {{ box-shadow: 0 0 0 7px rgba(22, 163, 74, 0); }}
      100% {{ box-shadow: 0 0 0 0 rgba(22, 163, 74, 0); }}
    }}
    .live-controls {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .ctl-select {{
      padding: 6px 8px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      font-size: 12px;
      font-family: inherit;
      background: #fff;
      color: #334155;
    }}
    .ctl-select:focus {{ outline: none; border-color: #0969da; }}
    .ctl-btn {{
      background: #ffffff;
      color: #334155;
      border: 1px solid #cbd5e1;
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease;
    }}
    .ctl-btn:hover {{ background: #f1f5f9; border-color: #94a3b8; }}
    .ctl-btn.on {{ background: #0969da; border-color: #0969da; color: #ffffff; }}
    .ctl-btn:disabled {{ opacity: 0.55; cursor: wait; }}
    .ctl-btn.spinning svg {{ animation: spin 0.8s linear infinite; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

    /* Results Toolbar */
    .results-toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 12px;
    }}
    .results-count {{ font-size: 12.5px; color: #64748b; }}
    .results-count b {{ color: #0f172a; }}
    .results-controls {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .ctl-label {{
      font-size: 11px;
      font-weight: 600;
      color: #64748b;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }}

    /* Interactive states */
    .metric-card.active {{
      border-color: #0969da;
      box-shadow: 0 0 0 3px rgba(9, 105, 218, 0.12);
    }}
    .metric-card.bump .metric-number {{ animation: bump 0.6s ease; }}
    @keyframes bump {{
      0% {{ transform: scale(1); }}
      40% {{ transform: scale(1.18); color: #0969da; }}
      100% {{ transform: scale(1); }}
    }}
    .chart-card canvas {{ cursor: pointer; }}
    .chart-hint {{ font-size: 10.5px; color: #94a3b8; margin-top: 6px; text-align: right; }}
    .ticket-card.is-new {{ animation: newFlash 2.4s ease; }}
    @keyframes newFlash {{
      0%, 40% {{ border-color: #16a34a; box-shadow: 0 0 0 3px rgba(22, 163, 74, 0.18); }}
      100% {{ border-color: var(--border); box-shadow: 0 1px 3px rgba(0,0,0,0.03); }}
    }}
    body.compact .ticket-card .card-body,
    body.compact .ticket-card .card-footer {{ display: none; }}
    body.compact .ticket-card.expanded .card-body {{ display: block; }}
    body.compact .ticket-card.expanded .card-footer {{ display: flex; }}
    body.compact .ticket-card .card-header {{ cursor: pointer; border-bottom: none; }}
    body.compact .ticket-card.expanded .card-header {{ border-bottom: 1px solid #f1f5f9; }}
    .kbd {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 10.5px;
      background: #f1f5f9;
      border: 1px solid #cbd5e1;
      border-bottom-width: 2px;
      border-radius: 4px;
      padding: 0 5px;
      color: #475569;
    }}

    /* Toasts */
    .toast-stack {{
      position: fixed;
      right: 20px;
      bottom: 20px;
      z-index: 2000;
      display: flex;
      flex-direction: column;
      gap: 8px;
      pointer-events: none;
    }}
    .toast {{
      background: #0f172a;
      color: #f8fafc;
      padding: 10px 14px;
      border-radius: 8px;
      font-size: 12.5px;
      box-shadow: 0 10px 20px rgba(0,0,0,0.18);
      max-width: 360px;
      opacity: 0;
      transform: translateY(8px);
      transition: opacity 0.2s ease, transform 0.2s ease;
      border-left: 3px solid #38bdf8;
      word-break: break-word;
    }}
    .toast.show {{ opacity: 1; transform: translateY(0); }}
    .toast.ok {{ border-left-color: #16a34a; }}
    .toast.err {{ border-left-color: #dc2626; }}

    @media (max-width: 640px) {{
      main {{ padding: 16px 16px 48px 16px; }}
      header {{ padding: 12px 16px; }}
      .search-row {{ flex-wrap: wrap; }}
      .btn-search-main {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="brand-group">
      <div class="brand-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"></path></svg>
      </div>
      <div>
        <div class="brand-title">NexTask Voice Operations & Audit Center</div>
      </div>
    </div>
    <div class="header-actions">
      <a href="/logout" class="btn-header" title="Log out from administrative session">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path><polyline points="16 17 21 12 16 7"></polyline><line x1="21" y1="12" x2="9" y2="12"></line></svg>
        Logout
      </a>
    </div>
  </header>

  <main>
    <!-- Live Refresh Bar -->
    <div class="live-bar">
      <div class="live-status" title="Dashboard data refreshes in place without reloading the page">
        <span id="liveDot" class="live-dot"></span>
        <span id="liveText">Auto-refresh off</span>
        <span>&middot; Updated <b id="lastUpdated">--:--:--</b></span>
      </div>
      <div class="live-controls">
        <span class="ctl-label">Press <span class="kbd">/</span> to search</span>
        <button type="button" id="autoRefreshBtn" class="ctl-btn" onclick="toggleAutoRefresh()" title="Toggle automatic live refresh">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
          <span id="autoRefreshLabel">Auto-Refresh</span>
        </button>
        <select id="refreshInterval" class="ctl-select" onchange="setRefreshInterval(this.value)" title="Refresh interval">
          <option value="10">Every 10s</option>
          <option value="30">Every 30s</option>
          <option value="60">Every 1 min</option>
          <option value="300">Every 5 min</option>
        </select>
        <button type="button" id="refreshNowBtn" class="ctl-btn" onclick="refreshDashboard(true)" title="Refresh dashboard data now">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="23 4 23 10 17 10"></polyline><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg>
          Refresh
        </button>
      </div>
    </div>

    <!-- Executive Audit Counters Grid -->
    <div class="metrics-grid">
      <div class="metric-card" data-cat="all" onclick="quickFilter('all')" title="Total tasks recorded in queue">
        <div class="metric-label">Total Tasks</div>
        <div class="metric-value-row">
          <div class="metric-number">{total_tickets}</div>
          <span class="metric-badge badge-blue">All</span>
        </div>
      </div>

      <div class="metric-card" data-cat="assigned" onclick="quickFilter('assigned')" title="Newly assigned tasks">
        <div class="metric-label">Assigned Tasks</div>
        <div class="metric-value-row">
          <div class="metric-number">{assigned_count}</div>
          <span class="metric-badge badge-cyan">New</span>
        </div>
      </div>

      <div class="metric-card" data-cat="reassigned" onclick="quickFilter('reassigned')" title="Tasks reassigned to a new technician">
        <div class="metric-label">Reassigned Tasks</div>
        <div class="metric-value-row">
          <div class="metric-number">{reassigned_count}</div>
          <span class="metric-badge badge-purple">Reassigned</span>
        </div>
      </div>

      <div class="metric-card" data-cat="pending" onclick="quickFilter('pending')" title="Active dialing or awaiting response">
        <div class="metric-label">Pending / In-Call</div>
        <div class="metric-value-row">
          <div class="metric-number">{pending_count}</div>
          <span class="metric-badge badge-amber">Dialing</span>
        </div>
      </div>

      <div class="metric-card" data-cat="escalated" onclick="quickFilter('escalated')" title="Escalated tasks to Team Leader or Manager">
        <div class="metric-label">Escalated Tasks</div>
        <div class="metric-value-row">
          <div class="metric-number">{escalated_count}</div>
          <span class="metric-badge badge-red">Tier 1 & 2</span>
        </div>
      </div>

      <div class="metric-card" data-cat="ack" onclick="quickFilter('ack')" title="Calls accepted and acknowledged">
        <div class="metric-label">Acknowledged</div>
        <div class="metric-value-row">
          <div class="metric-number">{ack_count}</div>
          <span class="metric-badge badge-green">{ack_rate} Rate</span>
        </div>
      </div>

      <div class="metric-card" data-cat="unresolved" onclick="quickFilter('unresolved')" title="Escalated tasks currently pending resolution">
        <div class="metric-label">Unresolved Escalations</div>
        <div class="metric-value-row">
          <div class="metric-number">{unresolved_count}</div>
          <span class="metric-badge badge-red">Open Attention</span>
        </div>
      </div>

      <div class="metric-card" onclick="filterBySms('delivered')" title="WhatsApp Template Alerts Sent / Delivered (click to show delivered SMS tasks)">
        <div class="metric-label">SMS Dispatched</div>
        <div class="metric-value-row">
          <div class="metric-number">{wa_total}</div>
          <span class="metric-badge badge-purple">{wa_delivered} Deliv</span>
        </div>
      </div>

      <div class="metric-card" title="Technical Failures or Call Exceptions">
        <div class="metric-label">Technical Exceptions</div>
        <div class="metric-value-row">
          <div class="metric-number">{calls_failed}</div>
          <span class="metric-badge badge-slate">Failed Calls</span>
        </div>
      </div>
    </div>

    <!-- Live Analytics Graphs Grid -->
    <div class="analytics-grid">
      <div class="chart-card">
        <div class="chart-header">
          <div class="chart-title">Task Status Distribution</div>
          <div class="chart-subtitle">Real-time status breakdown across all active helpdesk tasks</div>
        </div>
        <div class="chart-container">
          <canvas id="statusChart"></canvas>
        </div>
        <div class="chart-hint">Click a segment to filter tasks by status</div>
      </div>

      <div class="chart-card">
        <div class="chart-header">
          <div class="chart-title">Incident Severity Breakdown</div>
          <div class="chart-subtitle">Risk classification distribution across operational sites</div>
        </div>
        <div class="chart-container">
          <canvas id="severityChart"></canvas>
        </div>
        <div class="chart-hint">Click a bar to filter tasks by severity</div>
      </div>

      <div class="chart-card">
        <div class="chart-header">
          <div class="chart-title">Voice & SMS Execution</div>
          <div class="chart-subtitle">Automated IVR call volume vs Twilio SMS notifications</div>
        </div>
        <div class="chart-container">
          <canvas id="activityChart"></canvas>
        </div>
      </div>
    </div>

    <!-- Multi-Criteria Audit Search & Filter Console -->
    <div class="filter-console" id="ticketsSection">
      <div class="filter-console-header">
        <div class="console-title">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"></polygon></svg>
          Multi-Correlation Audit Search & Filters
        </div>
        <div class="console-actions">
          <button type="button" class="btn-apply-filters" onclick="runFilter()">Apply Filters</button>
          <button type="button" class="btn-reset" onclick="resetAllFilters()">Reset Filters</button>
        </div>
      </div>

      <!-- Primary Correlation Search Box -->
      <div class="search-row">
        <div class="search-box-wrap">
          <span class="search-icon-main">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
          </span>
          <input type="text" id="searchInput" class="search-input-main" 
                 placeholder="Search by Task ID, Alarm ID, Assignment ID, Technician Name, Mobile, Call SID, or WAMID..." 
                 oninput="runFilter()" onkeydown="if(event.key==='Enter') runFilter()">
        </div>
        <button type="button" class="btn-search-main" onclick="runFilter()">Search</button>
      </div>

      <!-- Multi-Criteria Dropdowns Grid -->
      <div class="filter-controls-grid">
        <div class="filter-field">
          <label>From Date</label>
          <input type="date" id="filterFromDate" onchange="runFilter()">
        </div>
        <div class="filter-field">
          <label>To Date</label>
          <input type="date" id="filterToDate" onchange="runFilter()">
        </div>
        <div class="filter-field">
          <label>Severity</label>
          <select id="filterSeverity" onchange="runFilter()">
            <option value="all">All Severities</option>
            <option value="emergency">Emergency</option>
            <option value="critical">Critical</option>
            <option value="major">Major</option>
            <option value="minor">Minor</option>
          </select>
        </div>
        <div class="filter-field">
          <label>Workflow</label>
          <select id="filterWorkflow" onchange="runFilter()">
            <option value="all">All Workflows</option>
            <option value="uc1_technician">Technician Initial</option>
            <option value="uc1_escalation">Supervisor Escalation</option>
            <option value="emergency">Emergency Alert</option>
          </select>
        </div>
        <div class="filter-field">
          <label>Language</label>
          <select id="filterLanguage" onchange="runFilter()">
            <option value="all">All Languages</option>
            <option value="en">English (en)</option>
            <option value="ar">Arabic (ar)</option>
            <option value="ur">Urdu (ur)</option>
            <option value="hi">Hindi (hi)</option>
          </select>
        </div>
        <div class="filter-field">
          <label>Call Outcome</label>
          <select id="filterOutcome" onchange="runFilter()">
            <option value="all">All Outcomes</option>
            <option value="acknowledged">Acknowledged</option>
            <option value="rejected">Rejected</option>
            <option value="no_input">No Input / Timeout</option>
            <option value="reached">Reached / Answered</option>
            <option value="reassign_yes">Reassign Yes</option>
            <option value="reassign_no">Reassign No</option>
          </select>
        </div>
        <div class="filter-field">
          <label>Acknowledgement</label>
          <select id="filterAck" onchange="runFilter()">
            <option value="all">All States</option>
            <option value="true">Acknowledged Only</option>
            <option value="false">Unacknowledged Only</option>
          </select>
        </div>
        <div class="filter-field">
          <label>SMS Status</label>
          <select id="filterWaStatus" onchange="runFilter()">
            <option value="all">All Message Statuses</option>
            <option value="delivered">Delivered</option>
            <option value="read">Read</option>
            <option value="sent">Sent</option>
            <option value="failed">Failed</option>
          </select>
        </div>
      </div>

      <!-- Quick Filter Pills -->
      <div class="filter-pills-row">
        <span class="pill-label">Quick Views:</span>
        <button type="button" id="tab-btn-all" class="filter-btn active" onclick="switchTab('all')">All Tasks ({total_tickets})</button>
        <button type="button" id="tab-btn-assigned" class="filter-btn" onclick="switchTab('assigned')">Assigned ({assigned_count})</button>
        <button type="button" id="tab-btn-reassigned" class="filter-btn" onclick="switchTab('reassigned')">Reassigned ({reassigned_count})</button>
        <button type="button" id="tab-btn-pending" class="filter-btn" onclick="switchTab('pending')">Pending / In-Call ({pending_count})</button>
        <button type="button" id="tab-btn-escalated" class="filter-btn" onclick="switchTab('escalated')">Escalated ({escalated_count})</button>
        <button type="button" id="tab-btn-ack" class="filter-btn" onclick="switchTab('ack')">Acknowledged ({ack_count})</button>
        <button type="button" id="tab-btn-unresolved" class="filter-btn" onclick="switchTab('unresolved')">Unresolved Escalations ({unresolved_count})</button>
      </div>
    </div>

    <!-- Active Filter Feedback Banner -->
    <div id="filterFeedbackBanner" class="filter-feedback-banner" style="display:none;"></div>

    <!-- Empty State -->
    <div id="emptyCategoryMsg" class="empty-state">
      No tasks found matching the selected search criteria or filter combinations.
      <div style="margin-top:10px;">
        <button type="button" class="btn-reset" onclick="resetAllFilters()">Reset All Filters</button>
      </div>
    </div>

    <!-- Results Toolbar -->
    <div class="results-toolbar">
      <div class="results-count" id="resultsCount">&nbsp;</div>
      <div class="results-controls">
        <span class="ctl-label">Sort</span>
        <select id="sortBy" class="ctl-select" onchange="setSort(this.value)">
          <option value="default">Default order</option>
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
          <option value="severity">Severity (high &rarr; low)</option>
          <option value="tid">Task ID (A &rarr; Z)</option>
        </select>
        <span class="ctl-label">Per page</span>
        <select id="pageSize" class="ctl-select" onchange="setPageSize(this.value)">
          <option value="10">10</option>
          <option value="25">25</option>
          <option value="50">50</option>
          <option value="100">100</option>
        </select>
        <button type="button" id="compactBtn" class="ctl-btn" onclick="toggleCompact()" title="Collapse cards to headers only; click a card header to expand it">Compact View</button>
        <button type="button" class="ctl-btn" onclick="exportFilteredCsv()" title="Download all tasks matching current filters as CSV">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
          Export CSV
        </button>
      </div>
    </div>

    <!-- Tasks Feed -->
    <div id="ticketsList">
      {cards or '<div class="empty-state" style="display:block;">No active tasks in queue. Use Alert Intake API (/api/alerts) or the manual dispatcher.</div>'}
    </div>

    <!-- Pagination Bar -->
    <div id="paginationBar" class="pagination-bar">
      <div id="paginationInfo" class="pagination-info">Showing 1 to 10 of tasks</div>
      <div id="paginationControls" class="pagination-controls"></div>
    </div>
  </main>

  <!-- Chronological Audit Timeline Modal -->
  <div id="timelineModal" class="modal-backdrop" onclick="closeTimelineBackdrop(event)">
    <div class="modal-card">
      <div class="modal-header">
        <div>
          <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.04em;">Audit Evidence Stream</div>
          <div class="modal-title" id="timelineModalTitle">Timeline Inspection</div>
        </div>
        <button type="button" class="btn-modal-close" onclick="closeTimeline()">&times;</button>
      </div>
      <div class="modal-body" id="timelineStreamContainer">
        <!-- Chronological stream populated by JS -->
      </div>
    </div>
  </div>

  <!-- Comprehensive Task Details Modal -->
  <div id="taskDetailsModal" class="modal-backdrop" onclick="closeTaskDetailsBackdrop(event)">
    <div class="modal-card" style="max-width: 680px;">
      <div class="modal-header">
        <div>
          <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.04em;">Task Intelligence & Assignment Record</div>
          <div class="modal-title" id="taskDetailsModalTitle">Task Details</div>
        </div>
        <button type="button" class="btn-modal-close" onclick="closeTaskDetails()">&times;</button>
      </div>
      <div class="modal-body" id="taskDetailsBody" style="padding: 20px;">
        <!-- Populated dynamically by JS -->
      </div>
    </div>
  </div>

  <div id="toastStack" class="toast-stack"></div>
  <script type="application/json" id="dashStats">{dash_stats_json}</script>

  <script>
    var activeCategory = 'all';
    var PAGE_SIZE = 10;
    var currentPage = 1;

    function normalizeIsoDate(dStr) {{
      if (!dStr) return '';
      dStr = dStr.trim();
      if (dStr.length === 10 && dStr.charAt(4) === '-' && dStr.charAt(7) === '-') return dStr;
      var parts = dStr.split(/[-/]/);
      if (parts.length === 3) {{
        if (parts[0].length === 4) {{
          return parts[0] + '-' + parts[1].padStart(2, '0') + '-' + parts[2].padStart(2, '0');
        }} else if (parts[2].length === 4) {{
          return parts[2] + '-' + parts[1].padStart(2, '0') + '-' + parts[0].padStart(2, '0');
        }}
      }}
      return dStr;
    }}

    function quickFilter(cat) {{
      activeCategory = cat;
      currentPage = 1;
      document.querySelectorAll('.filter-btn').forEach(function(btn) {{
        btn.classList.remove('active');
      }});
      var activeBtn = document.getElementById('tab-btn-' + cat);
      if (activeBtn) {{
        activeBtn.classList.add('active');
      }}
      var section = document.getElementById('ticketsSection');
      if (section) {{
        section.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      }}
      runFilter();
    }}

    function switchTab(cat) {{
      quickFilter(cat);
    }}

    function resetAllFilters() {{
      var elSearch = document.getElementById('searchInput'); if (elSearch) elSearch.value = '';
      var elFrom = document.getElementById('filterFromDate'); if (elFrom) elFrom.value = '';
      var elTo = document.getElementById('filterToDate'); if (elTo) elTo.value = '';
      var elWf = document.getElementById('filterWorkflow'); if (elWf) elWf.value = 'all';
      var elLang = document.getElementById('filterLanguage'); if (elLang) elLang.value = 'all';
      var elOutcome = document.getElementById('filterOutcome'); if (elOutcome) elOutcome.value = 'all';
      var elAck = document.getElementById('filterAck'); if (elAck) elAck.value = 'all';
      var elWa = document.getElementById('filterWaStatus'); if (elWa) elWa.value = 'all';
      quickFilter('all');
    }}

    function copyText(txt) {{
      if (!txt) return;
      navigator.clipboard.writeText(txt).then(function() {{
        alert('Copied to clipboard: ' + txt);
      }}).catch(function() {{
        prompt('Copy to clipboard:', txt);
      }});
    }}

    function goToPage(page) {{
      currentPage = page;
      runFilter();
      var section = document.getElementById('ticketsSection');
      if (section) {{
        section.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      }}
    }}

    function renderPagination(totalMatched, totalPages) {{
      var bar = document.getElementById('paginationBar');
      var info = document.getElementById('paginationInfo');
      var controls = document.getElementById('paginationControls');

      if (!bar || !info || !controls) return;

      if (totalMatched === 0) {{
        bar.style.display = 'none';
        return;
      }}
      bar.style.display = 'flex';

      var fromNum = (currentPage - 1) * PAGE_SIZE + 1;
      var toNum = Math.min(currentPage * PAGE_SIZE, totalMatched);
      info.innerHTML = 'Showing <b>' + fromNum + '</b> to <b>' + toNum + '</b> of <b>' + totalMatched + '</b> tasks';

      var html = '';
      html += '<button type="button" class="page-btn" ' + (currentPage === 1 ? 'disabled' : '') + ' onclick="goToPage(' + (currentPage - 1) + ')">Previous</button>';

      var pagesToShow = [];
      for (var p = 1; p <= totalPages; p++) {{
        if (p === 1 || p === totalPages || (p >= currentPage - 2 && p <= currentPage + 2)) {{
          pagesToShow.push(p);
        }}
      }}

      var lastP = 0;
      pagesToShow.forEach(function(p) {{
        if (lastP && p - lastP > 1) {{
          html += '<span class="page-ellipsis">...</span>';
        }}
        var activeCls = (p === currentPage) ? ' active' : '';
        html += '<button type="button" class="page-btn' + activeCls + '" onclick="goToPage(' + p + ')">' + p + '</button>';
        lastP = p;
      }});

      html += '<button type="button" class="page-btn" ' + (currentPage === totalPages ? 'disabled' : '') + ' onclick="goToPage(' + (currentPage + 1) + ')">Next</button>';
      controls.innerHTML = html;
    }}

    function runFilter() {{
      try {{
        var elSearch = document.getElementById('searchInput');
        var q = (elSearch ? elSearch.value : '').toLowerCase().trim();

        var elFrom = document.getElementById('filterFromDate');
        var fromDate = normalizeIsoDate(elFrom ? elFrom.value : '');

        var elTo = document.getElementById('filterToDate');
        var toDate = normalizeIsoDate(elTo ? elTo.value : '');

        var elWf = document.getElementById('filterWorkflow');
        var fWf = (elWf ? elWf.value : 'all').toLowerCase();

        var elLang = document.getElementById('filterLanguage');
        var fLang = (elLang ? elLang.value : 'all').toLowerCase();

        var elOutcome = document.getElementById('filterOutcome');
        var fOutcome = (elOutcome ? elOutcome.value : 'all').toLowerCase();

        var elAck = document.getElementById('filterAck');
        var fAck = (elAck ? elAck.value : 'all').toLowerCase();

        var elWa = document.getElementById('filterWaStatus');
        var fWa = (elWa ? elWa.value : 'all').toLowerCase();

        var cards = document.querySelectorAll('.ticket-card');
        var matchedCards = [];

        cards.forEach(function(card) {{
          var cat = card.getAttribute('data-category') || '';
          var date = card.getAttribute('data-date') || '';
          var lang = (card.getAttribute('data-lang') || '').toLowerCase();
          var outcome = (card.getAttribute('data-outcome') || '').toLowerCase();
          var wf = (card.getAttribute('data-wf') || '').toLowerCase();
          var ack = (card.getAttribute('data-ack') || '').toLowerCase();
          var unresolved = (card.getAttribute('data-unresolved') || '').toLowerCase();
          var waStatus = (card.getAttribute('data-wastatus') || '').toLowerCase();
          var text = (card.getAttribute('data-search') || '').toLowerCase();

          // 1. Quick View Pill matching
          var catMatch = (activeCategory === 'all');
          if (activeCategory === 'unresolved') {{
            catMatch = (unresolved === 'true');
          }} else if (activeCategory !== 'all') {{
            catMatch = (cat === activeCategory);
          }}

          // 2. Correlation Search box matching
          var searchMatch = true;
          if (q) {{
            searchMatch = (text.indexOf(q) !== -1);
          }}

          // 3. Date range matching (both YYYY-MM-DD)
          var dateMatch = true;
          if (fromDate) {{
            if (!date || date < fromDate) dateMatch = false;
          }}
          if (toDate) {{
            if (!date || date > toDate) dateMatch = false;
          }}

          // 4. Workflow matching
          var wfMatch = (fWf === 'all');
          if (!wfMatch) {{
            if (fWf === 'emergency') {{
              wfMatch = (wf.indexOf('emergency') !== -1 || text.indexOf('emg-') !== -1 || text.indexOf('emergency') !== -1);
            }} else {{
              wfMatch = (wf.indexOf(fWf) !== -1);
            }}
          }}

          // 5. Language matching
          var langMatch = (fLang === 'all' || lang.indexOf(fLang) !== -1);

          // 6. Outcome matching
          var outcomeMatch = (fOutcome === 'all' || outcome.indexOf(fOutcome) !== -1);

          // 7. Acknowledgement matching
          var ackMatch = (fAck === 'all' || ack === fAck);

          // 8. WhatsApp delivery status matching
          var waMatch = (fWa === 'all' || waStatus.indexOf(fWa) !== -1);

          if (catMatch && searchMatch && dateMatch && wfMatch && langMatch && outcomeMatch && ackMatch && waMatch) {{
            matchedCards.push(card);
          }} else {{
            card.style.display = 'none';
          }}
        }});

        var totalMatched = matchedCards.length;
        var totalPages = Math.ceil(totalMatched / PAGE_SIZE) || 1;
        if (currentPage > totalPages) currentPage = totalPages;
        if (currentPage < 1) currentPage = 1;

        var startIdx = (currentPage - 1) * PAGE_SIZE;
        var endIdx = startIdx + PAGE_SIZE;

        matchedCards.forEach(function(card, idx) {{
          if (idx >= startIdx && idx < endIdx) {{
            card.style.display = 'block';
          }} else {{
            card.style.display = 'none';
          }}
        }});

        var emptyEl = document.getElementById('emptyCategoryMsg');
        if (emptyEl) {{
          emptyEl.style.display = (totalMatched === 0) ? 'block' : 'none';
        }}

        // Update active filter feedback banner
        var banner = document.getElementById('filterFeedbackBanner');
        if (banner) {{
          var hasActiveFilter = (q !== '' || fromDate !== '' || toDate !== '' || fWf !== 'all' || fLang !== 'all' || fOutcome !== 'all' || fAck !== 'all' || fWa !== 'all' || activeCategory !== 'all');
          if (hasActiveFilter) {{
            var filterTags = [];
            if (activeCategory !== 'all') filterTags.push('Category: ' + activeCategory.toUpperCase());
            if (q) filterTags.push('Search: "' + q + '"');
            if (fromDate && toDate) filterTags.push('Date: ' + fromDate + ' to ' + toDate);
            else if (fromDate) filterTags.push('From: ' + fromDate);
            else if (toDate) filterTags.push('To: ' + toDate);
            if (fWf !== 'all') filterTags.push('Workflow: ' + fWf);
            if (fLang !== 'all') filterTags.push('Lang: ' + fLang);
            if (fOutcome !== 'all') filterTags.push('Outcome: ' + fOutcome);
            if (fAck !== 'all') filterTags.push('Ack: ' + (fAck === 'true' ? 'Yes' : 'No'));
            if (fWa !== 'all') filterTags.push('SMS: ' + fWa);

            banner.innerHTML = '<div><b>Active Filter:</b> ' + filterTags.join(' &bull; ') + ' &mdash; Showing <b>' + totalMatched + '</b> matching task(s)</div>' +
                               '<button type="button" onclick="resetAllFilters()" style="background:#2563eb;color:#fff;border:none;padding:4px 10px;border-radius:4px;font-size:11.5px;font-weight:600;cursor:pointer;">Reset</button>';
            banner.style.display = 'flex';
          }} else {{
            banner.style.display = 'none';
          }}
        }}

        renderPagination(totalMatched, totalPages);
      }} catch(err) {{
        console.error('runFilter error:', err);
      }}
    }}

    // Chronological Audit Timeline Inspection Modal
    function openTimeline(tid) {{
      var modal = document.getElementById('timelineModal');
      var title = document.getElementById('timelineModalTitle');
      var container = document.getElementById('timelineStreamContainer');

      title.innerText = 'Task: ' + tid + ' - Audit Evidence Stream';
      container.innerHTML = '<div style="text-align:center;color:#64748b;padding:30px;">Querying chronological audit logs from database...</div>';
      modal.style.display = 'flex';

      fetch('/api/tickets/' + encodeURIComponent(tid) + '/timeline')
        .then(function(res) {{ return res.json(); }})
        .then(function(data) {{
          var stream = (data && data.timeline) || [];
          if (!stream.length) {{
            container.innerHTML = '<div style="text-align:center;color:#94a3b8;padding:30px;">No audit log records recorded yet for ' + tid + '.</div>';
            return;
          }}

          var html = '';
          stream.forEach(function(item) {{
            var cat = item.category || 'event';
            var ts = item.timestamp || '';
            var desc = item.description || item.message || '';
            var sid = item.call_sid || '';
            var mid = item.provider_message_id || '';
            var eid = item.event_id || '';

            var nodeClass = 'node-voice';
            if (cat.indexOf('whatsapp') !== -1) nodeClass = 'node-whatsapp';
            else if (cat.indexOf('escalat') !== -1 || cat.indexOf('reject') !== -1) nodeClass = 'node-escalation';
            else if (cat.indexOf('ack') !== -1 || cat.indexOf('valid') !== -1) nodeClass = 'node-ack';

                        var pillsHtml = '';
            if (sid) pillsHtml += '<span class="corr-id" data-copy="' + sid + '" onclick="copyText(this.dataset.copy)" title="Click to copy Call SID">Call SID: ' + sid + '</span> ';
            if (mid) pillsHtml += '<span class="corr-id corr-id-wa" data-copy="' + mid + '" onclick="copyText(this.dataset.copy)" title="Click to copy WhatsApp ID">WAMID: ' + mid + '</span> ';
            if (eid) pillsHtml += '<span class="corr-id" data-copy="' + eid + '" onclick="copyText(this.dataset.copy)" title="Click to copy Event ID">Event ID: ' + eid + '</span> ';

            html += '<div class="timeline-item">' +
                      '<div class="timeline-node ' + nodeClass + '"></div>' +
                      '<div class="timeline-content">' +
                        '<div class="timeline-header-row">' +
                          '<span class="timeline-cat">' + cat.replace(/_/g, ' ') + '</span>' +
                          '<span class="timeline-time">' + ts + '</span>' +
                        '</div>' +
                        '<div class="timeline-desc">' + desc + '</div>' +
                        (pillsHtml ? '<div class="timeline-corr">' + pillsHtml + '</div>' : '') +
                      '</div>' +
                    '</div>';
          }});

          container.innerHTML = html;
        }})
        .catch(function(err) {{
          container.innerHTML = '<div style="color:#ef4444;padding:20px;text-align:center;">Failed to query audit stream: ' + err + '</div>';
        }});
    }}

    function closeTimeline() {{
      var modal = document.getElementById('timelineModal');
      if (modal) modal.style.display = 'none';
    }}

    function closeTimelineBackdrop(e) {{
      if (e.target.id === 'timelineModal') {{
        closeTimeline();
      }}
    }}

    // Task Details Modal Handlers
    function openTaskDetails(btn) {{
      try {{
        var raw = btn.getAttribute('data-task');
        if (!raw) return;
        var data = JSON.parse(raw);
        var modal = document.getElementById('taskDetailsModal');
        var title = document.getElementById('taskDetailsModalTitle');
        var body = document.getElementById('taskDetailsBody');

        title.innerText = 'Task ' + data.task_id + ' - Assignment Details';

        var reassignHtml = '';
        if (data.is_reassigned && data.reassignment) {{
          reassignHtml = 
            '<div style="background:#faf5ff;border:1px solid #e9d5ff;border-radius:8px;padding:14px;margin-bottom:16px;">' +
              '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">' +
                '<div style="font-size:12px;font-weight:700;color:#7e22ce;text-transform:uppercase;letter-spacing:0.5px;">Assignment & Reassignment History</div>' +
                '<span class="status-pill status-reassigned"><span class="status-dot"></span> Reassigned</span>' +
              '</div>' +
              '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">' +
                '<div style="background:#fff;border:1px solid #cbd5e1;border-radius:6px;padding:12px;">' +
                  '<div style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;margin-bottom:6px;">Previously Assigned To</div>' +
                  '<div style="font-size:14px;font-weight:600;color:#0f172a;margin-bottom:6px;">' + data.initial_assignment.name + '</div>' +
                  '<div style="font-size:12px;color:#475569;display:flex;flex-direction:column;gap:4px;">' +
                    '<div>Mobile: <b style="font-family:monospace;">' + data.initial_assignment.mobile + '</b></div>' +
                    '<div>Language: <b>' + data.initial_assignment.language + '</b></div>' +
                    '<div>Previous Assignment ID: <b style="font-family:monospace;">' + data.initial_assignment.assignment_id + '</b></div>' +
                  '</div>' +
                '</div>' +
                '<div style="background:#fff;border:1px solid #c084fc;border-radius:6px;padding:12px;box-shadow:0 1px 4px rgba(168,85,247,0.1);">' +
                  '<div style="font-size:11px;font-weight:700;color:#9333ea;text-transform:uppercase;margin-bottom:6px;">Reassigned To (Current)</div>' +
                  '<div style="font-size:14px;font-weight:600;color:#0f172a;margin-bottom:6px;">' + data.reassignment.name + '</div>' +
                  '<div style="font-size:12px;color:#475569;display:flex;flex-direction:column;gap:4px;">' +
                    '<div>Mobile: <b style="font-family:monospace;">' + data.reassignment.mobile + '</b></div>' +
                    '<div>Language: <b>' + data.reassignment.language + '</b></div>' +
                    '<div>New Assignment ID: <b style="font-family:monospace;">' + data.reassignment.assignment_id + '</b></div>' +
                  '</div>' +
                '</div>' +
              '</div>' +
            '</div>';
        }} else {{
          reassignHtml = 
            '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px;margin-bottom:16px;">' +
              '<div style="font-size:12px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px;">Assigned Technician</div>' +
              '<div style="background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:12px;">' +
                '<div style="font-size:14px;font-weight:600;color:#0f172a;margin-bottom:6px;">' + data.initial_assignment.name + '</div>' +
                '<div style="font-size:12px;color:#475569;display:grid;grid-template-columns:1fr 1fr;gap:8px;">' +
                  '<div>Mobile: <b style="font-family:monospace;">' + data.initial_assignment.mobile + '</b></div>' +
                  '<div>Language: <b>' + data.initial_assignment.language + '</b></div>' +
                  '<div>Assignment ID: <b style="font-family:monospace;">' + data.initial_assignment.assignment_id + '</b></div>' +
                  '<div>Reassignment Status: <b style="color:#059669;">Initial Assignment (Not Reassigned)</b></div>' +
                '</div>' +
              '</div>' +
            '</div>';
        }}

        var supervisorsHtml = 
          '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px;margin-bottom:16px;">' +
            '<div style="font-size:12px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px;">Supervisory Escalation Contacts</div>' +
            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">' +
              '<div style="background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:10px;">' +
                '<div style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;margin-bottom:4px;">Team Leader</div>' +
                '<div style="font-size:13px;font-weight:600;color:#0f172a;margin-bottom:4px;">' + data.team_leader.name + '</div>' +
                '<div style="font-size:11.5px;color:#64748b;">Mobile: <b style="font-family:monospace;">' + data.team_leader.mobile + '</b> &bull; Lang: <b>' + data.team_leader.language + '</b></div>' +
              '</div>' +
              '<div style="background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:10px;">' +
                '<div style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;margin-bottom:4px;">Operations Manager</div>' +
                '<div style="font-size:13px;font-weight:600;color:#0f172a;margin-bottom:4px;">' + data.manager.name + '</div>' +
                '<div style="font-size:11.5px;color:#64748b;">Mobile: <b style="font-family:monospace;">' + data.manager.mobile + '</b> &bull; Lang: <b>' + data.manager.language + '</b></div>' +
              '</div>' +
            '</div>' +
          '</div>';

        var metaHtml = 
          '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px;">' +
            '<div style="font-size:12px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px;">Task & Site Information</div>' +
            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px;color:#475569;">' +
              '<div>Site: <b>' + data.site_number + ' - ' + data.site_name + '</b></div>' +
              '<div>Alarm: <b>' + data.alarm_id + '</b></div>' +
              '<div>Severity: <b>' + data.severity + '</b></div>' +
              '<div>Task Status: <b>' + data.task_status + '</b></div>' +
              '<div>Alarm Status: <b>' + data.alarm_status + '</b></div>' +
              '<div>Movement: <b>' + data.operational_status + '</b></div>' +
              '<div style="grid-column:1/-1;">Event Timestamp: <b>' + data.event_timestamp + '</b></div>' +
              '<div style="grid-column:1/-1;">Description: <b>' + data.description + '</b></div>' +
            '</div>' +
          '</div>';

        body.innerHTML = reassignHtml + supervisorsHtml + metaHtml;
        modal.style.display = 'flex';
      }} catch(err) {{
        console.error('openTaskDetails error:', err);
      }}
    }}

    function closeTaskDetails() {{
      var modal = document.getElementById('taskDetailsModal');
      if (modal) modal.style.display = 'none';
    }}

    function closeTaskDetailsBackdrop(e) {{
      if (e.target && e.target.id === 'taskDetailsModal') closeTaskDetails();
    }}

    document.addEventListener('keydown', function(e) {{
      if (e.key === 'Escape') {{
        closeTimeline();
        closeTaskDetails();
      }}
    }});

    // Initialize Charts Safely (Never blocking)
    (function initCharts() {{
      if (typeof Chart === 'undefined') {{
        console.warn('Chart.js library is not available or blocked by network.');
        return;
      }}
      try {{
        const statusData = [{assigned_count}, {reassigned_count}, {pending_count}, {escalated_count}, {ack_count}];
        const elStatus = document.getElementById('statusChart');
        if (elStatus) {{
          new Chart(elStatus.getContext('2d'), {{
            type: 'doughnut',
            data: {{
              labels: ['Assigned', 'Reassigned', 'Pending / In-Call', 'Escalated', 'Acknowledged'],
              datasets: [{{
                data: statusData,
                backgroundColor: ['#06b6d4', '#a855f7', '#f59e0b', '#ef4444', '#10b981'],
                borderWidth: 2,
                borderColor: '#ffffff',
                hoverOffset: 6
              }}]
            }},
            options: {{
              responsive: true,
              maintainAspectRatio: false,
              plugins: {{
                legend: {{
                  position: 'bottom',
                  labels: {{ boxWidth: 12, padding: 14, font: {{ family: 'Inter', size: 11, weight: '500' }}, color: '#475569' }}
                }}
              }},
              cutout: '68%'
            }}
          }});
        }}

        const elSev = document.getElementById('severityChart');
        if (elSev) {{
          new Chart(elSev.getContext('2d'), {{
            type: 'bar',
            data: {{
              labels: ['Minor', 'Major', 'Critical', 'Emergency'],
              datasets: [{{
                label: 'Tasks',
                data: [{minor_count}, {major_count}, {critical_count}, {emergency_count}],
                backgroundColor: ['#2563eb', '#f59e0b', '#dc2626', '#991b1b'],
                borderRadius: 6,
                barThickness: 28
              }}]
            }},
            options: {{
              responsive: true,
              maintainAspectRatio: false,
              plugins: {{ legend: {{ display: false }} }},
              scales: {{
                y: {{ beginAtZero: true, ticks: {{ precision: 0, font: {{ family: 'Inter', size: 11 }} }}, grid: {{ color: '#f1f5f9' }} }},
                x: {{ grid: {{ display: false }}, ticks: {{ font: {{ family: 'Inter', size: 11 }} }} }}
              }}
            }}
          }});
        }}

        const elAct = document.getElementById('activityChart');
        if (elAct) {{
          new Chart(elAct.getContext('2d'), {{
            type: 'bar',
            data: {{
              labels: ['Total Calls', 'Completed Calls', 'SMS Alerts'],
              datasets: [{{
                label: 'Executions',
                data: [{calls_total}, {calls_completed}, {wa_total}],
                backgroundColor: ['#4f46e5', '#059669', '#7c3aed'],
                borderRadius: 6,
                barThickness: 28
              }}]
            }},
            options: {{
              responsive: true,
              maintainAspectRatio: false,
              plugins: {{ legend: {{ display: false }} }},
              scales: {{
                y: {{ beginAtZero: true, ticks: {{ precision: 0, font: {{ family: 'Inter', size: 11 }} }}, grid: {{ color: '#f1f5f9' }} }},
                x: {{ grid: {{ display: false }}, ticks: {{ font: {{ family: 'Inter', size: 11 }} }} }}
              }}
            }}
          }});
        }}
      }} catch(err) {{
        console.warn('Charts init notice:', err);
      }}
    }})();

    // Execute Initial Filter on Page Load
    runFilter();
  </script>
</body>
</html>"""

def _wants_wait(params):
    return str(params.get("wait", "")).lower() in ("1", "true", "yes")


def _wait_options(params, default_timeout=300):
    """?wait=true[&until=technician|final][&timeout=seconds] -> (until, timeout). Validated before waiting."""
    until = "final" if str(params.get("until", "")).lower() == "final" else "technician"
    try:
        return until, int(params.get("timeout", default_timeout))
    except ValueError:
        raise workflow.EventError("timeout must be a number of seconds") from None


def _wait(tid, assignment_id, options, accepted_body):
    """Hold until the answer is known. call_result 'pending' + timed_out true = still running at the timeout."""
    until, timeout = options
    waited = workflow.wait_for_result(tid, assignment_id, until, timeout)
    return dict(accepted_body, **waited)


KEEPALIVE_SEC = 10  # a space is sent this often while waiting (proxies drop silent requests: Cloudflare ~100 s)
CLIENT_GONE = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)


# --------------------------------------------------------------------------- HTTP
TICKET_PATH = re.compile(r"^/api/tickets/([^/]+)(/timeline|/followup)?$")


class Handler(BaseHTTPRequestHandler):
    def _get_cookie_session(self):
        cookie_header = self.headers.get("Cookie", "")
        if "flm_session=" in cookie_header:
            for part in cookie_header.split(";"):
                part = part.strip()
                if part.startswith("flm_session="):
                    token = part[len("flm_session="):].strip()
                    if token and store.verify_auth_token(token):
                        return token
        return None

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except CLIENT_GONE:
            self._client_gone()

    def _client_gone(self, waited=None, ticket_id=None):
        took = f" after {waited} s" if waited is not None else ""
        where = f"; the result is saved - read it with GET /api/call/{ticket_id}" if ticket_id else ""
        store.audit(ticket_id, "client_disconnected", f"Caller closed the connection{took} before the response "
                    f"was sent ({self.command} {self.path}){where}")
        self.close_connection = True

    def _json_waiting(self, ticket_id, compute):
        """Answer a ?wait=true request. Headers go out at once and a space every KEEPALIVE_SEC, so Cloudflare
        and clients keep the connection open however long the calls take; the JSON follows when ready
        (leading spaces are valid JSON). The HTTP status is 200; timed_out / call_result tell the outcome."""
        box = {}

        def run():
            try:
                box["body"] = compute()
            except Exception as e:  # noqa: BLE001
                box["body"] = {"error": f"{type(e).__name__}: {e}"}

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        started = time.time()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()
            while worker.is_alive():
                worker.join(KEEPALIVE_SEC)
                if worker.is_alive():
                    self.wfile.write(b" ")
                    self.wfile.flush()
            self.wfile.write(json.dumps(box["body"], indent=2, ensure_ascii=False).encode("utf-8"))
            self.wfile.flush()
        except CLIENT_GONE:
            self._client_gone(int(time.time() - started), ticket_id)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, indent=2, ensure_ascii=False), "application/json; charset=utf-8")

    def _parse(self):
        if hasattr(self, "_parsed"):
            return self._parsed
        url = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
        if self.headers.get("Content-Type", "").startswith("application/json"):
            try:
                form = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                form = None
        self._parsed = (url.path.rstrip("/") or "/", params, form)
        return self._parsed

    def _from_this_pc(self):
        """True only for requests made on this PC (not via the tunnel, not from another machine)."""
        tunnelled = bool(self.headers.get("Cf-Connecting-Ip"))  # cloudflared adds it
        return not tunnelled and self.client_address[0] in ("127.0.0.1", "::1")

    def _extract_token(self, form=None):
        auth_header = self.headers.get("Authorization", "")
        bearer_token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        query_key = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("key", [""])[0]
        body_token = (isinstance(form, dict) and (form.get("token") or form.get("api_key") or form.get("key"))) or ""
        return (self.headers.get("X-API-Key") or
                self.headers.get("x_api_key") or
                self.headers.get("api_key") or
                self.headers.get("apikey") or
                self.headers.get("X-Auth-Token") or
                bearer_token or
                query_key or
                body_token)

    def _resolve_caller(self, token):
        if not token:
            return None
        if CFG["api_key"] and token == CFG["api_key"]:
            return {
                "username": "api_admin",
                "role": "admin",
                "full_name": "API Key Administrator",
                "auth_type": "api_key"
            }
        username = store.verify_auth_token(token)
        if username:
            user = store.q("SELECT id, username, role, full_name, is_active FROM users WHERE username = ?", (username,), one=True)
            if user:
                return {
                    "username": user["username"],
                    "role": user["role"],
                    "full_name": user["full_name"],
                    "auth_type": "bearer_token"
                }
            return {
                "username": username,
                "role": "admin",
                "full_name": username,
                "auth_type": "bearer_token"
            }
        return None

    def _handle_authorize(self, form=None, params=None):
        params = params or {}
        # 1. First, check username & password (via Basic Auth, JSON body, or query params)
        username = (isinstance(form, dict) and (form.get("username") or form.get("user"))) or params.get("username") or params.get("user")
        password = (isinstance(form, dict) and (form.get("password") or form.get("pass"))) or params.get("password") or params.get("pass")

        if not username or not password:
            auth_header = self.headers.get("Authorization", "")
            if auth_header.lower().startswith("basic "):
                try:
                    decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
                    if ":" in decoded:
                        username, password = decoded.split(":", 1)
                except Exception:
                    pass

        if username and password:
            user = store.verify_user(str(username).strip(), str(password))
            if user:
                token, ttl = store.create_auth_token(user["username"])
                return self._json(200, {
                    "status": "success",
                    "authorized": True,
                    "http_code": 200,
                    "message": "User authenticated successfully. Use the provided api_key in the headers of other APIs.",
                    "api_key": CFG.get("api_key", ""),
                    "x_api_key": CFG.get("api_key", ""),
                    "token": token,
                    "token_type": "Bearer",
                    "expires_in": ttl,
                    "user": {
                        "username": user["username"],
                        "role": user.get("role", "admin"),
                        "full_name": user.get("full_name", user["username"]),
                        "auth_type": "credentials"
                    }
                })
            return self._json(401, {
                "status": "error",
                "authorized": False,
                "http_code": 401,
                "error": "Unauthorized: Invalid username or password.",
                "message": "The username or password provided is incorrect."
            })

        # 2. Check if caller passed existing X-API-Key / token to verify it
        token = self._extract_token(form)
        if token:
            caller = self._resolve_caller(token)
            if caller:
                return self._json(200, {
                    "status": "success",
                    "authorized": True,
                    "http_code": 200,
                    "message": "API Key is valid and active.",
                    "api_key": CFG.get("api_key", ""),
                    "x_api_key": CFG.get("api_key", ""),
                    "api_key_valid": True,
                    "user": caller
                })
            return self._json(401, {
                "status": "error",
                "authorized": False,
                "http_code": 401,
                "error": "Unauthorized: Invalid API Key provided.",
                "message": "The provided API Key is invalid or inactive.",
                "api_key_valid": False
            })

        # 3. Neither credentials nor API key provided
        return self._json(400, {
            "status": "error",
            "authorized": False,
            "http_code": 400,
            "error": "Bad Request: Missing credentials.",
            "message": "Please provide username and password via Basic Auth or JSON body to obtain your API Key.",
            "required": ["username", "password"]
        })
    def _authorized(self, path, form):
        if path.startswith("/twilio/"):
            if CFG["dry_run"] or not CFG["validate_twilio_signature"]:
                return True
            ok = twilio_client.valid_signature(CFG["base_url"] + self.path, form or {},
                                               self.headers.get("X-Twilio-Signature", ""))
            if not ok:
                store.audit(None, "twilio_signature_rejected", f"Rejected unsigned/forged request to {self.path}")
            return ok
        if path in ("/api/health", "/health", "/api/authenticate", "/authenticate", "/api/auth", "/api/login",
                    "/api/authorize", "/api/authorization", "/api/verify", "/docs", "/api/docs", "/documentation", "/login", "/logout"):
            return True
        if path.startswith("/api/"):
            sent_key = self._extract_token(form)
            if CFG["api_key"] and sent_key == CFG["api_key"]:
                return True
            if sent_key and store.verify_auth_token(sent_key):
                return True
            if not CFG["api_key"] and not sent_key:
                return self._from_this_pc()
            return False
            if not CFG["api_key"] and not sent_key:
                return self._from_this_pc()
            return False
        if path in ("/", "/login", "/logout"):
            return True
        return self._from_this_pc()

    def do_GET(self):
        path, params, form = self._parse()
        if not self._authorized(path, {}):
            return self._json(401, {
                "status": "error",
                "authorized": False,
                "http_code": 401,
                "error": "Unauthorized: Missing or invalid API Key.",
                "message": "Please provide 'X-API-Key: 5adJjBRSydznz8qO8e6Du128rWb5djvd' in request headers.",
                "required_header": "X-API-Key"
            })
        try:
            if path == "/logout":
                self.send_response(303)
                self.send_header("Set-Cookie", "flm_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
                self.send_header("Location", "/login")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                return

            if path == "/login":
                if self._get_cookie_session():
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.end_headers()
                    return
                return self._send(200, login_page_html(), extra={"Cache-Control": "no-cache, no-store, must-revalidate"})

            if path == "/":
                # Check if user is logged in
                session_token = self._get_cookie_session()
                if not session_token:
                    # Redirect to /login
                    self.send_response(303)
                    self.send_header("Location", "/login")
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.end_headers()
                    return
                return self._send(200, dashboard_html(), extra={"Cache-Control": "no-cache, no-store, must-revalidate"})
            if path in ("/docs", "/api/docs", "/documentation"):
                return self._send(200, docs_html())
            if path == "/api/docs/raw":
                doc_path = os.path.join(os.path.dirname(__file__), "API_DOCUMENTATION.md")
                if os.path.isfile(doc_path):
                    with open(doc_path, "r", encoding="utf-8") as df:
                        return self._send(200, df.read(), "text/markdown; charset=utf-8")
                return self._json(404, {"error": "API_DOCUMENTATION.md not found"})
            if path == "/api/health":
                return self._json(200, {"status": "ok", "dry_run": CFG["dry_run"], "time": store.now_iso()})
            if path in ("/api/authenticate", "/authenticate", "/api/auth", "/api/login"):
                return self._json(200, {
                    "status": "ok",
                    "endpoint": path,
                    "method": "POST",
                    "description": "Send a POST request with JSON body containing 'username' and 'password' to authenticate.",
                    "example": {"username": "admin", "password": "your_password"}
                })
            if path in ("/api/authorize", "/api/authorization", "/api/verify"):
                return self._handle_authorize(form=None, params=params)
            if path in ("/api/getalerts", "/api/get_alerts", "/api/alerts/get", "/api/alerts", "/api/alerts/intake"):
                # Other APIs require only X-API-Key
                api_key = self._extract_token(form)
                caller = self._resolve_caller(api_key)
                if not caller:
                    return self._json(401, {
                        "status": "error",
                        "authorized": False,
                        "http_code": 401,
                        "error": "Unauthorized: Missing or invalid API Key.",
                        "message": "Please provide 'X-API-Key: 5adJjBRSydznz8qO8e6Du128rWb5djvd' in request headers.",
                        "required_header": "X-API-Key"
                    })

                res = workflow.get_alerts_view(form)
                res["authorized"] = True
                res["auth"] = {
                    "status": "authorized",
                    "user": caller.get("username", "api_admin"),
                    "role": caller.get("role", "admin"),
                    "api_key_valid": True,
                    "auth_type": caller.get("auth_type", "api_key")
                }
                return self._json(200, res)

            if path in ("/api/getalerts", "/api/get_alerts", "/api/alerts/get"):
                api_key = self._extract_token(form)
                caller = self._resolve_caller(api_key)
                if not caller:
                    return self._json(401, {
                        "status": "error",
                        "authorized": False,
                        "http_code": 401,
                        "error": "Unauthorized: Missing or invalid API Key.",
                        "message": "Please provide 'X-API-Key: 5adJjBRSydznz8qO8e6Du128rWb5djvd' in request headers.",
                        "required_header": "X-API-Key"
                    })
                res = workflow.get_alerts_view(form)
                res["authorized"] = True
                res["auth"] = {
                    "status": "authorized",
                    "user": caller.get("username", "api_admin"),
                    "role": caller.get("role", "admin"),
                    "api_key_valid": True,
                    "auth_type": caller.get("auth_type", "api_key")
                }
                return self._json(200, res)
            if path in ("/api/alerts/update", "/api/update", "/api/updaterecords", "/api/update_records", "/api/records/update"):
                return self._json(200, {
                    "status": "ok",
                    "endpoint": path,
                    "method": "POST",
                    "description": "Update ticket records. Follows table (1).tsv requirements (update_type, task_id, optional status/severity, technician details required for reassignment).",
                    "required_fields": ["task_id", "update_type"],
                    "reassignment_fields": ["technician_name", "technician_mobile", "technician_preferred_lang"]
                })
            if path in ("/api/alerts", "/api/pushrecords", "/api/push_records", "/api/push-records"):
                return self._json(200, {
                    "status": "ok",
                    "endpoint": path,
                    "method": "POST",
                    "description": "Push ticket/task records directly into the DB. Accepts single ticket or array of tickets.",
                    "sample_payload": {
                        "tickets": [
                            {
                                "task_id": "TASK1001",
                                "issue_description": "Network connectivity failure",
                                "issue_type": "Network Failure",
                                "severity": "Critical",
                                "task_status": "Dispatch",
                                "alarm_status": "Active",
                                "site_number": "SITE001",
                                "site_name": "Site SITE001",
                                "created_date": "2026-11-03T10:30:00+03:00",
                                "technician_name": "Ali",
                                "technician_mobile": "+966500000001",
                                "technician_preferred_lang": "English",
                                "teamleader_name": "Mohammed",
                                "teamleader_mobile": "+966500000002",
                                "teamleader_preferred_lang": "Arabic",
                                "manager_name": "Khalid",
                                "manager_mobile": "+966500000003",
                                "manager_preferred_lang": "English"
                            }
                        ]
                    }
                })
            if path in ("/api/alerts/intake", "/api/alert_intake", "/api/alerts", "/api/alert"):
                return self._json(200, {
                    "status": "ok",
                    "endpoint": path,
                    "method": "POST",
                    "description": "Alert Intake API. Accepts single alert object or array of alerts.",
                    "required_fields": [
                        "ticket_id",
                        "site_name or site_number",
                        "description (or issue / alarm_type)",
                        "technician_mobile (or phone_number)"
                    ],
                    "all_fields": [
                        "ticket_id", "alarm_id", "site_number", "site_name",
                        "description", "severity", "ticket_status", "alarm_status",
                        "technician_id", "technician_name", "technician_mobile",
                        "team_leader_id", "team_leader_name", "team_leader_mobile",
                        "manager_id", "manager_name", "manager_mobile",
                        "region", "country_code", "date_time"
                    ]
                })
            if path == "/api/reload":
                return self._json(200, {
                    "status": "ok",
                    "action": "modules_reloaded",
                    "modules": _reload_modules(),
                    "time": store.now_iso(),
                })
            if path == "/api/config":
                return self._json(200, queries.config_view())
            if path == "/api/tickets":
                return self._json(200, queries.list_tickets(params))
            m = TICKET_PATH.match(path)
            if m and m.group(2) != "/followup":
                tid = urllib.parse.unquote(m.group(1)).upper()
                if not store.get_ticket(tid):
                    return self._json(404, {"error": "ticket not found"})
                if m.group(2) == "/timeline":
                    return self._json(200, {"ticket_id": tid, "timeline": queries.timeline(tid)})
                return self._json(200, workflow.ticket_view(tid))
            if path == "/api/calls":
                return self._json(200, queries.search_calls(params))
            if path == "/api/events":
                return self._json(200, queries.events(params))
            if path == "/api/escalations":
                return self._json(200, queries.escalations(params))
            if path == "/api/exceptions":
                return self._json(200, queries.exceptions(params))
            if path == "/api/timers":
                return self._json(200, queries.pending_timers())
            if path == "/api/audit/export":
                ctype, body, name = queries.export(params)
                return self._send(200, body, ctype, {"Content-Disposition": f'attachment; filename="{name}"'})
            if path in ("/api/logs/download", "/api/logs"):
                log_data = ""
                if os.path.exists(LOG_FILE_PATH):
                    try:
                        with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="replace") as f:
                            log_data = f.read()
                    except Exception as ex:
                        log_data = f"Error reading server.log: {ex}\n"
                if not log_data.strip():
                    rows = store.q("SELECT ts, ticket_id, kind, message FROM audit_log ORDER BY id DESC LIMIT 5000")
                    lines = [f"[{r['ts']}] {r['ticket_id'] or '-'} {r['kind']}: {r['message']}" for r in reversed(rows)]
                    log_data = "\n".join(lines)
                today = store.now_iso()[:10]
                return self._send(200, log_data, "text/plain; charset=utf-8", {
                    "Content-Disposition": f'attachment; filename="flm_voice_server_{today}.log"'
                })
            if path.startswith("/api/call/"):
                v = legacy_view(urllib.parse.unquote(path[len("/api/call/"):]).upper())
                return self._json(200, v) if v else self._json(404, {"error": "ticket not found"})
            self._json(404, {"error": "not found"})
        except ValueError as e:
            self._json(400, {"error": str(e)})

    def do_PUT(self):
        path, _, _ = self._parse()
        if path in ("/api/alerts/update", "/api/update", "/api/updaterecords", "/api/update_records", "/api/records/update",
                    "/api/alerts", "/api/pushrecords", "/api/push_records", "/api/push-records"):
            return self.do_POST()
        return self._json(405, {"status": "error", "http_code": 405, "error": "Method Not Allowed"})

    def do_DELETE(self):
        return self._json(405, {"status": "error", "http_code": 405, "error": "Method Not Allowed"})

    def do_POST(self):
        path, params, form = self._parse()
        if not self._authorized(path, form):
            if path.startswith("/twilio/"):
                return self._send(403, "invalid Twilio signature", "text/plain")
            return self._json(401, {
                "status": "error",
                "authorized": False,
                "http_code": 401,
                "error": "Unauthorized: Missing or invalid API Key.",
                "message": "Please provide 'X-API-Key: 5adJjBRSydznz8qO8e6Du128rWb5djvd' in request headers.",
                "required_header": "X-API-Key"
            })
        if form is None and path != "/api/reload":
            return self._json(400, {"error": "body is not valid JSON"})
        try:
            if path == "/login":
                username = str(form.get("username") or "").strip()
                password = str(form.get("password") or "").strip()
                user = store.verify_user(username, password)
                if not user:
                    return self._send(401, login_page_html("Invalid username or password. Please try again."))
                token, ttl = store.create_auth_token(user["username"])
                store.audit(None, "web_login_success", f"User '{username}' logged in via web portal")
                self.send_response(303)
                self.send_header("Set-Cookie", f"flm_session={token}; Path=/; Max-Age={ttl}; HttpOnly; SameSite=Lax")
                self.send_header("Location", "/")
                self.end_headers()
                return

            if path in ("/api/health", "/health"):
                return self._json(200, {
                    "status": "ok",
                    "message": "no-op: all engineers busy or ticket held",
                    "time": store.now_iso()
                })
            if path in ("/api/authenticate", "/authenticate", "/api/auth", "/api/login"):
                if not isinstance(form, dict):
                    return self._json(400, {
                        "status": "error",
                        "error": "body must be a valid JSON object with username and password"
                    })
                username = str(form.get("username") or form.get("user") or form.get("email") or "").strip()
                password = str(form.get("password") or form.get("pass") or "")
                if not username or not password:
                    return self._json(400, {
                        "status": "error",
                        "error": "username and password are required"
                    })
                user = store.verify_user(username, password)
                if not user:
                    store.audit(None, "auth_failed", f"Failed authentication attempt for username '{username}'")
                    return self._json(401, {
                        "status": "error",
                        "error": "Invalid username or password"
                    })
                token, ttl = store.create_auth_token(user["username"])
                store.audit(None, "auth_success", f"User '{user['username']}' authenticated successfully")
                return self._json(200, {
                    "status": "success",
                    "message": "Authentication successful",
                    "token": token,
                    "token_type": "Bearer",
                    "expires_in": ttl,
                    "api_key": CFG.get("api_key", ""),
                    "user": {
                        "username": user["username"],
                        "role": user.get("role", "admin"),
                        "full_name": user.get("full_name", user["username"]),
                    }
                })
            if path in ("/api/authorize", "/api/authorization", "/api/verify"):
                return self._handle_authorize(form=form, params=params)
            if path in ("/api/alerts/update", "/api/update", "/api/updaterecords", "/api/update_records", "/api/records/update"):
                code, body = workflow.handle_update_records(form)
                if isinstance(body, dict):
                    api_key = self._extract_token(form)
                    caller = self._resolve_caller(api_key) or {
                        "username": "api_admin",
                        "role": "admin",
                        "auth_type": "api_key"
                    }
                    body["authorized"] = True
                    body["auth"] = {
                        "status": "authorized",
                        "user": caller.get("username", "api_admin"),
                        "role": caller.get("role", "admin"),
                        "api_key_valid": True,
                        "auth_type": caller.get("auth_type", "api_key")
                    }
                return self._json(code, body)
            if path in ("/api/alerts", "/api/pushrecords", "/api/push_records", "/api/push-records"):
                code, body = workflow.handle_push_records(form)
                if isinstance(body, dict):
                    api_key = self._extract_token(form)
                    caller = self._resolve_caller(api_key) or {
                        "username": "api_admin",
                        "role": "admin",
                        "auth_type": "api_key"
                    }
                    body["authorized"] = True
                    body["auth"] = {
                        "status": "authorized",
                        "user": caller.get("username", "api_admin"),
                        "role": caller.get("role", "admin"),
                        "api_key_valid": True,
                        "auth_type": caller.get("auth_type", "api_key")
                    }
                return self._json(code, body)
            if path in ("/api/alerts/intake", "/api/alert_intake", "/api/alerts", "/api/alert"):
                code, body = workflow.handle_alert_intake(form)
                if isinstance(body, dict):
                    api_key = self._extract_token(form)
                    caller = self._resolve_caller(api_key) or {
                        "username": "api_admin",
                        "role": "admin",
                        "auth_type": "api_key"
                    }
                    body["authorized"] = True
                    body["auth"] = {
                        "status": "authorized",
                        "user": caller.get("username", "api_admin"),
                        "role": caller.get("role", "admin"),
                        "api_key_valid": True,
                        "auth_type": caller.get("auth_type", "api_key")
                    }
                return self._json(code, body)
            if path == "/api/events":
                wants = _wants_wait(params) and str(form.get("event_type", "")).upper() in (
                    "ASSIGNMENT_CREATED", "REASSIGNED")
                options = _wait_options(params) if wants else None
                code, body = workflow.ingest_event(form)
                if code == 202 and wants:
                    tid, aid = body["ticket_id"], body["ticket"]["assignment_id"]
                    return self._json_waiting(tid, lambda: _wait(tid, aid, options, body))
                return self._json(code, body)
            m = TICKET_PATH.match(path)
            if m and m.group(2) == "/followup":
                options = _wait_options(params, 120) if _wants_wait(params) else None
                code, body = workflow.trigger_followup(urllib.parse.unquote(m.group(1)).upper(),
                                                       form.get("assignment_id") if isinstance(form, dict) else None)
                if code == 202 and options:
                    return self._json_waiting(body["ticket_id"], lambda: dict(
                        body, **workflow.wait_for_call(body["call_id"], options[1])))
                return self._json(code, body)
            if path in ("/api/emergency", "/api/emergency_api"):
                options = _wait_options(params) if _wants_wait(params) else None
                code, body = emergency_call(form)
                if code == 202 and options:
                    tid = body["ticket_id"]
                    t = store.get_ticket(tid)
                    aid = (t and t.get("assignment_id")) or body.get("assignment_id")
                    return self._json_waiting(tid, lambda: _wait(tid, aid, options, body))
                return self._json(code, body)
            if path in ("/api/emergency_close", "/api/emergency/close", "/api/emergency_closure"):
                code, body = emergency_close_call(form)
                return self._json(code, body)
            if path == "/api/call":
                options = _wait_options(params) if _wants_wait(params) else None
                code, body = legacy_call(form)
                if code == 202 and options:
                    tid, aid, status_url = body["ticket_id"], body["assignment_id"], body["status_url"]

                    def compute():
                        waited = _wait(tid, aid, options, {})
                        return dict(legacy_view(tid), **{k: v for k, v in waited.items() if k != "ticket"},
                                    status_url=status_url)
                    return self._json_waiting(tid, compute)
                return self._json(code, body)
            if path == "/trigger":
                legacy_call(form)
                self.send_response(303)
                self.send_header("Location", "/")
                return self.end_headers()
            cid, retry = int(params.get("cid", 0)), int(params.get("r", 0))
            if path == "/twilio/voice":
                return self._send(200, workflow.twiml_voice(cid, retry), "text/xml")
            if path == "/twilio/lang":
                return self._send(200, workflow.twiml_lang(cid, retry, form.get("Digits", "")), "text/xml")
            if path == "/twilio/gather":
                return self._send(200, workflow.twiml_gather(cid, retry, form.get("Digits", "")), "text/xml")
            if path == "/twilio/reject_reason":
                return self._send(200, workflow.twiml_reject_reason(cid, form.get("Digits", "")), "text/xml")
            if path == "/twilio/call-status":
                workflow.call_status(cid, form)
                return self._send(204, "")
            self._json(404, {"error": "not found"})
        except workflow.EventError as e:
            self._json(400, {"error": str(e)})

    def log_message(self, fmt, *args):
        pass


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # a second copy on the same port fails loudly instead of sharing requests
    daemon_threads = True


def start_cloudflared(port):
    exe = "cloudflared"
    local_cf = os.path.join(os.path.dirname(__file__), "cloudflared.exe")
    if os.path.exists(local_cf):
        exe = local_cf
    proc = subprocess.Popen([exe, "tunnel", "--url", f"http://localhost:{port}"],
                            stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", proc.stderr.readline() or "")
        if m:
            threading.Thread(target=lambda: [None for _ in proc.stderr], daemon=True).start()
            return m.group(0)
    raise RuntimeError("cloudflared did not return a public URL; set PUBLIC_BASE_URL in .env instead")


def main():
    store.connect()
    try:
        import pg_store
        if pg_store.init_db():
            print(f"PostgreSQL DB    : Connected ({CFG.get('pg_name')}@{CFG.get('pg_host')}:{CFG.get('pg_port')})", flush=True)
    except Exception as e:
        print(f"PostgreSQL DB    : Offline or disabled ({e})", flush=True)
    try:
        server = Server(("0.0.0.0", CFG["port"]), Handler)
    except OSError:
        raise SystemExit(f"Port {CFG['port']} is already in use - is app.py already running?") from None
    if not CFG["dry_run"]:
        missing = [k for k, v in (("TWILIO_ACCOUNT_SID", CFG["sid"]), ("TWILIO_AUTH_TOKEN", CFG["token"]),
                                  ("TWILIO_FROM_NUMBER", CFG["from"])) if not v]
        if missing:
            raise SystemExit(f"Missing in .env: {', '.join(missing)}  (or set DRY_RUN=1 to test locally)")
        if not CFG["base_url"]:
            print("Starting cloudflared tunnel for Twilio webhooks...", flush=True)
            CFG["base_url"] = start_cloudflared(CFG["port"])
    workflow.Scheduler().start()
    base = CFG["base_url"] or f"http://localhost:{CFG['port']}"
    print(f"Public base URL  : {CFG['base_url'] or '(dry run)'}")
    print(f"Dashboard        : http://localhost:{CFG['port']}  (this PC only)")
    print(f"Auth API         : POST {base}/api/authenticate (and /api/authorize)")
    print(f"Event API        : POST {base}/api/events")
    print(f"Alerts / Push API: POST {base}/api/alerts")
    print(f"Update API       : PUT  {base}/api/alerts/update")
    print(f"Emergency API    : POST {base}/api/emergency_api")
    print(f"Emergency Close  : POST {base}/api/emergency_close")
    print(f"Follow-up API    : POST {base}/api/tickets/<ticket_id>/followup")
    print(f"Status API       : GET  {base}/api/tickets/<ticket_id>")
    print(f"Reload API       : GET/POST {base}/api/reload")
    print(f"API key header   : X-API-Key ({'set from .env' if CFG['api_key'] else 'NOT SET - API is local-only'})",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
