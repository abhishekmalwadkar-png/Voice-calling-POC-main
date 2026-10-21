"""SQLite store: tickets (current-state projection), events, calls, timers (jobs) and the audit log."""
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

from config import CFG

DB_LOCK = threading.RLock()
_TICKET_LOCKS = {}
_TICKET_LOCKS_GUARD = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    alarm_id TEXT, assignment_id TEXT, site_number TEXT, site_name TEXT, severity TEXT,
    status TEXT, alarm_status TEXT, operational_status TEXT, description TEXT,
    ack_digit TEXT, reject_digit TEXT, recipients TEXT, callback_url TEXT,
    version INTEGER, last_event_id TEXT, last_event_time TEXT, last_event_received_at REAL,
    uc1_assignment_id TEXT, uc1_state TEXT, uc1_reason TEXT, uc1_ack_via TEXT, uc1_ack_at TEXT,
    esc_reason TEXT, esc_detail TEXT,
    followup_state TEXT, followup_due_at TEXT,
    created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, ticket_id TEXT, event_type TEXT, version INTEGER, event_time TEXT,
    received_at TEXT, result TEXT, note TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, assignment_id TEXT, workflow TEXT,
    role TEXT, attempt_no INTEGER, recipient_name TEXT, to_number TEXT,
    language TEXT, language_source TEXT, template TEXT, template_version TEXT,
    call_sid TEXT, status TEXT, outcome TEXT, dtmf TEXT, ack_result TEXT, picked_up INTEGER,
    cause_code TEXT, error_message TEXT, duration_sec INTEGER,
    created_at TEXT, initiated_at TEXT, answered_at TEXT, ended_at TEXT,
    superseded TEXT, processed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, assignment_id TEXT, kind TEXT,
    attempt_no INTEGER, run_at REAL, status TEXT, note TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticket_id TEXT, kind TEXT, message TEXT,
    call_id INTEGER, event_id TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS idx_calls_ticket ON calls(ticket_id);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, run_at);
CREATE INDEX IF NOT EXISTS idx_audit_ticket ON audit_log(ticket_id);
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
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_at(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value):
    """ISO-8601 string -> epoch seconds, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def epoch():
    return time.time()


def audit(ticket_id, kind, message, call_id=None, event_id=None, data=None):
    print(f"[{now_iso()}] {ticket_id or '-'} {kind}: {message}", flush=True)
    q("INSERT INTO audit_log (ts, ticket_id, kind, message, call_id, event_id, data) VALUES (?,?,?,?,?,?,?)",
      (now_iso(), ticket_id, kind, message, call_id, event_id, json.dumps(data) if data is not None else None))


def get_ticket(ticket_id):
    t = q("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,), one=True)
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
