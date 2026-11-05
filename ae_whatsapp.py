"""360dialog WhatsApp API client using pre-approved Meta WABA Templates.
API: https://waba-v2.360dialog.io/messages
Header: D360-API-KEY
"""
import json
import urllib.error
import urllib.request
from datetime import datetime

from config import CFG

DEFAULT_360_API_KEY = "xCyHUYyCA24Dq6iWgISlP8hdAK"
DEFAULT_360_URL = "https://waba-v2.360dialog.io/messages"


def _format_date(dt_str=None):
    try:
        if dt_str:
            d = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
            return d.strftime("%d-%b-%Y %I:%M %p")
    except Exception:
        pass
    return datetime.now().strftime("%d-%b-%Y %I:%M %p")


def send_whatsapp_template(phone_number, template_name, parameters, lang_code="en", timeout=15):
    """Send an approved WhatsApp template via 360dialog."""
    api_key = CFG.get("d360_api_key") or DEFAULT_360_API_KEY
    url = CFG.get("d360_api_url") or DEFAULT_360_URL

    clean_number = "".join(str(phone_number).split()).lstrip("+")
    if len(clean_number) == 10:
        clean_number = "91" + clean_number

    body_params = [{"type": "text", "text": str(p)} for p in parameters]

    payload = {
        "to": clean_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {
                "code": lang_code
            },
            "components": [
                {
                    "type": "body",
                    "parameters": body_params
                }
            ]
        },
        "messaging_product": "whatsapp"
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "D360-API-KEY": api_key
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")


def send_no_answer_alert(phone_number, ticket_id, site, tech_name, attempt_no, date_str=None):
    """
    Template: shabakkat_noanswer (6 parameters):
    1: ticket_id
    2: site
    3: technician_name
    4: attempt_no
    5: reason / status ("no answer" or "Did not answer")
    6: date (e.g. 02-Nov-2026)
    """
    date_val = _format_date(date_str)
    params = [ticket_id, site, tech_name, attempt_no, "no answer", date_val]
    return send_whatsapp_template(phone_number, "shabakkat_noanswer", params)


def send_explicit_rejection_alert(phone_number, ticket_id, site, tech_name, reason_desc, attempt_no, date_str=None):
    """
    Template: shabakkat_explicitrejection (7 parameters):
    1: ticket_id
    2: site
    3: technician_name
    4: reason_desc (e.g. "Busy on another site")
    5: reason_desc (repeated/detail)
    6: attempt_no
    7: date (e.g. 02-Nov-2026)
    """
    date_val = _format_date(date_str)
    r_desc = reason_desc or "Declined assignment"
    params = [ticket_id, site, tech_name, r_desc, r_desc, attempt_no, date_val]
    return send_whatsapp_template(phone_number, "shabakkat_explicitrejection", params)


def _format_datetime(dt_str=None):
    try:
        if dt_str:
            d = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
            return d.strftime("%d-%b-%Y %I:%M %p")
    except Exception:
        pass
    return datetime.now().strftime("%d-%b-%Y %I:%M %p")


def _clean(value):
    """Template parameters cannot contain line breaks or tabs."""
    return " ".join(str(value if value is not None else "-").split()) or "-"


def send_emergency_opening_alert(phone_number, ticket_id, site, severity, issue, tech_name,
                                 status="New", date_str=None):
    """
    Template: shabakkat_emergency_opening1 (7 parameters):
    1: ticket_id
    2: site
    3: severity
    4: issue / description
    5: date and time (e.g. 01-Nov-2026 5:00 PM)
    6: technician name
    7: status ("New" for a new emergency, "Escalated" when it is escalated)
    """
    params = [_clean(ticket_id), _clean(site), _clean(severity), _clean(issue),
              _format_datetime(date_str), _clean(tech_name), _clean(status)]
    return send_whatsapp_template(phone_number, "shabakkat_emergency_opening1", params)


def send_emergency_closure_alert(phone_number, ticket_id, site, closed_at, resolution, status="Resolved"):
    """
    Template: shabakkat_emergency_closure1 (5 parameters):
    1: ticket_id
    2: site
    3: closure date and time (e.g. 01-Nov-2026 5:00 PM)
    4: status ("Resolved")
    5: resolution text
    """
    params = [_clean(ticket_id), _clean(site), _format_datetime(closed_at), _clean(status), _clean(resolution)]
    return send_whatsapp_template(phone_number, "shabakkat_emergency_closure1", params)


# Backwards compatibility fallback if free text is invoked
def send_whatsapp(phone_number, message, timeout=15):
    """Fallback text sender."""
    api_key = CFG.get("d360_api_key") or DEFAULT_360_API_KEY
    url = CFG.get("d360_api_url") or DEFAULT_360_URL
    clean_number = "".join(str(phone_number).split()).lstrip("+")
    if len(clean_number) == 10:
        clean_number = "91" + clean_number

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_number,
        "type": "text",
        "text": {"body": str(message)}
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "D360-API-KEY": api_key},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")
