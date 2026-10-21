// ES5 - pass the /api/call response as a JSON string in responseJson; every result is a plain variable.

var responseJson = '';   // <-- map the API response text here

var r = JSON.parse(responseJson);
var wf = r.call_workflow || {};
var esc = wf.escalation || {};
var rec = r.recipients || {};
var calls = r.calls || [];
var tech = rec.technician || {};
var tl = rec.team_leader || {};
var mgr = rec.manager || {};

// ---- ticket ----
var errorMessage = r.error || "";
var ticketId = r.ticket_id || "";
var alertId = r.alarm_id || "";
var assignmentId = r.assignment_id || "";
var site = r.site_name || r.site_number || "";

// ---- overall result ----
var callResult = r.call_result || r.visit_status || "";   // accepted | rejected | no_response | cancelled | pending
var visitStatus = r.visit_status || "";
var finalState = wf.state || "";                           // ACKNOWLEDGED | TL_REACHED | MANAGER_REACHED | ESCALATION_UNRESOLVED | ...
var isFinal = (wf.final === true || r.final === true);
var timedOut = (r.timed_out === true);
var waitedSec = r.waited_sec || 0;
var summary = wf.response_summary || r.response_summary || "";

// ---- technician ----
var technicianName = tech.name || "";
var technicianMobile = tech.mobile || "";
var technicianLanguage = tech.language || "";
var technicianResponse = wf.technician_response || "";     // acknowledged | rejected | not_picked_up | no_input | ...
var technicianPickedUp = (wf.technician_picked_up === true);

// ---- escalation ----
var escalated = (wf.escalation ? true : false);
var escalationReason = esc.reason || "";                    // rejected | no_acknowledgement
var teamLeaderName = tl.name || "";
var teamLeaderMobile = tl.mobile || "";
var teamLeaderReached = (esc.team_leader_reached === true);
var teamLeaderDecision = esc.team_leader_decision || "";    // assign_to_another_technician | do_not_reassign | ""
var managerName = mgr.name || "";
var managerMobile = mgr.mobile || "";
var managerReached = (esc.manager_reached === true);
var managerDecision = esc.manager_decision || "";
var reassign = esc.reassign || "";                          // final decision, whoever made it

// ---- last call of each person (newest first; skips calls ended by reassignment / closure) ----
var technicianCalls = 0;
var technicianLastOutcome = "", technicianLastKey = "", technicianLastCallSid = "";
var teamLeaderLastOutcome = "", teamLeaderLastKey = "", teamLeaderLastCallSid = "";
var managerLastOutcome = "", managerLastKey = "", managerLastCallSid = "";
for (var i = 0; i < calls.length; i++) {
  var c = calls[i];
  if (c.superseded) {
    continue;
  }
  if (c.role === "technician" && c.workflow === "uc1_technician") {
    technicianCalls++;
    technicianLastOutcome = c.outcome || "";
    technicianLastKey = c.dtmf || "";
    technicianLastCallSid = c.call_sid || "";
  } else if (c.role === "team_leader" && c.workflow === "uc1_escalation") {
    teamLeaderLastOutcome = c.outcome || "";
    teamLeaderLastKey = c.dtmf || "";
    teamLeaderLastCallSid = c.call_sid || "";
  } else if (c.role === "manager" && c.workflow === "uc1_escalation") {
    managerLastOutcome = c.outcome || "";
    managerLastKey = c.dtmf || "";
    managerLastCallSid = c.call_sid || "";
  }
}

// ---- what to do next ----
var nextAction;
if (errorMessage) {
  nextAction = "ERROR";
} else if (timedOut || callResult === "pending" || !isFinal) {
  nextAction = "POLL_STATUS";                    // GET /api/call/{ticket_id} until final
} else if (finalState === "ACKNOWLEDGED") {
  nextAction = "START_FOLLOWUP_TIMER";           // after 15 min: POST /api/tickets/{ticket_id}/followup
} else if (reassign === "assign_to_another_technician") {
  nextAction = "REASSIGN_TO_NEXT_TECHNICIAN";    // POST /api/call with the next technician
} else if (finalState === "ESCALATION_UNRESOLVED") {
  nextAction = "FLAG_UNRESOLVED";
} else if (finalState.indexOf("CANCELLED") === 0) {
  nextAction = "NONE";
} else {
  nextAction = "MANUAL_FOLLOW_UP";               // do_not_reassign, no decision, or no contacts
}
