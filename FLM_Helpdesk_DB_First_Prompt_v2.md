# FLM Helpdesk POC — Database-First Implementation (v2: Voice via Twilio Voice Call Platform + WhatsApp)

Act as a senior AutomationEdge developer, PostgreSQL database designer, ServiceNow integration developer, and voice/WhatsApp integration engineer.

## 1. Objective

Build a database-first FLM Helpdesk POC based on the provided solution-flow diagram, the workflow-design PDF ("FLM Helpdesk Final PoC Workflows"), the PostgreSQL schema, and the existing **FLM Voice Call Platform** (Twilio) described in Appendix A.

The system must:

- Store incoming alerts in PostgreSQL.
- Fetch unprocessed alerts from the database.
- Create a standard ServiceNow Incident for each eligible alert.
- Save the Incident number and sys_id in PostgreSQL.
- Manage engineers, Team Leaders, Managers, SPOCs, regions, shifts, availability, language preferences, and assignment rules in PostgreSQL.
- Notify the assigned engineer by **voice call through the FLM Voice Call Platform API** (Twilio). The engineer accepts or rejects with a keypad digit (DTMF).
- Send WhatsApp notifications through the existing/configured WhatsApp chatbot integration where the business rules require WhatsApp.
- Process Accept/Reject results, retry no-response cases, reassign engineers, and escalate unresolved cases.
- Update ServiceNow Incident state and work notes, and the corresponding PostgreSQL records.
- Maintain a complete audit trail for alert processing, assignments, calls, messages, responses, failures, and escalations.
- Support emergency-opening, severity-change, and closure notifications to the Engineer, Team Leader, and SPOC (WhatsApp) when these event types are enabled.

### Channel responsibilities

| Channel | Used for | Owned by |
|---|---|---|
| Voice (Twilio) | Engineer notification + Accept/Reject (DTMF), 2nd call attempt, 15-minute follow-up reminder call, Team Leader / Manager escalation calls | FLM Voice Call Platform — called only through its REST API (Appendix A) |
| WhatsApp | Escalation messages to Team Leader + Manager in parallel, emergency opening / severity change / closure notifications | Existing WhatsApp chatbot integration |

AutomationEdge must **never call Twilio directly** and must not build its own IVR, TwiML, or DTMF handling. The Voice Call Platform already implements: language selection per recipient (Arabic / English / Urdu, with configured fallback), dynamic acknowledgement digit, fixed reject digit 9, 2 technician attempts, Team Leader (1 attempt) → Manager (up to 2 attempts) escalation calls, contact guard (closed / reassigned / alarm cleared cancels pending calls), event deduplication, and a per-call audit (Call SID, attempt, DTMF, outcome, language, template/version, cause code, timestamps).

## 2. Architecture

Use one PostgreSQL database with the supplied schema.

The primary tables are:

- `alerts`: incoming alert queue.
- `regions`: region/site reference data.
- `shifts`: shift definitions.
- `engineers`: engineers and management/recipient roles (incl. mobile in E.164 and preferred language).
- `engineer_shifts`: engineer shift schedules.
- `dispatch_cases`: current dispatch lifecycle and assignment.
- `notification_attempts`: each outbound voice call or WhatsApp message per recipient.
- `acknowledgements`: Accept/Reject responses (from voice DTMF via the Voice Call Platform, or from the chatbot if enabled).
- `servicenow_sync_log`: Incident updates and work-note synchronization results.
- `notification_event_log`: auditable event timeline.

PostgreSQL is the system of record for the dispatch state. The Voice Call Platform keeps its own call audit; reconcile with it through `GET /api/tickets/{ticket_id}` when a webhook is missed or an outcome is unknown.

Use the provided SQL schema as the starting point. Review it against the actual POC requirements before applying it. If a change is needed, explain the change before modifying the schema. Expected candidates (justify each before applying, do not add tables unnecessarily):

