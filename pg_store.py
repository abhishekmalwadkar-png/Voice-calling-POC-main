"""
PostgreSQL Storage module for NextAsk Gateway & FLM Helpdesk.
Persists incoming alert intake payloads, tickets, assignments, events, and audit logs
directly into the PostgreSQL production schema defined in FLMHelpdesk (1).sql.
"""

import json
import logging
import threading
from datetime import datetime, timezone

from config import CFG

logger = logging.getLogger("flm.pg_store")

try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import Json
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False
    logger.warning("psycopg2 is not installed; PostgreSQL persistence will be skipped.")

_pg_pool = None
_pool_lock = threading.Lock()


def get_connection():
    """Retrieve a connection from the pool, or direct connection."""
    global _pg_pool
    if not HAS_PSYCOPG2:
        return None

    if not CFG.get("pg_enabled", True):
        return None

    host = CFG.get("pg_host") or "localhost"
    port = int(CFG.get("pg_port") or 5432)
    dbname = CFG.get("pg_name") or "FLM-Helpdesk"
    user = CFG.get("pg_user") or "postgres"
    password = CFG.get("pg_password") or "root"

    try:
        with _pool_lock:
            if _pg_pool is None or _pg_pool.closed:
                _pg_pool = psycopg2.pool.SimpleConnectionPool(
                    minconn=1,
                    maxconn=10,
                    host=host,
                    port=port,
                    dbname=dbname,
                    user=user,
                    password=password,
                    connect_timeout=5
                )
        return _pg_pool.getconn()
    except Exception as e:
        logger.warning(f"Failed to connect to PostgreSQL ({dbname}@{host}:{port}): {e}")
        return None


def release_connection(conn):
    """Return connection back to pool."""
    global _pg_pool
    if conn and _pg_pool and not _pg_pool.closed:
        try:
            _pg_pool.putconn(conn)
        except Exception:
            pass


def init_db():
    """Ensure tables exist according to FLMHelpdesk.sql specification."""
    conn = get_connection()
    if not conn:
        return False

    ddl = """
    CREATE TABLE IF NOT EXISTS public.tb_tickets (
        ticket_id character varying(50) NOT NULL PRIMARY KEY,
        alarm_id character varying(50),
        alert_id character varying(50),
        branch_id character varying(50),
        branch_name character varying(150),
        site_number character varying(50),
        site_name character varying(150),
        issue_description text,
        alarm_type character varying(100),
        severity character varying(30),
        ticket_status character varying(50),
        alarm_status character varying(50),
        operational_status character varying(50),
        current_assignment_id character varying(50),
        emergency_recipients jsonb,
        opened_at timestamp with time zone,
        closed_at timestamp with time zone,
        resolution text,
        last_event_version bigint,
        last_event_at timestamp with time zone,
        updated_at timestamp with time zone DEFAULT now() NOT NULL
    );

    CREATE TABLE IF NOT EXISTS public.tb_assignments (
        assignment_id character varying(50) NOT NULL PRIMARY KEY,
        ticket_id character varying(50) NOT NULL,
        previous_assignment_id character varying(50),
        technician_id character varying(50),
        technician_name character varying(150),
        technician_mobile character varying(30),
        preferred_language character varying(20),
        team_leader_details jsonb,
        manager_details jsonb,
        dtmf_digit character varying(10),
        assignment_type character varying(30),
        assignment_status character varying(30),
        assigned_at timestamp with time zone,
        ended_at timestamp with time zone
    );

    CREATE TABLE IF NOT EXISTS public.tb_nextask_events (
        id BIGSERIAL PRIMARY KEY,
        batch_id character varying(50),
        event_id character varying(50) NOT NULL,
        ticket_id character varying(50),
        assignment_id character varying(50),
        event_type character varying(60) NOT NULL,
        event_version bigint,
        event_timestamp timestamp with time zone,
        received_at timestamp with time zone DEFAULT now() NOT NULL,
        payload_json jsonb NOT NULL,
        processing_status character varying(30) DEFAULT 'Received' NOT NULL,
        error_details text
    );

    CREATE TABLE IF NOT EXISTS public.tb_voice_calls (
        id BIGSERIAL PRIMARY KEY,
        workflow_id character varying(50) NOT NULL,
        assignment_id character varying(50),
        provider_call_id character varying(100),
        recipient_role character varying(30),
        recipient_id character varying(50),
        recipient_name character varying(150),
        mobile_number character varying(30),
        language character varying(20),
        call_type character varying(40),
        attempt_number integer,
        prompt_version character varying(50),
        call_status character varying(40),
        raw_cause_code character varying(100),
        requested_digit character varying(10),
        captured_digit character varying(10),
        dtmf_status character varying(30),
        dtmf_received_at timestamp with time zone,
        failure_reason text,
        started_at timestamp with time zone,
        ended_at timestamp with time zone
    );

    CREATE TABLE IF NOT EXISTS public.tb_audit_logs (
        id BIGSERIAL PRIMARY KEY,
        ticket_id character varying(50),
        assignment_id character varying(50),
        workflow_id character varying(50),
        event_id character varying(50),
        activity_type character varying(60) NOT NULL,
        activity_description text,
        activity_status character varying(30),
        reference_id character varying(100),
        details_json jsonb,
        created_at timestamp with time zone DEFAULT now() NOT NULL
    );
    """
    try:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to initialize PostgreSQL schema: {e}")
        return False
    finally:
        release_connection(conn)


