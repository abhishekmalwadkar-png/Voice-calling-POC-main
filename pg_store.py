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

    CREATE TABLE IF NOT EXISTS public.tb_whatsapp_messages (
        id BIGSERIAL PRIMARY KEY,
        workflow_id character varying(50),
        ticket_id character varying(50) NOT NULL,
        assignment_id character varying(50),
        recipient_role character varying(30),
        recipient_id character varying(50),
        recipient_name character varying(150),
        mobile_number character varying(30),
        message_type character varying(40),
        provider_message_id character varying(100),
        template_name character varying(100),
        template_version character varying(50),
        delivery_status character varying(30),
        submitted_at timestamp with time zone,
        sent_at timestamp with time zone,
        delivered_at timestamp with time zone,
        read_at timestamp with time zone,
        failure_reason text
    );
    ALTER TABLE public.tb_whatsapp_messages ADD COLUMN IF NOT EXISTS channel character varying(10) DEFAULT 'sms';
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
                try:
                    event_version = int(float(str(item.get("event_version", "1"))))
                except Exception:
                    event_version = 1
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
                operational_status = ticket_data.get("operational_status") or item.get("operational_status")
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

                # 1. Insert/Upsert into tb_tickets FIRST (required by FK constraint tb_nextask_events_ticket_id_fkey)
                cur.execute("""
                    INSERT INTO public.tb_tickets (
                        ticket_id, alarm_id, alert_id, branch_id, branch_name,
                        site_number, site_name, issue_description, alarm_type,
                        severity, ticket_status, alarm_status, operational_status,
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
                        operational_status = COALESCE(EXCLUDED.operational_status, public.tb_tickets.operational_status),
                        last_event_version = EXCLUDED.last_event_version,
                        last_event_at = EXCLUDED.last_event_at,
                        updated_at = now();
                """, (
                    ticket_id, alarm_id, alert_id, branch_id, branch_name,
                    site_number, site_name, issue_desc, alarm_type,
                    severity, ticket_status, alarm_status, operational_status,
                    event_ts, event_version, event_ts
                ))

                # 2. Upsert into tb_nextask_events (Event ID is unique; update payload if same event_id arrives)
                cur.execute("""
                    INSERT INTO public.tb_nextask_events (
                        batch_id, event_id, ticket_id, assignment_id, event_type,
                        event_version, event_timestamp, payload_json, processing_status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_id) DO UPDATE SET
                        batch_id = EXCLUDED.batch_id,
                        ticket_id = EXCLUDED.ticket_id,
                        assignment_id = EXCLUDED.assignment_id,
                        event_type = EXCLUDED.event_type,
                        event_version = EXCLUDED.event_version,
                        event_timestamp = EXCLUDED.event_timestamp,
                        payload_json = EXCLUDED.payload_json,
                        received_at = now();
                """, (
                    batch_id, event_id, ticket_id, assignment_id, event_type,
                    event_version, event_ts, Json(item), "Received"
                ))

                # 3. Ensure previous_assignment_id exists to satisfy FK constraint tb_assignments_previous_assignment_id_fkey
                if prev_assignment_id:
                    cur.execute("""
                        INSERT INTO public.tb_assignments (
                            assignment_id, ticket_id, assignment_type, assignment_status, assigned_at
                        ) VALUES (%s, %s, 'INITIAL', 'Replaced', %s)
                        ON CONFLICT (assignment_id) DO NOTHING;
                    """, (prev_assignment_id, ticket_id, assigned_at))

                # Insert/Upsert into tb_assignments (Assignment ID, assignment type, technician details, Team Leader and Manager details, DTMF digit, assignment timestamp)
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

                # 4. Insert into tb_audit_logs (Event received, validation result, ticket creation, assignment creation)
                cur.execute("""
                    INSERT INTO public.tb_audit_logs (
                        ticket_id, assignment_id, event_id, activity_type,
                        activity_description, activity_status, details_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    ticket_id, assignment_id, event_id, "ALERT_INGESTED",
                    f"Event received & validated for ticket {ticket_id}, assignment {assignment_id} created",
                    "SUCCESS", Json({
                        "batch_id": batch_id,
                        "event_type": event_type,
                        "severity": severity,
                        "validation": "PASSED",
                        "ticket_created": True,
                        "assignment_created": True
                    })
                ))

                # 5. UPDATE tb_tickets: Set current_assignment_id after inserting the assignment
                cur.execute("""
                    UPDATE public.tb_tickets
                    SET current_assignment_id = %s, updated_at = now()
                    WHERE ticket_id = %s;
                """, (assignment_id, ticket_id))

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



def save_whatsapp_message_pg(msg_dict):
    """Insert sent WhatsApp message details into tb_whatsapp_messages in PostgreSQL."""
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.tb_whatsapp_messages (
                    workflow_id, ticket_id, assignment_id, recipient_role,
                    recipient_id, recipient_name, mobile_number, message_type,
                    provider_message_id, template_name, template_version,
                    delivery_status, submitted_at, sent_at, failure_reason, channel
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
            """, (
                msg_dict.get("workflow_id"),
                msg_dict.get("ticket_id"),
                msg_dict.get("assignment_id"),
                msg_dict.get("recipient_role"),
                msg_dict.get("recipient_id"),
                msg_dict.get("recipient_name"),
                msg_dict.get("mobile_number"),
                msg_dict.get("message_type"),
                msg_dict.get("provider_message_id"),
                msg_dict.get("template_name"),
                msg_dict.get("template_version", "v1"),
                msg_dict.get("delivery_status", "sent"),
                msg_dict.get("submitted_at"),
                msg_dict.get("sent_at"),
                msg_dict.get("failure_reason"),
                msg_dict.get("channel") or "sms"
            ))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Error saving whatsapp message to PostgreSQL: {e}")
        return False
    finally:
        release_connection(conn)


