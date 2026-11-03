--
-- PostgreSQL database dump
--

\restrict gIBHB5k25wTSkd7rmhuDVbWWnPZ4GcZ8ucSe2SZIhrPiCk7SEQDkhg5fuizwAPm

-- Dumped from database version 16.15
-- Dumped by pg_dump version 16.15

-- Started on 2026-11-02 11:52:31

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- TOC entry 215 (class 1259 OID 30431)
-- Name: tb_assignments; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tb_assignments (
    assignment_id character varying(50) NOT NULL,
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


ALTER TABLE public.tb_assignments OWNER TO postgres;

--
-- TOC entry 216 (class 1259 OID 30436)
-- Name: tb_audit_logs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tb_audit_logs (
    id bigint NOT NULL,
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


ALTER TABLE public.tb_audit_logs OWNER TO postgres;

--
-- TOC entry 217 (class 1259 OID 30442)
-- Name: tb_audit_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.tb_audit_logs ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tb_audit_logs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- TOC entry 218 (class 1259 OID 30443)
-- Name: tb_nextask_events; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tb_nextask_events (
    id bigint NOT NULL,
    batch_id character varying(50),
    event_id character varying(50) NOT NULL,
    ticket_id character varying(50),
    assignment_id character varying(50),
    event_type character varying(60) NOT NULL,
    event_version bigint,
    event_timestamp timestamp with time zone,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    payload_json jsonb NOT NULL,
    processing_status character varying(30) DEFAULT 'Received'::character varying NOT NULL,
    error_details text
);


ALTER TABLE public.tb_nextask_events OWNER TO postgres;

--
-- TOC entry 219 (class 1259 OID 30450)
-- Name: tb_nextask_events_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.tb_nextask_events ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tb_nextask_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- TOC entry 220 (class 1259 OID 30451)
-- Name: tb_tickets; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tb_tickets (
    ticket_id character varying(50) NOT NULL,
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


ALTER TABLE public.tb_tickets OWNER TO postgres;

--
-- TOC entry 221 (class 1259 OID 30457)
-- Name: tb_voice_calls; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tb_voice_calls (
    id bigint NOT NULL,
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
    ended_at timestamp with time zone,
    CONSTRAINT tb_voice_calls_attempt_number_check CHECK ((attempt_number > 0))
);


ALTER TABLE public.tb_voice_calls OWNER TO postgres;

--
-- TOC entry 222 (class 1259 OID 30463)
-- Name: tb_voice_calls_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.tb_voice_calls ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tb_voice_calls_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- TOC entry 223 (class 1259 OID 30464)
-- Name: tb_whatsapp_messages; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tb_whatsapp_messages (
    id bigint NOT NULL,
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


ALTER TABLE public.tb_whatsapp_messages OWNER TO postgres;

--
-- TOC entry 224 (class 1259 OID 30469)
-- Name: tb_whatsapp_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.tb_whatsapp_messages ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tb_whatsapp_messages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- TOC entry 225 (class 1259 OID 30470)
-- Name: tb_workflow_tracker; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tb_workflow_tracker (
    workflow_id character varying(50) NOT NULL,
    ticket_id character varying(50) NOT NULL,
    assignment_id character varying(50),
    workflow_type character varying(40) NOT NULL,
    workflow_status character varying(30) NOT NULL,
    current_stage character varying(60),
    retry_count integer DEFAULT 0 NOT NULL,
    acknowledged_at timestamp with time zone,
    next_action_at timestamp with time zone,
    next_action_type character varying(50),
    last_state_version bigint,
    last_decision_reason text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT tb_workflow_tracker_retry_count_check CHECK ((retry_count >= 0))
);


ALTER TABLE public.tb_workflow_tracker OWNER TO postgres;

--
-- TOC entry 4957 (class 0 OID 30431)
-- Dependencies: 215
-- Data for Name: tb_assignments; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.tb_assignments (assignment_id, ticket_id, previous_assignment_id, technician_id, technician_name, technician_mobile, preferred_language, team_leader_details, manager_details, dtmf_digit, assignment_type, assignment_status, assigned_at, ended_at) FROM stdin;
ASN003	INC1002	\N	ENG003	Ahmed	+966500000008	Arabic	{"name": "Mohammed", "mobile_number": "+966500000002", "team_leader_id": "TL001", "preferred_language": "English"}	{"name": "Khalid", "manager_id": "MGR001", "mobile_number": "+966500000003", "preferred_language": "English"}	2	INITIAL	Active	2026-10-30 12:11:28.450687+05:30	\N
\.


--
-- TOC entry 4958 (class 0 OID 30436)
-- Dependencies: 216
-- Data for Name: tb_audit_logs; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.tb_audit_logs (id, ticket_id, assignment_id, workflow_id, event_id, activity_type, activity_description, activity_status, reference_id, details_json, created_at) FROM stdin;
\.


--
-- TOC entry 4960 (class 0 OID 30443)
-- Dependencies: 218
-- Data for Name: tb_nextask_events; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.tb_nextask_events (id, batch_id, event_id, ticket_id, assignment_id, event_type, event_version, event_timestamp, received_at, payload_json, processing_status, error_details) FROM stdin;
1	BATCH002	EVT002	INC1002	ASN003	ASSIGNMENT_CREATED	1	2026-10-30 12:14:05.769081+05:30	2026-10-30 12:14:05.769081+05:30	{"event_id": "EVT002", "ticket_id": "INC1002", "dtmf_digit": "2", "event_type": "ASSIGNMENT_CREATED", "assignment_id": "ASN003", "technician_id": "ENG003", "technician_name": "Ahmed", "preferred_language": "Arabic"}	PENDING	\N
\.


--
-- TOC entry 4962 (class 0 OID 30451)
-- Dependencies: 220
-- Data for Name: tb_tickets; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.tb_tickets (ticket_id, alarm_id, alert_id, branch_id, branch_name, site_number, site_name, issue_description, alarm_type, severity, ticket_status, alarm_status, operational_status, current_assignment_id, emergency_recipients, opened_at, closed_at, resolution, last_event_version, last_event_at, updated_at) FROM stdin;
INC1002	ALM5002	ALT1002	BR001	Riyadh Main Office	SITE001	Riyadh Main Office	Power supply failure	Power Failure	Major	Assigned	Active	\N	ASN003	\N	2026-10-30 12:09:52.391084+05:30	\N	\N	\N	\N	2026-10-30 12:34:29.275002+05:30
\.


--
-- TOC entry 4963 (class 0 OID 30457)
-- Dependencies: 221
-- Data for Name: tb_voice_calls; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.tb_voice_calls (id, workflow_id, assignment_id, provider_call_id, recipient_role, recipient_id, recipient_name, mobile_number, language, call_type, attempt_number, prompt_version, call_status, raw_cause_code, requested_digit, captured_digit, dtmf_status, dtmf_received_at, failure_reason, started_at, ended_at) FROM stdin;
\.


--
-- TOC entry 4965 (class 0 OID 30464)
-- Dependencies: 223
-- Data for Name: tb_whatsapp_messages; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.tb_whatsapp_messages (id, workflow_id, ticket_id, assignment_id, recipient_role, recipient_id, recipient_name, mobile_number, message_type, provider_message_id, template_name, template_version, delivery_status, submitted_at, sent_at, delivered_at, read_at, failure_reason) FROM stdin;
\.


--
-- TOC entry 4967 (class 0 OID 30470)
-- Dependencies: 225
-- Data for Name: tb_workflow_tracker; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.tb_workflow_tracker (workflow_id, ticket_id, assignment_id, workflow_type, workflow_status, current_stage, retry_count, acknowledged_at, next_action_at, next_action_type, last_state_version, last_decision_reason, started_at, completed_at) FROM stdin;
\.


--
-- TOC entry 4973 (class 0 OID 0)
-- Dependencies: 217
-- Name: tb_audit_logs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.tb_audit_logs_id_seq', 1, false);


--
-- TOC entry 4974 (class 0 OID 0)
-- Dependencies: 219
-- Name: tb_nextask_events_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.tb_nextask_events_id_seq', 1, true);


--
-- TOC entry 4975 (class 0 OID 0)
-- Dependencies: 222
-- Name: tb_voice_calls_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.tb_voice_calls_id_seq', 1, false);


--
-- TOC entry 4976 (class 0 OID 0)
-- Dependencies: 224
-- Name: tb_whatsapp_messages_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.tb_whatsapp_messages_id_seq', 1, false);


--
-- TOC entry 4772 (class 2606 OID 30479)
-- Name: tb_assignments tb_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_assignments
    ADD CONSTRAINT tb_assignments_pkey PRIMARY KEY (assignment_id);


--
-- TOC entry 4776 (class 2606 OID 30481)
-- Name: tb_audit_logs tb_audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_audit_logs
    ADD CONSTRAINT tb_audit_logs_pkey PRIMARY KEY (id);


--
-- TOC entry 4780 (class 2606 OID 30483)
-- Name: tb_nextask_events tb_nextask_events_event_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_nextask_events
    ADD CONSTRAINT tb_nextask_events_event_id_key UNIQUE (event_id);


--
-- TOC entry 4782 (class 2606 OID 30485)
-- Name: tb_nextask_events tb_nextask_events_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_nextask_events
    ADD CONSTRAINT tb_nextask_events_pkey PRIMARY KEY (id);


--
-- TOC entry 4785 (class 2606 OID 30487)
-- Name: tb_tickets tb_tickets_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_tickets
    ADD CONSTRAINT tb_tickets_pkey PRIMARY KEY (ticket_id);


--
-- TOC entry 4788 (class 2606 OID 30489)
-- Name: tb_voice_calls tb_voice_calls_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_voice_calls
    ADD CONSTRAINT tb_voice_calls_pkey PRIMARY KEY (id);


--
-- TOC entry 4790 (class 2606 OID 30491)
-- Name: tb_voice_calls tb_voice_calls_provider_call_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_voice_calls
    ADD CONSTRAINT tb_voice_calls_provider_call_id_key UNIQUE (provider_call_id);


--
-- TOC entry 4793 (class 2606 OID 30493)
-- Name: tb_whatsapp_messages tb_whatsapp_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_whatsapp_messages
    ADD CONSTRAINT tb_whatsapp_messages_pkey PRIMARY KEY (id);


--
-- TOC entry 4795 (class 2606 OID 30495)
-- Name: tb_whatsapp_messages tb_whatsapp_messages_provider_message_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_whatsapp_messages
    ADD CONSTRAINT tb_whatsapp_messages_provider_message_id_key UNIQUE (provider_message_id);


--
-- TOC entry 4799 (class 2606 OID 30497)
-- Name: tb_workflow_tracker tb_workflow_tracker_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_workflow_tracker
    ADD CONSTRAINT tb_workflow_tracker_pkey PRIMARY KEY (workflow_id);


--
-- TOC entry 4770 (class 1259 OID 30498)
-- Name: idx_assignments_ticket; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_assignments_ticket ON public.tb_assignments USING btree (ticket_id);


--
-- TOC entry 4774 (class 1259 OID 30499)
-- Name: idx_audit_ticket_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_audit_ticket_time ON public.tb_audit_logs USING btree (ticket_id, created_at);


--
-- TOC entry 4786 (class 1259 OID 30500)
-- Name: idx_calls_workflow; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_calls_workflow ON public.tb_voice_calls USING btree (workflow_id);


--
-- TOC entry 4777 (class 1259 OID 30501)
-- Name: idx_events_processing; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_events_processing ON public.tb_nextask_events USING btree (processing_status);


--
-- TOC entry 4778 (class 1259 OID 30502)
-- Name: idx_events_ticket_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_events_ticket_time ON public.tb_nextask_events USING btree (ticket_id, event_timestamp);


--
-- TOC entry 4791 (class 1259 OID 30503)
-- Name: idx_messages_ticket; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_messages_ticket ON public.tb_whatsapp_messages USING btree (ticket_id);


--
-- TOC entry 4783 (class 1259 OID 30504)
-- Name: idx_ticket_alarm; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_ticket_alarm ON public.tb_tickets USING btree (alarm_id);


--
-- TOC entry 4796 (class 1259 OID 30505)
-- Name: idx_workflow_due; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_workflow_due ON public.tb_workflow_tracker USING btree (next_action_at) WHERE ((workflow_status)::text = ANY (ARRAY[('Active'::character varying)::text, ('Waiting'::character varying)::text]));


--
-- TOC entry 4797 (class 1259 OID 30506)
-- Name: idx_workflow_ticket; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_workflow_ticket ON public.tb_workflow_tracker USING btree (ticket_id);


--
-- TOC entry 4773 (class 1259 OID 30507)
-- Name: uq_active_assignment_per_ticket; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX uq_active_assignment_per_ticket ON public.tb_assignments USING btree (ticket_id) WHERE ((assignment_status)::text = 'Active'::text);


--
-- TOC entry 4806 (class 2606 OID 30508)
-- Name: tb_tickets fk_current_assignment; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_tickets
    ADD CONSTRAINT fk_current_assignment FOREIGN KEY (current_assignment_id) REFERENCES public.tb_assignments(assignment_id) DEFERRABLE INITIALLY DEFERRED;


--
-- TOC entry 4800 (class 2606 OID 30513)
-- Name: tb_assignments tb_assignments_previous_assignment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_assignments
    ADD CONSTRAINT tb_assignments_previous_assignment_id_fkey FOREIGN KEY (previous_assignment_id) REFERENCES public.tb_assignments(assignment_id);


--
-- TOC entry 4801 (class 2606 OID 30518)
-- Name: tb_assignments tb_assignments_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_assignments
    ADD CONSTRAINT tb_assignments_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.tb_tickets(ticket_id);


--
-- TOC entry 4802 (class 2606 OID 30523)
-- Name: tb_audit_logs tb_audit_logs_assignment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_audit_logs
    ADD CONSTRAINT tb_audit_logs_assignment_id_fkey FOREIGN KEY (assignment_id) REFERENCES public.tb_assignments(assignment_id);


--
-- TOC entry 4803 (class 2606 OID 30528)
-- Name: tb_audit_logs tb_audit_logs_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_audit_logs
    ADD CONSTRAINT tb_audit_logs_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.tb_tickets(ticket_id);


--
-- TOC entry 4804 (class 2606 OID 30533)
-- Name: tb_audit_logs tb_audit_logs_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_audit_logs
    ADD CONSTRAINT tb_audit_logs_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.tb_workflow_tracker(workflow_id);


--
-- TOC entry 4805 (class 2606 OID 30538)
-- Name: tb_nextask_events tb_nextask_events_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_nextask_events
    ADD CONSTRAINT tb_nextask_events_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.tb_tickets(ticket_id);


--
-- TOC entry 4807 (class 2606 OID 30543)
-- Name: tb_voice_calls tb_voice_calls_assignment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_voice_calls
    ADD CONSTRAINT tb_voice_calls_assignment_id_fkey FOREIGN KEY (assignment_id) REFERENCES public.tb_assignments(assignment_id);


--
-- TOC entry 4808 (class 2606 OID 30548)
-- Name: tb_voice_calls tb_voice_calls_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_voice_calls
    ADD CONSTRAINT tb_voice_calls_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.tb_workflow_tracker(workflow_id);


--
-- TOC entry 4809 (class 2606 OID 30553)
-- Name: tb_whatsapp_messages tb_whatsapp_messages_assignment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_whatsapp_messages
    ADD CONSTRAINT tb_whatsapp_messages_assignment_id_fkey FOREIGN KEY (assignment_id) REFERENCES public.tb_assignments(assignment_id);


--
-- TOC entry 4810 (class 2606 OID 30558)
-- Name: tb_whatsapp_messages tb_whatsapp_messages_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_whatsapp_messages
    ADD CONSTRAINT tb_whatsapp_messages_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.tb_tickets(ticket_id);


--
-- TOC entry 4811 (class 2606 OID 30563)
-- Name: tb_whatsapp_messages tb_whatsapp_messages_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_whatsapp_messages
    ADD CONSTRAINT tb_whatsapp_messages_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.tb_workflow_tracker(workflow_id);


--
-- TOC entry 4812 (class 2606 OID 30568)
-- Name: tb_workflow_tracker tb_workflow_tracker_assignment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_workflow_tracker
    ADD CONSTRAINT tb_workflow_tracker_assignment_id_fkey FOREIGN KEY (assignment_id) REFERENCES public.tb_assignments(assignment_id);


--
-- TOC entry 4813 (class 2606 OID 30573)
-- Name: tb_workflow_tracker tb_workflow_tracker_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tb_workflow_tracker
    ADD CONSTRAINT tb_workflow_tracker_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.tb_tickets(ticket_id);


-- Completed on 2026-11-02 11:52:32

--
-- PostgreSQL database dump complete
--

\unrestrict gIBHB5k25wTSkd7rmhuDVbWWnPZ4GcZ8ucSe2SZIhrPiCk7SEQDkhg5fuizwAPm

