"""PostgreSQL store: tickets (current-state projection), events, calls, timers (jobs), the audit log, users and
WhatsApp messages. Everything lives in the PostgreSQL database configured by DB_HOST / DB_PORT / DB_NAME /
DB_USER / DB_PASSWORD, in its own schema (RUNTIME_SCHEMA, default "flm_runtime") next to the production tb_* tables.
There is no local database file: the app, the AutomationEdge API and the dashboard all use the same PostgreSQL."""
import json
import re
import threading
import time
import hashlib
import secrets
from datetime import datetime, timezone, timedelta

from config import CFG

try:
    import psycopg2
    from psycopg2 import pool as _pgpool
    from psycopg2.extras import RealDictCursor
except ImportError:  # pragma: no cover
    psycopg2 = None

LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))  # IST (UTC+05:30)

DB_LOCK = threading.RLock()
_TICKET_LOCKS = {}
_TICKET_LOCKS_GUARD = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    seq BIGSERIAL,
    ticket_id TEXT PRIMARY KEY,
    alert_id TEXT, alarm_id TEXT, assignment_id TEXT, site_number TEXT, site_name TEXT, severity TEXT,
    status TEXT, alarm_status TEXT, operational_status TEXT, description TEXT,
    region TEXT, country_code TEXT,
    ack_digit TEXT, reject_digit TEXT, recipients TEXT, callback_url TEXT,
    version INTEGER, last_event_id TEXT, last_event_time TEXT, last_event_received_at DOUBLE PRECISION,
    uc1_assignment_id TEXT, uc1_state TEXT, uc1_reason TEXT, uc1_ack_via TEXT, uc1_ack_at TEXT,
    esc_reason TEXT, esc_detail TEXT, uc1_reject_reason TEXT,
    followup_state TEXT, followup_due_at TEXT,
    created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS events (
    seq BIGSERIAL,
    event_id TEXT PRIMARY KEY, ticket_id TEXT, event_type TEXT, version INTEGER, event_time TEXT,
    received_at TEXT, result TEXT, note TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS calls (
    id BIGSERIAL PRIMARY KEY, ticket_id TEXT, assignment_id TEXT, workflow TEXT,
    role TEXT, attempt_no INTEGER, recipient_name TEXT, to_number TEXT,
    language TEXT, language_source TEXT, template TEXT, template_version TEXT,
    call_sid TEXT, status TEXT, outcome TEXT, dtmf TEXT, ack_result TEXT, reject_reason TEXT, picked_up INTEGER,
    cause_code TEXT, error_message TEXT, duration_sec INTEGER,
    created_at TEXT, initiated_at TEXT, answered_at TEXT, ended_at TEXT,
    superseded TEXT, processed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs (
    id BIGSERIAL PRIMARY KEY, ticket_id TEXT, assignment_id TEXT, kind TEXT,
    attempt_no INTEGER, run_at DOUBLE PRECISION, status TEXT, note TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY, ts TEXT, ticket_id TEXT, kind TEXT, message TEXT,
    call_id BIGINT, event_id TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL,
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
    expires_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS whatsapp_messages (
    id BIGSERIAL PRIMARY KEY,
    workflow_id TEXT,
    ticket_id TEXT,
    assignment_id TEXT,
    recipient_role TEXT,
    recipient_id TEXT,
    recipient_name TEXT,
    mobile_number TEXT,
    message_type TEXT,
    provider_message_id TEXT,
    template_name TEXT,
    template_version TEXT,
    delivery_status TEXT,
    submitted_at TEXT,
    sent_at TEXT,
    delivered_at TEXT,
    read_at TEXT,
    failure_reason TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username ON users (LOWER(username));
CREATE INDEX IF NOT EXISTS idx_tickets_alert ON tickets (LOWER(alert_id));
CREATE INDEX IF NOT EXISTS idx_tickets_alarm ON tickets (LOWER(alarm_id));
CREATE INDEX IF NOT EXISTS idx_calls_ticket ON calls(ticket_id);
CREATE INDEX IF NOT EXISTS idx_whatsapp_ticket ON whatsapp_messages(ticket_id);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, run_at);
CREATE INDEX IF NOT EXISTS idx_audit_ticket ON audit_log(ticket_id);
CREATE INDEX IF NOT EXISTS idx_tokens_expires ON auth_tokens(expires_at);
"""

# Tables whose INSERT hands back the generated id (what SQLite called lastrowid)
_ID_TABLES = {"calls", "jobs", "audit_log", "users", "whatsapp_messages"}
# INSERT OR REPLACE -> INSERT ... ON CONFLICT (<key>) DO UPDATE; value = (conflict target, column to leave out)
_CONFLICT_KEY = {"events": ("event_id", "event_id"),
                 "users": ("(LOWER(username))", "username"),
                 "auth_tokens": ("token", "token")}

_pool = None
_schema = None


def _schema_name(name=None):
    name = str(name or CFG.get("runtime_schema") or "flm_runtime").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"invalid runtime schema name: {name!r}")
    return name


def _dsn_kwargs():
    return dict(host=CFG.get("pg_host") or "localhost", port=int(CFG.get("pg_port") or 5432),
                dbname=CFG.get("pg_name") or "FLM-Helpdesk", user=CFG.get("pg_user") or "postgres",
                password=CFG.get("pg_password") or "root", connect_timeout=10)


def connect(schema=None):
    """(Re)open the PostgreSQL pool and make sure the runtime schema and tables exist. Tests pass their own schema."""
    global _pool, _schema
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed; run: pip install psycopg2-binary")
    with DB_LOCK:
        if _pool:
            _pool.closeall()
            _pool = None
        name = _schema_name(schema)
        kw = _dsn_kwargs()
        try:
            boot = psycopg2.connect(**kw)
        except psycopg2.Error as e:
            raise RuntimeError(f"Cannot connect to PostgreSQL ({kw['dbname']}@{kw['host']}:{kw['port']}): "
                               f"{str(e).strip()} - check DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD.") from e
        try:
            boot.autocommit = True
            with boot.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{name}"')
                cur.execute(f'SET search_path TO "{name}", public')
                cur.execute("SELECT pg_advisory_lock(727001)")  # two processes starting together must not race on DDL
                try:
                    cur.execute(SCHEMA)
                finally:
                    cur.execute("SELECT pg_advisory_unlock(727001)")
        finally:
            boot.close()
        _schema = name
        _pool = _pgpool.ThreadedConnectionPool(1, 12, options=f'-c search_path="{name}",public',
                                               application_name="flm-workflow", **kw)
        if (q("SELECT COUNT(*) AS n FROM users", one=True) or {}).get("n", 0) == 0:
            create_user(CFG.get("auth_username") or "admin", CFG.get("auth_password") or "admin123",
                        role="admin", full_name="Administrator")


def close_pool():
    """Close every pooled connection (used before a hot reload so the old pool does not stay open)."""
    global _pool
    with DB_LOCK:
        if _pool:
            try:
                _pool.closeall()
            except Exception:
                pass
            _pool = None


def drop_schema(schema):
    """Remove a (test) schema and everything in it."""
    global _pool
    name = _schema_name(schema)
    if name == _schema_name():
        raise ValueError("refusing to drop the production runtime schema")
    with DB_LOCK:
        if _pool:
            _pool.closeall()
            _pool = None
        c = psycopg2.connect(**_dsn_kwargs())
        try:
            c.autocommit = True
            with c.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        finally:
            c.close()


_RE_NOCASE = re.compile(r"([\w.]+)\s*=\s*\?\s+COLLATE\s+NOCASE", re.I)
_RE_INSERT = re.compile(r"^\s*INSERT\s+(OR\s+(?:REPLACE|IGNORE)\s+)?INTO\s+(\w+)\s*\(([^)]*)\)", re.I)


def _translate(sql):
    """The SQLite-flavoured SQL used across the app -> PostgreSQL. Returns (sql, returns_id)."""
    sql = _RE_NOCASE.sub(r"LOWER(\1)=LOWER(?)", sql)
    sql = re.sub(r"\browid\b", "seq", sql, flags=re.I)
    sql = sql.replace("%", "%%").replace("?", "%s")
    returns_id = False
    m = _RE_INSERT.match(sql)
    if m:
        mode, table, cols = (m.group(1) or "").strip().upper(), m.group(2).lower(), m.group(3)
        if mode == "OR REPLACE":
            sql = re.sub(r"INSERT\s+OR\s+REPLACE", "INSERT", sql, count=1, flags=re.I)
            key = _CONFLICT_KEY.get(table)
            if key:
                names = [c.strip() for c in cols.split(",") if c.strip()]
                sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in names if c != key[1])
                sql = sql.rstrip().rstrip(";") + f" ON CONFLICT ({key[0]}) DO UPDATE SET {sets}"
        elif mode == "OR IGNORE":
            sql = re.sub(r"INSERT\s+OR\s+IGNORE", "INSERT", sql, count=1, flags=re.I)
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        if table in _ID_TABLES:
            sql += " RETURNING id"
            returns_id = True
    return sql, returns_id


def _adapt(args):
    return tuple(int(a) if isinstance(a, bool) else a for a in args)


def _get_conn():
    """A pooled connection; when all are busy, wait briefly for one instead of failing at once."""
    for _ in range(500):
        try:
            return _pool.getconn()
        except _pgpool.PoolError:
            time.sleep(0.02)
    raise RuntimeError("workflow database pool exhausted (all connections busy for 10 s)")


def q(sql, args=(), one=False):
    """Run one statement. INSERT returns the new row id, UPDATE/DELETE the affected row count."""
    if _pool is None:
        with DB_LOCK:                 # many threads can arrive together (e.g. right after a reload): open the pool once
            if _pool is None:
                connect()
    text, returns_id = _translate(sql)
    conn = _get_conn()
    broken = False
    try:
        conn.autocommit = True
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(text, _adapt(args))
            head = sql.lstrip()[:6].upper()
            if head == "INSERT":
                row = cur.fetchone() if returns_id else None
                return row["id"] if row else None
            if head in ("UPDATE", "DELETE"):
                return cur.rowcount
            rows = [dict(r) for r in cur.fetchall()]
    except psycopg2.OperationalError:
        broken = True
        raise
    finally:
        _pool.putconn(conn, close=broken or bool(conn.closed))
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
    t = q("SELECT * FROM tickets WHERE ticket_id=? COLLATE NOCASE OR alert_id=? COLLATE NOCASE OR alarm_id=? COLLATE NOCASE", (ticket_id, ticket_id, ticket_id), one=True)
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
    q(f"UPDATE tickets SET {cols} WHERE ticket_id=? COLLATE NOCASE", (*fields.values(), ticket_id))


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



def record_whatsapp_message(workflow_id, ticket_id, assignment_id, recipient_role,
                            recipient_id, recipient_name, mobile_number, message_type,
                            provider_message_id, template_name, template_version,
                            delivery_status, submitted_at, sent_at, failure_reason=None):
    return q("""INSERT INTO whatsapp_messages (
        workflow_id, ticket_id, assignment_id, recipient_role, recipient_id,
        recipient_name, mobile_number, message_type, provider_message_id,
        template_name, template_version, delivery_status, submitted_at, sent_at, failure_reason
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        workflow_id, ticket_id, assignment_id, recipient_role, recipient_id,
        recipient_name, mobile_number, message_type, provider_message_id,
        template_name, template_version, delivery_status, submitted_at, sent_at, failure_reason
    ))


def get_whatsapp_messages(ticket_id):
    rows = q("SELECT * FROM whatsapp_messages WHERE ticket_id=? ORDER BY id ASC", (ticket_id,))
    if not rows:
        return []
    res = []
    for r in rows:
        d = dict(r)
        res.append({
            "id": d.get("id"),
            "workflow_id": d.get("workflow_id"),
            "ticket_id": d.get("ticket_id"),
            "assignment_id": d.get("assignment_id"),
            "recipient_role": d.get("recipient_role"),
            "recipient_id": d.get("recipient_id") or "",
            "recipient_name": d.get("recipient_name") or "",
            "mobile_number": d.get("mobile_number"),
            "message_type": d.get("message_type"),
            "provider_message_id": d.get("provider_message_id"),
            "template_name": d.get("template_name"),
            "template_version": d.get("template_version") or "v1",
            "delivery_status": d.get("delivery_status") or "sent",
            "submitted_at": d.get("submitted_at"),
            "sent_at": d.get("sent_at"),
            "delivered_at": d.get("delivered_at"),
            "read_at": d.get("read_at"),
            "failure_reason": d.get("failure_reason")
        })
    return res
