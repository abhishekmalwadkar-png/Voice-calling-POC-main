# NextAsk Gateway API Documentation

**Base URL:** `https://nextaskgw.automationedge.com`  
**Authentication Scheme:** `Basic Auth` (to generate API key) & `X-API-Key` (for platform endpoints)

---

## Table of Contents
1. [Overview & Authentication Flow](#1-overview--authentication-flow)
2. [Authorization API (`/api/authorize`)](#2-authorization-api)
3. [Authentication API (`/api/authenticate`)](#3-authentication-api)
4. [Alert Intake API (`/api/alerts`)](#4-alert-intake-api)
5. [Field Mapping: Previous vs Additional Fields & Data Types](#5-field-mapping-previous-vs-additional-fields--data-types)
6. [Get Alerts API (`/api/getalerts`)](#6-get-alerts-api)
7. [Twilio Webhooks](#7-twilio-webhooks)
   - [Voice Callback (`/twilio/voice`)](#71-voice-callback)
   - [Call Status Callback (`/twilio/call-status`)](#72-status-callback)
8. [Error Handling & HTTP Status Codes](#8-error-handling--http-status-codes)

---

## 1. Overview & Authentication Flow

```mermaid
sequenceDiagram
    autonumber
    actor Client as Client / Integration / Postman
    participant Gateway as NextAsk Gateway (https://nextaskgw.automationedge.com)
    participant Twilio as Twilio Voice Service
    participant Engineer as Technician / Team Leader / Manager

    Note over Client,Gateway: Step 1: Obtain API Key
    Client->>Gateway: POST /api/authorize (Basic Auth: <USERNAME> / <PASSWORD>)
    Gateway-->>Client: 200 OK (Returns x_api_key)

    Note over Client,Gateway: Step 2: Trigger Alert Voice Calls
    Client->>Gateway: POST /api/alerts (Header: X-API-Key: <YOUR_API_KEY>, Body: Batch Alert JSON)
    Gateway->>Twilio: Initiate Outbound Call
    Gateway-->>Client: 200 OK (accepted, scheduled calls with event_id & event_timestamp)

    Note over Twilio,Engineer: Step 3: Call & DTMF Acknowledgment
    Twilio->>Engineer: Voice Call (Arabic / English prompt)
    Engineer-->>Twilio: Presses DTMF "1" (Acknowledge) or "9" (Reject)
    Twilio->>Gateway: POST /twilio/call-status (Record response)

    Note over Client,Gateway: Step 4: Check Batch Status
    Client->>Gateway: GET /api/getalerts (Header: X-API-Key: <YOUR_API_KEY>)
    Gateway-->>Client: 200 OK (dtmf_pressed, acknowledged: true)
```

---

## 2. Authorization API

Authenticates admin credentials and returns the active API Key required for all subsequent requests.

* **Endpoint:** `/api/authorize`
* **Method:** `POST` or `GET`
* **Authentication:** **Basic Auth** (`<USERNAME>` / `<PASSWORD>`) or JSON Body (`{"username": "<USERNAME>", "password": "<PASSWORD>"}`)

### Request Headers
| Header | Value | Required |
| :--- | :--- | :--- |
| `Authorization` | `Basic <BASE64_ENCODED_CREDENTIALS>` | **Yes** (or JSON body) |
| `Content-Type` | `application/json` | If sending JSON body |

### Request Body (Optional if using Basic Auth)
```json
{
  "username": "<YOUR_USERNAME>",
  "password": "<YOUR_PASSWORD>"
}
```

### Success Response (`200 OK`)
```json
{
  "status": "success",
  "authorized": true,
  "http_code": 200,
  "message": "User authenticated successfully. Use the provided api_key in the headers of other APIs.",
  "api_key": "<YOUR_API_KEY>",
  "x_api_key": "<YOUR_API_KEY>",
  "token": "<SESSION_BEARER_TOKEN>",
  "token_type": "Bearer",
  "expires_in": 86400,
  "user": {
    "username": "<YOUR_USERNAME>",
    "role": "admin",
    "auth_type": "credentials"
  }
}
```

### Error Responses
* **`400 Bad Request`** (Missing credentials):
  ```json
  {
    "status": "error",
    "authorized": false,
    "http_code": 400,
    "error": "Bad Request: Missing credentials.",
    "message": "Please provide username and password via Basic Auth or JSON body to obtain your API Key."
  }
  ```
* **`401 Unauthorized`** (Invalid credentials):
  ```json
  {
    "status": "error",
    "authorized": false,
    "http_code": 401,
    "error": "Unauthorized: Invalid username or password.",
    "message": "The username or password provided is incorrect."
  }
  ```

---

## 3. Authentication API

Standard JWT-style authentication endpoint that issues a bearer session token.

* **Endpoint:** `/api/authenticate`
* **Method:** `POST`
* **Headers:** `Content-Type: application/json`

### Request Body
```json
{
  "username": "<YOUR_USERNAME>",
  "password": "<YOUR_PASSWORD>"
}
```

### Success Response (`200 OK`)
```json
{
  "status": "success",
  "message": "Authentication successful",
  "token": "<SESSION_BEARER_TOKEN>",
  "token_type": "Bearer",
  "expires_in": 86400,
  "api_key": "<YOUR_API_KEY>",
  "user": {
    "username": "<YOUR_USERNAME>",
    "role": "admin",
    "full_name": "<YOUR_USERNAME>"
  }
}
```

---

## 4. Alert Intake API

Ingests a single ticket or an entire batch of network/power/HVAC alerts, creates assignments, schedules escalation timers, and immediately triggers automated Twilio voice calls to the designated technicians.

* **Endpoint:** `/api/alerts` *(alias: `/api/alerts/intake`)*
* **Method:** `POST`
* **Authentication:** Header `X-API-Key: <YOUR_API_KEY>`

### Request Headers
| Header | Value | Required |
| :--- | :--- | :--- |
| `X-API-Key` | `<YOUR_API_KEY>` | **Yes** |
| `Content-Type` | `application/json` | **Yes** |

### Request Body (Batch Alert Payload)
```json
{
  "batch_id": "BATCH001",
  "event_timestamp": "2026-10-30T10:30:00+03:00",
  "branch": {
    "branch_id": "BR001",
    "branch_name": "Riyadh Main Office",
    "site_number": "SITE001"
  },
  "alerts": [
    {
      "event_id": "EVT001",
      "event_type": "ASSIGNMENT_REASSIGNED",
      "event_version": 2,
      "alert_id": "ALT1001",
      "ticket": {
        "ticket_id": "INC1001",
        "alarm_id": "ALM5001",
        "issue_description": "Network connectivity failure",
        "alarm_type": "Network Failure",
        "severity": "Critical",
        "ticket_status": "Assigned",
        "alarm_status": "Active"
      },
      "assignment": {
        "assignment_id": "ASN002",
        "assignment_type": "REASSIGNMENT",
        "previous_assignment_id": "ASN001",
        "assigned_at": "2026-10-30T10:30:00+03:00"
      },
      "technician": {
        "technician_id": "ENG002",
        "name": "Ali",
        "mobile_number": "+9665XXXXXXXX",
        "preferred_language": "English"
      },
      "team_leader": {
        "team_leader_id": "TL001",
        "name": "Mohammed",
        "mobile_number": "+9665XXXXXXXX",
        "preferred_language": "English"
      },
      "manager": {
        "manager_id": "MGR001",
        "name": "Khalid",
        "mobile_number": "+9665XXXXXXXX",
        "preferred_language": "English"
      },
      "acknowledgement": {
        "dtmf_digit": "1"
      }
    }
  ]
}
```

### Success Response (`200 OK`)
Notice that each result item explicitly returns **`event_id`**, **`event_timestamp`**, and **`timestamp`** indicating when that event was generated.

```json
{
  "status": "success",
  "mode": "batch",
  "total": 1,
  "successful": 1,
  "failed": 0,
  "authorized": true,
  "auth": {
    "status": "authorized",
    "user": "api_admin",
    "role": "admin",
    "api_key_valid": true,
    "auth_type": "api_key"
  },
  "results": [
    {
      "index": 0,
      "ticket_id": "INC1001",
      "alert_id": "ALT1001",
      "event_id": "EVT001",
      "event_timestamp": "2026-10-30T10:30:00+03:00",
      "timestamp": "2026-10-30T10:30:00+03:00",
      "ack_digit": "1",
      "reject_digit": "9",
      "dtmf_pressed": null,
      "ack_number_pressed": null,
      "acknowledged": false,
      "status": "accepted",
      "http_code": 202,
      "actions": [
        "call workflow -> ASSIGNMENT_REASSIGNED",
        "tech_call #1 scheduled for 2026-10-30T10:30:00+03:00"
      ],
      "alert": {
        "ticket_id": "INC1001",
        "alert_id": "ALT1001",
        "alarm_id": "ALM5001",
        "event_id": "EVT001",
        "date_time": "2026-10-30T10:30:00+03:00",
        "site_name": "Riyadh Main Office",
        "site_number": "SITE001",
        "description": "Network connectivity failure",
        "technician_name": "Ali",
        "technician_number": "+9665XXXXXXXX",
        "severity": "Critical"
      }
    }
  ]
}
```

### Unauthorized Response (`401 Unauthorized`)
```json
{
  "status": "error",
  "authorized": false,
  "http_code": 401,
  "error": "Unauthorized: Missing or invalid API Key.",
  "message": "Please provide 'X-API-Key: <YOUR_API_KEY>' in request headers.",
  "required_header": "X-API-Key"
}
```

---

## 5. Field Mapping: Previous vs Additional Fields & Data Types

The table below contrasts the **Previous Fields** (original intake architecture) against the **Additional Fields** (expanded NexTask architecture), complete with sample values, data types mapped from the PostgreSQL schema (`tb_tickets`, `tb_nextask_events`, `tb_assignments`), and their functional purpose.

### 5.1 Additional Fields (NexTask Extension)

| No. | Additional Field | Data Type (PostgreSQL Schema) | Sample Value | Purpose |
| :-: | :--- | :--- | :--- | :--- |
| **1** | `batch_id` | `VARCHAR(50)` (`tb_nextask_events.batch_id`) | `"BATCH001"` | Identifies a batch of alerts processed together |
| **2** | `event_timestamp` (batch level) | `TIMESTAMP WITH TIME ZONE` (`tb_nextask_events.event_timestamp`) | `"2026-10-30T10:30:00+03:00"` | Records the batch event creation timestamp |
| **3** | `branch.branch_id` | `VARCHAR(50)` (`tb_tickets.branch_id`) | `"BR001"` | Unique identifier of the branch / facility |
| **4** | `alert_id` | `VARCHAR(50)` (`tb_tickets.alert_id`) | `"ALT1001"` | Separate alert reference distinct from ticket ID |
| **5** | `assignment.assignment_type` | `VARCHAR(30)` (`tb_assignments.assignment_type`) | `"INITIAL"` / `"REASSIGNMENT"` | Identifies assignment lifecycle type |
| **6** | `assignment.assigned_at` | `TIMESTAMP WITH TIME ZONE` (`tb_assignments.assigned_at`) | `"2026-10-30T10:20:00+03:00"` | Records the exact assignment timestamp |
| **7** | `assignment.previous_assignment_id` | `VARCHAR(50)` (`tb_assignments.previous_assignment_id`) | `null` / `"ASN001"` | Identifies the previous assignment during reassignment |

---

### 5.2 Previous vs Additional Architecture Comparison

| Field Category | Previous Field | Additional / Nested Field | Data Type | Sample Value | Purpose & Mapping |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Batch Context** | *(none)* | `batch_id` | `VARCHAR(50)` | `"BATCH001"` | Identifies alert batch |
| **Batch Context** | *(none)* | `event_timestamp` | `TIMESTAMPTZ` | `"2026-10-30T10:30:00+03:00"` | Root event generation timestamp |
| **Event Metadata** | `event_id` | `event_id` | `VARCHAR(50)` | `"EVT001"` | Unique event identifier |
| **Event Metadata** | `event_type` | `event_type` | `VARCHAR(60)` | `"ASSIGNMENT_REASSIGNED"` | Lifecycle event type |
| **Event Metadata** | `version` | `event_version` | `BIGINT` | `2` | Event sequence version |
| **Ticket Reference** | `ticket_id` | `ticket.ticket_id` | `VARCHAR(50)` | `"INC1001"` | Primary incident ticket reference |
| **Ticket Reference** | `alarm_id` | `ticket.alarm_id` | `VARCHAR(50)` | `"ALM5001"` | Monitoring alarm reference |
| **Ticket Reference** | *(none)* | `alert_id` | `VARCHAR(50)` | `"ALT1001"` | Dedicated alert reference ID |
| **Ticket Details** | `description` | `ticket.issue_description` | `TEXT` | `"Network connectivity failure"` | Incident problem summary |
| **Ticket Details** | `severity` | `ticket.severity` | `VARCHAR(30)` | `"Critical"` | Priority (`Critical`, `Major`, `Minor`) |
| **Ticket Details** | `ticket_status` | `ticket.ticket_status` | `VARCHAR(50)` | `"Assigned"` | Workflow ticket state |
| **Ticket Details** | `alarm_status` | `ticket.alarm_status` | `VARCHAR(50)` | `"Active"` | Equipment alarm state |
| **Site / Branch** | `site_name` | `branch.branch_name` | `VARCHAR(150)` | `"Riyadh Main Office"` | Physical branch name |
| **Site / Branch** | `site_number` | `branch.site_number` | `VARCHAR(50)` | `"SITE001"` | Site / branch code |
| **Site / Branch** | *(none)* | `branch.branch_id` | `VARCHAR(50)` | `"BR001"` | Branch unique identifier |
| **Assignment** | `assignment_id` | `assignment.assignment_id` | `VARCHAR(50)` | `"ASN002"` | Current dispatch assignment ID |
| **Assignment** | *(none)* | `assignment.assignment_type` | `VARCHAR(30)` | `"REASSIGNMENT"` | Assignment type |
| **Assignment** | *(none)* | `assignment.previous_assignment_id` | `VARCHAR(50)` | `"ASN001"` (or `null`) | Previous assignment ID for reassignment |
| **Assignment** | `date_time` | `assignment.assigned_at` | `TIMESTAMPTZ` | `"2026-10-30T10:30:00+03:00"` | Assignment timestamp |
| **Technician** | `technician_name` | `technician.name` | `VARCHAR(150)` | `"Ali"` | Field engineer name |
| **Technician** | `technician_mobile` | `technician.mobile_number` | `VARCHAR(30)` | `"+9665XXXXXXXX"` | Contact phone number |
| **Technician** | `technician_id` | `technician.technician_id` | `VARCHAR(50)` | `"ENG002"` | Technician employee/staff ID |
| **Technician** | `language` | `technician.preferred_language` | `VARCHAR(20)` | `"English"` / `"Arabic"` | Preferred call audio language |
| **Escalations** | `team_leader_*` | `team_leader.*` | `JSONB` / `VARCHAR` | `{"name":"Mohammed", "mobile_number":"..."}` | 1st escalation contact |
| **Escalations** | `manager_*` | `manager.*` | `JSONB` / `VARCHAR` | `{"name":"Khalid", "mobile_number":"..."}` | 2nd escalation contact |
| **DTMF Config** | `ack_digit` | `acknowledgement.dtmf_digit` | `VARCHAR(10)` | `"1"` | DTMF key to acknowledge call |

---

## 6. Get Alerts API

Retrieves batch alert status, ticket delivery states, and DTMF interaction history without triggering calls.

* **Endpoint:** `/api/getalerts` *(also supports `GET /api/alerts`)*
* **Method:** `GET` *(or `POST`)*
* **Authentication:** Header `X-API-Key: <YOUR_API_KEY>`

### Request Headers
| Header | Value | Required |
| :--- | :--- | :--- |
| `X-API-Key` | `<YOUR_API_KEY>` | **Yes** |

### Success Response (`200 OK`)
```json
{
  "status": "success",
  "mode": "batch",
  "total": 3,
  "successful": 3,
  "failed": 0,
  "authorized": true,
  "auth": {
    "status": "authorized",
    "user": "api_admin",
    "role": "admin",
    "api_key_valid": true,
    "auth_type": "api_key"
  },
  "results": [
    {
      "index": 0,
      "ticket_id": "INC1001",
      "alert_id": "ALT1001",
      "event_id": "EVT001",
      "event_timestamp": "2026-10-30T10:30:00+03:00",
      "ack_digit": "1",
      "reject_digit": "9",
      "dtmf_pressed": "1",
      "ack_number_pressed": "1",
      "acknowledged": true,
      "status": "accepted",
      "http_code": 200,
      "alert": {
        "ticket_id": "INC1001",
        "site_name": "Riyadh Main Office",
        "severity": "Critical",
        "technician_name": "Ali",
        "technician_number": "+9665XXXXXXXX"
      }
    }
  ]
}
```

---

## 7. Twilio Webhooks

The platform exposes two public endpoints for Twilio Voice communications.

### 7.1 Voice Callback
* **Endpoint:** `/twilio/voice`
* **Method:** `POST`
* **URL:** `https://nextaskgw.automationedge.com/twilio/voice?cid={call_id}`
* **Purpose:** Returns the dynamic TwiML (XML) audio script when a call is answered by the technician.

### 7.2 Status Callback
* **Endpoint:** `/twilio/call-status`
* **Method:** `POST`
* **URL:** `https://nextaskgw.automationedge.com/twilio/call-status?cid={call_id}`
* **Purpose:** Receives real-time Twilio call progression events (`initiated`, `ringing`, `answered`, `completed`, `busy`, `no-answer`) and handles auto-escalation.

---

## 8. Error Handling & HTTP Status Codes

| HTTP Code | Name | Scenario |
| :---: | :--- | :--- |
| **`200`** | `OK` | Request succeeded (data retrieved or processed). |
| **`202`** | `Accepted` | Alert accepted and call placed in background queue. |
| **`400`** | `Bad Request` | Malformed JSON body or missing required alert fields. |
| **`401`** | `Unauthorized` | Missing or invalid `X-API-Key` or incorrect credentials. |
| **`404`** | `Not Found` | The requested route does not exist. |
| **`405`** | `Method Not Allowed` | E.g., sending `DELETE` or `PUT` to a POST-only endpoint. |
| **`500`** | `Internal Server Error` | Unhandled runtime server exception. |
