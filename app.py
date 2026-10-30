
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
def dashboard_html():
    cards = ""
    for v in queries.list_tickets({"limit": 25}):
        cw = v["call_workflow"]
        calls = "".join(
            f"<li>{escape(c['role'])} #{c['attempt_no']} <small>{escape(c['workflow'])}</small> &rarr; "
            f"{escape(c['to_number'] or '')} | {escape(c['status'] or '')} | lang {escape(c['language'] or '-')}"
            f" | key {escape(c['dtmf'] or '-')} | <b>{escape(c['response'])}</b>"
            f"{' <i>(superseded)</i>' if c['superseded'] else ''}</li>" for c in v["calls"])
        state = cw["state"] or "-"
        cls = "ok" if state in ("ACKNOWLEDGED", "TL_REACHED", "MANAGER_REACHED") else \
            ("bad" if "UNRESOLVED" in state or "NO_CONTACTS" in state else "run")
        cards += (f"<div class=card><div class=hd><b>{escape(v['ticket_id'])}</b> {escape(v['site_number'] or '')} "
                  f"{escape(v['site_name'] or '')} · assignment {escape(v['assignment_id'] or '-')} · "
                  f"ticket {escape(v['status'] or '-')}<span class='st {cls}'>{escape(state)}</span></div>"
                  f"<div class=sub>{escape(cw['response_summary'])} · follow-up: {escape(v['followup']['state'] or '-')}"
                  f"</div><ul>{calls}</ul></div>")
    mode = "DRY RUN" if CFG["dry_run"] else f"LIVE · {escape(CFG['base_url'] or 'no public URL')}"
    options = "".join(f"<option value={c}>{texts.LANGS[c]['name']}</option>" for c in texts.LANGS)
    return f"""<!doctype html><html><head><meta charset=utf-8><meta http-equiv=refresh content=5>
<title>FLM Voice Platform</title><style>
body{{font:14px system-ui;margin:0;background:#f4f6fa;color:#1d2433}} header{{background:#0b3a75;color:#fff;padding:14px 24px}}
main{{max-width:1040px;margin:20px auto;padding:0 16px}} form{{background:#fff;padding:14px;border-radius:8px;display:flex;
flex-wrap:wrap;gap:10px;align-items:end}} label{{display:flex;flex-direction:column;font-size:12px;color:#556}}
input,select{{padding:6px;font:inherit}} button{{background:#f58220;color:#fff;border:0;padding:8px 16px;border-radius:6px;font-weight:600}}
.card{{background:#fff;border-radius:8px;padding:12px 16px;margin-top:12px}} .hd{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.st{{margin-left:auto;padding:2px 8px;border-radius:10px;font-size:12px}} .ok{{background:#d8f3e0;color:#17643a}}
.bad{{background:#fde0e0;color:#9b1c1c}} .run{{background:#e5ecfb;color:#1d4ed8}} .sub{{color:#556;font-size:12px;margin-top:4px}}
ul{{margin:8px 0;padding-left:18px}} small{{color:#889}} .dl-btn{{background:#0b3a75;border:1px solid #fff;color:#fff;padding:4px 10px;border-radius:4px;text-decoration:none;font-size:12px;float:right}}</style></head><body><header><a href="/api/logs/download" class="dl-btn" download>📥 Download Log File</a><b>FLM Helpdesk · Voice call platform</b><br>
<small style=color:#cdd>{mode} · technician attempts {CFG['tech_max_attempts']} · reject key {CFG['default_reject_digit']}</small></header><main>
<form method=post action=/trigger>
<label>Alert ID<input name=alert_id value="ALM1001" size=9></label>
<label>Ticket ID<input name=ticket_id placeholder="INC0012345" size=11 required></label>
<label>Site<input name=site_details value="Site 2040, Olaya Branch, Riyadh" size=24></label>
<label>Language<select name=language><option value=ask>Ask (menu)</option>{options}</select></label>
<label>Ack key<input name=ack_digit value=1 size=2></label>
<label>Technician<input name=phone_number placeholder="+91..." required></label>
<label>Team leader<input name=team_leader_number placeholder="optional"></label>
<label>Manager<input name=manager_number placeholder="optional"></label>
<button>Call technician</button></form>{cards or '<p>No tickets yet.</p>'}</main></body></html>"""


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
                    "/api/authorize", "/api/authorization", "/api/verify", "/docs", "/api/docs", "/documentation"):
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
        return self._from_this_pc()  # dashboard and its call form: this PC only

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
            if path == "/":
                return self._send(200, dashboard_html())
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