- `notification_attempts`: add `channel` (`VOICE` | `WHATSAPP`), `provider` (`TWILIO` | configured WhatsApp provider), `provider_ref` (Twilio Call SID or WhatsApp message ID), `voice_event_id` (the `event_id` sent to the Voice Call Platform), `outcome`, `dtmf`, `language`, `template`, `template_version`, `cause_code`, `answered_at`, `ended_at` — only the columns the existing schema lacks.
- `dispatch_cases`: ensure it can hold `assignment_version`, the Voice Call Platform `ticket_id` and current `assignment_id`, voice workflow state, escalation state, and next scheduled action/deadline.
- A unique constraint or table that makes received voice-platform webhooks idempotent (see A.6) — prefer reusing `notification_event_log` with a unique key over a new table.

## 3. Create three logical AutomationEdge projects/processes

### Project 1: FLM Alert Intake, Incident Creation and Dispatch

Responsibilities:

- Read alerts whose `intake_status` is `NEW`.
- Safely claim/process each alert so concurrent runs do not process it twice (e.g. `UPDATE … SET intake_status='PROCESSING' … WHERE intake_status='NEW' RETURNING …` or `SELECT … FOR UPDATE SKIP LOCKED`).
- Validate mandatory fields.
- Check for an existing Incident and prevent duplicate creation.
- Create a standard ServiceNow Incident if one does not already exist.
- Store its Incident number and sys_id in PostgreSQL.
- Select an eligible engineer based on region, active/available status, shift, and configured assignment rules.
- Create or update the dispatch case (assignment_version = 1).
- Start the engineer voice notification: `POST /api/events` with `event_type = ASSIGNMENT_CREATED` (Appendix A.2), recipients resolved from PostgreSQL, the engineer's language, the configured acknowledgement digit, and `callback_url` = the Project 2 webhook endpoint.
- Create the `notification_attempts` record(s) for the voice notification with the `event_id` sent; mark the send as accepted only when the API returns `202` with `result = accepted` (or `200` with `result = duplicate` on a safe retry of the same `event_id`).
- Save the next scheduled action (e.g. follow-up due time) in PostgreSQL.
- Write relevant ServiceNow work notes and a PostgreSQL audit record.

### Project 2: Voice Results, WhatsApp Escalation, Retry, Reassignment, Follow-up

Responsibilities:

- Receive Voice Call Platform webhooks (Appendix A.6) at the `callback_url` endpoint. Verify `X-Signature` before trusting the body. Deduplicate deliveries.
- Validate the ticket ID, case, engineer, and assignment against the current dispatch case. Ignore stale results for an old `assignment_id` (record them in the audit log only).
- On `uc1.acknowledged` (engineer pressed the acknowledgement digit, or acceptance was reported by a source system): record the acceptance in `acknowledgements`, update the dispatch state in PostgreSQL, and update the ServiceNow Incident state/work notes.
- On engineer rejection (`uc1.escalation_started` with `data.reason = "rejected"`): record the rejection, exclude the engineer according to the agreed policy, select the next eligible engineer, increment `assignment_version`, and send `REASSIGNED` (Appendix A.2) — or escalate if no eligible engineer remains.
- On no response after the platform's 2 call attempts (`uc1.escalation_started` with `data.reason = "no_acknowledgement"`): apply the configured reassignment/escalation policy.
- When escalation is required: send WhatsApp to **both** Team Leader and Manager in parallel (one `notification_attempts` row each) with the factual reason taken from the webhook (`ticket.call_workflow.response_summary`). Voice escalation calls (Team Leader → Manager) are placed by the Voice Call Platform only when `team_leader` / `manager` recipients were included in the event (see policy question Q1).
- Record escalation outcomes from `uc1.completed` (`TL_REACHED`, `MANAGER_REACHED`, `ESCALATION_UNRESOLVED`, `ESCALATION_NO_CONTACTS`) and flag unresolved escalation cases.
- 15-minute follow-up (PDF p.4): when the configured follow-up time after acceptance is due, re-check ServiceNow/PostgreSQL state, then call `POST /api/tickets/{ticket_id}/followup` with the current `assignment_id`. The platform places the reminder call only when it is still needed and answers `not_needed` with the reason otherwise (closed, reassigned, Departed/Arrived/Started recorded, alarm cleared).
- Before any delayed action, recheck the current Incident state and assignment. Send `TICKET_CLOSED`, `ALARM_STATUS` (Cleared) or `OPERATIONAL_STATUS` events to the Voice Call Platform when those changes happen so it cancels pending calls.
- Cancel old pending actions after reassignment, closure, or resolution.