def save_pushed_records(items):
    """
    Persists pushed ticket/task records into PostgreSQL:
    tb_tickets, tb_assignments, tb_nextask_events, tb_audit_logs.
    """
    if not isinstance(items, list):
        items = [items]
    conn = get_connection()
    if not conn:
        return False
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with conn.cursor() as cur:
            for item in items:
                if not isinstance(item, dict):
                    continue
                tid = str(item.get("task_id") or item.get("ticket_id") or "").strip().upper()
                if not tid:
                    continue
                cur.execute("SELECT 1 FROM public.tb_tickets WHERE UPPER(ticket_id)=UPPER(%s) LIMIT 1", (tid,))
                if cur.fetchone():
                    # Ticket already exists - skip inserting duplicate
                    continue
                alarm_id = str(item.get("alarm_id") or item.get("issue_type") or f"ALM-{tid}").strip()
                alert_id = str(item.get("alert_id") or tid).strip()
                site_num = str(item.get("site_number") or "").strip()
                site_name = str(item.get("site_name") or (f"Site {site_num}" if site_num else "Main Site")).strip()
                issue_desc = str(item.get("issue_description") or item.get("description") or item.get("issue_type") or "Issue reported").strip()
                alarm_type = str(item.get("issue_type") or item.get("alarm_type") or "Network/Hardware").strip()
                severity = str(item.get("severity") or "Normal").strip()
                ticket_status = str(item.get("task_status") or item.get("ticket_status") or item.get("status") or "Dispatch").strip()
                alarm_status = str(item.get("alarm_status") or "Active").strip()
                created_date = str(item.get("created_date") or item.get("created_at") or now_iso).strip()
                aid = str(item.get("assignment_id") or f"{tid}-ASN1").strip()

                tech_name = str(item.get("technician_name") or "").strip()
                tech_mob = str(item.get("technician_mobile") or item.get("phone_number") or "").strip()
                tech_lang = str(item.get("technician_preferred_lang") or item.get("technician_language") or "English").strip()

                tl_name = str(item.get("teamleader_name") or item.get("team_leader_name") or "").strip()
                tl_mob = str(item.get("teamleader_mobile") or item.get("team_leader_mobile") or "").strip()
                tl_lang = str(item.get("teamleader_preferred_lang") or item.get("team_leader_language") or "Arabic").strip()
                tl_dict = {"name": tl_name, "mobile": tl_mob, "preferred_language": tl_lang} if tl_mob else None

                mgr_name = str(item.get("manager_name") or "").strip()
                mgr_mob = str(item.get("manager_mobile") or "").strip()
                mgr_lang = str(item.get("manager_preferred_lang") or item.get("manager_language") or "English").strip()
                mgr_dict = {"name": mgr_name, "mobile": mgr_mob, "preferred_language": mgr_lang} if mgr_mob else None

                # 1. Upsert tb_tickets
                cur.execute("""
                    INSERT INTO public.tb_tickets (
                        ticket_id, alarm_id, alert_id, site_number, site_name,
                        issue_description, alarm_type, severity, ticket_status,
                        alarm_status, opened_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (ticket_id) DO UPDATE SET
                        alarm_id = EXCLUDED.alarm_id,
                        alert_id = EXCLUDED.alert_id,
                        site_number = EXCLUDED.site_number,
                        site_name = EXCLUDED.site_name,
                        issue_description = EXCLUDED.issue_description,
                        alarm_type = EXCLUDED.alarm_type,
                        severity = EXCLUDED.severity,
                        ticket_status = EXCLUDED.ticket_status,
                        alarm_status = EXCLUDED.alarm_status,
                        updated_at = now();
                """, (tid, alarm_id, alert_id, site_num, site_name, issue_desc, alarm_type, severity, ticket_status, alarm_status, created_date))

                # 2. Upsert tb_assignments
                cur.execute("""
                    INSERT INTO public.tb_assignments (
                        assignment_id, ticket_id, technician_name, technician_mobile,
                        preferred_language, team_leader_details, manager_details,
                        assignment_type, assignment_status, assigned_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'INITIAL', %s, %s)
                    ON CONFLICT (assignment_id) DO UPDATE SET
                        technician_name = EXCLUDED.technician_name,
                        technician_mobile = EXCLUDED.technician_mobile,
                        preferred_language = EXCLUDED.preferred_language,
                        team_leader_details = EXCLUDED.team_leader_details,
                        manager_details = EXCLUDED.manager_details,
                        assignment_status = EXCLUDED.assignment_status,
                        assigned_at = EXCLUDED.assigned_at;
                """, (aid, tid, tech_name, tech_mob, tech_lang, Json(tl_dict) if tl_dict else None, Json(mgr_dict) if mgr_dict else None, ticket_status, created_date))

                cur.execute("UPDATE public.tb_tickets SET current_assignment_id = %s WHERE ticket_id = %s", (aid, tid))

                # 3. Insert tb_nextask_events
                evt_id = f"EVT-PUSH-{int(datetime.now().timestamp()*1000)}-{tid}"
                cur.execute("""
                    INSERT INTO public.tb_nextask_events (
                        event_id, ticket_id, assignment_id, event_type,
                        event_version, event_timestamp, payload_json, processing_status
                    ) VALUES (%s, %s, %s, 'ALERT_INGESTED', 1, %s, %s, 'STORED')
                    ON CONFLICT (event_id) DO NOTHING;
                """, (evt_id, tid, aid, created_date, Json(item)))

                # 4. Insert tb_audit_logs
                cur.execute("""
                    INSERT INTO public.tb_audit_logs (
                        ticket_id, assignment_id, event_id, activity_type,
                        activity_description, activity_status, details_json
                    ) VALUES (%s, %s, %s, 'ALERT_INGESTED', %s, 'SUCCESS', %s);
                """, (tid, aid, evt_id, f"New alert ingested for ticket {tid}", Json(item)))

        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Error persisting pushed records to PostgreSQL: {e}")
        return False
    finally:
        release_connection(conn)


