
def docs_html():
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NextAsk Gateway - API Documentation</title>
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
    modules = [config, texts, ae_whatsapp, queries, workflow]
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
    Triggers emergency voice call to technician,
    and sends WhatsApp alert messages to team leader and manager.
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
  <title>Login · NextAsk Voice Platform</title>
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
    <h1>NextAsk Voice Platform</h1>
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
    tickets = queries.list_tickets({"limit": 50})
    total_tickets = len(tickets)
    ack_count = 0
    calling_count = 0
    escalated_count = 0

    cards = ""
    for v in tickets:
        cw = v.get("call_workflow") or {}
        state = (cw.get("state") or "-").upper()
        is_ack = v.get("acknowledged") or state in ("ACKNOWLEDGED", "TL_REACHED", "MANAGER_REACHED")
        
        has_active_call = any(c.get("status") in ("queued", "ringing", "in-progress") for c in v.get("calls", []))
        is_calling = has_active_call or "CALL" in state

        if is_ack:
            ack_count += 1
            filter_cat = "ack"
            cls = "status-ok"
            status_icon = "✓"
        elif is_calling:
            calling_count += 1
            filter_cat = "calling"
            cls = "status-calling"
            status_icon = "●"
        else:
            escalated_count += 1
            filter_cat = "escalated"
            cls = "status-bad" if "UNRESOLVED" in state or "NO_CONTACTS" in state else "status-warn"
            status_icon = "!"

        severity = str(v.get("severity") or "Normal").capitalize()
        sev_cls = "sev-critical" if severity.lower() == "critical" else ("sev-major" if severity.lower() == "major" else "sev-normal")

        calls_html = ""
        for c in v.get("calls", []):
            role_title = f"{safe_esc(c.get('role', 'Technician')).capitalize()} #{c.get('attempt_no', 1)}"
            c_status = safe_esc(c.get("status") or "unknown")
            c_lang = safe_esc(c.get("language") or "-")
            c_dtmf = safe_esc(str(c.get("dtmf") or "-"))
            c_resp = safe_esc(c.get("response") or "-")
            c_phone = safe_esc(c.get("to_number") or "")
            superseded = " <span class='badge-superseded'>superseded</span>" if c.get("superseded") else ""

            calls_html += f"""
            <div class="call-row">
              <div class="call-role"><span class="role-badge">{role_title}</span> <span class="phone-number">📞 {c_phone}</span></div>
              <div class="call-meta">
                <span class="meta-tag">Status: <b>{c_status}</b></span>
                <span class="meta-tag">Lang: <b>{c_lang}</b></span>
                <span class="meta-tag">Key: <b>{c_dtmf}</b></span>
                <span class="meta-tag response-tag">Resp: <b>{c_resp}</b></span>
                {superseded}
              </div>
            </div>"""

        time_str = safe_esc(v.get("last_event_time") or v.get("created_at") or "-")
        search_blob = f"{safe_esc(v.get('ticket_id')).lower()} {safe_esc(v.get('site_name')).lower()} {safe_esc(v.get('site_number')).lower()} {safe_esc(v.get('alert_id')).lower()}"

        cards += f"""
        <div class="ticket-card" data-category="{filter_cat}" data-search="{search_blob}">
          <div class="card-header">
            <div class="ticket-primary">
              <span class="ticket-id">{safe_esc(v.get('ticket_id') or 'INC000')}</span>
              <span class="site-badge">🏢 {safe_esc(v.get('site_number'))} {safe_esc(v.get('site_name') or 'Main Site')}</span>
              <span class="sev-badge {sev_cls}">{severity}</span>
              <span class="sub-badge">Alert: {safe_esc(v.get('alert_id') or '-')}</span>
              <span class="sub-badge">Assign: {safe_esc(v.get('assignment_id') or '-')}</span>
            </div>
            <div class="status-pill {cls}">
              <span class="status-dot">{status_icon}</span> {safe_esc(state)}
            </div>
          </div>
          <div class="card-body">
            <div class="card-summary">
              <div><b>Summary:</b> {safe_esc(cw.get('response_summary') or 'No call response yet.')}</div>
              <div class="followup-info">Follow-up: <b>{safe_esc(v.get('followup', {}).get('state') or 'None')}</b></div>
            </div>
            <div class="calls-timeline">
              {calls_html or '<div class="no-calls">No outbound call records logged yet for this ticket.</div>'}
            </div>
          </div>
          <div class="card-footer">
            <span>🕒 Event Timestamp: <b>{time_str}</b></span>
            <span>Ticket Status: <b>{safe_esc(v.get('status') or 'Active')}</b></span>
          </div>
        </div>"""

    ack_rate = f"{int((ack_count / total_tickets) * 100)}%" if total_tickets > 0 else "0%"
    mode = "DRY RUN (Simulator)" if CFG["dry_run"] else f"LIVE GATEWAY · {safe_esc(CFG.get('base_url') or 'Connected')}"
    lang_options = "".join(f"<option value='{c}'>{texts.LANGS[c]['name']}</option>" for c in texts.LANGS)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NextAsk Voice Platform · Live Operations Console</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
  <style>
    :root {{
      --primary: #0969da;
      --primary-hover: #0856b3;
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --border: #e2e8f0;
      --text: #0f172a;
      --text-muted: #64748b;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --purple: #8b5cf6;
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
    header {{
      background: #0f172a;
      color: #fff;
      padding: 16px 32px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.1);
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .brand-icon {{
      width: 40px;
      height: 40px;
      border-radius: 10px;
      background: linear-gradient(135deg, #38bdf8, #2563eb);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      box-shadow: 0 0 16px rgba(56, 189, 248, 0.4);
    }}
    .brand-title {{
      font-size: 17px;
      font-weight: 700;
      letter-spacing: -0.01em;
      color: #f8fafc;
    }}
    .brand-subtitle {{
      font-size: 12px;
      color: #94a3b8;
    }}
    .header-actions {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .live-pulse {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid rgba(16, 185, 129, 0.3);
      color: #34d399;
      padding: 6px 12px;
      border-radius: 20px;
      font-size: 12px;
      font-weight: 600;
    }}
    .pulse-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #10b981;
      animation: pulse 1.8s infinite;
    }}
    @keyframes pulse {{
      0% {{ transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }}
      70% {{ transform: scale(1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }}
      100% {{ transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }}
    }}
    .btn-header {{
      background: rgba(255, 255, 255, 0.1);
      border: 1px solid rgba(255, 255, 255, 0.2);
      color: #fff;
      padding: 7px 14px;
      border-radius: 6px;
      text-decoration: none;
      font-size: 13px;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
      transition: background 0.2s ease;
    }}
    .btn-header:hover {{
      background: rgba(255, 255, 255, 0.2);
    }}
    .btn-logout {{
      background: rgba(239, 68, 68, 0.2);
      border-color: rgba(239, 68, 68, 0.4);
      color: #fca5a5;
    }}
    .btn-logout:hover {{
      background: rgba(239, 68, 68, 0.35);
      color: #fff;
    }}
    main {{
      max-width: 1200px;
      margin: 24px auto;
      padding: 0 20px;
      flex: 1;
      width: 100%;
    }}
    /* Metric Cards Grid */
    .metrics-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }}
    .metric-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
      position: relative;
      overflow: hidden;
      cursor: pointer;
      transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease;
    }}
    .metric-card:hover {{
      transform: translateY(-2px);
      box-shadow: 0 6px 16px rgba(0,0,0,0.08);
      border-color: #cbd5e1;
    }}
    .metric-card::before {{
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 4px;
    }}
    .metric-blue::before {{ background: #2563eb; }}
    .metric-green::before {{ background: #10b981; }}
    .metric-amber::before {{ background: #f59e0b; }}
    .metric-purple::before {{ background: #8b5cf6; }}
    .metric-label {{
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 8px;
    }}
    .metric-value-row {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
    }}
    .metric-number {{
      font-size: 32px;
      font-weight: 800;
      color: var(--text);
      letter-spacing: -0.02em;
    }}
    .metric-badge {{
      font-size: 12px;
      font-weight: 700;
      padding: 4px 10px;
      border-radius: 20px;
    }}
    .badge-blue {{ background: #eff6ff; color: #1d4ed8; }}
    .badge-green {{ background: #ecfdf5; color: #047857; }}
    .badge-amber {{ background: #fffbeb; color: #b45309; }}
    .badge-purple {{ background: #f5f3ff; color: #6d28d9; }}
    /* Dispatch Bar */
    .dispatch-box {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      margin-bottom: 24px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
      overflow: hidden;
    }}
    .dispatch-header {{
      padding: 16px 20px;
      background: #f1f5f9;
      display: flex;
      justify-content: space-between;
      align-items: center;
      cursor: pointer;
      user-select: none;
    }}
    .dispatch-title {{
      font-size: 14px;
      font-weight: 700;
      color: #1e293b;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .dispatch-body {{
      padding: 20px;
      display: none;
    }}
    .dispatch-form {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      align-items: end;
    }}
    .form-group {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .form-group label {{
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
    }}
    .form-group input, .form-group select {{
      padding: 9px 12px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      font-size: 13px;
      font-family: inherit;
    }}
    .btn-dispatch {{
      background: #0969da;
      color: #fff;
      border: none;
      padding: 10px 18px;
      border-radius: 6px;
      font-weight: 600;
      font-size: 13px;
      cursor: pointer;
      height: 40px;
      transition: background 0.15s ease;
    }}
    .btn-dispatch:hover {{
      background: #0856b3;
    }}
    /* Tabs & Filter Bar */
    .controls-bar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      margin-bottom: 20px;
    }}
    .filter-pills {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .filter-btn {{
      background: #fff;
      border: 1px solid var(--border);
      color: #334155;
      padding: 9px 16px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s ease;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    .filter-btn:hover {{
      background: #f1f5f9;
      border-color: #cbd5e1;
    }}
    .filter-btn.active {{
      background: #0969da !important;
      color: #ffffff !important;
      border-color: #0969da !important;
      box-shadow: 0 2px 6px rgba(9, 105, 218, 0.35);
    }}
    .search-box {{
      position: relative;
      min-width: 280px;
    }}
    .search-input {{
      width: 100%;
      padding: 9px 14px 9px 36px;
      border: 1px solid var(--border);
      border-radius: 8px;
      font-size: 13px;
      font-family: inherit;
      background: #fff;
    }}
    .search-icon {{
      position: absolute;
      left: 12px;
      top: 50%;
      transform: translateY(-50%);
      color: #94a3b8;
      font-size: 14px;
    }}
    /* Ticket Cards */
    .ticket-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      margin-bottom: 16px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.04);
      transition: box-shadow 0.2s ease, border-color 0.2s ease;
    }}
    .ticket-card:hover {{
      box-shadow: 0 4px 12px rgba(0,0,0,0.08);
      border-color: #cbd5e1;
    }}
    .card-header {{
      padding: 16px 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
      border-bottom: 1px solid #f1f5f9;
    }}
    .ticket-primary {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .ticket-id {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 15px;
      font-weight: 700;
      color: #0f172a;
    }}
    .site-badge {{
      background: #f1f5f9;
      color: #334155;
      padding: 4px 10px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 600;
    }}
    .sev-badge {{
      font-size: 11px;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 4px;
      text-transform: uppercase;
    }}
    .sev-critical {{ background: #fee2e2; color: #991b1b; }}
    .sev-major {{ background: #fef3c7; color: #92400e; }}
    .sev-normal {{ background: #e0f2fe; color: #075985; }}
    .sub-badge {{
      font-size: 11px;
      color: #64748b;
    }}
    .status-pill {{
      padding: 5px 12px;
      border-radius: 20px;
      font-size: 12px;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    .status-ok {{ background: #dcfce7; color: #15803d; }}
    .status-calling {{ background: #dbeafe; color: #1e40af; }}
    .status-warn {{ background: #fef3c7; color: #b45309; }}
    .status-bad {{ background: #fee2e2; color: #b91c1c; }}
    .card-body {{
      padding: 16px 20px;
    }}
    .card-summary {{
      font-size: 13.5px;
      color: #334155;
      margin-bottom: 14px;
      display: flex;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .followup-info {{
      font-size: 12px;
      color: #64748b;
    }}
    .calls-timeline {{
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .call-row {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      padding: 10px 14px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      font-size: 12.5px;
    }}
    .call-role {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .role-badge {{
      background: #0f172a;
      color: #fff;
      padding: 2px 8px;
      border-radius: 4px;
      font-weight: 600;
      font-size: 11px;
    }}
    .phone-number {{
      font-family: 'JetBrains Mono', monospace;
      color: #334155;
      font-weight: 500;
    }}
    .call-meta {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .meta-tag {{
      color: #64748b;
    }}
    .meta-tag b {{
      color: #0f172a;
    }}
    .response-tag b {{
      color: #0969da;
    }}
    .badge-superseded {{
      background: #f1f5f9;
      color: #94a3b8;
      font-style: italic;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 11px;
    }}
    .no-calls {{
      font-size: 13px;
      color: #94a3b8;
      font-style: italic;
      padding: 8px 0;
    }}
    .card-footer {{
      padding: 10px 20px;
      background: #f8fafc;
      border-top: 1px solid #f1f5f9;
      border-bottom-left-radius: 12px;
      border-bottom-right-radius: 12px;
      display: flex;
      justify-content: space-between;
      font-size: 11.5px;
      color: #64748b;
    }}
    .empty-state {{
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 48px 24px;
      text-align: center;
      color: #64748b;
      font-size: 14px;
      display: none;
    }}
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="brand-icon">⚡</div>
      <div>
        <div class="brand-title">NextAsk Voice Operations Platform</div>
        <div class="brand-subtitle">{mode} · Attempts {CFG['tech_max_attempts']} · Reject Key {CFG['default_reject_digit']}</div>
      </div>
    </div>
    <div class="header-actions">
      <div class="live-pulse">
        <span class="pulse-dot"></span> Live Console
      </div>
      <button onclick="window.location.reload()" class="btn-header" title="Refresh data">🔄 Refresh</button>
      <a href="/api/logs/download" class="btn-header" download>📥 Export Logs</a>
      <a href="/logout" class="btn-header btn-logout">🚪 Logout</a>
    </div>
  </header>

  <main>
    <!-- Executive KPI Metrics (Clickable) -->
    <div class="metrics-grid">
      <div class="metric-card metric-blue" onclick="switchTab('all')" title="Click to view all tickets">
        <div class="metric-label">Total Dispatch Tickets</div>
        <div class="metric-value-row">
          <div class="metric-number">{total_tickets}</div>
          <span class="metric-badge badge-blue">View All</span>
        </div>
      </div>

      <div class="metric-card metric-green" onclick="switchTab('ack')" title="Click to view acknowledged tickets">
        <div class="metric-label">Calls Acknowledged</div>
        <div class="metric-value-row">
          <div class="metric-number">{ack_count}</div>
          <span class="metric-badge badge-green">{ack_rate} Rate</span>
        </div>
      </div>

      <div class="metric-card metric-amber" onclick="switchTab('calling')" title="Click to view active dialing tickets">
        <div class="metric-label">Active / Dialing</div>
        <div class="metric-value-row">
          <div class="metric-number">{calling_count}</div>
          <span class="metric-badge badge-amber">IVR Calling</span>
        </div>
      </div>

      <div class="metric-card metric-purple" onclick="switchTab('escalated')" title="Click to view escalated tickets">
        <div class="metric-label">Escalated / Pending</div>
        <div class="metric-value-row">
          <div class="metric-number">{escalated_count}</div>
          <span class="metric-badge badge-purple">Tier 1 & 2</span>
        </div>
      </div>
    </div>

    <!-- Manual Dispatch Collapsible Box -->
    <div class="dispatch-box">
      <div class="dispatch-header" onclick="toggleDispatch()">
        <div class="dispatch-title">
          <span>⚡ Manual Outbound Voice Dispatcher</span>
          <span style="font-size:11px;font-weight:500;color:#64748b;">(Click to test call flow)</span>
        </div>
        <span id="dispatch-toggle-icon" style="font-size:12px;color:#64748b;">▼ Open</span>
      </div>
      <div class="dispatch-body" id="dispatch-body">
        <form method="POST" action="/trigger" class="dispatch-form">
          <div class="form-group">
            <label>Alert ID</label>
            <input name="alert_id" value="ALM1001" required>
          </div>
          <div class="form-group">
            <label>Ticket ID</label>
            <input name="ticket_id" placeholder="INC0012345" required>
          </div>
          <div class="form-group">
            <label>Site Details</label>
            <input name="site_details" value="Site 2040, Olaya Branch, Riyadh">
          </div>
          <div class="form-group">
            <label>Language</label>
            <select name="language">
              <option value="ask">Ask in Call (IVR Menu)</option>
              {lang_options}
            </select>
          </div>
          <div class="form-group">
            <label>Ack Key</label>
            <input name="ack_digit" value="1" style="max-width:80px;">
          </div>
          <div class="form-group">
            <label>Technician Phone</label>
            <input name="phone_number" placeholder="+9665XXXXXXXX" required>
          </div>
          <div class="form-group">
            <label>Team Leader Phone</label>
            <input name="team_leader_number" placeholder="Optional">
          </div>
          <div class="form-group">
            <label>Manager Phone</label>
            <input name="manager_number" placeholder="Optional">
          </div>
          <button type="submit" class="btn-dispatch">⚡ Dial Call</button>
        </form>
      </div>
    </div>

    <!-- Tabs & Filter Bar -->
    <div class="controls-bar" id="ticketsSection">
      <div class="filter-pills">
        <button type="button" id="tab-btn-all" class="filter-btn active" onclick="switchTab('all')">📋 All Tickets ({total_tickets})</button>
        <button type="button" id="tab-btn-ack" class="filter-btn" onclick="switchTab('ack')">✅ Acknowledged ({ack_count})</button>
        <button type="button" id="tab-btn-calling" class="filter-btn" onclick="switchTab('calling')">📞 Dialing ({calling_count})</button>
        <button type="button" id="tab-btn-escalated" class="filter-btn" onclick="switchTab('escalated')">⚠️ Escalated ({escalated_count})</button>
      </div>
      <div class="search-box">
        <span class="search-icon">🔍</span>
        <input type="text" id="searchInput" class="search-input" placeholder="Search ticket, site, alert ID..." oninput="runFilter()">
      </div>
    </div>

    <!-- Empty State for filtered results -->
    <div id="emptyCategoryMsg" class="empty-state">
      No tickets found matching the selected filter or search keyword.
    </div>

    <!-- Tickets Feed -->
    <div id="ticketsList">
      {cards or '<div class="empty-state" style="display:block;">No active tickets in queue. Use Alert Intake API (/api/alerts) or the manual dispatcher above.</div>'}
    </div>
  </main>

  <script>
    function toggleDispatch() {{
      var b = document.getElementById('dispatch-body');
      var icon = document.getElementById('dispatch-toggle-icon');
      if (b.style.display === 'block') {{
        b.style.display = 'none';
        icon.innerText = '▼ Open';
      }} else {{
        b.style.display = 'block';
        icon.innerText = '▲ Close';
      }}
    }}

    var activeCategory = 'all';

    function switchTab(cat) {{
      activeCategory = cat;
      try {{
        sessionStorage.setItem('flm_active_tab', cat);
      }} catch(e) {{}}

      document.querySelectorAll('.filter-btn').forEach(function(btn) {{
        btn.classList.remove('active');
      }});

      var activeBtn = document.getElementById('tab-btn-' + cat);
      if (activeBtn) {{
        activeBtn.classList.add('active');
      }}

      runFilter();
    }}

    function runFilter() {{
      var q = (document.getElementById('searchInput').value || '').toLowerCase().trim();
      var cards = document.querySelectorAll('.ticket-card');
      var visible = 0;

      cards.forEach(function(card) {{
        var cat = card.getAttribute('data-category');
        var text = (card.getAttribute('data-search') || '').toLowerCase();
        var catMatch = (activeCategory === 'all' || cat === activeCategory);
        var searchMatch = (!q || text.indexOf(q) !== -1);

        if (catMatch && searchMatch) {{
          card.style.display = 'block';
          visible++;
        }} else {{
          card.style.display = 'none';
        }}
      }});

      var emptyEl = document.getElementById('emptyCategoryMsg');
      if (emptyEl) {{
        emptyEl.style.display = (visible === 0) ? 'block' : 'none';
      }}
    }}

    // Restore saved tab if user previously selected one
    try {{
      var savedTab = sessionStorage.getItem('flm_active_tab');
      if (savedTab && ['all', 'ack', 'calling', 'escalated'].indexOf(savedTab) !== -1) {{
        switchTab(savedTab);
      }}
    }} catch(e) {{}}
  </script>
</body>
</html>"""


# --------------------------------------------------------------------------- synchronous wait
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
        return url.path.rstrip("/") or "/", params, form

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
    print(f"Alert Intake API : POST {base}/api/alerts/intake")
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