### Project 3: Emergency, Severity Change, and Closure Notifications (WhatsApp)

Responsibilities:

- Detect or receive eligible emergency-opening, severity-change, and closure events.
- Deduplicate events before processing.
- Resolve the applicable Engineer, Team Leader, and SPOC from PostgreSQL.
- Send WhatsApp notifications to the required recipients in parallel through the configured chatbot integration.
- Create one notification-attempt record per recipient.
- Record provider message IDs, template/version, send/delivery/read status when available, and failure reason.
- Synchronize relevant Incident updates and work notes with ServiceNow.
- On closure, also send `TICKET_CLOSED` to the Voice Call Platform so any pending calls/reminders for that ticket are cancelled.
- Recheck the current ticket state before delayed notifications.

Reuse existing workflows and components if they already implement these responsibilities. Do not create duplicate workflows unnecessarily.

## 4. ServiceNow integration

Inspect the existing ServiceNow instance and determine the correct API, authentication configuration, required Incident fields, and valid choice values. Read credentials only from the AutomationEdge credential store / configuration — never from source files or logs.

For each alert:

- Create one standard Incident.
- Save its number and sys_id in PostgreSQL.
- Map the alert's short description, description, site, equipment, fault, severity, and SLA where appropriate.
- Update the Incident state and work notes when dispatch, voice call outcome, acceptance, rejection, reassignment, follow-up, or escalation occurs, according to the approved business rules. Include the Twilio Call SID and call outcome in voice-related work notes.
- Record each ServiceNow operation in `servicenow_sync_log`.
- Never assume that a ServiceNow update succeeded just because the request was sent. Check the response, record the result, and retry safely where appropriate.

## 5. Database, ServiceNow and Voice Platform consistency

The current state must be represented in PostgreSQL and synchronized with ServiceNow and the Voice Call Platform.

For each significant operation:

- Record the intended action and a correlation identifier (use the same identifier as the Voice Call Platform `event_id` for voice operations).
- Execute the required ServiceNow / Voice Call Platform operation.
- Verify the result.
- Update the PostgreSQL state and synchronization log.
- If either side fails, record the failure and provide a safe retry/reconciliation mechanism.

Rules:

- Avoid blind repeated Incident creation. If a ServiceNow create request times out, query for an existing matching Incident before retrying.
- Voice API retries must reuse the **same `event_id`**: the platform answers `duplicate` and does not place a second call. Never generate a new `event_id` to retry the same business action.
- If a voice webhook is not received within the expected time, reconcile with `GET /api/tickets/{ticket_id}` instead of assuming an outcome.
- Do not claim PostgreSQL, ServiceNow, and the Voice Call Platform are synchronized when a synchronization operation has failed or has an unknown outcome.

## 6. Business rules

- A WhatsApp message being sent, delivered, or read is not the same as engineer acceptance.
- A voice call being answered is not the same as engineer acceptance. Acceptance requires the acknowledgement digit (`outcome = acknowledged`); rejection requires digit 9 (`outcome = rejected`).
- Accept and Reject must be explicit, validated responses.
- Reject and no response are separate outcomes. Not picked up, busy/declined, picked up without a key, invalid key, and hung up are all "no response", never "reject".
- Provider send/call failures (`call_error`, `call_failed`) must not be treated as engineer rejection.
- Old-assignment responses must not overwrite the current assignment.
- Closed/resolved Incidents must not receive new dispatch calls or reminders.
- Duplicate alerts must not create duplicate Incidents.
- Parallel notifications must be tracked independently for each recipient.
- Do not invent retry counts, response deadlines, shift rules, escalation recipients, language fallbacks, or ServiceNow field mappings.
- Use the actual configured requirements. If they cannot be determined from the supplied files or existing configuration, report the precise questions that require confirmation (start from section 9).

## 7. Implementation and safety procedure