def save_updated_records(items):
    """
    Persists updates (REASSIGNMENT, STATUS_UPDATE, etc.) into PostgreSQL:
    tb_tickets, tb_assignments, tb_nextask_events, tb_audit_logs.
    """
    if not isinstance(items, list):
        items = [items]
    conn = get_connection()
    if not conn:
        return False
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with conn.cursor() as cur:
            for item in items:
                if not isinstance(item, dict):
                    continue
                tid = str(item.get("task_id") or item.get("ticket_id") or "").strip().upper()
                if not tid:
                    continue
                update_type = str(item.get("update_type") or "STATUS_UPDATE").strip().upper()

                cur.execute("SELECT current_assignment_id, ticket_status, ticket_id FROM public.tb_tickets WHERE UPPER(ticket_id)=UPPER(%s)", (tid,))
                row = cur.fetchone()
                current_aid = row[0] if row else None

                task_status = str(item.get("task_status") or item.get("ticket_status") or item.get("status") or "").strip()
                alarm_status = str(item.get("alarm_status") or "").strip()
                severity = str(item.get("severity") or "").strip()

                if update_type in ("REASSIGNMENT", "REASSIGN"):
                    tech_name = str(item.get("technician_name") or "").strip()
                    tech_mob = str(item.get("technician_mobile") or item.get("phone_number") or "").strip()
                    tech_lang = str(item.get("technician_preferred_lang") or item.get("technician_language") or "English").strip()

                    new_aid = str(item.get("assignment_id") or f"{tid}-ASN{int(datetime.now().timestamp()*1000)%10000}").strip()

                    # Team leader / manager: use the ones sent with the update, else keep the previous assignment's.
                    def _contact(prefix, *aliases):
                        mobile = ""
                        for key in (f"{prefix}_mobile", *[f"{a}_mobile" for a in aliases]):
                            if item.get(key):
                                mobile = str(item.get(key)).strip()
                                break
                        if not mobile:
                            return None
                        name = ""
                        for key in (f"{prefix}_name", *[f"{a}_name" for a in aliases]):
                            if item.get(key):
                                name = str(item.get(key)).strip()
                                break
                        lang = ""
                        for key in (f"{prefix}_preferred_lang", f"{prefix}_language",
                                    *[f"{a}_preferred_lang" for a in aliases]):
                            if item.get(key):
                                lang = str(item.get(key)).strip()
                                break
                        return {"name": name, "mobile": mobile, "preferred_language": lang or "English"}

                    tl_dict = _contact("teamleader", "team_leader")
                    mgr_dict = _contact("manager")
                    if current_aid and (tl_dict is None or mgr_dict is None):
                        cur.execute("SELECT team_leader_details, manager_details FROM public.tb_assignments "
                                    "WHERE assignment_id = %s", (current_aid,))
                        prev_row = cur.fetchone()
                        if prev_row:
                            if tl_dict is None:
                                tl_dict = prev_row[0]
                            if mgr_dict is None:
                                mgr_dict = prev_row[1]

                    if current_aid:
                        cur.execute("""
                            UPDATE public.tb_assignments
                            SET assignment_status = 'Replaced', ended_at = now()
                            WHERE assignment_id = %s
                        """, (current_aid,))

                    cur.execute("""
                        INSERT INTO public.tb_assignments (
                            assignment_id, ticket_id, previous_assignment_id,
                            technician_name, technician_mobile, preferred_language,
                            team_leader_details, manager_details,
                            assignment_type, assignment_status, assigned_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'REASSIGNMENT', %s, now())
                        ON CONFLICT (assignment_id) DO UPDATE SET
                            technician_name = EXCLUDED.technician_name,
                            technician_mobile = EXCLUDED.technician_mobile,
                            preferred_language = EXCLUDED.preferred_language,
                            team_leader_details = EXCLUDED.team_leader_details,
                            manager_details = EXCLUDED.manager_details,
                            assignment_status = EXCLUDED.assignment_status,
                            assigned_at = EXCLUDED.assigned_at;
                    """, (new_aid, tid, current_aid, tech_name, tech_mob, tech_lang,
                          Json(tl_dict) if tl_dict else None, Json(mgr_dict) if mgr_dict else None,
                          task_status or "Assigned"))

                    upd_fields = ["current_assignment_id = %s", "updated_at = now()"]
                    upd_args = [new_aid]
                    if task_status:
                        upd_fields.append("ticket_status = %s")
                        upd_args.append(task_status)
                    if severity:
                        upd_fields.append("severity = %s")
                        upd_args.append(severity)
                    if alarm_status:
                        upd_fields.append("alarm_status = %s")
                        upd_args.append(alarm_status)
                    upd_args.append(tid)

                    cur.execute(f"UPDATE public.tb_tickets SET {', '.join(upd_fields)} WHERE UPPER(ticket_id)=UPPER(%s)", tuple(upd_args))
                    current_aid = new_aid

                else:
                    upd_fields = ["updated_at = now()"]
                    upd_args = []
                    if task_status:
                        upd_fields.append("ticket_status = %s")
                        upd_args.append(task_status)
                        if task_status.lower() in ("closed", "resolved"):
                            upd_fields.append("closed_at = now()")
                    if alarm_status:
                        upd_fields.append("alarm_status = %s")
                        upd_args.append(alarm_status)
                    if severity:
                        upd_fields.append("severity = %s")
                        upd_args.append(severity)

                    if len(upd_fields) > 1:
                        upd_args.append(tid)
                        cur.execute(f"UPDATE public.tb_tickets SET {', '.join(upd_fields)} WHERE UPPER(ticket_id)=UPPER(%s)", tuple(upd_args))

                    if current_aid and task_status:
                        cur.execute("UPDATE public.tb_assignments SET assignment_status=%s WHERE assignment_id=%s", (task_status, current_aid))

                evt_id = f"EVT-UPD-{int(datetime.now().timestamp()*1000)}-{tid}"
                cur.execute("""
                    INSERT INTO public.tb_nextask_events (
                        event_id, ticket_id, assignment_id, event_type,
                        event_version, event_timestamp, payload_json, processing_status
                    ) VALUES (%s, %s, %s, %s, 1, %s, %s, 'STORED')
                    ON CONFLICT (event_id) DO NOTHING;
                """, (evt_id, tid, current_aid, update_type, now_iso, Json(item)))

                cur.execute("""
                    INSERT INTO public.tb_audit_logs (
                        ticket_id, assignment_id, event_id, activity_type,
                        activity_description, activity_status, details_json
                    ) VALUES (%s, %s, %s, %s, %s, 'SUCCESS', %s);
                """, (tid, current_aid, evt_id, update_type, f"Ticket {tid} updated: {update_type}", Json(item)))

        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Error saving updated records to PostgreSQL: {e}")
        return False
    finally:
        release_connection(conn)


