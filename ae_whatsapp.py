"""AutomationEdge WhatsApp push notification client.
Uses AE_AUTH_SECRET from .env to generate dynamic Bearer tokens on demand.
"""
import json
import urllib.error
import urllib.request

from config import CFG


def get_token(timeout=10):
    """Fetch a fresh dynamic JWT Bearer token from AutomationEdge using AE_AUTH_SECRET."""
    secret = CFG.get("ae_auth_secret") or "aistudio_qa"
    url = CFG.get("ae_auth_url") or "https://dev-p280.ac2.automationedge.com/authenticate"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {secret}"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8").strip()


def send_whatsapp(phone_number, message, timeout=15):
    """Send a WhatsApp message to a phone number using AutomationEdge Push Notifications API."""
    token = get_token()
    push_url = CFG.get("ae_push_url") or "https://dev-p280.ac2.automationedge.com/api/push-notifications"
    bot_id = CFG.get("ae_bot_id") or "717940961400025"
    service_url = CFG.get("ae_service_url") or "http://localhost:3978/whatsapp"
    if "automationedge.com" in service_url:
        service_url = "http://localhost:3978/whatsapp"

    clean_number = "".join(str(phone_number).split()).lstrip("+")
    user_ids = [clean_number]
    if clean_number.startswith("91") and len(clean_number) == 12:
        user_ids.append(clean_number[2:])
    elif len(clean_number) == 10:
        user_ids.append("91" + clean_number)

    items = [
        {
            "bot_id": bot_id,
            "chat_channel": "whatsapp",
            "user_id": uid,
            "service_url": service_url,
            "trigger_aistudio_dialog": {
                "dialog_id": "notification",
                "params": {
                    "message": str(message)
                }
            }
        }
        for uid in set(user_ids)
    ]

    payload = {
        "additionalInfo": {
            "auth_header": f"Bearer {token}"
        },
        "notifications": items
    }

    req = urllib.request.Request(
        push_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "7709890643"
    msg = sys.argv[2] if len(sys.argv) > 2 else "Test notification from ae_whatsapp module using .env"
    print(f"Generating token and sending WhatsApp message to {target}...")
    status, body = send_whatsapp(target, msg)
    print(f"Status: {status}, Response: {body}")
