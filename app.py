"""
FLM Helpdesk - voice call platform (Twilio). Implements the call side of "FLM Helpdesk Final PoC Workflows":
UC1 technician notification + acknowledgement, 2nd attempt, Team Leader / Manager escalation calls,
15-minute follow-up reminder (triggered by the caller), contact guard, deduplication and a call audit trail.
WhatsApp is out of scope (handled by another system - use the outbound webhook events to drive it).

Run:  python app.py      (standard library only)
API:  see "FLM Voice Call API Guide"; every /api/* call needs header X-API-Key.
"""
import json
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

import queries
import store
import texts
import twilio_client
import workflow
from config import CFG


# --------------------------------------------------------------------------- legacy single-call API (/api/call)
def legacy_call(form):
    """POST /api/call {alert_id, ticket_id, phone_number, site_details, team_leader_number?, manager_number?,
    language?, technician_language?, team_leader_language?, manager_language?, technician_name?,
    team_leader_name?, manager_name?, callback_url?, description?, ack_digit?} -> mapped onto an ASSIGNMENT_CREATED event. language is the default for everyone; the
    per-person *_language fields override it (en, ar, ur, hi, or ask = menu)."""
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
                   "site_name": form["site_details"], "description": form.get("description"), "status": "Assigned"},
        "ack_digit": form.get("ack_digit") or "1", "reject_digit": form.get("reject_digit"),
        "callback_url": form.get("callback_url"), "recipients": recipients,
    }
    workflow.ingest_event(event)
    return 202, dict(legacy_view(tid), status_url=f"/api/call/{tid}")


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
ul{{margin:8px 0;padding-left:18px}} small{{color:#889}}</style></head><body><header><b>FLM Helpdesk · Voice call platform</b><br>
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

    def _authorized(self, path, form):
        if path.startswith("/twilio/"):
            if CFG["dry_run"] or not CFG["validate_twilio_signature"]:
                return True
            ok = twilio_client.valid_signature(CFG["base_url"] + self.path, form or {},
                                               self.headers.get("X-Twilio-Signature", ""))
            if not ok:
                store.audit(None, "twilio_signature_rejected", f"Rejected unsigned/forged request to {self.path}")
            return ok
        if path == "/api/health":
            return True
        if path.startswith("/api/"):
            if CFG["api_key"]:
                return self.headers.get("X-API-Key") == CFG["api_key"]
            return self._from_this_pc()
        return self._from_this_pc()  # dashboard and its call form: this PC only

    def do_GET(self):
        path, params, _ = self._parse()
        if not self._authorized(path, {}):
            return self._json(401, {"error": "unauthorized - send header X-API-Key"})
        try:
            if path == "/":
                return self._send(200, dashboard_html())
            if path == "/api/health":
                return self._json(200, {"status": "ok", "dry_run": CFG["dry_run"], "time": store.now_iso()})
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
            if path.startswith("/api/call/"):
                v = legacy_view(urllib.parse.unquote(path[len("/api/call/"):]).upper())
                return self._json(200, v) if v else self._json(404, {"error": "ticket not found"})
            self._json(404, {"error": "not found"})
        except ValueError as e:
            self._json(400, {"error": str(e)})

    def do_POST(self):
        path, params, form = self._parse()
        if not self._authorized(path, form):
            if path.startswith("/twilio/"):
                return self._send(403, "invalid Twilio signature", "text/plain")
            return self._json(401, {"error": "unauthorized - send header X-API-Key"})
        if form is None:
            return self._json(400, {"error": "body is not valid JSON"})
        try:
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
    proc = subprocess.Popen(["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
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
    print(f"Event API        : POST {base}/api/events")
    print(f"Follow-up API    : POST {base}/api/tickets/<ticket_id>/followup")
    print(f"Status API       : GET  {base}/api/tickets/<ticket_id>")
    print(f"API key header   : X-API-Key ({'set from .env' if CFG['api_key'] else 'NOT SET - API is local-only'})",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
