# FLM Voice Call Platform – How to Run

Python 3.12, standard library only (no `pip install` needed).

## 1. Prerequisites

| Need | Why | Check |
|---|---|---|
| Python 3.12+ | runs the server | `python --version` |
| cloudflared | gives Twilio a public URL to call back | `cloudflared --version` (install: `choco install cloudflared`) |
| Twilio account + phone number | places the real calls | Twilio Console |

## 2. Configure `.env`

Required for real calls:

```
TWILIO_ACCOUNT_SID=ACxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxx
TWILIO_FROM_NUMBER=+15551234567
API_KEY=<any secret string>      # callers of /api/* send it as header X-API-Key
PORT=80
PUBLIC_BASE_URL=                 # empty = auto cloudflared tunnel; or put your ngrok URL
DRY_RUN=0                        # 1 = no real calls, no tunnel (local testing)
```

Other settings (retries, ring timeout, languages, escalation attempts) are documented in the comments inside `.env`.

## 3. Start the server

**Option A – double-click / run `start_server.bat`** (recommended, run as Administrator if port 80 is used by nginx).
It stops any old copy of the server, frees port 80, then runs `python app.py`.

**Option B – manually:**

```
cd "D:\POC\Voice Calling"
python app.py
```

On success you will see:

```
Starting cloudflared tunnel for Twilio webhooks...
Public base URL  : https://xxxx.trycloudflare.com
Dashboard        : http://localhost:80
Event API        : POST https://xxxx.trycloudflare.com/api/events
Follow-up API    : POST https://xxxx.trycloudflare.com/api/tickets/<ticket_id>/followup
Status API       : GET  https://xxxx.trycloudflare.com/api/tickets/<ticket_id>
API key header   : X-API-Key (set from .env)
```

Keep the window open – closing it stops the server. The cloudflared URL changes on every restart.

## 4. Trigger a call

Send an event (PowerShell example, replace URL and key):

```powershell
$base = "https://xxxx.trycloudflare.com"
$body = @'
{
  "event_id": "EVT-1001",
  "event_type": "ASSIGNMENT_CREATED",
  "ack_digit": "1",
  "ticket": {
    "ticket_id": "TKT-1001",
    "assignment_id": "TKT-1001-A1",
    "alarm_id": "ALM-77",
    "site_number": "2040",
    "site_name": "Olaya Branch",
    "severity": "High",
    "status": "Assigned",
    "alarm_status": "Active"
  },
  "recipients": [
    {"role": "technician",  "name": "Ahmed", "mobile": "+911111111111", "language": "en"},
    {"role": "team_leader", "name": "Imran", "mobile": "+912222222222", "language": "ur"},
    {"role": "manager",     "name": "Sara",  "mobile": "+913333333333", "language": "en"}
  ],
  "callback_url": "http://localhost:5058/visit-result"
}
'@
Invoke-RestMethod -Method Post -Uri "$base/api/events" -Headers @{ "X-API-Key" = "<API_KEY>" } -ContentType "application/json" -Body $body
```

Then check status:

```powershell
Invoke-RestMethod -Uri "$base/api/tickets/TKT-1001" -Headers @{ "X-API-Key" = "<API_KEY>" }
```

Trigger the follow-up reminder call:

```powershell
Invoke-RestMethod -Method Post -Uri "$base/api/tickets/TKT-1001/followup" -Headers @{ "X-API-Key" = "<API_KEY>" }
```

Supported `event_type` values: `ASSIGNMENT_CREATED`, `REASSIGNED`, `ASSIGNMENT_ACCEPTED`, `ASSIGNMENT_REJECTED`,
`OPERATIONAL_STATUS`, `ALARM_STATUS`, `TICKET_CREATED`, `TICKET_UPDATED`, `SEVERITY_CHANGED`, `TICKET_CLOSED`.

The same requests are ready-made in **`FLM_Voice_Platform.postman_collection.json`** – import it into Postman, set the base URL and API key variables.

## 5. Other useful endpoints (all need `X-API-Key` except `/api/health`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | server alive check (no key needed) |
| GET | `/api/tickets` | list tickets |
| GET | `/api/calls` | call log |
| GET | `/api/events` | received events |
| GET | `/api/escalations` | escalation calls |
| GET | `/api/exceptions` | failures / exceptions |
| GET | `/api/timers` | pending retries & reminders |
| GET | `/api/audit/export` | download audit trail |
| POST | `/api/call` | legacy single-call API |

## 6. Callback URL – where call results are sent

The callback is a **separate listener on a different port** from the main server:

| Process | Port | Role |
|---|---|---|
| `app.py` | 80 (`PORT` in `.env`) | receives events, places calls, receives Twilio webhooks |
| `callback_receiver.py` | 5058 (default) | sample system that *receives* the call results |

The server **POSTs** each result (acknowledged, no answer, escalated, follow-up result…) to the callback URL,
signed with header `X-Signature` = HMAC-SHA256 of the body using `API_KEY`.

Where the URL comes from (first match wins):
1. `callback_url` field in the event (per ticket)
2. `RESULT_WEBHOOK_URL` in `.env` (one URL for all tickets)
3. none → no notifications are sent

### Local test (both on the same PC)

Terminal 1: `python app.py` (or `start_server.bat`)
Terminal 2:

```
python callback_receiver.py          # http://localhost:5058/visit-result
python callback_receiver.py 6000     # or choose another port
```

Use `"callback_url": "http://localhost:5058/visit-result"` in the event. Results are printed and appended to `callback_results.jsonl`.
`localhost` works here because the server itself sends the callback, not Twilio – no tunnel needed for it.

### Public callback URL via Cloudflare

Run **`start_callback.bat`** instead of `python callback_receiver.py`. It stops any old receiver/tunnel on port 5058,
starts the receiver, opens a second Cloudflare tunnel and prints:

```
Public callback URL: https://yyyy.trycloudflare.com/visit-result
```

Use that as `callback_url` (or `RESULT_WEBHOOK_URL`). Same as `python callback_receiver.py --tunnel`.
The URL changes on every restart. The receiver rejects (401) any request without a valid `X-Signature`.

### Real use (e.g. AutomationEdge workflow webhook)

Put the AE webhook URL as `callback_url` (or `RESULT_WEBHOOK_URL`). It must be reachable **from the PC running `app.py`** –
use the AE server address, not `localhost`, if AE runs on another machine.

## 7. Test without real calls

```
python simulate_test.py
```

Runs a full offline end-to-end test (DRY_RUN, temporary database on port 5055, fake Twilio webhooks). No internet or Twilio needed.

## 8. Troubleshooting

| Message | Fix |
|---|---|
| `Port 80 is already in use` | another copy or nginx is running – use `start_server.bat` as Administrator, or set `PORT=5000` in `.env` |
| `Missing in .env: TWILIO_...` | fill Twilio values, or set `DRY_RUN=1` |
| `cloudflared did not return a public URL` | check internet / cloudflared install, or set `PUBLIC_BASE_URL` to an ngrok URL |
| 401 / unauthorized on `/api/*` | send header `X-API-Key` equal to `API_KEY` in `.env` |

Data is stored in `flm_calls.db` (SQLite) in this folder.
