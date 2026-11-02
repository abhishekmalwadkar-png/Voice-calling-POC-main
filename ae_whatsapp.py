"""360dialog WhatsApp API client.
Sends outbound WhatsApp messages using 360dialog Cloud API.
API: https://waba-v2.360dialog.io/messages
Header: D360-API-KEY
"""
import json
import urllib.error
import urllib.request

from config import CFG

DEFAULT_360_API_KEY = "xCyHUYyCA24Dq6iWgISlP8hdAK"
DEFAULT_360_URL = "https://waba-v2.360dialog.io/messages"


def send_whatsapp(phone_number, message, timeout=15):
    """Send a WhatsApp text message to a phone number using 360dialog API."""
    api_key = CFG.get("d360_api_key") or DEFAULT_360_API_KEY
    url = CFG.get("d360_api_url") or DEFAULT_360_URL

    clean_number = "".join(str(phone_number).split()).lstrip("+")
    if len(clean_number) == 10:
        clean_number = "91" + clean_number

    clean_message = str(message).replace("\\n", "\n").replace("\r\n", "\n")

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_number,
        "type": "text",
        "text": {
            "body": clean_message
        }
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


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "7709890643"
    msg = sys.argv[2] if len(sys.argv) > 2 else "Test message from 360dialog WhatsApp client"
    print(f"Sending WhatsApp message to {target} via 360dialog...")
    status, body = send_whatsapp(target, msg)
    print(f"Status: {status}, Response: {body}")