Before coding:

- Read the complete solution-flow diagram, the workflow-design PDF, and Appendix A.
- Inspect the current workspace and identify reusable workflows/components.
- Review the provided SQL schema.
- Check the Voice Call Platform is reachable: `GET /api/health` and `GET /api/config` (no secrets returned).
- List the exact projects, workflows, files, and configuration that need to be created or changed.
- Explain the intended data flow and state transitions (PostgreSQL dispatch state ↔ Voice Call Platform `call_workflow.state` ↔ ServiceNow Incident state).
- Identify missing configuration and ask for clarification where necessary.

Then implement the smallest complete solution, test it with approved test records and recipients (Twilio trial accounts can only call numbers verified in the Twilio console), and inspect all changes.

Do not expose credentials, tokens, API keys, or sensitive data in logs or source files.

## 8. Required deliverables

Provide:

- Final architecture and project/workflow list.
- Database schema and any justified modifications.
- Table-by-table explanation and sample test data.
- ServiceNow field mapping.
- Engineer-selection and shift logic.
- Voice notification flow (API calls, webhooks, state mapping) and WhatsApp notification/response flow.
- Retry, reassignment, follow-up, and escalation behavior.
- Error handling and reconciliation design.
- Test cases with actual pass/fail results.
- Remaining configuration and manual setup.
- Deployment instructions and rollback procedure.

Do not claim that tests passed unless they were actually executed and their results observed.

### POC simplification — ServiceNow work notes

For this POC, all operational work notes must be written to the corresponding ServiceNow Incident. Do not maintain a duplicate copy of the full work notes text in PostgreSQL.

PostgreSQL should store the dispatch state, assigned engineer, assignment version, notification attempts (voice and WhatsApp), call outcomes and WhatsApp delivery status, Accept/Reject responses, retry deadlines, escalation status, and audit information needed to operate the POC.

When a significant event occurs — Incident creation, engineer assignment, voice call outcome, acceptance, rejection, retry, reassignment, follow-up, or escalation — update the ServiceNow Incident work notes using the configured ServiceNow API.

Record the ServiceNow operation result in `servicenow_sync_log`, including the Incident identifiers, operation type, synchronization status, error details, and timestamps. Do not store the full work notes text in this table.

If writing work notes to ServiceNow fails, record the failure in PostgreSQL and retry safely. Do not mark the ServiceNow update as successful until success is confirmed. Ensure retries do not create duplicate Incidents or repeat business actions (including repeat calls).

Keep the implementation simple and appropriate for a POC. Do not add unnecessary database tables or duplicate information that ServiceNow or the Voice Call Platform already stores.

## 9. Questions to confirm before building (do not assume answers)

1. **Q1 — Reject vs escalation policy.** The Voice Call Platform escalates by voice (Team Leader → Manager) on rejection or 2 unanswered calls *if* `team_leader` / `manager` recipients are in the event. The DB-first rules say Reject → reassign to the next engineer. Proposed: omit `team_leader`/`manager` from events for intermediate engineers (so the platform ends with `ESCALATION_NO_CONTACTS` and AutomationEdge reassigns), and include them only for the last eligible engineer. Confirm, or specify another policy.
2. Maximum number of reassignments before escalation, and how rejected engineers are excluded (this case only / for a time window).
3. Follow-up delay after acceptance (PDF says 15 minutes) and who owns the timer (AutomationEdge scheduler calling the follow-up API is assumed).
4. Acknowledgement digit per ticket (configured value or fixed), and language fallback when an engineer has no preference.
5. ServiceNow: Incident state values for Assigned / Accepted / Escalated / Resolved, assignment group, caller, category, and SLA mapping.
6. Shift rules, region-to-engineer mapping, and availability source.
7. WhatsApp chatbot API, approved template texts, and which events are enabled (emergency opening, severity change, closure).
8. Public URL of the Voice Call Platform (a quick tunnel URL changes on every restart — a fixed URL is needed for anything beyond a demo) and where AutomationEdge exposes its webhook endpoint.

---

## Appendix A — FLM Voice Call Platform API contract (Twilio)

### A.1 Basics

