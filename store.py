"""SQLite store: tickets (current-state projection), events, calls, timers (jobs) and the audit log."""
import json
import sqlite3
import threading
import time
import hashlib
import secrets
from datetime import datetime, timezone, timedelta

from config import CFG

LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))  # IST (UTC+05:30)

DB_LOCK = threading.RLock()
_TICKET_LOCKS = {}
_TICKET_LOCKS_GUARD = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    alert_id TEXT, alarm_id TEXT, assignment_id TEXT, site_number TEXT, site_name TEXT, severity TEXT,
    status TEXT, alarm_status TEXT, operational_status TEXT, description TEXT,
    region TEXT, country_code TEXT,
    ack_digit TEXT, reject_digit TEXT, recipients TEXT, callback_url TEXT,
    version INTEGER, last_event_id TEXT, last_event_time TEXT, last_event_received_at REAL,
    uc1_assignment_id TEXT, uc1_state TEXT, uc1_reason TEXT, uc1_ack_via TEXT, uc1_ack_at TEXT,
    esc_reason TEXT, esc_detail TEXT, uc1_reject_reason TEXT,
    followup_state TEXT, followup_due_at TEXT,
    created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, ticket_id TEXT, event_type TEXT, version INTEGER, event_time TEXT,
    received_at TEXT, result TEXT, note TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, assignment_id TEXT, workflow TEXT,
    role TEXT, attempt_no INTEGER, recipient_name TEXT, to_number TEXT,
    language TEXT, language_source TEXT, template TEXT, template_version TEXT,
    call_sid TEXT, status TEXT, outcome TEXT, dtmf TEXT, ack_result TEXT, reject_reason TEXT, picked_up INTEGER,
    cause_code TEXT, error_message TEXT, duration_sec INTEGER,
    created_at TEXT, initiated_at TEXT, answered_at TEXT, ended_at TEXT,
    superseded TEXT, processed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, assignment_id TEXT, kind TEXT,
    attempt_no INTEGER, run_at REAL, status TEXT, note TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticket_id TEXT, kind TEXT, message TEXT,
    call_id INTEGER, event_id TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT DEFAULT 'admin',
    full_name TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT,
    last_login TEXT);
CREATE TABLE IF NOT EXISTS auth_tokens (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT,
    expires_at REAL);
CREATE INDEX IF NOT EXISTS idx_calls_ticket ON calls(ticket_id);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, run_at);
CREATE INDEX IF NOT EXISTS idx_audit_ticket ON audit_log(ticket_id);
CREATE INDEX IF NOT EXISTS idx_tokens_expires ON auth_tokens(expires_at);
"""

_conn = None


def connect(path=None):
    """(Re)open the database. Tests call this with a temporary path."""
    global _conn
    with DB_LOCK:
        if _conn:
            _conn.close()
        _conn = sqlite3.connect(path or CFG["db_path"], check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _keep_v1_tables()
        _conn.executescript(SCHEMA)
        # Safe column additions for existing tables
        _cols_t = [r[1] for r in _conn.execute("PRAGMA table_info(tickets)")]
        if "uc1_reject_reason" not in _cols_t:
            _conn.execute("ALTER TABLE tickets ADD COLUMN uc1_reject_reason TEXT")
        if "alert_id" not in _cols_t:
            _conn.execute("ALTER TABLE tickets ADD COLUMN alert_id TEXT")
        if "region" not in _cols_t:
            _conn.execute("ALTER TABLE tickets ADD COLUMN region TEXT")
        if "country_code" not in _cols_t:
            _conn.execute("ALTER TABLE tickets ADD COLUMN country_code TEXT")
        _cols_c = [r[1] for r in _conn.execute("PRAGMA table_info(calls)")]
        if "reject_reason" not in _cols_c:
            _conn.execute("ALTER TABLE calls ADD COLUMN reject_reason TEXT")
        # Seed default user if none exists
        count = _conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            def_user = CFG.get("auth_username") or "admin"
            def_pass = CFG.get("auth_password") or "admin123"
            create_user(def_user, def_pass, role="admin", full_name="Administrator")


def _keep_v1_tables():
    """The first POC used a different layout under the same table names: keep it as *_v1 tables."""
    for table, new_column in (("tickets", "uc1_state"), ("events", "event_type")):
        cols = [r[1] for r in _conn.execute(f"PRAGMA table_info({table})")]
        if cols and new_column not in cols:
            _conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")
            print(f"Kept the old '{table}' table as '{table}_v1'", flush=True)


def q(sql, args=(), one=False):
    """Run one statement. INSERT returns the new row id, UPDATE/DELETE the affected row count."""
    with DB_LOCK:
        cur = _conn.execute(sql, args)
        head = sql.lstrip()[:6].upper()
        if head == "INSERT":
            return cur.lastrowid
        if head in ("UPDATE", "DELETE"):
            return cur.rowcount
        rows = [dict(r) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def ticket_lock(ticket_id):
    """Serialises event handling, timers and Twilio callbacks for one ticket."""
    with _TICKET_LOCKS_GUARD:
        return _TICKET_LOCKS.setdefault(ticket_id, threading.RLock())


def now_iso():
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S+05:30")


def iso_at(epoch):
    return datetime.fromtimestamp(epoch, LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S+05:30")


def parse_time(value):
    """ISO-8601 string -> epoch seconds, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)).timestamp()
    except ValueError:
        return None