def save_alert_intake(payload):
    """
    Persists an incoming batch or single alert intake request into PostgreSQL.
    Populates tb_nextask_events, tb_tickets, tb_assignments, and tb_audit_logs.
    """
    if not isinstance(payload, dict):
        return False

    conn = get_connection()
    if not conn:
        return False

    batch_id = payload.get("batch_id")
    batch_ts = payload.get("event_timestamp")
    branch = payload.get("branch") if isinstance(payload.get("branch"), dict) else {}
    branch_id = branch.get("branch_id")
    branch_name = branch.get("branch_name")
    site_number = branch.get("site_number")

    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        if "ticket" in payload or "ticket_id" in payload:
            alerts = [payload]
        else:
            release_connection(conn)
            return False

    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        with conn.cursor() as cur:
            for item in alerts:
                if not isinstance(item, dict):
                    continue

                event_id = item.get("event_id") or f"EVT-{int(datetime.now().timestamp()*1000)}"
                event_type = item.get("event_type") or "ASSIGNMENT_CREATED"
                event_version = item.get("event_version") or 1
                event_ts = item.get("event_timestamp") or batch_ts or now_iso
                alert_id = item.get("alert_id")

                ticket_data = item.get("ticket") if isinstance(item.get("ticket"), dict) else {}
                ticket_id = ticket_data.get("ticket_id") or item.get("ticket_id")
                if not ticket_id:
                    continue

                alarm_id = ticket_data.get("alarm_id") or item.get("alarm_id")
                issue_desc = ticket_data.get("issue_description") or item.get("description") or item.get("issue_description")
                alarm_type = ticket_data.get("alarm_type") or item.get("alarm_type")
                severity = ticket_data.get("severity") or item.get("severity") or "Normal"
                ticket_status = ticket_data.get("ticket_status") or item.get("ticket_status") or "Assigned"
                alarm_status = ticket_data.get("alarm_status") or item.get("alarm_status") or "Active"
                site_name = branch_name or ticket_data.get("site_name") or item.get("site_details")

                assignment = item.get("assignment") if isinstance(item.get("assignment"), dict) else {}
                assignment_id = assignment.get("assignment_id") or item.get("assignment_id") or f"{ticket_id}-ASN1"
                prev_assignment_id = assignment.get("previous_assignment_id")
                assignment_type = assignment.get("assignment_type") or ("REASSIGNMENT" if prev_assignment_id else "INITIAL")
                assigned_at = assignment.get("assigned_at") or event_ts

                tech = item.get("technician") if isinstance(item.get("technician"), dict) else {}
                tech_id = tech.get("technician_id") or tech.get("id")
                tech_name = tech.get("name") or tech.get("technician_name")
                tech_mobile = tech.get("mobile_number") or tech.get("mobile") or item.get("phone_number")
                preferred_lang = tech.get("preferred_language") or item.get("language")

                tl = item.get("team_leader") if isinstance(item.get("team_leader"), dict) else None
                mgr = item.get("manager") if isinstance(item.get("manager"), dict) else None

                ack = item.get("acknowledgement") if isinstance(item.get("acknowledgement"), dict) else {}
                dtmf_digit = str(ack.get("dtmf_digit") or item.get("ack_digit") or "1")

                # 1. Insert into tb_nextask_events
                cur.execute("""
                    INSERT INTO public.tb_nextask_events (
                        batch_id, event_id, ticket_id, assignment_id, event_type,
                        event_version, event_timestamp, payload_json, processing_status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    batch_id, event_id, ticket_id, assignment_id, event_type,
                    event_version, event_ts, Json(item), "Received"
                ))

                # 2. Upsert into tb_tickets
                cur.execute("""
                    INSERT INTO public.tb_tickets (
                        ticket_id, alarm_id, alert_id, branch_id, branch_name,
                        site_number, site_name, issue_description, alarm_type,
                        severity, ticket_status, alarm_status, current_assignment_id,
                        opened_at, last_event_version, last_event_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()
                    )
                    ON CONFLICT (ticket_id) DO UPDATE SET
                        alarm_id = EXCLUDED.alarm_id,
                        alert_id = COALESCE(EXCLUDED.alert_id, public.tb_tickets.alert_id),
                        branch_id = COALESCE(EXCLUDED.branch_id, public.tb_tickets.branch_id),
                        branch_name = COALESCE(EXCLUDED.branch_name, public.tb_tickets.branch_name),
                        site_number = COALESCE(EXCLUDED.site_number, public.tb_tickets.site_number),
                        site_name = COALESCE(EXCLUDED.site_name, public.tb_tickets.site_name),
                        issue_description = COALESCE(EXCLUDED.issue_description, public.tb_tickets.issue_description),
                        alarm_type = COALESCE(EXCLUDED.alarm_type, public.tb_tickets.alarm_type),
                        severity = EXCLUDED.severity,
                        ticket_status = EXCLUDED.ticket_status,
                        alarm_status = EXCLUDED.alarm_status,
                        current_assignment_id = EXCLUDED.current_assignment_id,
                        last_event_version = EXCLUDED.last_event_version,
                        last_event_at = EXCLUDED.last_event_at,
                        updated_at = now();
                """, (
                    ticket_id, alarm_id, alert_id, branch_id, branch_name,
                    site_number, site_name, issue_desc, alarm_type,
                    severity, ticket_status, alarm_status, assignment_id,
                    event_ts, event_version, event_ts
                ))

                # 3. Upsert into tb_assignments
                cur.execute("""
                    INSERT INTO public.tb_assignments (
                        assignment_id, ticket_id, previous_assignment_id,
                        technician_id, technician_name, technician_mobile,
                        preferred_language, team_leader_details, manager_details,
                        dtmf_digit, assignment_type, assignment_status, assigned_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (assignment_id) DO UPDATE SET
                        technician_id = EXCLUDED.technician_id,
                        technician_name = EXCLUDED.technician_name,
                        technician_mobile = EXCLUDED.technician_mobile,
                        preferred_language = EXCLUDED.preferred_language,
                        team_leader_details = EXCLUDED.team_leader_details,
                        manager_details = EXCLUDED.manager_details,
                        dtmf_digit = EXCLUDED.dtmf_digit,
                        assignment_type = EXCLUDED.assignment_type,
                        assignment_status = EXCLUDED.assignment_status,
                        assigned_at = EXCLUDED.assigned_at;
                """, (
                    assignment_id, ticket_id, prev_assignment_id,
                    tech_id, tech_name, tech_mobile,
                    preferred_lang, Json(tl) if tl else None, Json(mgr) if mgr else None,
                    dtmf_digit, assignment_type, "Assigned", assigned_at
                ))

                # 4. Insert into tb_audit_logs
                cur.execute("""
                    INSERT INTO public.tb_audit_logs (
                        ticket_id, assignment_id, event_id, activity_type,
                        activity_description, activity_status, details_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    ticket_id, assignment_id, event_id, "ALERT_INGESTED",
                    f"Alert ingested via API for {ticket_id} (batch: {batch_id or '-'})",
                    "SUCCESS", Json({"batch_id": batch_id, "event_type": event_type, "severity": severity})
                ))

        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Error persisting alert to PostgreSQL: {e}")
        return False
    finally:
        release_connection(conn)


def save_voice_call_record(call_dict):
    """Saves outbound call status into tb_voice_calls."""
    conn = get_connection()
    if not conn:
        return False

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.tb_voice_calls (
                    workflow_id, assignment_id, provider_call_id,
                    recipient_role, recipient_name, mobile_number,
                    language, attempt_number, call_status,
                    requested_digit, captured_digit, dtmf_status,
                    failure_reason, started_at, ended_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
            """, (
                call_dict.get("workflow", "uc1_technician"),
                call_dict.get("assignment_id"),
                call_dict.get("call_sid"),
                call_dict.get("role", "technician"),
                call_dict.get("recipient_name"),
                call_dict.get("to_number"),
                call_dict.get("language"),
                call_dict.get("attempt_no", 1),
                call_dict.get("status", "queued"),
                call_dict.get("ack_digit", "1"),
                call_dict.get("dtmf"),
                "ACCEPTED" if call_dict.get("response") == "acknowledged" else "PENDING",
                call_dict.get("error_message"),
                call_dict.get("created_at"),
                call_dict.get("ended_at")
            ))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Error saving voice call record to PostgreSQL: {e}")
        return False
    finally:
        release_connection(conn)
