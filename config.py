"""Settings, read once from .env (real environment variables win)."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env(path=os.path.join(BASE_DIR, ".env"), override=False):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            k, v = key.strip(), value.strip()
            if override or k not in os.environ:
                os.environ[k] = v


load_env()


def _env(name, default="", *fallbacks):
    for key in (name, *fallbacks):
        if os.getenv(key, "") != "":
            return os.getenv(key)
    return default


def _int(name, default, *fallbacks):
    return int(_env(name, str(default), *fallbacks))


def _set(name, default):
    return {v.strip().lower() for v in _env(name, default).split(",") if v.strip()}


CFG = {
    # Twilio
    "sid": _env("TWILIO_ACCOUNT_SID"),
    "token": _env("TWILIO_AUTH_TOKEN"),
    "from": "".join(_env("TWILIO_FROM_NUMBER").split()),
    "validate_twilio_signature": _env("VALIDATE_TWILIO_SIGNATURE", "1") == "1",
    # Server
    "base_url": _env("PUBLIC_BASE_URL").rstrip("/"),
    "port": _int("PORT", 5000),
    "dry_run": _env("DRY_RUN", "0") == "1",
    "api_key": _env("API_KEY"),
    "db_path": _env("DB_PATH", os.path.join(BASE_DIR, "flm_calls.db")),
    # Technician (PDF p.2-3: maximum 2 initial attempts)
    "tech_max_attempts": _int("TECH_MAX_ATTEMPTS", 2, "MAX_ATTEMPTS"),
    "tech_retry_sec": _int("TECH_RETRY_INTERVAL_SEC", 60, "RETRY_INTERVAL_SEC"),
    "ring_timeout": _int("RING_TIMEOUT_SEC", 25),
    # Follow-up reminder after acknowledgement (PDF p.4: 15 minutes). 0 = no internal timer:
    # the caller triggers it with POST /api/tickets/{ticket_id}/followup when its own timer fires
    "followup_sec": _int("FOLLOWUP_REMINDER_SEC", 0),
    # Escalation (PDF p.5 / p.7: Team Leader 1 attempt, Manager up to 2)
    "tl_max_attempts": _int("TL_MAX_ATTEMPTS", 1),
    "manager_max_attempts": _int("MANAGER_MAX_ATTEMPTS", 2),
    "manager_retry_sec": _int("MANAGER_RETRY_INTERVAL_SEC", 60),
    # Language (PDF p.2: NexTask preference, configured fallback when absent)
    "default_language": _env("DEFAULT_LANGUAGE", "en"),
    "menu_languages": _env("LANGUAGES", "en,ar,ur"),
    # DTMF (PDF p.7: dynamic NexTask acknowledgement digit)
    "default_ack_digit": _env("DEFAULT_ACK_DIGIT", "1"),
    "default_reject_digit": _env("DEFAULT_REJECT_DIGIT", "9"),
    # State guard (PDF p.7: hold contact when event state is stale). 0 = disabled
    "state_max_age_sec": _int("STATE_MAX_AGE_SEC", 0),
    # NexTask value mappings (PDF p.7 "decisions to confirm")
    "closed_statuses": _set("CLOSED_STATUSES", "closed,resolved,cancelled,canceled"),
    "alarm_cleared_values": _set("ALARM_CLEARED_VALUES", "cleared,clear,inactive"),
    "operational_done_statuses": _set("OPERATIONAL_DONE_STATUSES", "departed,arrived,started"),
    # Outbound notifications (e.g. to drive WhatsApp from another system)
    "webhook_url": _env("RESULT_WEBHOOK_URL"),
    # AutomationEdge WhatsApp Integration
    "ae_auth_secret": _env("AE_AUTH_SECRET", "AistudioQA"),
    "ae_auth_url": _env("AE_AUTH_URL", "https://dev-p187.ac2.automationedge.com/authenticate"),
    "ae_push_url": _env("AE_PUSH_URL", "https://dev-p187.ac2.automationedge.com/api/push-notifications"),
    "ae_service_url": _env("AE_SERVICE_URL", "http://localhost:3978/whatsapp"),
    "ae_bot_id": _env("AE_BOT_ID", "104650799293101"),
}