def ticket_exists_pg(ticket_id):
    """Check if ticket exists in PostgreSQL tb_tickets."""
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public.tb_tickets WHERE UPPER(ticket_id)=UPPER(%s) LIMIT 1", (str(ticket_id).strip().upper(),))
            return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        release_connection(conn)


# --------------------------------------------------------------------------- dashboard (read-only)
def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _person(details, language=None):
    """tb_assignments.team_leader_details / manager_details (jsonb) -> {name, mobile, language}."""
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError:
            details = None
    if isinstance(details, list):
        details = details[0] if details and isinstance(details[0], dict) else None
    if not isinstance(details, dict):
        return None

    def pick(*keys):
        return next((str(details[k]) for k in keys if details.get(k) not in (None, "")), "")

    return {"name": pick("name", "teamleader_name", "team_leader_name", "manager_name"),
            "mobile": pick("mobile", "mobile_number", "phone_number", "teamleader_mobile", "manager_mobile"),
            "language": pick("language", "preferred_language", "preferred_lang") or language or ""}


_REJECT_REASONS = {"1": "Busy on another site", "2": "On leave / Off duty",
                   "3": "Equipment or transport issue", "4": "Other / Personal reasons"}
_ACTIVE_CALL_STATUSES = {"queued", "initiating", "initiated", "ringing", "in-progress"}
_ROLE_NAMES = (("technician", "Technician"), ("team_leader", "Team leader"), ("manager", "Manager"))


