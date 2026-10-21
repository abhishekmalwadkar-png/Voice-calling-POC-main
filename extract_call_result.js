/*
 * extractCallResult(response) -> flat object with the main fields of a /api/call response.
 *
 * Works with the response of:
 *   POST /api/call?wait=true[&until=final]   and   GET /api/call/{ticket_id}
 * `response` may be the parsed object or the raw JSON text (leading spaces from a long wait are fine).
 *
 * Plain ES5 (no arrow functions, no ?.) so it runs in AutomationEdge JavaScript steps, Postman, Node, browsers.
 */
function extractCallResult(response) {
  var r = typeof response === "string" ? JSON.parse(response) : (response || {});

  if (r.error) {
    return { ok: false, error: r.error, next_action: "ERROR" };
  }

  var wf = r.call_workflow || {};
  var esc = wf.escalation || {};
  var rec = r.recipients || {};
  var calls = r.calls || [];

  function person(role) {
    var p = rec[role] || {};
    return { name: p.name || "", mobile: p.mobile || "", language: p.language || "" };
  }

  // last call of a given role (ignores calls ended by a reassignment/closure)
  function lastCall(role, workflow) {
    for (var i = calls.length - 1; i >= 0; i--) {
      var c = calls[i];
      if (c.role === role && c.workflow === workflow && !c.superseded) {
        return c;
      }
    }
    return null;
  }

  function callInfo(c) {
    if (!c) {
      return null;
    }
    return {
      attempt_no: c.attempt_no,
      call_sid: c.call_sid,
      outcome: c.outcome,            // acknowledged, rejected, reassign_yes, reassign_no, not_picked_up, ...
      picked_up: c.picked_up,
      key_pressed: c.dtmf || "",
      language: c.language,
      response: c.response,          // plain-language outcome
      started_at: c.created_at,
      ended_at: c.ended_at
    };
  }

  var state = wf.state || "";
  var finalState = wf.final === true || r.final === true;
  var reassign = esc.reassign || null;

  // What the caller should do next
  var next;
  if (r.timed_out === true || r.call_result === "pending" || !finalState) {
    next = "POLL_STATUS";                         // GET /api/call/{ticket_id} until final
  } else if (state === "ACKNOWLEDGED") {
    next = "START_FOLLOWUP_TIMER";                // after 15 min: POST /api/tickets/{id}/followup
  } else if (reassign === "assign_to_another_technician") {
    next = "REASSIGN_TO_NEXT_TECHNICIAN";         // POST /api/call with the next technician
  } else if (state === "ESCALATION_UNRESOLVED") {
    next = "FLAG_UNRESOLVED";
  } else if (state.indexOf("CANCELLED") === 0) {
    next = "NONE";
  } else {
    next = "MANUAL_FOLLOW_UP";                    // do_not_reassign, no decision, or no contacts
  }

  return {
    ok: true,
    next_action: next,

    // ticket
    ticket_id: r.ticket_id || "",
    alert_id: r.alarm_id || "",
    assignment_id: r.assignment_id || "",
    site: r.site_name || r.site_number || "",

    // overall result
    call_result: r.call_result || r.visit_status || "",  // accepted | rejected | no_response | cancelled | pending
    visit_status: r.visit_status || "",           // accepted | rejected | no_response | cancelled | pending
    state: state,                                 // ACKNOWLEDGED | TL_REACHED | MANAGER_REACHED | ESCALATION_UNRESOLVED | ...
    final: finalState,
    timed_out: r.timed_out === true,
    waited_sec: r.waited_sec || 0,
    summary: wf.response_summary || r.response_summary || "",

    // technician
    technician: person("technician"),
    technician_response: wf.technician_response || "",
    technician_picked_up: wf.technician_picked_up,
    technician_calls: calls.filter(function (c) { return c.role === "technician" && c.workflow === "uc1_technician"; }).length,
    technician_last_call: callInfo(lastCall("technician", "uc1_technician")),

    // escalation
    escalated: !!wf.escalation,
    escalation_reason: esc.reason || "",          // rejected | no_acknowledgement
    team_leader: person("team_leader"),
    team_leader_reached: esc.team_leader_reached === undefined ? null : esc.team_leader_reached,
    team_leader_decision: esc.team_leader_decision || null,   // assign_to_another_technician | do_not_reassign | null
    team_leader_last_call: callInfo(lastCall("team_leader", "uc1_escalation")),
    manager: person("manager"),
    manager_reached: esc.manager_reached === undefined ? null : esc.manager_reached,
    manager_decision: esc.manager_decision || null,
    manager_last_call: callInfo(lastCall("manager", "uc1_escalation")),
    reassign: reassign                            // final decision, whoever made it
  };
}

// Node / CommonJS export (ignored elsewhere)
if (typeof module !== "undefined" && module.exports) {
  module.exports = extractCallResult;
}