- Base URL: configured value `VOICE_API_BASE_URL` (e.g. `https://<host>`). All paths below are relative to it.
- Authentication: header `X-API-Key: <value from the credential store>` on every `/api/*` call (except `/api/health`).
- Bodies: JSON, `Content-Type: application/json`. Times are UTC ISO-8601 (`2026-10-14T10:05:00Z`).
- Phone numbers: E.164 (`+9665XXXXXXXX`). Languages: `en`, `ar`, `ur`, `hi`, or `ask` (caller picks from a menu); missing → platform fallback language.
- `ticket_id` is stored upper-case. Use the ServiceNow Incident number as `ticket_id`.

### A.2 `POST /api/events` — send every ticket/assignment change

```json
{
  "event_id": "CASE-1001-v1-ASSIGNMENT_CREATED",
  "event_type": "ASSIGNMENT_CREATED",
  "event_time": "2026-10-14T10:05:00Z",
  "version": 1,
  "ticket": {
    "ticket_id": "INC0012345",
    "alarm_id": "ALERT-778",
    "assignment_id": "CASE-1001-v1",
    "site_number": "2040",
    "site_name": "Olaya Branch, Riyadh",
    "severity": "High",
    "status": "Assigned",
    "alarm_status": "Active",
    "description": "ATM cash dispenser fault"
  },
  "ack_digit": "1",
  "callback_url": "https://<automationedge-webhook>/voice-results",
  "recipients": [
    { "role": "technician",  "name": "Ahmed", "mobile": "+9665XXXXXXXX", "language": "ar" },
    { "role": "team_leader", "name": "Imran", "mobile": "+9665XXXXXXXX", "language": "ur" },
    { "role": "manager",     "name": "Sara",  "mobile": "+9665XXXXXXXX", "language": "en" }
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `event_id` | Yes | Unique per business action; reuse it on retries (idempotent) |
| `event_type` | Yes | See table below |
| `ticket.ticket_id` | Yes | ServiceNow Incident number |
| `ticket.assignment_id` | For `ASSIGNMENT_CREATED`, `REASSIGNED` | Change it on every reassignment (e.g. case id + assignment_version) |
| `version` | Recommended | Monotonic per ticket; an older version is ignored as `stale` |
| `recipients[]` | `technician` with mobile required for `ASSIGNMENT_CREATED`, `REASSIGNED` | `team_leader` / `manager` only when voice escalation is wanted (Q1) |
| `ack_digit` | No | Single digit; reject is always 9 (another digit is used if ack is 9) |
| `callback_url` | Recommended | Receives the webhooks in A.6 |

| `event_type` | Platform action |
|---|---|
| `ASSIGNMENT_CREATED` | Calls the technician (attempt 1; attempt 2 after the configured interval if no valid key) |
| `REASSIGNED` | Ends any live call, cancels pending calls, calls the new technician |
| `ASSIGNMENT_ACCEPTED` / `ASSIGNMENT_REJECTED` | Acceptance/rejection recorded elsewhere: stops calls / starts escalation |
| `OPERATIONAL_STATUS` (`ticket.operational_status` = Departed / Arrived / Started) | Follow-up reminder becomes not needed |
| `ALARM_STATUS` (`ticket.alarm_status` = Cleared) | Cancels pending calls and escalation |
| `TICKET_CLOSED` (`ticket.status` = Closed) | Cancels everything for the ticket |
| `TICKET_CREATED`, `TICKET_UPDATED`, `SEVERITY_CHANGED` | State update only (no call) |

Responses: `202 {"result":"accepted","actions":[…],"ticket":{…}}` · `200 {"result":"duplicate"}` (same `event_id` already received) · `200 {"result":"stale","note":"…"}` (older version) · `400 {"error":"…"}` validation · `401` bad/missing key.

### A.3 `POST /api/tickets/{ticket_id}/followup` — the caller's follow-up timer fired

Body (optional): `{"assignment_id": "CASE-1001-v1"}`

| Response | Meaning |
|---|---|
| `202 {"result":"reminder_call_placed","call_id":…}` | Reminder voice call placed to the technician |
| `200 {"result":"not_needed","reason":"…"}` | Closed, reassigned, Departed/Arrived/Started recorded, or alarm cleared |
| `409 {"result":"rejected","reason":"…"}` | Technician has not acknowledged the current assignment, or a reminder is already in progress |
| `404` | Unknown ticket |

### A.4 `GET /api/tickets/{ticket_id}` — current state (use for reconciliation)

Key fields: `assignment_id`, `call_workflow.state`, `call_workflow.final`, `call_workflow.acknowledged`, `call_workflow.acknowledged_via` (`voice` | `nextask`), `call_workflow.technician_response`, `call_workflow.escalation` (`reason`, `technician_calls`, `team_leader_reached`, `team_leader_confirmed`, `manager_reached`, `manager_confirmed`), `call_workflow.response_summary`, `followup.state`, `pending_timers[]`, `calls[]`.

`call_workflow.state` values: `TECH_CALLING`, `TECH_WAITING_RETRY`, `ACKNOWLEDGED`, `ESCALATING`, `TL_REACHED`, `MANAGER_REACHED`, `ESCALATION_UNRESOLVED`, `ESCALATION_NO_CONTACTS`, `CANCELLED_REASSIGNED`, `CANCELLED_CLOSED`, `CANCELLED_ALARM_CLEARED`.

Each `calls[]` entry: `role`, `workflow` (`uc1_technician` | `uc1_escalation` | `uc1_reminder`), `attempt_no`, `to_number`, `call_sid`, `status`, `outcome`, `picked_up`, `dtmf`, `language`, `language_source`, `template`, `template_version`, `cause_code`, `duration_sec`, `created_at`, `answered_at`, `ended_at`, `superseded`, `response`.

Call `outcome` values: `acknowledged`, `rejected`, `confirmed` (TL/Manager pressed the key), `reached` (TL/Manager answered, no key), `reminder_delivered`, `no_input`, `invalid_input`, `hung_up_without_response`, `not_picked_up`, `busy_or_declined`, `call_failed`, `canceled`, `call_error`.

### A.5 Audit and search APIs

`GET /api/tickets/{ticket_id}/timeline` · `GET /api/calls?ticket_id=&role=&workflow=&status=&outcome=&language=&from=&to=` · `GET /api/events?ticket_id=&event_type=&result=` · `GET /api/escalations?status=unresolved|open|resolved|all` · `GET /api/exceptions` · `GET /api/audit/export?type=calls|events|timeline&format=csv|json&from=&to=` · `GET /api/config` · `GET /api/health`.

### A.6 Webhooks to `callback_url`

`POST` with JSON body `{"event", "ticket_id", "sent_at", "data", "ticket"}` where `ticket` is the same object as A.4. Headers: `X-Event: <event>`, `X-Signature: <hex HMAC-SHA256 of the raw body, keyed with the API key>`. Reply with any `2xx`; the platform retries up to 4 times (immediately, then after 5, 15, 30 s) and stops early on a `4xx` other than 408/429.

| `event` | `data` | Fired when |
|---|---|---|
| `uc1.acknowledged` | `{"via": "voice" \| "nextask"}` | Technician pressed the ack digit (or `ASSIGNMENT_ACCEPTED` was sent) |
| `uc1.escalation_started` | `{"reason": "rejected" \| "no_acknowledgement", "technician_calls": n}` | Technician rejected, or no valid key after all attempts |
| `uc1.completed` | `{"state": "<final call_workflow.state>"}` | Call workflow reached a final state (also sent right after `uc1.acknowledged`) |
| `uc1.cancelled` | `{"reason": "…"}` | Pending calls cancelled by closure, reassignment, or alarm clearance |
| `uc1.reminder_called` | `{"outcome": "<call outcome>"}` | Follow-up reminder call ended |

Verify the signature over the raw bytes before parsing. Deliveries have no unique id: deduplicate on `ticket_id` + `event` + `ticket.assignment_id` + `ticket.call_workflow.state` (+ `data.outcome` for reminders), and ignore any webhook whose `ticket.assignment_id` is not the dispatch case's current assignment.