def _call_result_text(c):
    """One short phrase for what happened on a call, from the saved call row."""
    dtmf = str(c.get("dtmf") or "")
    status = str(c.get("status") or "").lower()
    if c.get("response") == "acknowledged":
        text = "acknowledged"
    elif "reason:" in dtmf:
        digit = dtmf.split("reason:", 1)[1][:1]
        text = "rejected (" + _REJECT_REASONS.get(digit, "Other / unspecified") + ")"
    elif status in ("no-answer", "no_answer"):
        text = "no answer"
    elif status == "busy":
        text = "busy"
    elif status in ("failed", "canceled"):
        text = status
    elif status == "completed":
        text = "answered, no key pressed"
    else:
        text = "in progress"
    if c.get("superseded") and text in ("in progress", "answered, no key pressed"):
        text = "ended, task reassigned"
    return text


def _ticket_summary(t_calls, wa_rows, recipients):
    """Plain-text summary shown on a task card: call results per person, alerts sent, reassignment."""
    parts = []
    for role, name in _ROLE_NAMES:
        mine = [c for c in t_calls if c.get("role") == role]
        if mine:
            parts.append(f"{name}: " + "; ".join(
                f"call {c.get('attempt_no') or 1} {_call_result_text(c)}" for c in mine) + ".")
    sent = {}
    for w in wa_rows:
        chan = "WhatsApp" if str(w.get("channel") or "").lower() == "whatsapp" else "SMS"
        role = str(w.get("recipient_role") or "").lower()
        label = dict(_ROLE_NAMES).get(role, role.replace("_", " ").title() or "Recipient")
        if label not in sent.setdefault(chan, []):
            sent[chan].append(label)
    if sent:
        parts.append("Alerts: " + "; ".join(f"{chan} to {', '.join(who)}" for chan, who in sent.items()) + ".")
    prev = (recipients or {}).get("previous_technician") or {}
    cur = (recipients or {}).get("technician") or {}
    if prev.get("name"):
        parts.append(f"Reassigned from {prev['name']}" + (f" to {cur['name']}" if cur.get("name") else "") + ".")
    return " ".join(parts)


