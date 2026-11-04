"""
Syncs data from PostgreSQL (tb_tickets, tb_assignments, tb_voice_calls)
into the call-workflow tables (tickets ...) of the PostgreSQL runtime schema so that the call flow
matches the production tb_* tables.

Usage:
  python sync_db.py          # Syncs / updates tickets from tb_* into the runtime tables
  python sync_db.py --clean  # Clears the runtime tables first, then mirrors tb_* exactly
"""

import re
import sys
import json
import logging

import store
import pg_store
from config import CFG

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("sync_db")


def sync(clean=False):
    conn = pg_store.get_connection()
    if not conn:
        logger.error("Could not connect to PostgreSQL. Check DB_HOST, DB_NAME, DB_USER, DB_PASSWORD in .env")
        return False

    if clean:
        logger.info("Cleaning old runtime records to match PostgreSQL 1-to-1...")
        with store.DB_LOCK:
            store.connect()
            for t in ["tickets", "events", "calls", "jobs", "audit_log", "whatsapp_messages"]:
                try:
                    store.q(f"DELETE FROM {t}")
                except Exception as e:
                    logger.warning(f"Notice deleting from {t}: {e}")
        logger.info("Runtime tables cleared.")

    try:
        with conn.cursor() as cur:
            # 1. Fetch tickets
            cur.execute("""
                SELECT 
                    ticket_id, alert_id, alarm_id, site_number, site_name,
                    issue_description, alarm_type, severity, ticket_status, alarm_status,
                    operational_status, opened_at, updated_at
                FROM tb_tickets
                ORDER BY ticket_id
            """)
            ticket_rows = cur.fetchall()
            t_cols = [d[0] for d in cur.description]
            tickets = [dict(zip(t_cols, r)) for r in ticket_rows]

            # 2. Fetch assignments
            cur.execute("""
                SELECT 
                    assignment_id, ticket_id, previous_assignment_id,
                    technician_name, technician_mobile, preferred_language,
                    team_leader_details, manager_details, assignment_type, assignment_status,
                    assigned_at, dtmf_digit
                FROM tb_assignments
                ORDER BY ticket_id, assigned_at ASC NULLS LAST
            """)
            asn_rows = cur.fetchall()
            a_cols = [d[0] for d in cur.description]
            assignments = [dict(zip(a_cols, r)) for r in asn_rows]

        # Group assignments by ticket_id
        asn_by_ticket = {}
        for a in assignments:
            tid = a["ticket_id"]
            asn_by_ticket.setdefault(tid, []).append(a)

        synced = 0
        for t in tickets:
            tid = t["ticket_id"]
            t_asns = asn_by_ticket.get(tid, [])
            
            # Determine initial vs current assignment
            initial_asn = t_asns[0] if t_asns else {}
            curr_asn = t_asns[-1] if t_asns else {}
            is_reassigned = len(t_asns) > 1 or bool(curr_asn.get("previous_assignment_id"))

            curr_aid = curr_asn.get("assignment_id") or f"{tid}-ASN1"
            
            def parse_details(d, default_name, default_lang):
                if isinstance(d, dict):
                    return d
                if isinstance(d, str):
                    try:
                        return json.loads(d)
                    except:
                        pass
                return {"name": default_name, "mobile": "", "preferred_language": default_lang}

            tl = parse_details(curr_asn.get("team_leader_details"), "Team Leader", "ar")
            mgr = parse_details(curr_asn.get("manager_details"), "Operations Manager", "en")

            recipients = {
                "technician": {
                    "name": curr_asn.get("technician_name") or "Assigned Technician",
                    "mobile": curr_asn.get("technician_mobile") or "",
                    "language": curr_asn.get("preferred_language") or "en"
                },
                "team_leader": {
                    "name": tl.get("name") or "Team Leader",
                    "mobile": tl.get("mobile") or "",
                    "language": tl.get("preferred_language") or tl.get("language") or "ar"
                },
                "manager": {
                    "name": mgr.get("name") or "Operations Manager",
                    "mobile": mgr.get("mobile") or "",
                    "language": mgr.get("preferred_language") or mgr.get("language") or "en"
                }
            }

            if is_reassigned and initial_asn:
                recipients["previous_technician"] = {
                    "name": initial_asn.get("technician_name") or "Previous Technician",
                    "mobile": initial_asn.get("technician_mobile") or "",
                    "language": initial_asn.get("preferred_language") or "en",
                    "assignment_id": initial_asn.get("assignment_id") or f"{tid}-ASN1"
                }

            recip_json = json.dumps(recipients, ensure_ascii=False)
            site_num = t.get("site_number") or ""
            site_name = t.get("site_name") or (f"Site {site_num}" if site_num else "Main Site")
            severity = t.get("severity") or "Major"
            status = curr_asn.get("assignment_status") or t.get("ticket_status") or "Dispatch"
            alarm_status = t.get("alarm_status") or "Active"
            op_status = t.get("operational_status") or ""
            desc = t.get("issue_description") or ""
            alarm_id = t.get("alarm_id") or t.get("alarm_type") or f"ALM-{tid}"
            alert_id = t.get("alert_id") or tid
            created_at = str(t.get("opened_at") or store.now_iso())
            updated_at = str(t.get("updated_at") or store.now_iso())
            uc1_state = "REASSIGNED" if is_reassigned else "ASSIGNED"

            existing = store.get_ticket(tid)
            if not existing:
                store.q("""INSERT INTO tickets (
                    ticket_id, alert_id, alarm_id, assignment_id, site_number,
                    site_name, severity, status, alarm_status, operational_status,
                    description, recipients, uc1_assignment_id, uc1_state, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    tid, alert_id, alarm_id, curr_aid, site_num,
                    site_name, severity, status, alarm_status, op_status,
                    desc, recip_json, curr_aid, uc1_state, created_at, updated_at
                ))
                synced += 1
            else:
                store.update_ticket(
                    tid,
                    alert_id=alert_id,
                    alarm_id=alarm_id,
                    assignment_id=curr_aid,
                    site_number=site_num,
                    site_name=site_name,
                    severity=severity,
                    status=status,
                    alarm_status=alarm_status,
                    operational_status=op_status,
                    description=desc,
                    recipients=recip_json,
                    uc1_assignment_id=curr_aid,
                    uc1_state=uc1_state
                )
                synced += 1

            # Use the acknowledge key stored on the assignment (only if the ticket has none yet)
            _d = str(curr_asn.get("dtmf_digit") or "").strip()
            _cur = store.get_ticket(tid)
            if re.fullmatch(r"[0-9]", _d) and _cur and not _cur.get("ack_digit"):
                store.update_ticket(tid, ack_digit=_d)

        logger.info(f"Sync complete! {synced} ticket(s) from PostgreSQL are now 100% in sync in the runtime tables.")
        return True
    except Exception as e:
        logger.error(f"Error during synchronization: {e}", exc_info=True)
        return False
    finally:
        pg_store.release_connection(conn)


if __name__ == "__main__":
    is_clean = "--clean" in sys.argv
    success = sync(clean=is_clean)
    sys.exit(0 if success else 1)