def epoch():
    return time.time()


def audit(ticket_id, kind, message, call_id=None, event_id=None, data=None):
    print(f"[{now_iso()}] {ticket_id or '-'} {kind}: {message}", flush=True)
    q("INSERT INTO audit_log (ts, ticket_id, kind, message, call_id, event_id, data) VALUES (?,?,?,?,?,?,?)",
      (now_iso(), ticket_id, kind, message, call_id, event_id, json.dumps(data) if data is not None else None))


def get_ticket(ticket_id):
    t = q("SELECT * FROM tickets WHERE ticket_id=? OR alert_id=? OR alarm_id=?", (ticket_id, ticket_id, ticket_id), one=True)
    if t:
        t["recipients"] = json.loads(t["recipients"] or "{}")
    return t


def update_ticket(ticket_id, **fields):
    if not fields:
        return
    if "recipients" in fields and not isinstance(fields["recipients"], str):
        fields["recipients"] = json.dumps(fields["recipients"], ensure_ascii=False)
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    q(f"UPDATE tickets SET {cols} WHERE ticket_id=?", (*fields.values(), ticket_id))


def get_call(call_id):
    return q("SELECT * FROM calls WHERE id=?", (call_id,), one=True)


def update_call(call_id, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    q(f"UPDATE calls SET {cols} WHERE id=?", (*fields.values(), call_id))


# --------------------------------------------------------------------------- Authentication & User Management
def _hash_password(password, salt=None):
    if not salt:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()
    return h, salt


def create_user(username, password, role="admin", full_name="Administrator"):
    pwd_hash, salt = _hash_password(password)
    now = now_iso()
    q("INSERT OR REPLACE INTO users (username, password_hash, salt, role, full_name, is_active, created_at) "
      "VALUES (?, ?, ?, ?, ?, 1, ?)",
      (username.strip(), pwd_hash, salt, role, full_name, now))
    return {"username": username.strip(), "role": role, "full_name": full_name}


def verify_user(username, password):
    if not username or not password:
        return None
    u = q("SELECT * FROM users WHERE username = ? COLLATE NOCASE AND is_active = 1", (username.strip(),), one=True)
    if u:
        h, _ = _hash_password(password, u["salt"])
        if h == u["password_hash"]:
            q("UPDATE users SET last_login = ? WHERE id = ?", (now_iso(), u["id"]))
            return dict(u)

    # Fallback / sync from .env configuration
    env_user = CFG.get("auth_username") or "admin"
    env_pass = CFG.get("auth_password") or "admin123"
    if env_user and env_pass and username.strip().lower() == env_user.lower() and password == env_pass:
        create_user(username.strip(), password, role="admin", full_name="Administrator")
        return {"username": username.strip(), "role": "admin", "full_name": "Administrator"}
    return None


def create_auth_token(username, ttl_sec=86400):
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + ttl_sec
    q("INSERT OR REPLACE INTO auth_tokens (token, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
      (token, username.strip(), now_iso(), expires_at))
    return token, ttl_sec


def verify_auth_token(token):
    if not token:
        return None
    row = q("SELECT * FROM auth_tokens WHERE token = ?", (str(token).strip(),), one=True)
    if not row:
        return None
    if row["expires_at"] and row["expires_at"] < time.time():
        q("DELETE FROM auth_tokens WHERE token = ?", (str(token).strip(),))
        return None
    return row["username"]