def dashboard_data(limit=1000):
    """Everything the dashboard shows, read from the PostgreSQL tb_* tables only .

    Returns {"tickets": [...], "wa_total", "wa_delivered", "calls_total", "calls_completed",
    "calls_failed"}, with each ticket shaped the way the dashboard expects. Returns None when
    PostgreSQL cannot be reached. The live call-workflow state is not stored in PostgreSQL, so it is
    derived here from the assignment status and the voice call rows."""
    conn = get_connection()
    if not conn:
        return None
    try:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM public.tb_tickets ORDER BY updated_at DESC NULLS LAST LIMIT %s", (int(limit),))
            tickets = cur.fetchall()
            ids = [t["ticket_id"] for t in tickets]
            assignments, calls, wa = [], [], []
            if ids:
                cur.execute("SELECT * FROM public.tb_assignments WHERE ticket_id = ANY(%s) "
                            "ORDER BY assigned_at NULLS FIRST, assignment_id", (ids,))
                assignments = cur.fetchall()
                aids = [a["assignment_id"] for a in assignments]
                if aids:
                    cur.execute("SELECT * FROM public.tb_voice_calls WHERE assignment_id = ANY(%s) ORDER BY id", (aids,))
                    calls = cur.fetchall()
                cur.execute("SELECT * FROM public.tb_whatsapp_messages WHERE ticket_id = ANY(%s) ORDER BY id", (ids,))
                wa = cur.fetchall()
            cur.execute("SELECT COUNT(*) AS n FROM public.tb_whatsapp_messages")
            wa_total = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM public.tb_whatsapp_messages "
                        "WHERE LOWER(delivery_status) IN ('delivered','read')")
            wa_delivered = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM public.tb_voice_calls")
            calls_total = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM public.tb_voice_calls WHERE LOWER(call_status)='completed'")
            calls_completed = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM public.tb_voice_calls WHERE LOWER(call_status) IN "
                        "('failed','busy','no-answer') OR failure_reason IS NOT NULL")
            calls_failed = cur.fetchone()["n"]
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(f"Error reading dashboard data from PostgreSQL: {e}")
        return None
    finally:
        release_connection(conn)

    by_ticket, by_assignment = {}, {a["assignment_id"]: a for a in assignments}
    for a in assignments:
        by_ticket.setdefault(a["ticket_id"], []).append(a)
    views = []
    for t in tickets:
        tid = t["ticket_id"]
        cur_aid = t.get("current_assignment_id")
        cur = by_assignment.get(cur_aid)
        if not cur and by_ticket.get(tid):
            # current_assignment_id missing on the ticket: fall back to its latest assignment
            cur = by_ticket[tid][-1]
            cur_aid = cur["assignment_id"]
        prev =by_assignment.get(cur.get("previous_assignment_id")) if cur else None
        aids = {a["assignment_id"] for a in by_ticket.get(tid, [])}
        recipients = {}
        if cur:
            recipients["technician"] = {"name": cur.get("technician_name") or "",
                                        "mobile": cur.get("technician_mobile") or "",
                                        "language": cur.get("preferred_language") or ""}
            for role, key in (("team_leader", "team_leader_details"), ("manager", "manager_details")):
                person = _person(cur.get(key), cur.get("preferred_language"))
                if person:
                    recipients[role] = person
        if prev and (prev.get("technician_name") or prev.get("technician_mobile")):
            recipients["previous_technician"] = {"name": prev.get("technician_name") or "",
                                                 "mobile": prev.get("technician_mobile") or "",
                                                 "language": prev.get("preferred_language") or "",
                                                 "assignment_id": prev["assignment_id"]}
        t_calls = []
        for c in calls:
            if c["assignment_id"] not in aids:
                continue
            digit_ok = str(c.get("dtmf_status") or "").upper() in ("ACCEPTED", "ACKNOWLEDGED")
            t_calls.append({
                "id": c["id"], "role": c.get("recipient_role") or "technician", "workflow": c.get("workflow_id") or "",
                "attempt_no": c.get("attempt_number") or 1, "recipient_name": c.get("recipient_name"),
                "to_number": c.get("mobile_number"), "language": c.get("language"),
                "call_sid": c.get("provider_call_id"), "status": c.get("call_status"), "dtmf": c.get("captured_digit"),
                "outcome": (c.get("dtmf_status") or "").lower() or None,
                "response": "acknowledged" if digit_ok else (c.get("failure_reason") or c.get("dtmf_status") or "call in progress"),
                "created_at": _iso(c.get("started_at")), "ended_at": _iso(c.get("ended_at")),
                "superseded": c["assignment_id"] != cur_aid, "_ack": digit_ok})
        live = [c for c in t_calls if not c["superseded"]]
        acked = any(c["_ack"] for c in live) or \
            str((cur or {}).get("assignment_status") or "").lower() in ("accepted", "acknowledged")
        roles = {c["role"] for c in live}
        if acked:
            state = "ACKNOWLEDGED"
        elif "manager" in roles:
            state = "MANAGER_CALLED"
        elif "team_leader" in roles:
            state = "TEAM_LEADER_CALLED"
        elif live:
            state = "TECH_CALLING"
        else:
            state = ""
        for c in t_calls:
            c.pop("_ack", None)
        # A call is "active" only while it is still ringing or connected (not just because it exists).
        call_active = any(not c["superseded"] and not c.get("ended_at")
                          and str(c.get("status") or "").lower() in _ACTIVE_CALL_STATUSES for c in t_calls)
        views.append({
            "ticket_id": tid, "alert_id": t.get("alert_id"), "alarm_id": t.get("alarm_id"), "assignment_id": cur_aid,
            "site_number": t.get("site_number"), "site_name": t.get("site_name"), "severity": t.get("severity"),
            "status": t.get("ticket_status"), "alarm_status": t.get("alarm_status"),
            "operational_status": t.get("operational_status"), "description": t.get("issue_description"),
            "created_at": _iso(t.get("opened_at")),
            "last_event_time": _iso(t.get("last_event_at") or t.get("updated_at")),
            "last_event_id": None, "recipients": recipients, "calls": t_calls, "acknowledged": acked,
            "call_active": call_active,
            "call_workflow": {"state": state, "acknowledged": acked,
                              "response_summary": _ticket_summary(t_calls, [w for w in wa if w["ticket_id"] == tid], recipients)},
            "followup": {},
            "whatsapp_messages": [
                {"recipient_role": w.get("recipient_role"), "recipient_name": w.get("recipient_name"),
                 "mobile_number": w.get("mobile_number"), "template_name": w.get("template_name"),
                 "channel": w.get("channel") or "sms",
                 "delivery_status": w.get("delivery_status"), "provider_message_id": w.get("provider_message_id"),
                 "sent_at": _iso(w.get("sent_at")), "submitted_at": _iso(w.get("submitted_at"))}
                for w in wa if w["ticket_id"] == tid]})
    return {"tickets": views, "wa_total": wa_total, "wa_delivered": wa_delivered, "calls_total": calls_total,
            "calls_completed": calls_completed, "calls_failed": calls_failed}
