"""Minimal Twilio REST client (standard library only) + webhook signature check."""
import base64
import hashlib
import hmac
import itertools
import json
import urllib.error
import urllib.parse
import urllib.request

from config import CFG

_dry_ids = itertools.count(1)
DRY_RUN_LOG = []  # (action, params) - lets tests see what would have been sent


class TwilioError(RuntimeError):
    pass


def _request(method, path, params):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{CFG['sid']}/{path}"
    data = urllib.parse.urlencode(params, doseq=True).encode()
    req = urllib.request.Request(url, data=data, method=method)
    auth = base64.b64encode(f"{CFG['sid']}:{CFG['token']}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode() or "{}")
        raise TwilioError(f"Twilio {e.code} (code {body.get('code')}): {body.get('message', e)}") from None


def create_call(to_number, call_id):
    """Start an outbound call whose TwiML and status events come back to /twilio/* for this call row."""
    params = {
        "To": to_number,
        "From": CFG["from"],
        "Url": f"{CFG['base_url']}/twilio/voice?cid={call_id}",
        "Method": "POST",
        "StatusCallback": f"{CFG['base_url']}/twilio/call-status?cid={call_id}",
        "StatusCallbackMethod": "POST",
        "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
        "Timeout": str(CFG["ring_timeout"]),
    }
    if CFG["dry_run"]:
        DRY_RUN_LOG.append(("create_call", params))
        return f"DRYCA{next(_dry_ids):06d}"
    return _request("POST", "Calls.json", params)["sid"]


def end_call(call_sid, current_status):
    """Hang up (in progress) or cancel (queued/ringing) a call we no longer want."""
    status = "completed" if current_status == "in-progress" else "canceled"
    if CFG["dry_run"] or not call_sid:
        DRY_RUN_LOG.append(("end_call", {"sid": call_sid, "Status": status}))
        return
    _request("POST", f"Calls/{call_sid}.json", {"Status": status})


def signature_for(url, params):
    """X-Twilio-Signature: base64(HMAC-SHA1(auth token, url + sorted POST key/values))."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(CFG["token"].encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_signature(url, params, signature):
    return bool(signature) and hmac.compare_digest(signature_for(url, params), signature)
