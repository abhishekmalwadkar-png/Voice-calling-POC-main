// ES5 - pass the follow-up API response as a JSON string in responseJson; every result is a plain variable.
// Works for POST /api/tickets/{ticket_id}/followup  with or without ?wait=true

var responseJson = '';   // <-- map the API response text here

var r = JSON.parse(responseJson);
var fu = r.followup || {};
var call = r.call || {};

var errorMessage = r.error || "";
var ticketId = r.ticket_id || "";
var result = r.result || "";                    // reminder_call_placed | not_needed | rejected
var reason = r.reason || "";                    // why not_needed / rejected (e.g. "operational status Arrived already recorded")
var callId = r.call_id || "";

// filled only with ?wait=true and result = reminder_call_placed
var reminderResult = r.reminder_result || "";   // reminder_delivered | not_picked_up | busy_or_declined | call_failed | pending
var reminderAnswered = (r.answered === true);
var reminderResponse = r.response || "";        // plain-language outcome
var timedOut = (r.timed_out === true);
var waitedSec = r.waited_sec || 0;
var followupState = fu.state || "";             // REMINDER_CALLING | REMINDER_CALLED | STOPPED_...
var callSid = call.call_sid || "";
var callStatus = call.status || "";
var callDurationSec = call.duration_sec || 0;

var nextAction;
if (errorMessage) {
  nextAction = "ERROR";                         // e.g. ticket not found
} else if (result === "not_needed") {
  nextAction = "NONE";                          // closed, reassigned, status already updated, or alarm cleared
} else if (result === "rejected") {
  nextAction = "CHECK_TICKET";                  // not acknowledged yet, or a reminder is already in progress
} else if (result === "reminder_call_placed" && (timedOut || reminderResult === "" || reminderResult === "pending")) {
  nextAction = "POLL_STATUS";                   // GET /api/tickets/{ticket_id} -> followup.state
} else if (reminderAnswered) {
  nextAction = "REMINDER_DELIVERED";
} else {
  nextAction = "REMINDER_NOT_ANSWERED";         // decide whether to escalate manually
}
