"""
Offline end-to-end test of the call platform. Runs in DRY_RUN mode with a temporary database and plays
Twilio's part by posting the same webhooks Twilio would. No real calls, no internet.

    python simulate_test.py
"""
import hashlib
import hmac
import itertools
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.update({
    "DRY_RUN": "1", "API_KEY": "test-key", "LANGUAGES": "en,ar,ur", "DEFAULT_LANGUAGE": "en",
    "TECH_MAX_ATTEMPTS": "2", "TECH_RETRY_INTERVAL_SEC": "1", "MANAGER_RETRY_INTERVAL_SEC": "1",
    "TL_MAX_ATTEMPTS": "1", "MANAGER_MAX_ATTEMPTS": "2", "FOLLOWUP_REMINDER_SEC": "0", "STATE_MAX_AGE_SEC": "0",
    "DEFAULT_REJECT_DIGIT": "9", "RESULT_WEBHOOK_URL": "", "PORT": "5055",
})
import app  # noqa: E402
import store  # noqa: E402
import twilio_client  # noqa: E402
import workflow  # noqa: E402
from config import CFG  # noqa: E402

store.connect(os.path.join(tempfile.mkdtemp(), "test.db"))
workflow.WEBHOOK_DELAYS = (0, 0.2, 0.2)
server = app.Server(("127.0.0.1", 5055), app.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
scheduler = workflow.Scheduler(tick=0.05)
scheduler.start()
BASE = "http://127.0.0.1:5055"
ids = itertools.count(1)
PASSED = []


# ----------------------------------------------------------------------------- helpers
def http(method, path, body=None, key="test-key", headers=None, form=False):
    h = dict(headers or {})
    if key:
        h["X-API-Key"] = key
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
        else:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw and "json" in r.headers.get("Content-Type", "") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def twilio(path, body):
    return http("POST", path, body, key=None, form=True)[1]


def event(event_type, ticket_id, **kw):
    ev = {"event_id": kw.pop("event_id", f"EVT-{next(ids)}"), "event_type": event_type,
          "ticket": dict({"ticket_id": ticket_id}, **kw.pop("ticket", {})), **kw}
    return http("POST", "/api/events", ev)


def assign(ticket_id, assignment="A1", tech="+911111111111", lang="en", ack="5", tl="+912222222222",
           mgr="+913333333333", **kw):
    recipients = [{"role": "technician", "name": "Ahmed", "mobile": tech, "language": lang}]
    if tl:
        recipients.append({"role": "team_leader", "name": "Imran", "mobile": tl, "language": "ur"})
    if mgr:
        recipients.append({"role": "manager", "name": "Sara", "mobile": mgr, "language": "en"})
    return event("ASSIGNMENT_CREATED", ticket_id, ack_digit=ack, recipients=recipients,
                 ticket={"assignment_id": assignment, "alarm_id": "ALM-77", "site_number": "2040",
                         "site_name": "Olaya Branch", "severity": "High", "status": "Assigned",
                         "alarm_status": "Active"}, **kw)


def wait_call(tid, role, attempt, wf="uc1_technician", timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        c = store.q("SELECT * FROM calls WHERE ticket_id=? AND role=? AND attempt_no=? AND workflow=? "
                    "AND superseded IS NULL ORDER BY id DESC", (tid, role, attempt, wf), one=True)
        if c and c["call_sid"]:
            return c["id"]
        time.sleep(0.05)
    raise AssertionError(f"{tid}: {role} {wf} call #{attempt} never placed")


def answer(cid, digits=None, lang_key=None):
    xml = twilio(f"/twilio/voice?cid={cid}", {"CallStatus": "in-progress"})
    if "/twilio/lang" in xml:
        assert lang_key, "menu played but no language key given"
        twilio(f"/twilio/lang?cid={cid}&r=0", {"Digits": lang_key})
        xml = twilio(f"/twilio/voice?cid={cid}", {})
    if digits:
        twilio(f"/twilio/gather?cid={cid}&r=0", {"Digits": digits})
    twilio(f"/twilio/call-status?cid={cid}", {"CallStatus": "completed", "CallDuration": "20"})
    return xml


def no_answer(cid, status="no-answer"):
    twilio(f"/twilio/call-status?cid={cid}", {"CallStatus": status, "CallDuration": "0", "SipResponseCode": "480"})


def state(tid, expected, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        s = store.get_ticket(tid)["uc1_state"]
        if s == expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"{tid}: expected {expected}, got {s}")


def view(tid):
    return http("GET", f"/api/tickets/{tid}")[1]


def calls_of(tid, wf=None):
    return store.q("SELECT * FROM calls WHERE ticket_id=?" + (" AND workflow=?" if wf else "") + " ORDER BY id",
                   (tid, wf) if wf else (tid,))


def wait_until_new_reminder(tid, count, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        rows = store.q("SELECT call_sid FROM calls WHERE ticket_id=? AND workflow='uc1_reminder'", (tid,))
        if len(rows) >= count and rows[-1]["call_sid"]:
            return True
        time.sleep(0.05)
    raise AssertionError(f"{tid}: reminder call #{count} never placed")


def ok(name):
    PASSED.append(name)
    print(f"PASS {len(PASSED):2d}: {name}", flush=True)


# ----------------------------------------------------------------------------- tests
# Validation + auth
assert http("POST", "/api/events", {"event_id": "X"}, key=None)[0] == 401
assert http("POST", "/api/events", {"event_id": "X", "event_type": "NOPE", "ticket": {"ticket_id": "T"}})[0] == 400
code, res = http("POST", "/api/events", {"event_id": "X", "event_type": "ASSIGNMENT_CREATED",
                                          "ticket": {"ticket_id": "T"}})
assert code == 400 and "assignment_id" in res["error"], res
code, res = assign("TV1", tech="9112381254")
assert code == 400 and "E.164" in res["error"], res
code, res = assign("TV1", lang="fr")
assert code == 400 and "language" in res["error"], res
assert http("GET", "/api/health", key=None)[0] == 200
ok("validation: 401 without key, 400 for bad type / missing assignment / bad mobile / bad language")

# UC1 p.2: language from the event (no menu), dynamic ack digit, acknowledged
code, res = assign("INC1", lang="ar", ack="5")
assert code == 202 and res["result"] == "accepted" and "tech_call #1" in res["actions"][0], res
cid = wait_call("INC1", "technician", 1)
xml = answer(cid, "5")
assert "/twilio/lang" not in xml and "Polly.Zeina" in xml and "اضغط 5" in xml and "اضغط 9" in xml, xml
state("INC1", "ACKNOWLEDGED")
v = view("INC1")
c = v["calls"][0]
assert c["language"] == "ar" and c["language_source"] == "nextask" and c["dtmf"] == "5", c
assert c["template"] == "uc1_technician_ack" and c["template_version"] == "v2" and c["ack_result"] == "valid", c
assert v["call_workflow"]["acknowledged_via"] == "voice" and v["followup"]["state"] == "AWAITING_TRIGGER", v
ok("ack: language from event (ar, no menu), dynamic digit 5, reject 9 announced, template/version audited")

# Duplicate event id + duplicate assignment + stale version
dup = {"event_id": "DUP-1", "event_type": "TICKET_UPDATED", "version": 5, "ticket": {"ticket_id": "INC1"}}
assert http("POST", "/api/events", dup)[1]["result"] == "accepted"
assert http("POST", "/api/events", dup)[1]["result"] == "duplicate"
code, res = event("TICKET_UPDATED", "INC1", version=3)
assert code == 200 and res["result"] == "stale" and "older" in res["note"], res
code, res = assign("INC1", assignment="A1", ack="5", lang="ar")
assert "suppressed" in res["actions"][0] and len(calls_of("INC1")) == 1, res
ok("dedup: same event_id -> duplicate; older version -> stale; same assignment again -> suppressed")

# p.4 follow-up triggered by the caller after 15 min
code, res = http("POST", "/api/tickets/INC1/followup", {})
assert code == 202 and res["result"] == "reminder_call_placed", res
rc = wait_call("INC1", "technician", 1, "uc1_reminder")
xml = answer(rc)
assert "Polly.Zeina" in xml and "تذكير" in xml, xml
time.sleep(0.2)
assert view("INC1")["followup"]["state"] == "REMINDER_CALLED"
assert calls_of("INC1", "uc1_reminder")[0]["outcome"] == "reminder_delivered"
ok("follow-up API: acknowledged + no operational status -> reminder call in recipient language")

assign("INC2", ack="1")
answer(wait_call("INC2", "technician", 1), "1")
state("INC2", "ACKNOWLEDGED")
event("OPERATIONAL_STATUS", "INC2", ticket={"operational_status": "Arrived"})
code, res = http("POST", "/api/tickets/INC2/followup", {})
assert code == 200 and res["result"] == "not_needed" and "Arrived" in res["reason"], res
assign("INC3")
answer(wait_call("INC3", "technician", 1), "5")
state("INC3", "ACKNOWLEDGED")
event("ALARM_STATUS", "INC3", ticket={"alarm_status": "Cleared"})
assert http("POST", "/api/tickets/INC3/followup", {})[1]["reason"] == "alarm cleared"
code, res = http("POST", "/api/tickets/INC3/followup", {"assignment_id": "OLD"})
assert "reassigned" in res["reason"], res
assign("INC4")
answer(wait_call("INC4", "technician", 1), "5")
event("TICKET_CLOSED", "INC4")
assert http("POST", "/api/tickets/INC4/followup", {})[1]["reason"] == "ticket closed"
assert http("POST", "/api/tickets/NOPE/followup", {})[0] == 404
assign("INC5")
code, res = http("POST", "/api/tickets/INC5/followup", {})
assert code == 409 and "not acknowledged" in res["reason"], res
ok("follow-up API: stops for Arrived / alarm cleared / reassigned / closed; 404 unknown; 409 before ack")

# p.2-3: attempt 1 not picked up -> attempt 2 (same language, no menu) -> ack
no_answer(wait_call("INC5", "technician", 1))
answer(wait_call("INC5", "technician", 2), "5")
state("INC5", "ACKNOWLEDGED")
c1 = calls_of("INC5")[0]
assert c1["outcome"] == "not_picked_up" and c1["cause_code"] == "480" and c1["picked_up"] == 0, c1
ok("2nd attempt: not picked up (cause 480 stored) -> retry after interval -> acknowledged")

# p.5: reject -> TL call (press key to confirm) -> TL reached -> no manager call
assign("INC6", lang="en")
answer(wait_call("INC6", "technician", 1), "9")
tl = wait_call("INC6", "team_leader", 1, "uc1_escalation")
xml = answer(tl, "5")
assert "हेल्पडेस्क एस्केलेशन" in xml and "मुस्तरद" in xml and "दूसरे टेक्नीशियन" in xml, xml
assert "हाँ के लिए 5 दबाइए" in xml and "नहीं के लिए 9 दबाइए" in xml and "नेक्स्ट टास्क" not in xml, xml
state("INC6", "TL_REACHED")
esc = view("INC6")["call_workflow"]["escalation"]
assert esc["reason"] == "rejected" and esc["team_leader_reached"], esc
assert esc["team_leader_decision"] == "assign_to_another_technician" == esc["reassign"], esc
assert not calls_of("INC6", "uc1_escalation")[1:], "manager must not be called"
ok("reject (9) -> TL called in Urdu, asked to reassign, pressed 5 (yes) -> TL_REACHED, no manager call")

# TL answers but presses nothing -> still reached (answered counts)
assign("INC7")
answer(wait_call("INC7", "technician", 1), "9")
answer(wait_call("INC7", "team_leader", 1, "uc1_escalation"))       # picks up, presses nothing
answer(wait_call("INC7", "manager", 1, "uc1_escalation"), "9")         # so the manager is called
state("INC7", "MANAGER_REACHED")
esc = view("INC7")["call_workflow"]["escalation"]
assert esc["team_leader_reached"] is True and esc["team_leader_decision"] is None, esc
assert esc["manager_decision"] == "do_not_reassign" == esc["reassign"], esc
assign("INC7C")
answer(wait_call("INC7C", "technician", 1), "9")
answer(wait_call("INC7C", "team_leader", 1, "uc1_escalation"))
answer(wait_call("INC7C", "manager", 1, "uc1_escalation"))
answer(wait_call("INC7C", "manager", 2, "uc1_escalation"))
state("INC7C", "ESCALATION_UNRESOLVED")
assign("INC7B")
answer(wait_call("INC7B", "technician", 1), "9")
answer(wait_call("INC7B", "team_leader", 1, "uc1_escalation"), "9")
state("INC7B", "TL_REACHED")
esc = view("INC7B")["call_workflow"]["escalation"]
assert esc["team_leader_decision"] == "do_not_reassign" == esc["reassign"], esc
assert not [c for c in calls_of("INC7B") if c["role"] == "manager"]
ok("TL picks up without a key -> manager called (9 -> do_not_reassign); no key anywhere -> UNRESOLVED; "
   "TL presses 9 -> do_not_reassign, manager not called")

# 2 technician misses -> TL no answer -> manager attempt 1 busy -> attempt 2 answered
assign("INC8")
no_answer(wait_call("INC8", "technician", 1))
no_answer(wait_call("INC8", "technician", 2))
no_answer(wait_call("INC8", "team_leader", 1, "uc1_escalation"))
no_answer(wait_call("INC8", "manager", 1, "uc1_escalation"), "busy")
xml = answer(wait_call("INC8", "manager", 2, "uc1_escalation"), "5")
assert "Should we assign this alert to another technician? Press 5 for yes. Press 9 for no." in xml, xml
assert "follow up in NexTask" not in xml, xml
state("INC8", "MANAGER_REACHED")
v = view("INC8")
esc = v["call_workflow"]["escalation"]
assert esc["reason"] == "no_acknowledgement" and esc["technician_calls"] == 2, esc
assert esc["team_leader_reached"] is False and esc["manager_reached"], esc
assert esc["manager_decision"] == "assign_to_another_technician" == esc["reassign"], esc
assert "Manager: call 1 line was busy" in v["call_workflow"]["response_summary"], v["call_workflow"]
ok("no ack x2 -> TL not answered -> manager busy -> manager attempt 2 answered -> MANAGER_REACHED")

# Manager never answers -> unresolved escalation listed
assign("INC9")
no_answer(wait_call("INC9", "technician", 1))
no_answer(wait_call("INC9", "technician", 2))
no_answer(wait_call("INC9", "team_leader", 1, "uc1_escalation"))
no_answer(wait_call("INC9", "manager", 1, "uc1_escalation"))
no_answer(wait_call("INC9", "manager", 2, "uc1_escalation"))
state("INC9", "ESCALATION_UNRESOLVED")
assert any(e["ticket_id"] == "INC9" for e in http("GET", "/api/escalations?status=unresolved")[1])
assert any(x["type"] == "unresolved_escalation" and x["ticket_id"] == "INC9"
           for x in http("GET", "/api/exceptions")[1])
ok("nobody answers -> ESCALATION_UNRESOLVED, listed in /api/escalations and /api/exceptions")

# No TL / manager numbers -> escalation ends without calls
assign("INC10", tl=None, mgr=None)
answer(wait_call("INC10", "technician", 1), "9")
state("INC10", "ESCALATION_NO_CONTACTS")
assert len(calls_of("INC10")) == 1
ok("reject with no TL/manager numbers -> ESCALATION_NO_CONTACTS, no further calls")

# p.3: reassignment between attempts cancels old work, new technician called; late result ignored
assign("INC11", assignment="A1", tech="+911111111111")
old = wait_call("INC11", "technician", 1)
twilio(f"/twilio/voice?cid={old}", {})
twilio(f"/twilio/call-status?cid={old}", {"CallStatus": "in-progress"})
code, res = event("REASSIGNED", "INC11", ticket={"assignment_id": "A2"},
                  recipients=[{"role": "technician", "name": "Omar", "mobile": "+914444444444", "language": "en"}])
assert code == 202 and any("CANCELLED_REASSIGNED" in a for a in res["actions"]), res
assert ("end_call", {"sid": store.get_call(old)["call_sid"], "Status": "completed"}) in twilio_client.DRY_RUN_LOG
new = wait_call("INC11", "technician", 1)
assert new != old and store.get_call(new)["to_number"] == "+914444444444"
twilio(f"/twilio/gather?cid={old}&r=0", {"Digits": "5"})
twilio(f"/twilio/call-status?cid={old}", {"CallStatus": "completed"})
time.sleep(0.2)
assert store.get_ticket("INC11")["uc1_state"] == "TECH_CALLING", "late result of old call must be ignored"
answer(new, "5")
state("INC11", "ACKNOWLEDGED")
ok("reassignment: live call ended, old result ignored, new technician called and acknowledged")

# Closed while waiting for attempt 2 -> pending retry cancelled
CFG["tech_retry_sec"] = 2
assign("INC12")
no_answer(wait_call("INC12", "technician", 1))
state("INC12", "TECH_WAITING_RETRY")
code, res = event("TICKET_CLOSED", "INC12", ticket={"status": "Closed"})
assert any("CANCELLED_CLOSED" in a for a in res["actions"]), res
time.sleep(2.5)
assert len(calls_of("INC12")) == 1, "no 2nd call after closure"
CFG["tech_retry_sec"] = 1
ok("ticket closed during retry wait -> timer cancelled, no 2nd call")

# Alarm cleared during escalation -> manager not called
assign("INC13")
answer(wait_call("INC13", "technician", 1), "9")
tl = wait_call("INC13", "team_leader", 1, "uc1_escalation")
event("ALARM_STATUS", "INC13", ticket={"alarm_status": "Cleared"})
no_answer(tl)
time.sleep(0.4)
assert store.get_ticket("INC13")["uc1_state"] == "CANCELLED_ALARM_CLEARED"
assert not [c for c in calls_of("INC13") if c["role"] == "manager"]
ok("alarm cleared during escalation -> escalation cancelled, manager never called")

# Accepted in the source system before the call is answered -> calls stop
CFG["tech_retry_sec"] = 2
assign("INC14")
no_answer(wait_call("INC14", "technician", 1))
code, res = event("ASSIGNMENT_ACCEPTED", "INC14")
assert any("acknowledged via nextask" in a for a in res["actions"]), res
time.sleep(2.5)
assert len(calls_of("INC14")) == 1 and view("INC14")["call_workflow"]["acknowledged_via"] == "nextask"
CFG["tech_retry_sec"] = 1
ok("ASSIGNMENT_ACCEPTED event -> retry cancelled, acknowledged_via=nextask")

# Rejected in the source system -> escalation without waiting for retry
assign("INC15")
event("ASSIGNMENT_REJECTED", "INC15")
wait_call("INC15", "team_leader", 1, "uc1_escalation")
assert view("INC15")["call_workflow"]["escalation"]["reason"] == "rejected"
ok("ASSIGNMENT_REJECTED event -> escalation straight to team leader")

# Stale state -> contact held, released by next event
CFG["state_max_age_sec"] = 1
CFG["tech_retry_sec"] = 2
assign("INC16")
no_answer(wait_call("INC16", "technician", 1))
time.sleep(2.5)
assert store.q("SELECT status FROM jobs WHERE ticket_id='INC16' AND kind='tech_call' AND attempt_no=2",
               one=True)["status"] == "held"
assert any(x["type"] == "contact_held" and x["ticket_id"] == "INC16" for x in http("GET", "/api/exceptions")[1])
CFG["state_max_age_sec"] = 0
CFG["tech_retry_sec"] = 1
code, res = event("TICKET_UPDATED", "INC16")
assert any("released" in a for a in res["actions"]), res
answer(wait_call("INC16", "technician", 2), "5")
state("INC16", "ACKNOWLEDGED")
ok("stale ticket state -> 2nd call held + flagged; fresh event releases it")

# Language menu ('ask') + answered-then-hung-up outcome
assign("INC17", lang="ask")
cid = wait_call("INC17", "technician", 1)
xml = answer(cid, "5", lang_key="3")
assert 'language="hi-IN"' in xml and "क़ुबूल" in xml, xml
state("INC17", "ACKNOWLEDGED")
assert store.get_call(cid)["language_source"] == "menu"
assign("INC18")
c18 = wait_call("INC18", "technician", 1)
twilio(f"/twilio/call-status?cid={c18}", {"CallStatus": "in-progress"})
twilio(f"/twilio/call-status?cid={c18}", {"CallStatus": "completed", "CallDuration": "3"})
time.sleep(0.2)
assert store.get_call(c18)["outcome"] == "hung_up_without_response"
ok("language 'ask' -> menu (3 = Urdu); answered + hung up -> hung_up_without_response")

# Call could not start -> call_error, retried
real = twilio_client.create_call
calls_made = []


def failing(to, cid):
    calls_made.append(cid)
    if len(calls_made) == 1:
        raise twilio_client.TwilioError("Twilio 400 (code 21219): number not verified")
    return real(to, cid)


twilio_client.create_call = failing
assign("INC19")
wait_call("INC19", "technician", 2)
twilio_client.create_call = real
first = calls_of("INC19")[0]
assert first["outcome"] == "call_error" and "21219" in first["error_message"], first
assert any(x["type"] == "call_failure" and x["ticket_id"] == "INC19" for x in http("GET", "/api/exceptions")[1])
ok("Twilio refuses call -> call_error recorded with reason, listed in exceptions, attempt 2 placed")

# Audit APIs
tl = http("GET", "/api/tickets/INC6/timeline")[1]["timeline"]
kinds = [e["kind"] for e in tl]
for k in ("event_accepted", "timer_scheduled", "call_placed", "call_status", "dtmf", "call_finished",
          "escalation_started", "workflow_finished"):
    assert k in kinds, (k, kinds)
assert all(c["role"] == "manager" for c in http("GET", "/api/calls?role=manager")[1])
assert all(c["language"] == "ar" for c in http("GET", "/api/calls?language=ar")[1])
assert http("GET", "/api/events?result=stale")[1][0]["ticket_id"] == "INC1"
code, csv_body = http("GET", "/api/audit/export?type=calls&format=csv")
assert code == 200 and "call_sid" in csv_body.splitlines()[0] and "INC6" in csv_body
assert "event_type" in http("GET", "/api/audit/export?type=events")[1].splitlines()[0]
assert http("GET", "/api/audit/export?type=bad")[0] == 400
cfg = http("GET", "/api/config")[1]
assert "token" not in json.dumps(cfg) and cfg["default_reject_digit"] == "9"
assert http("GET", "/api/tickets?state=ACKNOWLEDGED")[1]
ok("audit: timeline, call search by role/language, events, CSV export, config without secrets")

# Outbound webhook (signed) on escalation and completion
received = []


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        received.append((json.loads(raw), self.headers.get("X-Signature"), raw))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


rcv = HTTPServer(("127.0.0.1", 5057), Receiver)
threading.Thread(target=rcv.serve_forever, daemon=True).start()
assign("INC20", callback_url="http://127.0.0.1:5057/hook")
answer(wait_call("INC20", "technician", 1), "9")
answer(wait_call("INC20", "team_leader", 1, "uc1_escalation"), "5")
state("INC20", "TL_REACHED")
end = time.time() + 4
while time.time() < end and len(received) < 2:
    time.sleep(0.05)
events_got = [r[0]["event"] for r in received]
assert "uc1.escalation_started" in events_got and "uc1.completed" in events_got, events_got
esc_evt = next(r for r in received if r[0]["event"] == "uc1.escalation_started")
assert esc_evt[0]["data"]["reason"] == "rejected" and esc_evt[0]["ticket"]["recipients"]["manager"]["mobile"]
assert esc_evt[1] == hmac.new(b"test-key", esc_evt[2], hashlib.sha256).hexdigest()
rcv.shutdown()
ok("webhook: uc1.escalation_started (for WhatsApp to TL+Manager) and uc1.completed, HMAC-signed")

# Legacy /api/call still works
code, res = http("POST", "/api/call", {"alert_id": "ALM-1", "ticket_id": "LEG1", "phone_number": "+911111111111",
                                       "site_details": "Site 9, Riyadh"})
assert code == 202 and res["visit_status"] == "pending" and res["status_url"] == "/api/call/LEG1", res
assert http("POST", "/api/call", {"alert_id": "A", "ticket_id": "LEG1", "phone_number": "+911111111111",
                                  "site_details": "x"})[0] == 409
answer(wait_call("LEG1", "technician", 1), "1", lang_key="1")
state("LEG1", "ACKNOWLEDGED")
assert http("GET", "/api/call/LEG1")[1]["visit_status"] == "accepted"
assert http("POST", "/api/call", {"ticket_id": "X"})[0] == 400
ok("legacy /api/call: menu, ack 1, visit_status accepted, 409 duplicate, 400 missing fields")

# /api/call with a language per person
code, res = http("POST", "/api/call", {"alert_id": "A", "ticket_id": "LANG1", "phone_number": "+911111111111",
                                       "site_details": "Site 3", "technician_language": "ar",
                                       "team_leader_number": "+912222222222", "team_leader_language": "ur",
                                       "manager_number": "+913333333333", "manager_language": "en"})
assert code == 202, res
rec = res["recipients"]
assert (rec["technician"]["language"], rec["team_leader"]["language"], rec["manager"]["language"]) == ("ar", "ur", "en")
xml = answer(wait_call("LANG1", "technician", 1), "9")
assert "Polly.Zeina" in xml and "/twilio/lang" not in xml, xml
xml = twilio(f"/twilio/voice?cid={wait_call('LANG1', 'team_leader', 1, 'uc1_escalation')}", {})
assert 'language="hi-IN"' in xml and "दूसरे टेक्नीशियन" in xml, xml
code, res = http("POST", "/api/call", {"alert_id": "A", "ticket_id": "LANG2", "phone_number": "+911111111111",
                                       "site_details": "x", "language": "ar", "manager_number": "+913333333333",
                                       "manager_language": "en"})
assert res["recipients"]["technician"]["language"] == "ar" and res["recipients"]["manager"]["language"] == "en"
assert http("POST", "/api/call", {"alert_id": "A", "ticket_id": "LANG3", "phone_number": "+911111111111",
                                  "site_details": "x", "manager_number": "+913333333333",
                                  "manager_language": "fr"})[0] == 400
ok("/api/call per-person language: technician ar, TL ur, manager en; language = default; bad value -> 400")

# ?wait=true: one request returns the technician's answer
def in_background(fn, *a, **kw):
    box = {}
    th = threading.Thread(target=lambda: box.update(res=fn(*a, **kw)), daemon=True)
    th.start()
    return th, box


ev = {"event_id": "WAIT-1", "event_type": "ASSIGNMENT_CREATED",
      "ticket": {"ticket_id": "WAIT1", "assignment_id": "W1", "site_number": "7"},
      "recipients": [{"role": "technician", "mobile": "+911111111111", "language": "en"}]}
th, box = in_background(http, "POST", "/api/events?wait=true&timeout=20", ev)
answer(wait_call("WAIT1", "technician", 1), "1")
th.join(25)
code, res = box["res"]
assert code == 200 and res["call_result"] == "accepted" and res["timed_out"] is False, res
assert res["result"] == "accepted" and res["technician_response"] == "acknowledged", res
ok("POST /api/events?wait=true -> same response carries call_result=accepted")

th, box = in_background(http, "POST", "/api/call?wait=true&timeout=20",
                        {"alert_id": "A1", "ticket_id": "WAIT2", "phone_number": "+911111111111",
                         "site_details": "Site 1", "language": "en"})
answer(wait_call("WAIT2", "technician", 1), "9")
th.join(25)
code, res = box["res"]
assert code == 200 and res["call_result"] == "rejected" and res["visit_status"] == "rejected", res
ok("POST /api/call?wait=true -> rejected returned in the same response")

ev2 = dict(ev, event_id="WAIT-3", ticket={"ticket_id": "WAIT3", "assignment_id": "W3"})
th, box = in_background(http, "POST", "/api/events?wait=true&timeout=1", ev2)
th.join(10)
code, res = box["res"]
assert code == 200 and res["call_result"] == "pending" and res["timed_out"] is True, res
ok("wait timeout -> call_result=pending, timed_out=true (call keeps running)")

no_answer(wait_call("WAIT3", "technician", 1))
ev4 = dict(ev, event_id="WAIT-4", ticket={"ticket_id": "WAIT4", "assignment_id": "W4"},
           recipients=ev["recipients"] + [{"role": "team_leader", "mobile": "+912222222222", "language": "en"}])
th, box = in_background(http, "POST", "/api/events?wait=true&until=final&timeout=20", ev4)
answer(wait_call("WAIT4", "technician", 1), "9")
time.sleep(0.5)
assert th.is_alive(), "until=final must keep waiting during escalation"
answer(wait_call("WAIT4", "team_leader", 1, "uc1_escalation"), "1")
th.join(25)
code, res = box["res"]
assert code == 200 and res["call_result"] == "rejected" and res["ticket"]["call_workflow"]["state"] == "TL_REACHED", res
ok("wait until=final -> returns after escalation finished (TL_REACHED)")

# Follow-up ?wait=true: answered / not answered / not needed
ev5 = dict(ev, event_id="WAIT-5", ticket={"ticket_id": "WAIT5", "assignment_id": "W5"})
http("POST", "/api/events", ev5)
answer(wait_call("WAIT5", "technician", 1), "1")
state("WAIT5", "ACKNOWLEDGED")
th, box = in_background(http, "POST", "/api/tickets/WAIT5/followup?wait=true&timeout=20", {"assignment_id": "W5"})
answer(wait_call("WAIT5", "technician", 1, "uc1_reminder"))
th.join(25)
code, res = box["res"]
assert code == 200 and res["result"] == "reminder_call_placed" and res["reminder_result"] == "reminder_delivered", res
assert res["answered"] is True and res["followup"]["state"] == "REMINDER_CALLED" and res["call"]["call_sid"], res
th, box = in_background(http, "POST", "/api/tickets/WAIT5/followup?wait=true&timeout=20", {})
wait_until_new_reminder("WAIT5", 2)
no_answer(store.q("SELECT id FROM calls WHERE ticket_id='WAIT5' AND workflow='uc1_reminder' ORDER BY id DESC",
                  one=True)["id"])
th.join(25)
code, res = box["res"]
assert code == 200 and res["reminder_result"] == "not_picked_up" and res["answered"] is False, res
event("OPERATIONAL_STATUS", "WAIT5", ticket={"operational_status": "Started"})
code, res = http("POST", "/api/tickets/WAIT5/followup?wait=true&timeout=5", {})
assert code == 200 and res["result"] == "not_needed" and "Started" in res["reason"], res
ok("follow-up ?wait=true -> reminder answered / not picked up returned in response; not_needed returns at once")

# Names: technician greeted by name, TL greeted by name and told the technician's name
th, box = in_background(http, "POST", "/api/call?wait=true&until=final&timeout=20",
                        {"alert_id": "A9", "ticket_id": "NAME1", "phone_number": "+911111111111",
                         "site_details": "Site 9", "language": "en", "technician_name": "Ahmed Khan",
                         "team_leader_number": "+912222222222", "team_leader_name": "Imran"})
xml = answer(wait_call("NAME1", "technician", 1), "9")
assert "Hello Ahmed Khan. This is the Automation Edge helpdesk." in xml, xml
xml = answer(wait_call("NAME1", "team_leader", 1, "uc1_escalation"), "1")
assert "Hello Imran. This is the Automation Edge helpdesk escalation." in xml, xml
assert "The technician Ahmed Khan has rejected the assignment." in xml, xml
th.join(25)
code, res = box["res"]
assert code == 200 and res["call_workflow"]["state"] == "TL_REACHED", res
assert res["recipients"]["team_leader"]["name"] == "Imran", res["recipients"]
ok("names: 'Hello Ahmed Khan', TL hears 'Hello Imran' + 'The technician Ahmed Khan has rejected'")

# Menu choice remembered: technician call 2 and manager call 2 do not ask the language again
assign("MENU1", lang="ask", mgr="+913333333333")
r = store.get_ticket("MENU1")["recipients"]
store.update_ticket("MENU1", recipients=dict(r, team_leader={}, manager=dict(r["manager"], language="ask")))
c1 = wait_call("MENU1", "technician", 1)
twilio(f"/twilio/voice?cid={c1}", {})
twilio(f"/twilio/lang?cid={c1}&r=0", {"Digits": "2"})
twilio(f"/twilio/voice?cid={c1}&r=1", {})
twilio(f"/twilio/call-status?cid={c1}", {"CallStatus": "completed"})
c2 = wait_call("MENU1", "technician", 2)
xml = twilio(f"/twilio/voice?cid={c2}", {})
assert "/twilio/lang" not in xml and "Polly.Zeina" in xml, "technician call 2 must reuse Arabic"
assert store.get_call(c2)["language_source"] == "menu_earlier_call"
twilio(f"/twilio/voice?cid={c2}&r=1", {})
twilio(f"/twilio/call-status?cid={c2}", {"CallStatus": "completed"})
m1 = wait_call("MENU1", "manager", 1, "uc1_escalation")
assert "/twilio/lang" in twilio(f"/twilio/voice?cid={m1}", {}), "manager's first call asks"
twilio(f"/twilio/lang?cid={m1}&r=0", {"Digits": "3"})
no_answer(m1)
m2 = wait_call("MENU1", "manager", 2, "uc1_escalation")
xml = twilio(f"/twilio/voice?cid={m2}", {})
assert "/twilio/lang" not in xml and 'language="hi-IN"' in xml, "manager call 2 must reuse Urdu"
ok("menu choice remembered: technician call 2 and manager call 2 skip the language menu")

# Keep-alive: a long wait streams spaces first, then valid JSON
app.KEEPALIVE_SEC = 0.2
ev6 = dict(ev, event_id="WAIT-6", ticket={"ticket_id": "WAIT6", "assignment_id": "W6"})
raw_box = {}


def raw_post():
    req = urllib.request.Request(BASE + "/api/events?wait=true&timeout=5", method="POST",
                                 data=json.dumps(ev6).encode(),
                                 headers={"X-API-Key": "test-key", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        raw_box["status"], raw_box["body"] = r.status, r.read().decode()


th = threading.Thread(target=raw_post, daemon=True)
th.start()
cid6 = wait_call("WAIT6", "technician", 1)
time.sleep(1.2)
answer(cid6, "1")
th.join(10)
app.KEEPALIVE_SEC = 10
assert raw_box["status"] == 200 and raw_box["body"].startswith("  "), raw_box["body"][:20]
assert json.loads(raw_box["body"])["call_result"] == "accepted"
ok("long wait streams keep-alive spaces, then valid JSON (survives Cloudflare's 100 s limit)")

# Twilio signature validation
CFG.update(dry_run=False, base_url="https://example.trycloudflare.com", token="secret-token")
params = {"CallStatus": "ringing", "CallSid": "CA123"}
path = "/twilio/call-status?cid=999999"
good = twilio_client.signature_for(CFG["base_url"] + path, params)
assert http("POST", path, params, key=None, form=True, headers={"X-Twilio-Signature": good})[0] == 204
assert http("POST", path, params, key=None, form=True, headers={"X-Twilio-Signature": "forged"})[0] == 403
assert http("POST", path, dict(params, CallStatus="completed"), key=None, form=True,
            headers={"X-Twilio-Signature": good})[0] == 403
CFG.update(dry_run=True, base_url="")
ok("Twilio webhook signature: valid -> accepted; forged or tampered -> 403")

# Dashboard local-only
assert http("GET", "/", key=None)[0] == 200
assert http("GET", "/", key=None, headers={"Cf-Connecting-Ip": "1.2.3.4"})[0] == 401
ok("dashboard renders locally, blocked through the tunnel")

scheduler.stopped.set()
server.shutdown()
print(f"\nALL {len(PASSED)} TESTS PASSED")
