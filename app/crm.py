from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .data_quality import (
    company_domain_key,
    company_name_key,
    is_technical_whatsapp_jid,
    location_key,
    normalize_contacts,
    normalize_phone,
    normalize_whatsapp_jid,
    phone_keys,
)

CAMPAIGN_STATUSES = {
    "draft",
    "discovering",
    "analyzing",
    "ready",
    "active",
    "paused",
    "done",
}
LEAD_STATUSES = {
    "new",
    "researching",
    "analyzed",
    "qualified",
    "drafted",
    "approved",
    "contacted",
    "replied",
    "interested",
    "meeting",
    "proposal",
    "follow_up",
    "won",
    "lost",
    "archived",
}
DRAFT_STATUSES = {"draft", "approved", "sending", "sent", "cancelled", "failed"}
FOLLOWUP_STATUSES = {"pending", "completed", "cancelled"}
AUTOPILOT_MODES = {"safe", "semi_auto", "autopilot"}
WORKSPACE_ROLES = {"viewer", "operator", "owner"}
WORKSPACE_MEMBER_STATUSES = {"active", "revoked"}
COMPANY_ONBOARDING_STATUSES = {"not_started", "in_progress", "ready"}
COMPANY_KNOWLEDGE_CATEGORIES = {
    "service",
    "price",
    "case",
    "current_client",
    "closed_client",
    "faq",
    "objection",
    "constraint",
    "proof",
    "document",
    "sales_process",
}
COMPANY_KNOWLEDGE_STATUSES = {"active", "archived"}
AGENT_INBOX_STATUSES = {
    "new",
    "acknowledged",
    "processing",
    "drafted",
    "needs_review",
    "resolved",
    "ignored",
}
AGENT_INBOX_OPEN_STATUSES = {
    "new",
    "acknowledged",
    "processing",
    "drafted",
    "needs_review",
}
CONVERSATION_AUTONOMY_MODES = {"observe", "draft"}
CONVERSATION_STAGES = {
    "new",
    "discovery",
    "qualification",
    "interested",
    "objection",
    "proposal",
    "handoff",
    "closed",
}
WEEKDAYS = {
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical_company_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("website_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("website_url must not contain credentials")

    host = parsed.hostname.rstrip(".").lower()
    port = parsed.port
    netloc = host
    if port and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "/", "", ""))


def normalize_datetime(value: str | None) -> str:
    if not value:
        return utc_now()
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class SalesCRM:
    """Small persistent CRM backed by SQLite and safe for multi-process reads/writes."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    industry TEXT,
                    location TEXT,
                    search_query TEXT,
                    target_count INTEGER NOT NULL DEFAULT 20,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS leads (
                    id TEXT PRIMARY KEY,
                    company_name TEXT NOT NULL,
                    website_url TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    industry TEXT,
                    location TEXT,
                    source TEXT NOT NULL DEFAULT 'manual',
                    status TEXT NOT NULL DEFAULT 'new',
                    score INTEGER,
                    score_reason TEXT,
                    score_details_json TEXT,
                    summary TEXT,
                    contacts_json TEXT,
                    analysis_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_analyzed_at TEXT,
                    inspection_json TEXT,
                    last_inspected_at TEXT,
                    evidence_expires_at TEXT,
                    domain_key TEXT,
                    name_key TEXT,
                    location_key TEXT
                );

                CREATE TABLE IF NOT EXISTS lead_phone_keys (
                    lead_id TEXT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                    phone_key TEXT NOT NULL,
                    PRIMARY KEY (lead_id, phone_key)
                );

                CREATE TABLE IF NOT EXISTS campaign_leads (
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                    lead_id TEXT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                    source_rank INTEGER,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (campaign_id, lead_id)
                );

                CREATE TABLE IF NOT EXISTS outreach_drafts (
                    id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL,
                    recipient TEXT,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    approved_at TEXT,
                    sent_at TEXT
                );

                CREATE TABLE IF NOT EXISTS interactions (
                    id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'recorded',
                    external_id TEXT,
                    occurred_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS followups (
                    id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                    due_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    notes TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS verticals (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    region TEXT NOT NULL,
                    search_query TEXT,
                    days_json TEXT NOT NULL DEFAULT '[]',
                    daily_target INTEGER NOT NULL DEFAULT 10,
                    min_score INTEGER NOT NULL DEFAULT 65,
                    weight REAL NOT NULL DEFAULT 1.0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_selected_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS autopilot_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    running INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'safe',
                    interval_minutes INTEGER NOT NULL DEFAULT 60,
                    max_verticals_per_cycle INTEGER NOT NULL DEFAULT 2,
                    leads_per_vertical INTEGER NOT NULL DEFAULT 10,
                    score_threshold INTEGER NOT NULL DEFAULT 65,
                    last_cycle_at TEXT,
                    next_cycle_at TEXT,
                    lock_until TEXT,
                    current_cycle_id TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS autopilot_cycles (
                    id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    selected_verticals_json TEXT NOT NULL DEFAULT '[]',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS autopilot_campaigns (
                    cycle_id TEXT NOT NULL REFERENCES autopilot_cycles(id) ON DELETE CASCADE,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                    vertical_id TEXT NOT NULL REFERENCES verticals(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (cycle_id, campaign_id)
                );

                CREATE TABLE IF NOT EXISTS google_sheets_sync_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_sync_at TEXT,
                    status TEXT NOT NULL DEFAULT 'never',
                    error TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outreach_send_requests (
                    id TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL REFERENCES outreach_drafts(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    requested_at TEXT NOT NULL,
                    processed_at TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS admin_audit_log (
                    id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_type TEXT,
                    target_id TEXT,
                    outcome TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workspace_members (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    subject TEXT NOT NULL,
                    email TEXT NOT NULL COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT,
                    UNIQUE (workspace_id, subject)
                );

                CREATE TABLE IF NOT EXISTS workspace_invitations (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    email TEXT NOT NULL COLLATE NOCASE,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    invited_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    accepted_at TEXT
                );

                CREATE TABLE IF NOT EXISTS company_profiles (
                    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
                    company_name TEXT,
                    website_url TEXT,
                    industry TEXT,
                    geography TEXT,
                    positioning TEXT,
                    target_customer TEXT,
                    sales_process TEXT,
                    tone_of_voice TEXT,
                    primary_goal TEXT,
                    constraints TEXT,
                    language TEXT,
                    onboarding_status TEXT NOT NULL DEFAULT 'not_started',
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS company_knowledge_items (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'chat',
                    source_name TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_inbox_events (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    source TEXT NOT NULL DEFAULT 'whatsapp',
                    external_id TEXT NOT NULL,
                    lead_id TEXT REFERENCES leads(id) ON DELETE SET NULL,
                    chat_jid TEXT NOT NULL,
                    sender_label TEXT,
                    message_text TEXT NOT NULL,
                    media_type TEXT,
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    draft_id TEXT REFERENCES outreach_drafts(id) ON DELETE SET NULL,
                    agent_attempts INTEGER NOT NULL DEFAULT 0,
                    agent_lock_until TEXT,
                    agent_error TEXT,
                    decision_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (workspace_id, source, external_id)
                );

                CREATE TABLE IF NOT EXISTS conversation_agent_settings (
                    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    autonomy_mode TEXT NOT NULL DEFAULT 'draft',
                    niche TEXT NOT NULL DEFAULT 'auto',
                    objective TEXT,
                    instructions TEXT,
                    tone TEXT,
                    qualification_questions_json TEXT NOT NULL DEFAULT '[]',
                    forbidden_topics_json TEXT NOT NULL DEFAULT '[]',
                    escalation_rules_json TEXT NOT NULL DEFAULT '[]',
                    max_context_messages INTEGER NOT NULL DEFAULT 12,
                    max_reply_chars INTEGER NOT NULL DEFAULT 700,
                    max_inbound_age_hours INTEGER NOT NULL DEFAULT 168,
                    response_sla_minutes INTEGER NOT NULL DEFAULT 60,
                    confidence_threshold INTEGER NOT NULL DEFAULT 65,
                    auto_create_inbound_leads INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL DEFAULT 'whatsapp',
                    external_chat_id TEXT NOT NULL,
                    lead_id TEXT REFERENCES leads(id) ON DELETE SET NULL,
                    stage TEXT NOT NULL DEFAULT 'new',
                    intent TEXT,
                    sentiment TEXT,
                    summary TEXT,
                    facts_json TEXT NOT NULL DEFAULT '{}',
                    unanswered_question TEXT,
                    next_action TEXT,
                    escalation_status TEXT NOT NULL DEFAULT 'none',
                    escalation_reason TEXT,
                    last_response_id TEXT,
                    last_draft_id TEXT REFERENCES outreach_drafts(id) ON DELETE SET NULL,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    last_inbound_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (workspace_id, channel, external_chat_id)
                );

                CREATE INDEX IF NOT EXISTS idx_campaign_leads_campaign ON campaign_leads(campaign_id);
                CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score DESC);
                CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
                CREATE INDEX IF NOT EXISTS idx_drafts_lead ON outreach_drafts(lead_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_interactions_lead ON interactions(lead_id, occurred_at DESC);
                CREATE INDEX IF NOT EXISTS idx_followups_due ON followups(status, due_at);
                CREATE INDEX IF NOT EXISTS idx_verticals_enabled ON verticals(enabled, weight DESC);
                CREATE INDEX IF NOT EXISTS idx_autopilot_campaigns_vertical ON autopilot_campaigns(vertical_id, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_send_requests_pending_draft
                    ON outreach_send_requests(draft_id) WHERE status = 'pending';
                CREATE INDEX IF NOT EXISTS idx_admin_audit_created
                    ON admin_audit_log(created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_members_active_email
                    ON workspace_members(workspace_id, email) WHERE status = 'active';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_invitations_pending_email
                    ON workspace_invitations(workspace_id, email) WHERE status = 'pending';
                CREATE INDEX IF NOT EXISTS idx_workspace_members_subject
                    ON workspace_members(subject, status);
                CREATE INDEX IF NOT EXISTS idx_company_knowledge_workspace
                    ON company_knowledge_items(workspace_id, status, category, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_inbox_workspace
                    ON agent_inbox_events(workspace_id, status, received_at DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_inbox_chat
                    ON agent_inbox_events(workspace_id, chat_jid, received_at DESC);
                CREATE INDEX IF NOT EXISTS idx_conversation_sessions_workspace
                    ON conversation_sessions(workspace_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_conversation_sessions_lead
                    ON conversation_sessions(lead_id, updated_at DESC);
                """
            )
            lead_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(leads)").fetchall()
            }
            for name, column_type in (
                ("inspection_json", "TEXT"),
                ("last_inspected_at", "TEXT"),
                ("evidence_expires_at", "TEXT"),
                ("domain_key", "TEXT"),
                ("name_key", "TEXT"),
                ("location_key", "TEXT"),
            ):
                if name not in lead_columns:
                    connection.execute(
                        f"ALTER TABLE leads ADD COLUMN {name} {column_type}"
                    )
            inbox_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(agent_inbox_events)"
                ).fetchall()
            }
            for name, column_type in (
                ("agent_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("agent_lock_until", "TEXT"),
                ("agent_error", "TEXT"),
                ("decision_json", "TEXT"),
            ):
                if name not in inbox_columns:
                    connection.execute(
                        f"ALTER TABLE agent_inbox_events ADD COLUMN {name} {column_type}"
                    )
            conversation_settings_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(conversation_agent_settings)"
                ).fetchall()
            }
            if "max_inbound_age_hours" not in conversation_settings_columns:
                connection.execute(
                    "ALTER TABLE conversation_agent_settings "
                    "ADD COLUMN max_inbound_age_hours INTEGER NOT NULL DEFAULT 168"
                )
            if "response_sla_minutes" not in conversation_settings_columns:
                connection.execute(
                    "ALTER TABLE conversation_agent_settings "
                    "ADD COLUMN response_sla_minutes INTEGER NOT NULL DEFAULT 60"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_inbox_claim
                ON agent_inbox_events(workspace_id, status, agent_lock_until, received_at)
                """
            )
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_leads_domain_key
                    ON leads(domain_key, created_at);
                CREATE INDEX IF NOT EXISTS idx_leads_name_location
                    ON leads(name_key, location_key, created_at);
                CREATE INDEX IF NOT EXISTS idx_lead_phone_keys_phone
                    ON lead_phone_keys(phone_key, lead_id);
                """
            )
            if schema_version < 6:
                connection.execute("DELETE FROM lead_phone_keys")
                lookup_rows = connection.execute("SELECT id FROM leads").fetchall()
            else:
                lookup_rows = connection.execute(
                    """
                    SELECT id FROM leads
                    WHERE domain_key IS NULL OR name_key IS NULL OR location_key IS NULL
                    """
                ).fetchall()
            for row in lookup_rows:
                self._sync_lead_lookup_keys(connection, row["id"])
            connection.execute("PRAGMA user_version = 12")
            timestamp = utc_now()
            connection.execute(
                """
                INSERT OR IGNORE INTO autopilot_state (
                    id, running, mode, interval_minutes, max_verticals_per_cycle,
                    leads_per_vertical, score_threshold, created_at, updated_at
                ) VALUES (1, 0, 'safe', 60, 2, 10, 65, ?, ?)
                """,
                (timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO google_sheets_sync_state (
                    id, status, details_json, updated_at
                ) VALUES (1, 'never', '{}', ?)
                """,
                (timestamp,),
            )

    @staticmethod
    def _workspace_role(value: str) -> str:
        role = value.strip().lower()
        if role not in WORKSPACE_ROLES:
            raise ValueError(
                f"role must be one of: {', '.join(sorted(WORKSPACE_ROLES))}"
            )
        return role

    @staticmethod
    def _workspace_id(value: str) -> str:
        workspace_id = value.strip().lower()
        if not workspace_id or len(workspace_id) > 64:
            raise ValueError("workspace_id must contain between 1 and 64 characters")
        if any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in workspace_id
        ):
            raise ValueError("workspace_id may only contain a-z, 0-9, '-' and '_'")
        return workspace_id

    def ensure_workspace(self, workspace_id: str, name: str) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 120:
            raise ValueError("workspace name must contain between 1 and 120 characters")
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO workspaces (id, name, status, created_at, updated_at)
                VALUES (?, ?, 'active', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    updated_at = excluded.updated_at
                """,
                (workspace_id, clean_name, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def get_workspace(self, workspace_id: str) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise ValueError("workspace not found")
        return dict(row)

    @staticmethod
    def _workspace_member_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "id",
                "workspace_id",
                "subject",
                "email",
                "display_name",
                "role",
                "status",
                "created_at",
                "updated_at",
                "last_login_at",
            )
        }

    def authorize_workspace_identity(
        self,
        *,
        workspace_id: str,
        workspace_name: str,
        subject: str,
        email: str,
        display_name: str,
        bootstrap_allowed: bool,
        owner_emails: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Bind a verified OIDC identity to a closed-beta workspace.

        Existing memberships are matched by immutable OIDC subject. A new identity
        must have either a pending invitation or be present in the deployment
        bootstrap allowlist. Email alone never rebinds an existing subject.
        """
        workspace = self.ensure_workspace(workspace_id, workspace_name)
        clean_subject = subject.strip()
        clean_email = email.strip().lower()
        clean_name = display_name.strip() or clean_email
        if not clean_subject or not clean_email:
            raise ValueError("verified subject and email are required")
        timestamp = utc_now()
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM workspace_members
                WHERE workspace_id = ? AND subject = ? AND status = 'active'
                """,
                (workspace["id"], clean_subject),
            ).fetchone()
            if existing is not None:
                if existing["email"].lower() != clean_email:
                    raise ValueError(
                        "OIDC subject email changed; owner review is required"
                    )
                connection.execute(
                    """
                    UPDATE workspace_members
                    SET display_name = ?, updated_at = ?, last_login_at = ?
                    WHERE id = ?
                    """,
                    (clean_name, timestamp, timestamp, existing["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM workspace_members WHERE id = ?", (existing["id"],)
                ).fetchone()
                assert row is not None
                return {**self._workspace_member_view(row), "workspace": workspace}

            email_member = connection.execute(
                """
                SELECT id FROM workspace_members
                WHERE workspace_id = ? AND email = ? AND status = 'active'
                """,
                (workspace["id"], clean_email),
            ).fetchone()
            if email_member is not None:
                raise ValueError("email is already bound to another OIDC subject")

            invitation = connection.execute(
                """
                SELECT * FROM workspace_invitations
                WHERE workspace_id = ? AND email = ? AND status = 'pending'
                    AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (workspace["id"], clean_email, timestamp),
            ).fetchone()
            if invitation is not None:
                role = self._workspace_role(invitation["role"])
            elif bootstrap_allowed:
                active_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM workspace_members
                        WHERE workspace_id = ? AND status = 'active'
                        """,
                        (workspace["id"],),
                    ).fetchone()[0]
                )
                role = (
                    "owner"
                    if active_count == 0
                    or clean_email in {e.lower() for e in owner_emails}
                    else "operator"
                )
            else:
                raise ValueError("account is not invited to this closed beta")

            member_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO workspace_members (
                    id, workspace_id, subject, email, display_name, role, status,
                    created_at, updated_at, last_login_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    member_id,
                    workspace["id"],
                    clean_subject,
                    clean_email,
                    clean_name,
                    role,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            if invitation is not None:
                connection.execute(
                    """
                    UPDATE workspace_invitations
                    SET status = 'accepted', accepted_at = ? WHERE id = ?
                    """,
                    (timestamp, invitation["id"]),
                )
            row = connection.execute(
                "SELECT * FROM workspace_members WHERE id = ?", (member_id,)
            ).fetchone()
        assert row is not None
        return {**self._workspace_member_view(row), "workspace": workspace}

    def get_workspace_member(
        self, *, workspace_id: str, subject: str
    ) -> dict[str, Any] | None:
        workspace_id = self._workspace_id(workspace_id)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM workspace_members
                WHERE workspace_id = ? AND subject = ? AND status = 'active'
                """,
                (workspace_id, subject.strip()),
            ).fetchone()
        if row is None:
            return None
        return self._workspace_member_view(row)

    def list_workspace_members(self, workspace_id: str) -> list[dict[str, Any]]:
        workspace_id = self._workspace_id(workspace_id)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workspace_members
                WHERE workspace_id = ?
                ORDER BY status ASC,
                    CASE role WHEN 'owner' THEN 0 WHEN 'operator' THEN 1 ELSE 2 END,
                    email ASC
                """,
                (workspace_id,),
            ).fetchall()
        return [self._workspace_member_view(row) for row in rows]

    def invite_workspace_member(
        self,
        *,
        workspace_id: str,
        email: str,
        role: str,
        invited_by: str,
        expires_in_days: int = 7,
    ) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        clean_email = email.strip().lower()
        clean_role = self._workspace_role(role)
        if not clean_email or "@" not in clean_email or len(clean_email) > 254:
            raise ValueError("a valid email is required")
        if not 1 <= int(expires_in_days) <= 30:
            raise ValueError("expires_in_days must be between 1 and 30")
        timestamp = utc_now()
        expires_at = (
            datetime.now(UTC) + timedelta(days=int(expires_in_days))
        ).isoformat(timespec="seconds")
        invitation_id = str(uuid.uuid4())
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM workspace_members
                WHERE workspace_id = ? AND email = ? AND status = 'active'
                """,
                (workspace_id, clean_email),
            ).fetchone()
            if existing is not None:
                raise ValueError("email is already an active workspace member")
            connection.execute(
                """
                UPDATE workspace_invitations SET status = 'cancelled'
                WHERE workspace_id = ? AND email = ? AND status = 'pending'
                """,
                (workspace_id, clean_email),
            )
            connection.execute(
                """
                INSERT INTO workspace_invitations (
                    id, workspace_id, email, role, status, invited_by,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    invitation_id,
                    workspace_id,
                    clean_email,
                    clean_role,
                    invited_by.strip(),
                    timestamp,
                    expires_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM workspace_invitations WHERE id = ?", (invitation_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_workspace_invitations(
        self, workspace_id: str, *, status: str = "pending"
    ) -> list[dict[str, Any]]:
        workspace_id = self._workspace_id(workspace_id)
        clean_status = status.strip().lower()
        if clean_status not in {"pending", "accepted", "cancelled"}:
            raise ValueError("unsupported invitation status")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workspace_invitations
                WHERE workspace_id = ? AND status = ?
                ORDER BY created_at DESC
                """,
                (workspace_id, clean_status),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_workspace_member_role(
        self, *, workspace_id: str, member_id: str, role: str
    ) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        clean_role = self._workspace_role(role)
        timestamp = utc_now()
        with self.connect() as connection:
            current = connection.execute(
                """
                SELECT * FROM workspace_members
                WHERE id = ? AND workspace_id = ? AND status = 'active'
                """,
                (member_id, workspace_id),
            ).fetchone()
            if current is None:
                raise ValueError("active workspace member not found")
            if current["role"] == "owner" and clean_role != "owner":
                owner_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM workspace_members
                        WHERE workspace_id = ? AND role = 'owner' AND status = 'active'
                        """,
                        (workspace_id,),
                    ).fetchone()[0]
                )
                if owner_count <= 1:
                    raise ValueError("the last workspace owner cannot be demoted")
            connection.execute(
                """
                UPDATE workspace_members SET role = ?, updated_at = ? WHERE id = ?
                """,
                (clean_role, timestamp, member_id),
            )
            row = connection.execute(
                "SELECT * FROM workspace_members WHERE id = ?", (member_id,)
            ).fetchone()
        assert row is not None
        return self._workspace_member_view(row)

    @staticmethod
    def _company_profile_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _company_knowledge_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["content"] = _load_json(result.pop("content_json", None), {})
        return result

    @staticmethod
    def _agent_inbox_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["decision"] = _load_json(result.pop("decision_json", None), {})
        return result

    @staticmethod
    def _conversation_settings_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["auto_create_inbound_leads"] = bool(result["auto_create_inbound_leads"])
        for source, target in (
            ("qualification_questions_json", "qualification_questions"),
            ("forbidden_topics_json", "forbidden_topics"),
            ("escalation_rules_json", "escalation_rules"),
        ):
            result[target] = _load_json(result.pop(source, None), [])
        result["send_enabled"] = False
        result["approval_policy"] = "exact_draft_then_separate_send"
        return result

    @staticmethod
    def _conversation_session_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["facts"] = _load_json(result.pop("facts_json", None), {})
        return result

    def get_company_profile(self, workspace_id: str) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM company_profiles WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        if row is not None:
            return self._company_profile_from_row(row)
        return {
            "workspace_id": workspace_id,
            "company_name": None,
            "website_url": None,
            "industry": None,
            "geography": None,
            "positioning": None,
            "target_customer": None,
            "sales_process": None,
            "tone_of_voice": None,
            "primary_goal": None,
            "constraints": None,
            "language": None,
            "onboarding_status": "not_started",
            "revision": 0,
            "created_at": None,
            "updated_at": None,
            "completed_at": None,
        }

    def update_company_profile(
        self, workspace_id: str, **fields: str | None
    ) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        self.get_workspace(workspace_id)
        limits = {
            "company_name": 200,
            "website_url": 2048,
            "industry": 300,
            "geography": 800,
            "positioning": 4000,
            "target_customer": 4000,
            "sales_process": 6000,
            "tone_of_voice": 3000,
            "primary_goal": 3000,
            "constraints": 6000,
            "language": 80,
        }
        unexpected = set(fields) - set(limits)
        if unexpected:
            raise ValueError(
                f"unsupported company profile fields: {', '.join(sorted(unexpected))}"
            )
        if not fields:
            raise ValueError("at least one company profile field is required")
        clean: dict[str, str | None] = {}
        for name, value in fields.items():
            normalized = " ".join(str(value or "").split()) or None
            if normalized and len(normalized) > limits[name]:
                raise ValueError(f"{name} exceeds {limits[name]} characters")
            if name == "website_url" and normalized:
                normalized = canonical_company_url(normalized)
            clean[name] = normalized

        timestamp = utc_now()
        assignments = ", ".join(f"{name} = ?" for name in clean)
        values = list(clean.values())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO company_profiles (
                    workspace_id, onboarding_status, revision, created_at, updated_at
                ) VALUES (?, 'not_started', 0, ?, ?)
                """,
                (workspace_id, timestamp, timestamp),
            )
            connection.execute(
                f"""
                UPDATE company_profiles
                SET {assignments},
                    onboarding_status = CASE
                        WHEN onboarding_status = 'ready' THEN 'in_progress'
                        ELSE 'in_progress'
                    END,
                    revision = revision + 1,
                    updated_at = ?,
                    completed_at = NULL
                WHERE workspace_id = ?
                """,
                (*values, timestamp, workspace_id),
            )
            row = connection.execute(
                "SELECT * FROM company_profiles WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        assert row is not None
        return self._company_profile_from_row(row)

    def save_company_knowledge(
        self,
        workspace_id: str,
        *,
        category: str,
        title: str,
        content: Any,
        source_type: str = "chat",
        source_name: str | None = None,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        self.get_workspace(workspace_id)
        clean_category = self._validate_status(
            category, COMPANY_KNOWLEDGE_CATEGORIES, "knowledge category"
        )
        clean_title = " ".join(title.split())
        if not clean_title or len(clean_title) > 240:
            raise ValueError(
                "knowledge title must contain between 1 and 240 characters"
            )
        clean_source_type = " ".join(source_type.split()) or "chat"
        clean_source_name = " ".join(str(source_name or "").split()) or None
        if len(clean_source_type) > 80:
            raise ValueError("source_type exceeds 80 characters")
        if clean_source_name and len(clean_source_name) > 500:
            raise ValueError("source_name exceeds 500 characters")
        encoded = _json(content)
        if len(encoded.encode("utf-8")) > 64_000:
            raise ValueError(
                "knowledge content exceeds 64 KB; split it into smaller facts"
            )

        timestamp = utc_now()
        knowledge_id = str(item_id or uuid.uuid4())
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM company_knowledge_items
                WHERE id = ? AND workspace_id = ?
                """,
                (knowledge_id, workspace_id),
            ).fetchone()
            if item_id and existing is None:
                raise ValueError("company knowledge item not found")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO company_knowledge_items (
                        id, workspace_id, category, title, content_json, source_type,
                        source_name, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        knowledge_id,
                        workspace_id,
                        clean_category,
                        clean_title,
                        encoded,
                        clean_source_type,
                        clean_source_name,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE company_knowledge_items
                    SET category = ?, title = ?, content_json = ?, source_type = ?,
                        source_name = ?, status = 'active', updated_at = ?
                    WHERE id = ? AND workspace_id = ?
                    """,
                    (
                        clean_category,
                        clean_title,
                        encoded,
                        clean_source_type,
                        clean_source_name,
                        timestamp,
                        knowledge_id,
                        workspace_id,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM company_knowledge_items WHERE id = ?",
                (knowledge_id,),
            ).fetchone()
        assert row is not None
        return self._company_knowledge_from_row(row)

    def list_company_knowledge(
        self,
        workspace_id: str,
        *,
        category: str | None = None,
        status: str = "active",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        workspace_id = self._workspace_id(workspace_id)
        clean_status = self._validate_status(
            status, COMPANY_KNOWLEDGE_STATUSES, "knowledge status"
        )
        params: list[Any] = [workspace_id, clean_status]
        where = "workspace_id = ? AND status = ?"
        if category:
            clean_category = self._validate_status(
                category, COMPANY_KNOWLEDGE_CATEGORIES, "knowledge category"
            )
            where += " AND category = ?"
            params.append(clean_category)
        params.append(max(1, min(int(limit), 500)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM company_knowledge_items
                WHERE {where}
                ORDER BY updated_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._company_knowledge_from_row(row) for row in rows]

    def archive_company_knowledge(
        self, workspace_id: str, item_id: str
    ) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        timestamp = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE company_knowledge_items
                SET status = 'archived', updated_at = ?
                WHERE id = ? AND workspace_id = ? AND status = 'active'
                """,
                (timestamp, item_id, workspace_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("active company knowledge item not found")
            row = connection.execute(
                "SELECT * FROM company_knowledge_items WHERE id = ?", (item_id,)
            ).fetchone()
        assert row is not None
        return self._company_knowledge_from_row(row)

    def get_company_onboarding_state(self, workspace_id: str) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        profile = self.get_company_profile(workspace_id)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT category, COUNT(*) AS item_count
                FROM company_knowledge_items
                WHERE workspace_id = ? AND status = 'active'
                GROUP BY category
                """,
                (workspace_id,),
            ).fetchall()
        counts = {str(row["category"]): int(row["item_count"]) for row in rows}

        profile_checks = (
            "company_name",
            "industry",
            "geography",
            "target_customer",
            "positioning",
            "sales_process",
            "tone_of_voice",
            "primary_goal",
        )
        missing = [name for name in profile_checks if not profile.get(name)]
        knowledge_checks = {
            "services": counts.get("service", 0) > 0,
            "prices": counts.get("price", 0) > 0,
            "customer_proof": counts.get("case", 0) + counts.get("closed_client", 0)
            > 0,
            "active_clients": counts.get("current_client", 0) > 0,
        }
        missing.extend(
            name for name, complete in knowledge_checks.items() if not complete
        )
        completed = len(profile_checks) - sum(
            1 for name in profile_checks if name in missing
        )
        completed += sum(1 for complete in knowledge_checks.values() if complete)
        completion_percent = round(
            completed / (len(profile_checks) + len(knowledge_checks)) * 100
        )
        ready_requirements = (
            bool(profile.get("company_name")),
            bool(profile.get("industry")),
            bool(profile.get("target_customer")),
            bool(profile.get("positioning")),
            knowledge_checks["services"],
            knowledge_checks["prices"],
        )
        ready = all(ready_requirements)

        questions: list[dict[str, Any]] = []
        if not profile.get("company_name") or not profile.get("website_url"):
            questions.append(
                {
                    "id": "identity",
                    "prompt": "Как называется компания и какой у неё основной сайт? Если сайта нет, так и скажите.",
                    "accepts": ["free_text", "url"],
                }
            )
        if not profile.get("industry") or not profile.get("geography"):
            questions.append(
                {
                    "id": "market",
                    "prompt": "В какой отрасли вы работаете и в каких городах или странах продаёте?",
                    "accepts": ["free_text"],
                }
            )
        if not profile.get("target_customer"):
            questions.append(
                {
                    "id": "target_customer",
                    "prompt": "Кто ваш идеальный клиент: тип компании, размер, роль принимающего решение и типичная задача?",
                    "accepts": ["free_text", "file"],
                }
            )
        if not knowledge_checks["services"]:
            questions.append(
                {
                    "id": "services",
                    "prompt": "Опишите услуги или продукты. Можно прикрепить презентацию, каталог или объяснить свободным текстом.",
                    "accepts": ["free_text", "file"],
                }
            )
        if not knowledge_checks["prices"]:
            questions.append(
                {
                    "id": "prices",
                    "prompt": "Какие цены, пакеты, минимальный чек или правила расчёта можно использовать в продажах? Можно приложить прайс.",
                    "accepts": ["free_text", "file"],
                }
            )
        if not profile.get("positioning"):
            questions.append(
                {
                    "id": "positioning",
                    "prompt": "Почему клиент выбирает вас: ключевые отличия, подтверждённые преимущества и обещаемый результат?",
                    "accepts": ["free_text", "file"],
                }
            )
        if not knowledge_checks["customer_proof"]:
            questions.append(
                {
                    "id": "closed_clients",
                    "prompt": "Каких клиентов вы уже закрыли и какие результаты можно безопасно упоминать? Не раскрывайте то, что под NDA.",
                    "accepts": ["free_text", "file"],
                }
            )
        if not knowledge_checks["active_clients"] or not profile.get("sales_process"):
            questions.append(
                {
                    "id": "pipeline",
                    "prompt": "Какие клиенты сейчас в работе и как устроены стадии продажи от первого контакта до сделки?",
                    "accepts": ["free_text", "file"],
                }
            )
        if not profile.get("tone_of_voice"):
            questions.append(
                {
                    "id": "tone",
                    "prompt": "Каким тоном агент должен писать и какие формулировки, обещания или темы запрещены?",
                    "accepts": ["free_text", "file"],
                }
            )

        return {
            "profile": profile,
            "knowledge_counts": counts,
            "completion_percent": completion_percent,
            "ready_for_sales": ready,
            "missing": missing,
            "next_questions": questions[:3],
            "onboarding_status": profile["onboarding_status"],
        }

    def complete_company_onboarding(
        self, workspace_id: str, *, confirm_ready: bool
    ) -> dict[str, Any]:
        if not confirm_ready:
            raise ValueError(
                "confirm_ready=true is required after reviewing the profile"
            )
        workspace_id = self._workspace_id(workspace_id)
        state = self.get_company_onboarding_state(workspace_id)
        if not state["ready_for_sales"]:
            raise ValueError("onboarding is incomplete: " + ", ".join(state["missing"]))
        timestamp = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE company_profiles
                SET onboarding_status = 'ready', completed_at = ?, updated_at = ?,
                    revision = revision + 1
                WHERE workspace_id = ?
                """,
                (timestamp, timestamp, workspace_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("company profile not found")
        return self.get_company_onboarding_state(workspace_id)

    def get_conversation_agent_settings(self, workspace_id: str) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        self.get_workspace(workspace_id)
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_agent_settings (
                    workspace_id, enabled, autonomy_mode, niche,
                    max_context_messages, max_reply_chars, max_inbound_age_hours,
                    response_sla_minutes, confidence_threshold,
                    auto_create_inbound_leads, created_at, updated_at
                ) VALUES (?, 1, 'draft', 'auto', 12, 700, 168, 60, 65, 1, ?, ?)
                """,
                (workspace_id, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM conversation_agent_settings WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        assert row is not None
        return self._conversation_settings_from_row(row)

    def update_conversation_agent_settings(
        self, workspace_id: str, **fields: Any
    ) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        self.get_conversation_agent_settings(workspace_id)
        allowed = {
            "enabled",
            "autonomy_mode",
            "niche",
            "objective",
            "instructions",
            "tone",
            "qualification_questions",
            "forbidden_topics",
            "escalation_rules",
            "max_context_messages",
            "max_reply_chars",
            "max_inbound_age_hours",
            "response_sla_minutes",
            "confidence_threshold",
            "auto_create_inbound_leads",
        }
        unexpected = set(fields) - allowed
        if unexpected:
            raise ValueError(
                "unsupported conversation agent fields: "
                + ", ".join(sorted(unexpected))
            )
        assignments: list[str] = []
        values: list[Any] = []
        text_limits = {
            "niche": 120,
            "objective": 3000,
            "instructions": 6000,
            "tone": 2000,
        }
        for name, value in fields.items():
            if name in {"enabled", "auto_create_inbound_leads"}:
                assignments.append(f"{name} = ?")
                values.append(int(bool(value)))
            elif name == "autonomy_mode":
                clean = self._validate_status(
                    str(value), CONVERSATION_AUTONOMY_MODES, "autonomy_mode"
                )
                assignments.append("autonomy_mode = ?")
                values.append(clean)
            elif name in text_limits:
                clean = " ".join(str(value or "").split())
                if len(clean) > text_limits[name]:
                    raise ValueError(
                        f"{name} must not exceed {text_limits[name]} characters"
                    )
                assignments.append(f"{name} = ?")
                values.append(clean or None)
            elif name in {
                "qualification_questions",
                "forbidden_topics",
                "escalation_rules",
            }:
                if not isinstance(value, list):
                    raise ValueError(f"{name} must be a list")
                clean_items = [
                    " ".join(str(item).split())[:500]
                    for item in value[:30]
                    if " ".join(str(item).split())
                ]
                assignments.append(f"{name}_json = ?")
                values.append(_json(clean_items))
            elif name == "max_context_messages":
                assignments.append("max_context_messages = ?")
                values.append(max(4, min(int(value), 30)))
            elif name == "max_reply_chars":
                assignments.append("max_reply_chars = ?")
                values.append(max(120, min(int(value), 700)))
            elif name == "max_inbound_age_hours":
                assignments.append("max_inbound_age_hours = ?")
                values.append(max(1, min(int(value), 720)))
            elif name == "response_sla_minutes":
                assignments.append("response_sla_minutes = ?")
                values.append(max(5, min(int(value), 10_080)))
            elif name == "confidence_threshold":
                assignments.append("confidence_threshold = ?")
                values.append(max(40, min(int(value), 95)))
        if not assignments:
            return self.get_conversation_agent_settings(workspace_id)
        assignments.append("updated_at = ?")
        values.extend([utc_now(), workspace_id])
        with self.connect() as connection:
            connection.execute(
                f"UPDATE conversation_agent_settings SET {', '.join(assignments)} "
                "WHERE workspace_id = ?",
                values,
            )
        return self.get_conversation_agent_settings(workspace_id)

    def get_conversation_session(
        self, workspace_id: str, chat_id: str, *, channel: str = "whatsapp"
    ) -> dict[str, Any] | None:
        workspace_id = self._workspace_id(workspace_id)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM conversation_sessions
                WHERE workspace_id = ? AND channel = ? AND external_chat_id = ?
                """,
                (workspace_id, channel.strip().lower(), chat_id.strip()),
            ).fetchone()
        return self._conversation_session_from_row(row) if row is not None else None

    def upsert_conversation_session(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        channel: str = "whatsapp",
        lead_id: str | None = None,
        stage: str = "new",
        intent: str | None = None,
        sentiment: str | None = None,
        summary: str | None = None,
        facts: dict[str, Any] | None = None,
        unanswered_question: str | None = None,
        next_action: str | None = None,
        escalation_status: str = "none",
        escalation_reason: str | None = None,
        last_response_id: str | None = None,
        last_draft_id: str | None = None,
        last_inbound_at: str | None = None,
        increment_turn: bool = False,
    ) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        self.get_workspace(workspace_id)
        clean_chat_id = chat_id.strip()
        clean_channel = channel.strip().lower()
        clean_stage = self._validate_status(stage, CONVERSATION_STAGES, "stage")
        if not clean_chat_id or len(clean_chat_id) > 500:
            raise ValueError("chat_id is required and must not exceed 500 characters")
        if clean_channel not in {"whatsapp"}:
            raise ValueError("only whatsapp conversation sessions are supported")
        if lead_id:
            self.get_lead(lead_id)
        if last_draft_id:
            self.get_outreach_draft(last_draft_id)
        timestamp = utc_now()
        session_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_sessions (
                    id, workspace_id, channel, external_chat_id, lead_id, stage,
                    intent, sentiment, summary, facts_json, unanswered_question,
                    next_action, escalation_status, escalation_reason,
                    last_response_id, last_draft_id, turn_count, last_inbound_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, channel, external_chat_id) DO UPDATE SET
                    lead_id = COALESCE(excluded.lead_id, conversation_sessions.lead_id),
                    stage = excluded.stage,
                    intent = excluded.intent,
                    sentiment = excluded.sentiment,
                    summary = excluded.summary,
                    facts_json = excluded.facts_json,
                    unanswered_question = excluded.unanswered_question,
                    next_action = excluded.next_action,
                    escalation_status = excluded.escalation_status,
                    escalation_reason = excluded.escalation_reason,
                    last_response_id = COALESCE(excluded.last_response_id, conversation_sessions.last_response_id),
                    last_draft_id = COALESCE(excluded.last_draft_id, conversation_sessions.last_draft_id),
                    turn_count = conversation_sessions.turn_count + excluded.turn_count,
                    last_inbound_at = COALESCE(excluded.last_inbound_at, conversation_sessions.last_inbound_at),
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    workspace_id,
                    clean_channel,
                    clean_chat_id,
                    lead_id,
                    clean_stage,
                    " ".join(str(intent or "").split())[:240] or None,
                    " ".join(str(sentiment or "").split())[:80] or None,
                    " ".join(str(summary or "").split())[:3000] or None,
                    _json(facts or {}),
                    " ".join(str(unanswered_question or "").split())[:1000] or None,
                    " ".join(str(next_action or "").split())[:1000] or None,
                    " ".join(str(escalation_status or "none").split())[:80] or "none",
                    " ".join(str(escalation_reason or "").split())[:2000] or None,
                    " ".join(str(last_response_id or "").split())[:500] or None,
                    last_draft_id,
                    int(bool(increment_turn)),
                    normalize_datetime(last_inbound_at) if last_inbound_at else None,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM conversation_sessions
                WHERE workspace_id = ? AND channel = ? AND external_chat_id = ?
                """,
                (workspace_id, clean_channel, clean_chat_id),
            ).fetchone()
        assert row is not None
        return self._conversation_session_from_row(row)

    def list_conversation_sessions(
        self,
        workspace_id: str,
        *,
        stage: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        workspace_id = self._workspace_id(workspace_id)
        params: list[Any] = [workspace_id]
        where = "workspace_id = ?"
        if stage:
            where += " AND stage = ?"
            params.append(self._validate_status(stage, CONVERSATION_STAGES, "stage"))
        params.append(max(1, min(int(limit), 500)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM conversation_sessions
                WHERE {where}
                ORDER BY updated_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._conversation_session_from_row(row) for row in rows]

    def claim_next_agent_inbox_event(
        self,
        workspace_id: str,
        *,
        lease_seconds: int = 180,
        chat_jid: str | None = None,
    ) -> dict[str, Any] | None:
        workspace_id = self._workspace_id(workspace_id)
        clean_jid = normalize_whatsapp_jid(chat_jid) if chat_jid else None
        now = datetime.now(UTC)
        now_value = now.isoformat(timespec="seconds")
        lock_until = (now + timedelta(seconds=max(30, lease_seconds))).isoformat(
            timespec="seconds"
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            where_jid = " AND chat_jid = ?" if clean_jid else ""
            params: list[Any] = [workspace_id, now_value]
            if clean_jid:
                params.append(clean_jid)
            row = connection.execute(
                f"""
                SELECT id FROM agent_inbox_events
                WHERE workspace_id = ?
                  AND (
                    status = 'new'
                    OR (status = 'processing' AND agent_lock_until <= ?)
                  )
                  {where_jid}
                ORDER BY received_at ASC, created_at ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            event_id = str(row["id"])
            connection.execute(
                """
                UPDATE agent_inbox_events
                SET status = 'processing', agent_attempts = agent_attempts + 1,
                    agent_lock_until = ?, agent_error = NULL, updated_at = ?
                WHERE id = ? AND workspace_id = ?
                """,
                (lock_until, now_value, event_id, workspace_id),
            )
            claimed = connection.execute(
                "SELECT * FROM agent_inbox_events WHERE id = ?", (event_id,)
            ).fetchone()
        assert claimed is not None
        return self._agent_inbox_from_row(claimed)

    def recover_expired_agent_inbox_leases(
        self,
        workspace_id: str,
        *,
        max_inbound_age_hours: int = 168,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        """Recover abandoned leases and quarantine stale inbound conversations."""

        workspace_id = self._workspace_id(workspace_id)
        now = datetime.now(UTC)
        now_value = now.isoformat(timespec="seconds")
        bounded_age_hours = max(1, min(int(max_inbound_age_hours), 720))
        bounded_attempts = max(1, min(int(max_attempts), 10))
        stale_before = (now - timedelta(hours=bounded_age_hours)).isoformat(
            timespec="seconds"
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stale = connection.execute(
                """
                UPDATE agent_inbox_events
                SET status = 'needs_review', agent_lock_until = NULL,
                    agent_error = 'Inbound message is older than the configured response window',
                    updated_at = ?
                WHERE workspace_id = ?
                  AND status IN ('new', 'processing')
                  AND received_at < ?
                  AND (
                    status = 'new'
                    OR agent_lock_until IS NULL
                    OR agent_lock_until <= ?
                  )
                """,
                (now_value, workspace_id, stale_before, now_value),
            )
            exhausted = connection.execute(
                """
                UPDATE agent_inbox_events
                SET status = 'needs_review', agent_lock_until = NULL,
                    agent_error = 'ChatGPT did not complete this event after the configured lease attempts',
                    updated_at = ?
                WHERE workspace_id = ? AND status = 'processing'
                  AND (agent_lock_until IS NULL OR agent_lock_until <= ?)
                  AND agent_attempts >= ?
                """,
                (now_value, workspace_id, now_value, bounded_attempts),
            )
            requeued = connection.execute(
                """
                UPDATE agent_inbox_events
                SET status = 'new', agent_lock_until = NULL, agent_error = NULL,
                    updated_at = ?
                WHERE workspace_id = ? AND status = 'processing'
                  AND (agent_lock_until IS NULL OR agent_lock_until <= ?)
                  AND agent_attempts < ?
                """,
                (now_value, workspace_id, now_value, bounded_attempts),
            )
        stale_count = int(stale.rowcount)
        exhausted_count = int(exhausted.rowcount)
        requeued_count = int(requeued.rowcount)
        return {
            "checked_at": now_value,
            "max_inbound_age_hours": bounded_age_hours,
            "max_attempts": bounded_attempts,
            "stale_quarantined": stale_count,
            "leases_exhausted": exhausted_count,
            "leases_requeued": requeued_count,
            "changed": stale_count + exhausted_count + requeued_count,
            "sent": False,
        }

    def finish_agent_inbox_event(
        self,
        workspace_id: str,
        event_id: str,
        *,
        status: str,
        decision: dict[str, Any] | None = None,
        draft_id: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        clean_status = self._validate_status(
            status, AGENT_INBOX_STATUSES, "agent inbox status"
        )
        if draft_id:
            self.get_outreach_draft(draft_id)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_inbox_events
                SET status = ?, draft_id = COALESCE(?, draft_id),
                    decision_json = ?, agent_error = ?, agent_lock_until = NULL,
                    updated_at = ?
                WHERE id = ? AND workspace_id = ?
                """,
                (
                    clean_status,
                    draft_id,
                    _json(decision or {}),
                    " ".join(str(error or "").split())[:2000] or None,
                    utc_now(),
                    event_id,
                    workspace_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("agent inbox event not found")
            row = connection.execute(
                "SELECT * FROM agent_inbox_events WHERE id = ?", (event_id,)
            ).fetchone()
        assert row is not None
        return self._agent_inbox_from_row(row)

    def conversation_agent_summary(self, workspace_id: str) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        with self.connect() as connection:
            session_rows = connection.execute(
                """
                SELECT stage, COUNT(*) AS item_count FROM conversation_sessions
                WHERE workspace_id = ? GROUP BY stage
                """,
                (workspace_id,),
            ).fetchall()
        return {
            "settings": self.get_conversation_agent_settings(workspace_id),
            "inbox": self.agent_inbox_summary(workspace_id),
            "sessions": {
                str(row["stage"]): int(row["item_count"]) for row in session_rows
            },
            "active_sessions": sum(
                int(row["item_count"])
                for row in session_rows
                if str(row["stage"]) not in {"closed"}
            ),
            "send_enabled": False,
        }

    def _record_inbound_interaction_once(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        source: str,
        external_id: str,
        lead_id: str,
        content: str,
        occurred_at: str,
        timestamp: str,
    ) -> bool:
        """Persist one inbox-derived interaction without duplicating sync retries."""
        interaction_external_id = f"agent_inbox:{workspace_id}:{source}:{external_id}"
        existing = connection.execute(
            """
            SELECT id FROM interactions
            WHERE direction = 'inbound' AND external_id = ?
            LIMIT 1
            """,
            (interaction_external_id,),
        ).fetchone()
        if existing is not None:
            return False
        normalized_occurred_at = normalize_datetime(occurred_at)
        connection.execute(
            """
            INSERT INTO interactions (
                id, lead_id, channel, direction, content, status, external_id,
                occurred_at, created_at
            ) VALUES (?, ?, ?, 'inbound', ?, 'recorded', ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                lead_id,
                "whatsapp" if source == "whatsapp" else source,
                content,
                interaction_external_id,
                normalized_occurred_at,
                timestamp,
            ),
        )
        connection.execute(
            """
            UPDATE leads
            SET status = CASE
                    WHEN status IN (
                        'new', 'researching', 'analyzed', 'qualified', 'drafted',
                        'approved', 'contacted', 'follow_up'
                    ) AND EXISTS (
                        SELECT 1 FROM interactions outbound
                        WHERE outbound.lead_id = leads.id
                          AND outbound.direction = 'outbound'
                          AND outbound.occurred_at <= ?
                    ) THEN 'replied'
                    ELSE status
                END,
                updated_at = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM interactions outbound
                        WHERE outbound.lead_id = leads.id
                          AND outbound.direction = 'outbound'
                          AND outbound.occurred_at <= ?
                    ) THEN ?
                    ELSE updated_at
                END
            WHERE id = ?
            """,
            (
                normalized_occurred_at,
                normalized_occurred_at,
                timestamp,
                lead_id,
            ),
        )
        return True

    def upsert_agent_inbox_event(
        self,
        workspace_id: str,
        *,
        external_id: str,
        chat_jid: str,
        message_text: str,
        received_at: str,
        sender_label: str | None = None,
        media_type: str | None = None,
        lead_id: str | None = None,
        source: str = "whatsapp",
    ) -> tuple[dict[str, Any], bool]:
        workspace_id = self._workspace_id(workspace_id)
        self.get_workspace(workspace_id)
        clean_external_id = external_id.strip()
        clean_jid = normalize_whatsapp_jid(chat_jid)
        clean_message = " ".join(message_text.split())
        clean_source = source.strip().lower()
        if not clean_external_id or len(clean_external_id) > 500:
            raise ValueError(
                "external_id is required and must not exceed 500 characters"
            )
        if not clean_jid or len(clean_jid) > 500:
            raise ValueError("chat_jid is required and must not exceed 500 characters")
        if is_technical_whatsapp_jid(clean_jid):
            raise ValueError("technical WhatsApp JIDs cannot enter the agent inbox")
        if not clean_message:
            raise ValueError("message_text must not be empty")
        if len(clean_message) > 4000:
            clean_message = clean_message[:4000]
        if lead_id:
            self.get_lead(lead_id)
        timestamp = utc_now()
        event_id = str(uuid.uuid4())
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM agent_inbox_events
                WHERE workspace_id = ? AND source = ? AND external_id = ?
                """,
                (workspace_id, clean_source, clean_external_id),
            ).fetchone()
            created = existing is None
            if created:
                connection.execute(
                    """
                    INSERT INTO agent_inbox_events (
                        id, workspace_id, source, external_id, lead_id, chat_jid,
                        sender_label, message_text, media_type, received_at, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                    """,
                    (
                        event_id,
                        workspace_id,
                        clean_source,
                        clean_external_id,
                        lead_id,
                        clean_jid,
                        " ".join(str(sender_label or "").split()) or None,
                        clean_message,
                        media_type,
                        normalize_datetime(received_at),
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                event_id = str(existing["id"])
                connection.execute(
                    """
                    UPDATE agent_inbox_events
                    SET lead_id = COALESCE(?, lead_id), sender_label = ?,
                        message_text = ?, media_type = ?, received_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        lead_id,
                        " ".join(str(sender_label or "").split()) or None,
                        clean_message,
                        media_type,
                        normalize_datetime(received_at),
                        timestamp,
                        event_id,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM agent_inbox_events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is not None and row["lead_id"]:
                self._record_inbound_interaction_once(
                    connection,
                    workspace_id=workspace_id,
                    source=clean_source,
                    external_id=clean_external_id,
                    lead_id=str(row["lead_id"]),
                    content=clean_message,
                    occurred_at=str(row["received_at"]),
                    timestamp=timestamp,
                )
        assert row is not None
        return self._agent_inbox_from_row(row), created

    def get_agent_inbox_event(self, workspace_id: str, event_id: str) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_inbox_events
                WHERE id = ? AND workspace_id = ?
                """,
                (event_id, workspace_id),
            ).fetchone()
        if row is None:
            raise ValueError("agent inbox event not found")
        return self._agent_inbox_from_row(row)

    def _agent_inbox_operational_view(
        self,
        event: dict[str, Any],
        *,
        agent_settings: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        result = dict(event)
        try:
            received_at = datetime.fromisoformat(str(event["received_at"]))
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=UTC)
            received_at = received_at.astimezone(UTC)
            age_minutes: int | None = max(
                0, int((now - received_at).total_seconds() // 60)
            )
        except (TypeError, ValueError):
            received_at = None
            age_minutes = None

        sla_minutes = int(agent_settings["response_sla_minutes"])
        max_age_minutes = int(agent_settings["max_inbound_age_hours"]) * 60
        is_open = str(event["status"]) in AGENT_INBOX_OPEN_STATUSES
        if not is_open:
            sla_state = "closed"
        elif age_minutes is None:
            sla_state = "unknown"
        elif age_minutes >= sla_minutes:
            sla_state = "overdue"
        elif age_minutes >= max(1, round(sla_minutes * 0.75)):
            sla_state = "at_risk"
        else:
            sla_state = "on_track"

        retry_block_reason: str | None = None
        if event["status"] != "needs_review":
            retry_block_reason = "event_not_in_review"
        elif event.get("draft_id"):
            retry_block_reason = "draft_already_exists"
        elif not event.get("lead_id"):
            retry_block_reason = "lead_not_linked"
        elif received_at is None:
            retry_block_reason = "invalid_received_at"
        elif age_minutes is not None and age_minutes > max_age_minutes:
            retry_block_reason = "inbound_expired"

        result.update(
            {
                "age_minutes": age_minutes,
                "response_sla_minutes": sla_minutes,
                "sla_state": sla_state,
                "retryable": retry_block_reason is None,
                "retry_block_reason": retry_block_reason,
            }
        )
        return result

    def inspect_agent_inbox_event(
        self, workspace_id: str, event_id: str
    ) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        settings = self.get_conversation_agent_settings(workspace_id)
        event = self.get_agent_inbox_event(workspace_id, event_id)
        return self._agent_inbox_operational_view(
            event, agent_settings=settings, now=datetime.now(UTC)
        )

    def list_agent_inbox_events(
        self,
        workspace_id: str,
        *,
        status: str | None = None,
        chat_jid: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        workspace_id = self._workspace_id(workspace_id)
        params: list[Any] = [workspace_id]
        where = "workspace_id = ?"
        if status:
            clean_status = self._validate_status(
                status, AGENT_INBOX_STATUSES, "agent inbox status"
            )
            where += " AND status = ?"
            params.append(clean_status)
        if chat_jid:
            where += " AND chat_jid = ?"
            params.append(normalize_whatsapp_jid(chat_jid))
        params.append(max(1, min(int(limit), 500)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM agent_inbox_events
                WHERE {where}
                ORDER BY received_at DESC, created_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        settings = self.get_conversation_agent_settings(workspace_id)
        now = datetime.now(UTC)
        return [
            self._agent_inbox_operational_view(
                self._agent_inbox_from_row(row), agent_settings=settings, now=now
            )
            for row in rows
        ]

    def update_agent_inbox_event(
        self,
        workspace_id: str,
        event_id: str,
        *,
        status: str,
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        clean_status = self._validate_status(
            status, AGENT_INBOX_STATUSES, "agent inbox status"
        )
        if draft_id:
            self.get_outreach_draft(draft_id)
        timestamp = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_inbox_events
                SET status = ?, draft_id = COALESCE(?, draft_id),
                    agent_lock_until = CASE WHEN ? = 'processing' THEN agent_lock_until ELSE NULL END,
                    updated_at = ?
                WHERE id = ? AND workspace_id = ?
                """,
                (
                    clean_status,
                    draft_id,
                    clean_status,
                    timestamp,
                    event_id,
                    workspace_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("agent inbox event not found")
            row = connection.execute(
                "SELECT * FROM agent_inbox_events WHERE id = ?", (event_id,)
            ).fetchone()
        assert row is not None
        return self._agent_inbox_from_row(row)

    def requeue_agent_inbox_event(
        self, workspace_id: str, event_id: str
    ) -> dict[str, Any]:
        """Safely return one reviewed, fresh event to the ChatGPT work queue."""

        workspace_id = self._workspace_id(workspace_id)
        agent_settings = self.get_conversation_agent_settings(workspace_id)
        now = datetime.now(UTC)
        timestamp = now.isoformat(timespec="seconds")
        stale_before = now - timedelta(
            hours=int(agent_settings["max_inbound_age_hours"])
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM agent_inbox_events
                WHERE id = ? AND workspace_id = ?
                """,
                (event_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ValueError("agent inbox event not found")
            event = self._agent_inbox_from_row(row)
            if event["status"] == "new":
                return {
                    **self._agent_inbox_operational_view(
                        event, agent_settings=agent_settings, now=now
                    ),
                    "requeued": False,
                    "idempotent": True,
                    "previous_status": "new",
                }
            if event["status"] != "needs_review":
                raise ValueError("only needs_review events can be retried")
            if event.get("draft_id"):
                raise ValueError("event already has a draft and cannot be retried")
            if not event.get("lead_id"):
                raise ValueError("link the event to a CRM lead before retrying")
            try:
                received_at = datetime.fromisoformat(str(event["received_at"]))
                if received_at.tzinfo is None:
                    received_at = received_at.replace(tzinfo=UTC)
                received_at = received_at.astimezone(UTC)
            except (TypeError, ValueError) as exc:
                raise ValueError("event received_at is invalid") from exc
            if received_at < stale_before:
                raise ValueError(
                    "event is outside max_inbound_age_hours and cannot be retried"
                )

            cursor = connection.execute(
                """
                UPDATE agent_inbox_events
                SET status = 'new', agent_attempts = 0, agent_lock_until = NULL,
                    agent_error = NULL, decision_json = NULL, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND status = 'needs_review'
                  AND draft_id IS NULL AND lead_id IS NOT NULL
                """,
                (timestamp, event_id, workspace_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("agent inbox event changed during retry")
            updated = connection.execute(
                "SELECT * FROM agent_inbox_events WHERE id = ?", (event_id,)
            ).fetchone()
        assert updated is not None
        return {
            **self._agent_inbox_operational_view(
                self._agent_inbox_from_row(updated),
                agent_settings=agent_settings,
                now=now,
            ),
            "requeued": True,
            "idempotent": False,
            "previous_status": "needs_review",
        }

    def link_agent_inbox_event(
        self,
        workspace_id: str,
        event_id: str,
        lead_id: str,
    ) -> dict[str, Any]:
        """Link an unmatched inbound event to an existing CRM lead."""
        workspace_id = self._workspace_id(workspace_id)
        self.get_lead(lead_id)
        timestamp = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_inbox_events
                SET lead_id = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ?
                """,
                (lead_id, timestamp, event_id, workspace_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("agent inbox event not found")
            row = connection.execute(
                "SELECT * FROM agent_inbox_events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is not None:
                self._record_inbound_interaction_once(
                    connection,
                    workspace_id=workspace_id,
                    source=str(row["source"]),
                    external_id=str(row["external_id"]),
                    lead_id=lead_id,
                    content=str(row["message_text"]),
                    occurred_at=str(row["received_at"]),
                    timestamp=timestamp,
                )
        assert row is not None
        return self._agent_inbox_from_row(row)

    def agent_inbox_summary(self, workspace_id: str) -> dict[str, Any]:
        workspace_id = self._workspace_id(workspace_id)
        agent_settings = self.get_conversation_agent_settings(workspace_id)
        now = datetime.now(UTC)
        now_value = now.isoformat(timespec="seconds")
        stale_before = (
            now - timedelta(hours=int(agent_settings["max_inbound_age_hours"]))
        ).isoformat(timespec="seconds")
        sla_before = (
            now - timedelta(minutes=int(agent_settings["response_sla_minutes"]))
        ).isoformat(timespec="seconds")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS event_count
                FROM agent_inbox_events WHERE workspace_id = ? GROUP BY status
                """,
                (workspace_id,),
            ).fetchall()
            health = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'processing'
                        AND agent_lock_until > ? THEN 1 ELSE 0 END) AS processing_active,
                    SUM(CASE WHEN status = 'processing'
                        AND (agent_lock_until IS NULL OR agent_lock_until <= ?)
                        THEN 1 ELSE 0 END) AS processing_expired,
                    SUM(CASE WHEN status IN ('new', 'processing')
                        AND received_at < ? THEN 1 ELSE 0 END) AS stale_actionable,
                    SUM(CASE WHEN status IN (
                            'new', 'acknowledged', 'processing', 'drafted', 'needs_review'
                        ) AND received_at < ? THEN 1 ELSE 0 END) AS sla_overdue,
                    MIN(CASE WHEN status IN (
                            'new', 'acknowledged', 'processing', 'drafted', 'needs_review'
                        ) THEN received_at END) AS oldest_open_at
                FROM agent_inbox_events WHERE workspace_id = ?
                """,
                (now_value, now_value, stale_before, sla_before, workspace_id),
            ).fetchone()
        counts = {status: 0 for status in AGENT_INBOX_STATUSES}
        counts.update({str(row["status"]): int(row["event_count"]) for row in rows})
        counts["total"] = sum(counts[status] for status in AGENT_INBOX_STATUSES)
        counts["processing_active"] = int(health["processing_active"] or 0)
        counts["processing_expired"] = int(health["processing_expired"] or 0)
        counts["stale_actionable"] = int(health["stale_actionable"] or 0)
        counts["sla_overdue"] = int(health["sla_overdue"] or 0)
        counts["response_sla_minutes"] = int(agent_settings["response_sla_minutes"])
        oldest_open_at = health["oldest_open_at"]
        if oldest_open_at:
            oldest = datetime.fromisoformat(str(oldest_open_at))
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=UTC)
            counts["oldest_open_minutes"] = max(
                0, int((now - oldest.astimezone(UTC)).total_seconds() // 60)
            )
        else:
            counts["oldest_open_minutes"] = 0
        return counts

    def agent_coordination_summary(
        self,
        workspace_id: str,
        *,
        include_leads: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return shared two-chat state without private message or recipient data."""
        workspace_id = self._workspace_id(workspace_id)
        self.get_workspace(workspace_id)
        bounded_limit = max(1, min(int(limit), 100))
        onboarding = self.get_company_onboarding_state(workspace_id)
        inbox = self.agent_inbox_summary(workspace_id)
        with self.connect() as connection:
            prospecting_counts = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_leads,
                    SUM(CASE WHEN status IN ('new', 'researching')
                        THEN 1 ELSE 0 END) AS unreviewed,
                    SUM(CASE WHEN status = 'analyzed'
                        THEN 1 ELSE 0 END) AS analyzed,
                    SUM(CASE WHEN status = 'qualified'
                        THEN 1 ELSE 0 END) AS qualified,
                    MAX(score) AS top_score
                FROM leads
                WHERE source != 'whatsapp_inbound'
                """
            ).fetchone()
            response_counts = connection.execute(
                """
                WITH activity AS (
                    SELECT
                        l.id,
                        MIN(CASE WHEN i.direction = 'outbound'
                            THEN i.occurred_at END) AS first_outbound_at,
                        MAX(CASE WHEN i.direction = 'outbound'
                            THEN i.occurred_at END) AS last_outbound_at,
                        MAX(CASE WHEN i.direction = 'inbound'
                            THEN i.occurred_at END) AS last_inbound_at
                    FROM leads l
                    LEFT JOIN interactions i ON i.lead_id = l.id
                    GROUP BY l.id
                )
                SELECT
                    SUM(CASE WHEN first_outbound_at IS NOT NULL
                        THEN 1 ELSE 0 END) AS contacted,
                    SUM(CASE WHEN first_outbound_at IS NOT NULL
                        AND last_inbound_at >= first_outbound_at
                        THEN 1 ELSE 0 END) AS replied,
                    SUM(CASE WHEN first_outbound_at IS NOT NULL
                        AND (last_inbound_at IS NULL
                            OR last_inbound_at < first_outbound_at)
                        THEN 1 ELSE 0 END) AS never_replied,
                    SUM(CASE WHEN last_outbound_at IS NOT NULL
                        AND (last_inbound_at IS NULL
                            OR last_outbound_at > last_inbound_at)
                        THEN 1 ELSE 0 END) AS awaiting_reply
                FROM activity
                """
            ).fetchone()
            knowledge_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM company_knowledge_items
                    WHERE workspace_id = ? AND status = 'active'
                    """,
                    (workspace_id,),
                ).fetchone()[0]
            )
            active_sessions = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM conversation_sessions
                    WHERE workspace_id = ? AND stage != 'closed'
                    """,
                    (workspace_id,),
                ).fetchone()[0]
            )
            waiting_drafts = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM outreach_drafts draft
                    WHERE draft.status = 'draft'
                      AND NOT EXISTS (
                          SELECT 1 FROM agent_inbox_events inbox_event
                          WHERE inbox_event.draft_id = draft.id
                      )
                    """
                ).fetchone()[0]
            )

            def response_leads(condition: str) -> list[dict[str, Any]]:
                rows = connection.execute(
                    f"""
                    WITH activity AS (
                        SELECT
                            l.id, l.company_name, l.website_url, l.status, l.score,
                            MIN(CASE WHEN i.direction = 'outbound'
                                THEN i.occurred_at END) AS first_outbound_at,
                            MAX(CASE WHEN i.direction = 'outbound'
                                THEN i.occurred_at END) AS last_outbound_at,
                            MAX(CASE WHEN i.direction = 'inbound'
                                THEN i.occurred_at END) AS last_inbound_at
                        FROM leads l
                        LEFT JOIN interactions i ON i.lead_id = l.id
                        GROUP BY l.id
                    )
                    SELECT * FROM activity
                    WHERE first_outbound_at IS NOT NULL AND ({condition})
                    ORDER BY COALESCE(last_inbound_at, last_outbound_at) DESC
                    LIMIT ?
                    """,
                    (bounded_limit,),
                ).fetchall()
                return [dict(row) for row in rows]

            response_lists = (
                {
                    "replied_leads": response_leads(
                        "last_inbound_at >= first_outbound_at"
                    ),
                    "never_replied_leads": response_leads(
                        "last_inbound_at IS NULL OR last_inbound_at < first_outbound_at"
                    ),
                    "awaiting_reply_leads": response_leads(
                        "last_inbound_at IS NULL OR last_outbound_at > last_inbound_at"
                    ),
                }
                if include_leads
                else {}
            )
        contacted = int(response_counts["contacted"] or 0)
        replied = int(response_counts["replied"] or 0)
        return {
            "execution_mode": "chatgpt_mcp_two_chat",
            "onboarding": {
                "status": onboarding["onboarding_status"],
                "ready_for_sales": bool(onboarding["ready_for_sales"]),
                "next_questions": onboarding["next_questions"],
            },
            "lanes": {
                "inbox": {
                    "responsibility": "new inbound WhatsApp messages only",
                    **inbox,
                },
                "prospecting": {
                    "responsibility": "new companies, analysis, scoring and drafts only",
                    "total_leads": int(prospecting_counts["total_leads"] or 0),
                    "unreviewed": int(prospecting_counts["unreviewed"] or 0),
                    "analyzed": int(prospecting_counts["analyzed"] or 0),
                    "qualified": int(prospecting_counts["qualified"] or 0),
                    "drafts_waiting_review": waiting_drafts,
                    "top_score": prospecting_counts["top_score"],
                },
            },
            "responses": {
                "contacted": contacted,
                "replied": replied,
                "never_replied": int(response_counts["never_replied"] or 0),
                "awaiting_reply": int(response_counts["awaiting_reply"] or 0),
                "reply_rate_percent": (
                    round(replied * 100 / contacted, 1) if contacted else 0.0
                ),
                **response_lists,
            },
            "shared_memory": {
                "company_knowledge_items": knowledge_count,
                "active_conversation_sessions": active_sessions,
                "storage": "persistent_server_crm",
                "guesses_promoted_to_company_facts": False,
            },
            "safety": {
                "external_send": False,
                "approves": False,
                "sends": False,
                "private_message_text_included": False,
            },
        }

    def resolve_agent_inbox_for_draft(self, workspace_id: str, draft_id: str) -> int:
        workspace_id = self._workspace_id(workspace_id)
        timestamp = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_inbox_events
                SET status = 'resolved', updated_at = ?
                WHERE workspace_id = ? AND draft_id = ?
                    AND status IN ('new', 'acknowledged', 'processing', 'drafted', 'needs_review')
                """,
                (timestamp, workspace_id, draft_id),
            )
            return int(cursor.rowcount)

    @staticmethod
    def _validate_status(value: str, allowed: set[str], field: str) -> str:
        normalized = value.strip().lower()
        if normalized not in allowed:
            raise ValueError(f"{field} must be one of: {', '.join(sorted(allowed))}")
        return normalized

    @staticmethod
    def _lead_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["contacts"] = _load_json(result.pop("contacts_json", None), {})
        result["analysis"] = _load_json(result.pop("analysis_json", None), {})
        result["score_details"] = _load_json(result.pop("score_details_json", None), {})
        result["inspection"] = _load_json(result.pop("inspection_json", None), {})
        result.pop("domain_key", None)
        result.pop("name_key", None)
        result.pop("location_key", None)
        return result

    @staticmethod
    def _sync_lead_lookup_keys(connection: sqlite3.Connection, lead_id: str) -> None:
        row = connection.execute(
            """
            SELECT company_name, website_url, location, contacts_json
            FROM leads WHERE id = ?
            """,
            (lead_id,),
        ).fetchone()
        if row is None:
            raise ValueError("lead not found")
        connection.execute(
            """
            UPDATE leads SET domain_key = ?, name_key = ?, location_key = ?
            WHERE id = ?
            """,
            (
                company_domain_key(row["website_url"]),
                company_name_key(row["company_name"]),
                location_key(row["location"]),
                lead_id,
            ),
        )
        contacts = _load_json(row["contacts_json"], {})
        phones = phone_keys(
            (contacts.get("phones") or []) if isinstance(contacts, dict) else []
        )
        connection.execute("DELETE FROM lead_phone_keys WHERE lead_id = ?", (lead_id,))
        connection.executemany(
            "INSERT INTO lead_phone_keys (lead_id, phone_key) VALUES (?, ?)",
            [(lead_id, phone) for phone in sorted(phones)],
        )

    @staticmethod
    def _draft_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _vertical_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["days"] = _load_json(result.pop("days_json", None), [])
        result["enabled"] = bool(result["enabled"])
        return result

    @staticmethod
    def _autopilot_state_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["running"] = bool(result["running"])
        return result

    def stats(self) -> dict[str, int]:
        with self.connect() as connection:
            return {
                "campaigns": connection.execute(
                    "SELECT COUNT(*) FROM campaigns"
                ).fetchone()[0],
                "leads": connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
                "outreach_drafts": connection.execute(
                    "SELECT COUNT(*) FROM outreach_drafts"
                ).fetchone()[0],
                "pending_followups": connection.execute(
                    "SELECT COUNT(*) FROM followups WHERE status = 'pending'"
                ).fetchone()[0],
            }

    def remove_production_safe_check_artifacts(self) -> int:
        """Delete legacy synthetic verification leads and their cascaded records."""
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM leads WHERE source = ?", ("production-safe-check",)
            )
            return int(cursor.rowcount)

    def create_campaign(
        self,
        name: str,
        *,
        industry: str | None = None,
        location: str | None = None,
        search_query: str | None = None,
        target_count: int = 20,
        status: str = "draft",
    ) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("campaign name must not be empty")
        status = self._validate_status(status, CAMPAIGN_STATUSES, "campaign status")
        campaign_id = str(uuid.uuid4())
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO campaigns (
                    id, name, industry, location, search_query, target_count, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    name.strip(),
                    industry.strip() if industry else None,
                    location.strip() if location else None,
                    search_query.strip() if search_query else None,
                    max(1, min(int(target_count), 1000)),
                    status,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_campaign(campaign_id)

    def get_or_create_campaign(
        self,
        name: str,
        *,
        industry: str | None = None,
        location: str | None = None,
        search_query: str | None = None,
        target_count: int = 20,
        status: str = "draft",
    ) -> tuple[dict[str, Any], bool]:
        normalized_name = name.strip()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM campaigns
                WHERE name = ? COLLATE NOCASE
                  AND COALESCE(industry, '') = COALESCE(?, '') COLLATE NOCASE
                  AND COALESCE(location, '') = COALESCE(?, '') COLLATE NOCASE
                ORDER BY created_at DESC LIMIT 1
                """,
                (normalized_name, industry, location),
            ).fetchone()
        if row is not None:
            return self.get_campaign(row["id"]), False
        return (
            self.create_campaign(
                normalized_name,
                industry=industry,
                location=location,
                search_query=search_query,
                target_count=target_count,
                status=status,
            ),
            True,
        )

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT c.*,
                       COUNT(cl.lead_id) AS lead_count,
                       SUM(CASE WHEN l.status IN ('analyzed','qualified','drafted','approved','contacted','replied','won') THEN 1 ELSE 0 END) AS analyzed_count,
                       SUM(CASE WHEN l.status IN ('contacted','replied','won') THEN 1 ELSE 0 END) AS contacted_count
                FROM campaigns c
                LEFT JOIN campaign_leads cl ON cl.campaign_id = c.id
                LEFT JOIN leads l ON l.id = cl.lead_id
                WHERE c.id = ?
                GROUP BY c.id
                """,
                (campaign_id,),
            ).fetchone()
        if row is None:
            raise ValueError("campaign not found")
        result = dict(row)
        result["analyzed_count"] = result["analyzed_count"] or 0
        result["contacted_count"] = result["contacted_count"] or 0
        return result

    def list_campaigns(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        values: list[Any] = []
        where = ""
        if status:
            values.append(
                self._validate_status(status, CAMPAIGN_STATUSES, "campaign status")
            )
            where = "WHERE c.status = ?"
        values.append(max(1, min(int(limit), 200)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*, COUNT(cl.lead_id) AS lead_count
                FROM campaigns c
                LEFT JOIN campaign_leads cl ON cl.campaign_id = c.id
                {where}
                GROUP BY c.id
                ORDER BY c.created_at DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_campaign_status(self, campaign_id: str, status: str) -> dict[str, Any]:
        status = self._validate_status(status, CAMPAIGN_STATUSES, "campaign status")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now(), campaign_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("campaign not found")
        return self.get_campaign(campaign_id)

    def find_lead_by_website_url(self, website_url: str) -> dict[str, Any] | None:
        duplicate = self.find_duplicate_lead(
            company_name="", website_url=website_url, phones=None, location=None
        )
        return duplicate

    def find_duplicate_lead(
        self,
        *,
        company_name: str,
        website_url: str,
        phones: list[str] | None = None,
        location: str | None = None,
        exclude_lead_id: str | None = None,
    ) -> dict[str, Any] | None:
        domain = company_domain_key(website_url)
        name = company_name_key(company_name)
        region = location_key(location)
        candidate_phone_keys = phone_keys(phones or [])
        excluded_sql = " AND id != ?" if exclude_lead_id else ""
        excluded_values: list[Any] = [exclude_lead_id] if exclude_lead_id else []
        with self.connect() as connection:
            domain_row = connection.execute(
                f"""
                SELECT id FROM leads
                WHERE domain_key = ?{excluded_sql}
                ORDER BY created_at ASC LIMIT 1
                """,
                [domain, *excluded_values],
            ).fetchone()
            matched_id = domain_row["id"] if domain_row else None

            if matched_id is None and candidate_phone_keys:
                placeholders = ",".join("?" for _ in candidate_phone_keys)
                phone_exclusion = " AND l.id != ?" if exclude_lead_id else ""
                phone_row = connection.execute(
                    f"""
                    SELECT l.id
                    FROM lead_phone_keys p
                    JOIN leads l ON l.id = p.lead_id
                    WHERE p.phone_key IN ({placeholders}){phone_exclusion}
                    ORDER BY l.created_at ASC LIMIT 1
                    """,
                    [*sorted(candidate_phone_keys), *excluded_values],
                ).fetchone()
                matched_id = phone_row["id"] if phone_row else None

            cautious_name = bool(
                name and (region or (len(name) >= 10 and len(name.split()) >= 2))
            )
            if matched_id is None and cautious_name:
                name_row = connection.execute(
                    f"""
                    SELECT id FROM leads
                    WHERE name_key = ? AND location_key = ?{excluded_sql}
                    ORDER BY created_at ASC LIMIT 1
                    """,
                    [name, region, *excluded_values],
                ).fetchone()
                matched_id = name_row["id"] if name_row else None
        return self.get_lead(matched_id) if matched_id else None

    def find_leads_by_phone(
        self, value: str, *, limit: int = 5
    ) -> list[dict[str, Any]]:
        phone = normalize_phone(value)
        if not phone:
            return []
        result_limit = max(1, min(int(limit), 20))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT l.id, l.company_name, l.website_url, l.status
                FROM lead_phone_keys p
                JOIN leads l ON l.id = p.lead_id
                WHERE p.phone_key = ?
                ORDER BY l.updated_at DESC
                LIMIT ?
                """,
                (phone, result_limit),
            ).fetchall()
        return [
            {
                "lead_id": row["id"],
                "company_name": row["company_name"],
                "website_url": row["website_url"],
                "status": row["status"],
            }
            for row in rows
        ]

    def upsert_lead(
        self,
        company_name: str,
        website_url: str,
        *,
        industry: str | None = None,
        location: str | None = None,
        source: str = "manual",
        campaign_id: str | None = None,
        source_rank: int | None = None,
        phones: list[str] | None = None,
    ) -> dict[str, Any]:
        website_url = canonical_company_url(website_url)
        name = company_name.strip() or urlsplit(website_url).hostname or website_url
        timestamp = utc_now()
        duplicate = self.find_duplicate_lead(
            company_name=name,
            website_url=website_url,
            phones=phones,
            location=location,
        )
        lead_id = duplicate["id"] if duplicate else str(uuid.uuid4())
        with self.connect() as connection:
            if duplicate:
                connection.execute(
                    """
                    UPDATE leads SET
                        industry = COALESCE(industry, ?),
                        location = COALESCE(location, ?),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        industry.strip() if industry else None,
                        location.strip() if location else None,
                        timestamp,
                        lead_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO leads (
                        id, company_name, website_url, industry, location, source,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lead_id,
                        name,
                        website_url,
                        industry.strip() if industry else None,
                        location.strip() if location else None,
                        source.strip() or "manual",
                        timestamp,
                        timestamp,
                    ),
                )
            normalized_phones = sorted(phone_keys(phones or []))
            if normalized_phones:
                row = connection.execute(
                    "SELECT contacts_json FROM leads WHERE id = ?", (lead_id,)
                ).fetchone()
                contacts = normalize_contacts(_load_json(row["contacts_json"], {}))
                contacts["phones"] = sorted(
                    set(contacts["phones"]) | set(normalized_phones)
                )
                connection.execute(
                    "UPDATE leads SET contacts_json = ?, updated_at = ? WHERE id = ?",
                    (_json(contacts), timestamp, lead_id),
                )
            self._sync_lead_lookup_keys(connection, lead_id)
            if campaign_id:
                if (
                    connection.execute(
                        "SELECT 1 FROM campaigns WHERE id = ?", (campaign_id,)
                    ).fetchone()
                    is None
                ):
                    raise ValueError("campaign not found")
                connection.execute(
                    """
                    INSERT INTO campaign_leads (campaign_id, lead_id, source_rank, added_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(campaign_id, lead_id) DO UPDATE SET
                        source_rank = COALESCE(excluded.source_rank, campaign_leads.source_rank)
                    """,
                    (campaign_id, lead_id, source_rank, timestamp),
                )
        return self.get_lead(lead_id)

    def get_lead(self, lead_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM leads WHERE id = ?", (lead_id,)
            ).fetchone()
            if row is None:
                raise ValueError("lead not found")
            result = self._lead_from_row(row)
            result["campaign_ids"] = [
                item[0]
                for item in connection.execute(
                    "SELECT campaign_id FROM campaign_leads WHERE lead_id = ? ORDER BY added_at",
                    (lead_id,),
                ).fetchall()
            ]
        return result

    def list_leads(
        self,
        *,
        campaign_id: str | None = None,
        status: str | None = None,
        min_score: int | None = None,
        limit: int = 50,
        order_by_score: bool = True,
        fresh_evidence_only: bool = False,
    ) -> list[dict[str, Any]]:
        joins = ""
        conditions: list[str] = []
        values: list[Any] = []
        if campaign_id:
            joins = "JOIN campaign_leads cl ON cl.lead_id = l.id"
            conditions.append("cl.campaign_id = ?")
            values.append(campaign_id)
        if status:
            conditions.append("l.status = ?")
            values.append(self._validate_status(status, LEAD_STATUSES, "lead status"))
        if min_score is not None:
            conditions.append("COALESCE(l.score, 0) >= ?")
            values.append(max(0, min(int(min_score), 100)))
        if fresh_evidence_only:
            conditions.extend(
                [
                    "COALESCE(l.inspection_json, '{}') != '{}'",
                    "l.evidence_expires_at IS NOT NULL",
                    "l.evidence_expires_at > ?",
                ]
            )
            values.append(utc_now())
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        order = (
            "COALESCE(l.score, -1) DESC, l.updated_at DESC"
            if order_by_score
            else "l.created_at DESC"
        )
        values.append(max(1, min(int(limit), 200)))
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT DISTINCT l.* FROM leads l {joins} {where} ORDER BY {order} LIMIT ?",
                values,
            ).fetchall()
        return [self._lead_from_row(row) for row in rows]

    def save_inspection(
        self,
        lead_id: str,
        snapshot: dict[str, Any],
        *,
        ttl_hours: int = 168,
    ) -> dict[str, Any]:
        if not isinstance(snapshot, dict) or not snapshot:
            raise ValueError("snapshot must be a non-empty object")
        lead = self.get_lead(lead_id)
        evidence_url = str(
            snapshot.get("final_url") or snapshot.get("requested_url") or ""
        ).strip()
        if evidence_url:
            lead_domain = company_domain_key(lead["website_url"]).split(":", 1)[0]
            evidence_domain = company_domain_key(evidence_url).split(":", 1)[0]
            related_domains = (
                lead_domain == evidence_domain
                or lead_domain.endswith(f".{evidence_domain}")
                or evidence_domain.endswith(f".{lead_domain}")
            )
            if not related_domains:
                raise ValueError(
                    "inspection evidence URL does not match the lead domain"
                )
        inspected_at = datetime.now(UTC)
        expires_at = inspected_at + timedelta(hours=max(1, min(int(ttl_hours), 720)))
        timestamp = inspected_at.isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE leads SET inspection_json = ?, last_inspected_at = ?,
                    evidence_expires_at = ?, updated_at = ? WHERE id = ?
                """,
                (
                    _json(snapshot),
                    timestamp,
                    expires_at.isoformat(timespec="seconds"),
                    timestamp,
                    lead_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("lead not found")
        return self.get_lead(lead_id)

    def get_inspection(
        self, lead_id: str, *, allow_stale: bool = False
    ) -> dict[str, Any] | None:
        lead = self.get_lead(lead_id)
        snapshot = lead.get("inspection")
        expires_at = lead.get("evidence_expires_at")
        if not isinstance(snapshot, dict) or not snapshot or not expires_at:
            return None
        expires = datetime.fromisoformat(str(expires_at))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        expires = expires.astimezone(UTC)
        fresh = expires > datetime.now(UTC)
        if not fresh and not allow_stale:
            return None
        return {
            "snapshot": snapshot,
            "fresh": fresh,
            "inspected_at": lead.get("last_inspected_at"),
            "expires_at": expires.isoformat(timespec="seconds"),
        }

    def require_fresh_evidence(self, lead_id: str) -> dict[str, Any]:
        inspection = self.get_inspection(lead_id)
        if inspection is None:
            raise ValueError(
                "Fresh website evidence is required; call sales_analyze_lead or "
                "sales_inspect_website again."
            )
        return inspection

    def update_lead_status(self, lead_id: str, status: str) -> dict[str, Any]:
        status = self._validate_status(status, LEAD_STATUSES, "lead status")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE leads SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now(), lead_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("lead not found")
        return self.get_lead(lead_id)

    def save_analysis(self, lead_id: str, analysis: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(analysis, dict) or not analysis:
            raise ValueError("analysis must be a non-empty object")
        timestamp = utc_now()
        score = analysis.get("lead_score")
        normalized_score = max(0, min(int(score), 100)) if score is not None else None
        contacts_value = (
            analysis.get("contacts")
            if isinstance(analysis.get("contacts"), dict)
            else {}
        )
        contacts = normalize_contacts(contacts_value)
        analysis = {**analysis, "contacts": contacts}
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE leads SET
                    company_name = COALESCE(NULLIF(?, ''), company_name),
                    industry = COALESCE(NULLIF(?, ''), industry),
                    location = COALESCE(NULLIF(?, ''), location),
                    summary = ?,
                    contacts_json = ?,
                    analysis_json = ?,
                    score = COALESCE(?, score),
                    score_reason = COALESCE(NULLIF(?, ''), score_reason),
                    status = CASE WHEN status IN ('new','researching') THEN 'analyzed' ELSE status END,
                    last_analyzed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    str(analysis.get("company_name") or "").strip(),
                    str(analysis.get("industry") or "").strip(),
                    str(analysis.get("location") or "").strip(),
                    str(analysis.get("summary") or "").strip() or None,
                    _json(contacts),
                    _json(analysis),
                    normalized_score,
                    str(analysis.get("score_reason") or "").strip(),
                    timestamp,
                    timestamp,
                    lead_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("lead not found")
            self._sync_lead_lookup_keys(connection, lead_id)
        return self.get_lead(lead_id)

    def score_lead(
        self,
        lead_id: str,
        *,
        fit: int | None = None,
        need: int | None = None,
        budget: int | None = None,
        timing: int | None = None,
        confidence: int | None = None,
        rationale: str | None = None,
        qualify_at: int = 65,
    ) -> dict[str, Any]:
        lead = self.get_lead(lead_id)
        analysis = lead.get("analysis") or {}

        problems = analysis.get("website_problems") or []
        opportunities = analysis.get("opportunities") or []
        services = analysis.get("recommended_ollum_services") or []
        angles = analysis.get("outreach_angles") or []
        contacts = analysis.get("contacts") or lead.get("contacts") or {}
        contact_count = sum(
            len(value) for value in contacts.values() if isinstance(value, list)
        )

        heuristic = {
            "fit": min(100, 25 + len(services) * 15 + len(opportunities) * 5),
            "need": min(100, len(problems) * 18 + len(opportunities) * 10),
            "budget": 50,
            "timing": 40,
            "confidence": min(
                100,
                20 + len(problems) * 8 + len(angles) * 6 + min(contact_count, 4) * 8,
            ),
        }

        def component(value: int | None, key: str) -> int:
            return max(0, min(int(value if value is not None else heuristic[key]), 100))

        details = {
            "fit": component(fit, "fit"),
            "need": component(need, "need"),
            "budget": component(budget, "budget"),
            "timing": component(timing, "timing"),
            "confidence": component(confidence, "confidence"),
            "weights": {"fit": 0.35, "need": 0.30, "budget": 0.20, "timing": 0.15},
        }
        score = round(
            details["fit"] * 0.35
            + details["need"] * 0.30
            + details["budget"] * 0.20
            + details["timing"] * 0.15
        )
        reason = (rationale or "").strip() or (
            f"Fit {details['fit']}, need {details['need']}, budget {details['budget']}, "
            f"timing {details['timing']}; confidence {details['confidence']}."
        )
        threshold = max(0, min(int(qualify_at), 100))
        details["qualify_at"] = threshold
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE leads SET score = ?, score_reason = ?, score_details_json = ?,
                    status = CASE
                        WHEN ? >= ? AND status IN ('new','researching','analyzed','qualified') THEN 'qualified'
                        WHEN ? < ? AND status IN ('new','researching','analyzed','qualified') THEN 'analyzed'
                        ELSE status
                    END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    score,
                    reason,
                    _json(details),
                    score,
                    threshold,
                    score,
                    threshold,
                    utc_now(),
                    lead_id,
                ),
            )
        return self.get_lead(lead_id)

    def save_agent_reply_draft(
        self,
        workspace_id: str,
        event_id: str,
        *,
        lead_id: str,
        recipient: str,
        message: str,
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically finalize one leased inbox event with exactly one reply draft."""

        workspace_id = self._workspace_id(workspace_id)
        self.get_lead(lead_id)
        clean_message = message.strip()
        if not clean_message:
            raise ValueError("message must not be empty")
        if len(clean_message) > 4000:
            raise ValueError("message must be at most 4000 characters")
        clean_recipient = recipient.strip()
        if not clean_recipient:
            raise ValueError("recipient must not be empty")

        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event_row = connection.execute(
                """
                SELECT * FROM agent_inbox_events
                WHERE id = ? AND workspace_id = ?
                """,
                (event_id, workspace_id),
            ).fetchone()
            if event_row is None:
                raise ValueError("agent inbox event not found")
            if event_row["lead_id"] != lead_id:
                raise ValueError("inbox event belongs to a different lead")

            existing_draft_id = event_row["draft_id"]
            if existing_draft_id:
                draft_row = connection.execute(
                    "SELECT * FROM outreach_drafts WHERE id = ?",
                    (existing_draft_id,),
                ).fetchone()
                assert draft_row is not None
                return {
                    "event": self._agent_inbox_from_row(event_row),
                    "draft": self._draft_from_row(draft_row),
                    "idempotent": True,
                }
            if event_row["status"] != "processing":
                raise ValueError("agent inbox event is not leased for processing")

            draft_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO outreach_drafts (
                    id, lead_id, channel, recipient, message, status, created_at, updated_at
                ) VALUES (?, ?, 'whatsapp', ?, ?, 'draft', ?, ?)
                """,
                (
                    draft_id,
                    lead_id,
                    clean_recipient,
                    clean_message,
                    timestamp,
                    timestamp,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE agent_inbox_events
                SET status = 'drafted', draft_id = ?, decision_json = ?,
                    agent_error = NULL, agent_lock_until = NULL, updated_at = ?
                WHERE id = ? AND workspace_id = ?
                  AND status = 'processing' AND draft_id IS NULL
                """,
                (draft_id, _json(decision), timestamp, event_id, workspace_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "agent inbox event changed during draft finalization"
                )
            connection.execute(
                """
                UPDATE leads
                SET status = CASE
                    WHEN status IN ('new','researching','analyzed','qualified')
                    THEN 'drafted' ELSE status END,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, lead_id),
            )
            saved_event = connection.execute(
                "SELECT * FROM agent_inbox_events WHERE id = ?", (event_id,)
            ).fetchone()
            saved_draft = connection.execute(
                "SELECT * FROM outreach_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        assert saved_event is not None and saved_draft is not None
        return {
            "event": self._agent_inbox_from_row(saved_event),
            "draft": self._draft_from_row(saved_draft),
            "idempotent": False,
        }

    def save_outreach_draft(
        self,
        lead_id: str,
        *,
        channel: str,
        message: str,
        recipient: str | None = None,
    ) -> dict[str, Any]:
        self.get_lead(lead_id)
        if not message.strip():
            raise ValueError("message must not be empty")
        if len(message) > 4000:
            raise ValueError("message must be at most 4000 characters")
        channel = channel.strip().lower()
        if channel not in {"whatsapp", "email", "other"}:
            raise ValueError("channel must be whatsapp, email, or other")
        draft_id = str(uuid.uuid4())
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO outreach_drafts (
                    id, lead_id, channel, recipient, message, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    draft_id,
                    lead_id,
                    channel,
                    recipient.strip() if recipient else None,
                    message.strip(),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE leads SET status = CASE WHEN status IN ('new','researching','analyzed','qualified') THEN 'drafted' ELSE status END, updated_at = ? WHERE id = ?",
                (timestamp, lead_id),
            )
        return self.get_outreach_draft(draft_id)

    def get_outreach_draft(self, draft_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM outreach_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        if row is None:
            raise ValueError("outreach draft not found")
        return self._draft_from_row(row)

    def list_outreach_drafts(
        self,
        *,
        lead_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        values: list[Any] = []
        if lead_id:
            conditions.append("lead_id = ?")
            values.append(lead_id)
        if status:
            conditions.append("status = ?")
            values.append(self._validate_status(status, DRAFT_STATUSES, "draft status"))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(max(1, min(int(limit), 200)))
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM outreach_drafts {where} ORDER BY created_at DESC LIMIT ?",
                values,
            ).fetchall()
        return [self._draft_from_row(row) for row in rows]

    def approve_outreach_draft(self, draft_id: str) -> dict[str, Any]:
        timestamp = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT lead_id, status FROM outreach_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise ValueError("outreach draft not found")
            if row["status"] != "draft":
                raise ValueError("only a draft can be approved")
            connection.execute(
                "UPDATE outreach_drafts SET status = 'approved', approved_at = ?, updated_at = ? WHERE id = ?",
                (timestamp, timestamp, draft_id),
            )
            connection.execute(
                "UPDATE leads SET status = CASE WHEN status = 'drafted' THEN 'approved' ELSE status END, updated_at = ? WHERE id = ?",
                (timestamp, row["lead_id"]),
            )
        return self.get_outreach_draft(draft_id)

    def claim_outreach_draft_for_send(self, draft_id: str) -> dict[str, Any] | None:
        """Atomically reserve one approved draft so duplicate calls cannot send it twice."""
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE outreach_drafts SET status = 'sending', updated_at = ?
                WHERE id = ? AND status = 'approved'
                """,
                (timestamp, draft_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT 1 FROM outreach_drafts WHERE id = ?", (draft_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("outreach draft not found")
                return None
        return self.get_outreach_draft(draft_id)

    def release_outreach_send_claim(self, draft_id: str) -> dict[str, Any]:
        """Return a blocked, unsent draft to its approved state."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE outreach_drafts SET status = 'approved', updated_at = ?
                WHERE id = ? AND status = 'sending'
                """,
                (utc_now(), draft_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("outreach draft does not have an active send claim")
        return self.get_outreach_draft(draft_id)

    def mark_outreach_sent(self, draft_id: str, *, success: bool) -> dict[str, Any]:
        timestamp = utc_now()
        status = "sent" if success else "failed"
        with self.connect() as connection:
            row = connection.execute(
                "SELECT lead_id, status FROM outreach_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise ValueError("outreach draft not found")
            if row["status"] != "sending":
                raise ValueError("outreach draft does not have an active send claim")
            connection.execute(
                "UPDATE outreach_drafts SET status = ?, sent_at = ?, updated_at = ? WHERE id = ?",
                (status, timestamp if success else None, timestamp, draft_id),
            )
            if success:
                connection.execute(
                    "UPDATE leads SET status = 'contacted', updated_at = ? WHERE id = ?",
                    (timestamp, row["lead_id"]),
                )
        return self.get_outreach_draft(draft_id)

    def record_interaction(
        self,
        lead_id: str,
        *,
        channel: str,
        direction: str,
        content: str,
        status: str = "recorded",
        external_id: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        self.get_lead(lead_id)
        direction = direction.strip().lower()
        if direction not in {"inbound", "outbound", "internal"}:
            raise ValueError("direction must be inbound, outbound, or internal")
        if not content.strip():
            raise ValueError("interaction content must not be empty")
        interaction_id = str(uuid.uuid4())
        timestamp = utc_now()
        occurred = normalize_datetime(occurred_at)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO interactions (
                    id, lead_id, channel, direction, content, status, external_id, occurred_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction_id,
                    lead_id,
                    channel.strip().lower(),
                    direction,
                    content.strip(),
                    status.strip().lower() or "recorded",
                    external_id.strip() if external_id else None,
                    occurred,
                    timestamp,
                ),
            )
            if direction == "inbound":
                connection.execute(
                    "UPDATE leads SET status = CASE WHEN status NOT IN ('won','lost','archived') THEN 'replied' ELSE status END, updated_at = ? WHERE id = ?",
                    (timestamp, lead_id),
                )
        return {
            "id": interaction_id,
            "lead_id": lead_id,
            "channel": channel.strip().lower(),
            "direction": direction,
            "content": content.strip(),
            "status": status.strip().lower() or "recorded",
            "external_id": external_id.strip() if external_id else None,
            "occurred_at": occurred,
            "created_at": timestamp,
        }

    def list_interactions(
        self, lead_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.get_lead(lead_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM interactions WHERE lead_id = ? ORDER BY occurred_at DESC LIMIT ?",
                (lead_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def schedule_followup(
        self,
        lead_id: str,
        *,
        due_at: str,
        action: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        self.get_lead(lead_id)
        if not action.strip():
            raise ValueError("follow-up action must not be empty")
        followup_id = str(uuid.uuid4())
        due = normalize_datetime(due_at)
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO followups (id, lead_id, due_at, action, notes, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    followup_id,
                    lead_id,
                    due,
                    action.strip(),
                    notes.strip() if notes else None,
                    timestamp,
                ),
            )
        return {
            "id": followup_id,
            "lead_id": lead_id,
            "due_at": due,
            "action": action.strip(),
            "notes": notes.strip() if notes else None,
            "status": "pending",
            "created_at": timestamp,
            "completed_at": None,
        }

    def list_due_followups(
        self, *, before: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        cutoff = normalize_datetime(before)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.*, l.company_name, l.website_url, l.score
                FROM followups f
                JOIN leads l ON l.id = f.lead_id
                WHERE f.status = 'pending' AND f.due_at <= ?
                ORDER BY f.due_at ASC
                LIMIT ?
                """,
                (cutoff, max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_followup(
        self, followup_id: str, *, status: str = "completed"
    ) -> dict[str, Any]:
        status = self._validate_status(status, FOLLOWUP_STATUSES, "follow-up status")
        if status == "pending":
            raise ValueError("completion status must be completed or cancelled")
        completed_at = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE followups SET status = ?, completed_at = ? WHERE id = ? AND status = 'pending'",
                (status, completed_at, followup_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("pending follow-up not found")
            row = connection.execute(
                "SELECT * FROM followups WHERE id = ?", (followup_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def overview(self, campaign_id: str | None = None) -> dict[str, Any]:
        conditions = ""
        values: list[Any] = []
        join = ""
        if campaign_id:
            join = "JOIN campaign_leads cl ON cl.lead_id = l.id"
            conditions = "WHERE cl.campaign_id = ?"
            values.append(campaign_id)
        with self.connect() as connection:
            status_rows = connection.execute(
                f"SELECT l.status, COUNT(DISTINCT l.id) AS count FROM leads l {join} {conditions} GROUP BY l.status",
                values,
            ).fetchall()
            score_row = connection.execute(
                f"SELECT COUNT(DISTINCT l.id), ROUND(AVG(l.score), 1), MAX(l.score) FROM leads l {join} {conditions}",
                values,
            ).fetchone()
            pending_followups = connection.execute(
                "SELECT COUNT(*) FROM followups WHERE status = 'pending'"
            ).fetchone()[0]
        return {
            "campaign_id": campaign_id,
            "lead_count": score_row[0] if score_row else 0,
            "average_score": score_row[1] if score_row else None,
            "top_score": score_row[2] if score_row else None,
            "by_status": {row["status"]: row["count"] for row in status_rows},
            "pending_followups": pending_followups,
        }

    @staticmethod
    def _normalize_days(days: list[str] | None) -> list[str]:
        if not days:
            return []
        normalized = list(dict.fromkeys(str(day).strip().lower() for day in days))
        invalid = [day for day in normalized if day not in WEEKDAYS]
        if invalid:
            raise ValueError(
                f"days must use English weekday names; invalid: {', '.join(invalid)}"
            )
        return normalized

    def create_vertical(
        self,
        name: str,
        *,
        region: str,
        search_query: str | None = None,
        days: list[str] | None = None,
        daily_target: int = 10,
        min_score: int = 65,
        weight: float = 1.0,
        enabled: bool = True,
    ) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("vertical name must not be empty")
        if not region.strip():
            raise ValueError("vertical region must not be empty")
        vertical_id = str(uuid.uuid4())
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO verticals (
                    id, name, region, search_query, days_json, daily_target,
                    min_score, weight, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vertical_id,
                    name.strip(),
                    region.strip(),
                    search_query.strip() if search_query else None,
                    _json(self._normalize_days(days)),
                    max(1, min(int(daily_target), 50)),
                    max(0, min(int(min_score), 100)),
                    max(0.1, min(float(weight), 100.0)),
                    1 if enabled else 0,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_vertical(vertical_id)

    def get_vertical(self, vertical_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM verticals WHERE id = ?", (vertical_id,)
            ).fetchone()
        if row is None:
            raise ValueError("vertical not found")
        return self._vertical_from_row(row)

    def list_verticals(
        self, *, enabled: bool | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        where = "WHERE enabled = ?" if enabled is not None else ""
        values: list[Any] = [1 if enabled else 0] if enabled is not None else []
        values.append(max(1, min(int(limit), 500)))
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM verticals {where} ORDER BY weight DESC, name ASC LIMIT ?",
                values,
            ).fetchall()
        return [self._vertical_from_row(row) for row in rows]

    def update_vertical(
        self,
        vertical_id: str,
        *,
        name: str | None = None,
        region: str | None = None,
        search_query: str | None = None,
        days: list[str] | None = None,
        daily_target: int | None = None,
        min_score: int | None = None,
        weight: float | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        self.get_vertical(vertical_id)
        updates: list[str] = []
        values: list[Any] = []
        if name is not None:
            if not name.strip():
                raise ValueError("vertical name must not be empty")
            updates.append("name = ?")
            values.append(name.strip())
        if region is not None:
            if not region.strip():
                raise ValueError("vertical region must not be empty")
            updates.append("region = ?")
            values.append(region.strip())
        if search_query is not None:
            updates.append("search_query = ?")
            values.append(search_query.strip() or None)
        if days is not None:
            updates.append("days_json = ?")
            values.append(_json(self._normalize_days(days)))
        if daily_target is not None:
            updates.append("daily_target = ?")
            values.append(max(1, min(int(daily_target), 50)))
        if min_score is not None:
            updates.append("min_score = ?")
            values.append(max(0, min(int(min_score), 100)))
        if weight is not None:
            updates.append("weight = ?")
            values.append(max(0.1, min(float(weight), 100.0)))
        if enabled is not None:
            updates.append("enabled = ?")
            values.append(1 if enabled else 0)
        if not updates:
            return self.get_vertical(vertical_id)
        updates.append("updated_at = ?")
        values.extend([utc_now(), vertical_id])
        with self.connect() as connection:
            connection.execute(
                f"UPDATE verticals SET {', '.join(updates)} WHERE id = ?", values
            )
        return self.get_vertical(vertical_id)

    def get_autopilot_state(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM autopilot_state WHERE id = 1"
            ).fetchone()
        assert row is not None
        return self._autopilot_state_from_row(row)

    def start_autopilot(
        self,
        *,
        mode: str = "safe",
        interval_minutes: int = 60,
        max_verticals_per_cycle: int = 2,
        leads_per_vertical: int = 10,
        score_threshold: int = 65,
    ) -> dict[str, Any]:
        mode = self._validate_status(mode, AUTOPILOT_MODES, "autopilot mode")
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE autopilot_state SET
                    running = 1, mode = ?, interval_minutes = ?,
                    max_verticals_per_cycle = ?, leads_per_vertical = ?,
                    score_threshold = ?, next_cycle_at = ?, last_error = NULL,
                    updated_at = ?
                WHERE id = 1
                """,
                (
                    mode,
                    max(5, min(int(interval_minutes), 24 * 60)),
                    max(1, min(int(max_verticals_per_cycle), 10)),
                    max(1, min(int(leads_per_vertical), 50)),
                    max(0, min(int(score_threshold), 100)),
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_autopilot_state()

    def stop_autopilot(self) -> dict[str, Any]:
        with self.connect() as connection:
            state = connection.execute(
                "SELECT current_cycle_id FROM autopilot_state WHERE id = 1"
            ).fetchone()
            if state is not None and state["current_cycle_id"]:
                connection.execute(
                    """
                    UPDATE autopilot_cycles SET status = 'failed',
                        error = 'Autopilot was stopped by the operator.', completed_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (utc_now(), state["current_cycle_id"]),
                )
            connection.execute(
                """
                UPDATE autopilot_state SET running = 0, next_cycle_at = NULL,
                    lock_until = NULL, current_cycle_id = NULL, updated_at = ?
                WHERE id = 1
                """,
                (utc_now(),),
            )
        return self.get_autopilot_state()

    def begin_autopilot_cycle(self, *, force: bool = False) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        timestamp = now.isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT * FROM autopilot_state WHERE id = 1"
            ).fetchone()
            assert state is not None
            if not force and not bool(state["running"]):
                return None
            if state["lock_until"]:
                lock_until = datetime.fromisoformat(state["lock_until"])
                if lock_until > now:
                    return None
            if state["current_cycle_id"]:
                stale_error = "Recovered stale Autopilot cycle after its lock expired."
                connection.execute(
                    """
                    UPDATE autopilot_cycles SET status = 'failed', error = ?,
                        completed_at = ? WHERE id = ? AND status = 'running'
                    """,
                    (stale_error, timestamp, state["current_cycle_id"]),
                )
                connection.execute(
                    """
                    UPDATE autopilot_state SET current_cycle_id = NULL,
                        lock_until = NULL, last_error = ?, updated_at = ? WHERE id = 1
                    """,
                    (stale_error, timestamp),
                )
            if not force and state["next_cycle_at"]:
                next_cycle = datetime.fromisoformat(state["next_cycle_at"])
                if next_cycle > now:
                    return None
            cycle_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO autopilot_cycles (
                    id, mode, status, selected_verticals_json, metrics_json, started_at
                ) VALUES (?, ?, 'running', '[]', '{}', ?)
                """,
                (cycle_id, state["mode"], timestamp),
            )
            connection.execute(
                """
                UPDATE autopilot_state SET current_cycle_id = ?, lock_until = ?,
                    updated_at = ? WHERE id = 1
                """,
                (
                    cycle_id,
                    (now + timedelta(minutes=120)).isoformat(timespec="seconds"),
                    timestamp,
                ),
            )
        return {
            "id": cycle_id,
            "mode": state["mode"],
            "status": "running",
            "started_at": timestamp,
        }

    def set_cycle_verticals(
        self, cycle_id: str, vertical_ids: list[str]
    ) -> dict[str, Any]:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE autopilot_cycles SET selected_verticals_json = ? WHERE id = ? AND status = 'running'",
                (_json(vertical_ids), cycle_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("running autopilot cycle not found")
        return self.get_autopilot_cycle(cycle_id)

    def get_autopilot_cycle(self, cycle_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM autopilot_cycles WHERE id = ?", (cycle_id,)
            ).fetchone()
        if row is None:
            raise ValueError("autopilot cycle not found")
        result = dict(row)
        result["selected_verticals"] = _load_json(
            result.pop("selected_verticals_json", None), []
        )
        result["metrics"] = _load_json(result.pop("metrics_json", None), {})
        return result

    def list_autopilot_cycles(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM autopilot_cycles ORDER BY started_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["selected_verticals"] = _load_json(
                item.pop("selected_verticals_json", None), []
            )
            item["metrics"] = _load_json(item.pop("metrics_json", None), {})
            result.append(item)
        return result

    def record_admin_audit(
        self,
        *,
        actor: str,
        action: str,
        outcome: str,
        target_type: str | None = None,
        target_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "id": str(uuid.uuid4()),
            "actor": actor.strip()[:320],
            "action": action.strip()[:120],
            "target_type": target_type.strip()[:80] if target_type else None,
            "target_id": target_id.strip()[:160] if target_id else None,
            "outcome": outcome.strip()[:40],
            "details": details or {},
            "created_at": utc_now(),
        }
        if not event["actor"] or not event["action"] or not event["outcome"]:
            raise ValueError("audit actor, action and outcome are required")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_audit_log (
                    id, actor, action, target_type, target_id,
                    outcome, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["id"],
                    event["actor"],
                    event["action"],
                    event["target_type"],
                    event["target_id"],
                    event["outcome"],
                    _json(event["details"]),
                    event["created_at"],
                ),
            )
        return event

    def list_admin_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM admin_audit_log ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["details"] = _load_json(item.pop("details_json", None), {})
            result.append(item)
        return result

    def complete_autopilot_cycle(
        self,
        cycle_id: str,
        *,
        metrics: dict[str, Any],
        error: str | None = None,
    ) -> dict[str, Any]:
        state = self.get_autopilot_state()
        now = datetime.now(UTC)
        timestamp = now.isoformat(timespec="seconds")
        next_cycle = (
            now + timedelta(minutes=int(state["interval_minutes"]))
        ).isoformat(timespec="seconds")
        status = "failed" if error else "completed"
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE autopilot_cycles SET status = ?, metrics_json = ?, error = ?,
                    completed_at = ? WHERE id = ? AND status = 'running'
                """,
                (status, _json(metrics), error, timestamp, cycle_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("running autopilot cycle not found")
            connection.execute(
                """
                UPDATE autopilot_state SET current_cycle_id = NULL, lock_until = NULL,
                    last_cycle_at = ?, next_cycle_at = CASE WHEN running = 1 THEN ? ELSE NULL END,
                    last_error = ?, updated_at = ? WHERE id = 1
                """,
                (timestamp, next_cycle, error, timestamp),
            )
        return self.get_autopilot_cycle(cycle_id)

    def register_autopilot_campaign(
        self, *, cycle_id: str, campaign_id: str, vertical_id: str
    ) -> None:
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO autopilot_campaigns (
                    cycle_id, campaign_id, vertical_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (cycle_id, campaign_id, vertical_id, timestamp),
            )
            connection.execute(
                "UPDATE verticals SET last_selected_at = ?, updated_at = ? WHERE id = ?",
                (timestamp, timestamp, vertical_id),
            )

    def record_google_sheets_sync(
        self,
        *,
        status: str,
        details: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE google_sheets_sync_state SET last_sync_at = ?, status = ?,
                    error = ?, details_json = ?, updated_at = ? WHERE id = 1
                """,
                (timestamp, status, error, _json(details or {}), timestamp),
            )
        return self.get_google_sheets_sync_state()

    def get_google_sheets_sync_state(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM google_sheets_sync_state WHERE id = 1"
            ).fetchone()
        assert row is not None
        result = dict(row)
        result["details"] = _load_json(result.pop("details_json", None), {})
        return result

    def queue_outreach_send_request(self, draft_id: str) -> dict[str, Any]:
        draft = self.get_outreach_draft(draft_id)
        if draft["status"] != "approved":
            raise ValueError("only an approved draft can be queued for sending")
        request_id = str(uuid.uuid4())
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO outreach_send_requests (
                    id, draft_id, status, requested_at
                ) VALUES (?, ?, 'pending', ?)
                """,
                (request_id, draft_id, timestamp),
            )
            row = connection.execute(
                """
                SELECT * FROM outreach_send_requests
                WHERE draft_id = ? AND status = 'pending'
                """,
                (draft_id,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_pending_send_requests(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.*, d.lead_id, d.channel, d.recipient, d.message,
                    d.status AS draft_status
                FROM outreach_send_requests r
                JOIN outreach_drafts d ON d.id = r.draft_id
                WHERE r.status = 'pending'
                ORDER BY r.requested_at ASC
                LIMIT ?
                """,
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_send_request(
        self, request_id: str, *, success: bool, error: str | None = None
    ) -> dict[str, Any]:
        status = "completed" if success else "failed"
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE outreach_send_requests SET status = ?, processed_at = ?, error = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status, utc_now(), error, request_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("pending send request not found")
            row = connection.execute(
                "SELECT * FROM outreach_send_requests WHERE id = ?", (request_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def daily_report(self, day: str | None = None) -> dict[str, Any]:
        report_day = day or datetime.now(UTC).date().isoformat()
        try:
            datetime.fromisoformat(report_day)
        except ValueError as exc:
            raise ValueError("day must be an ISO date") from exc
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM leads WHERE substr(created_at, 1, 10) = ?),
                    (SELECT COUNT(*) FROM leads WHERE substr(last_analyzed_at, 1, 10) = ?),
                    (SELECT COUNT(*) FROM leads WHERE score IS NOT NULL AND score >= 65
                        AND substr(updated_at, 1, 10) = ?),
                    (SELECT COUNT(*) FROM outreach_drafts WHERE substr(created_at, 1, 10) = ?),
                    (SELECT COUNT(*) FROM interactions WHERE direction = 'outbound'
                        AND substr(occurred_at, 1, 10) = ?),
                    (SELECT COUNT(*) FROM interactions WHERE direction = 'inbound'
                        AND substr(occurred_at, 1, 10) = ?),
                    (SELECT COUNT(*) FROM leads WHERE status = 'meeting'
                        AND substr(updated_at, 1, 10) = ?),
                    (SELECT COUNT(*) FROM leads WHERE status = 'won'
                        AND substr(updated_at, 1, 10) = ?)
                """,
                (report_day,) * 8,
            ).fetchone()
            due_followups = connection.execute(
                "SELECT COUNT(*) FROM followups WHERE status = 'pending' AND substr(due_at, 1, 10) <= ?",
                (report_day,),
            ).fetchone()[0]
        assert row is not None
        return {
            "day": report_day,
            "leads_found": row[0],
            "analyzed": row[1],
            "qualified": row[2],
            "drafts_created": row[3],
            "messages_sent": row[4],
            "replies": row[5],
            "meetings": row[6],
            "deals": row[7],
            "due_followups": due_followups,
        }

    def vertical_performance(self, *, since: str | None = None) -> list[dict[str, Any]]:
        since_value = (
            normalize_datetime(since) if since else "0001-01-01T00:00:00+00:00"
        )
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT v.id, v.name, v.region, v.weight, v.min_score,
                    COUNT(DISTINCT cl.lead_id) AS leads,
                    COUNT(DISTINCT CASE WHEN l.score >= v.min_score THEN l.id END) AS qualified,
                    COUNT(DISTINCT d.id) AS drafts,
                    COUNT(DISTINCT CASE WHEN i.direction = 'outbound' THEN i.id END) AS messages,
                    COUNT(DISTINCT CASE WHEN i.direction = 'inbound' THEN i.id END) AS replies,
                    COUNT(DISTINCT CASE WHEN l.status = 'meeting' THEN l.id END) AS meetings,
                    COUNT(DISTINCT CASE WHEN l.status = 'won' THEN l.id END) AS deals,
                    ROUND(AVG(l.score), 1) AS average_score
                FROM verticals v
                LEFT JOIN autopilot_campaigns ac ON ac.vertical_id = v.id
                    AND ac.created_at >= ?
                LEFT JOIN campaign_leads cl ON cl.campaign_id = ac.campaign_id
                LEFT JOIN leads l ON l.id = cl.lead_id
                LEFT JOIN outreach_drafts d ON d.lead_id = l.id
                LEFT JOIN interactions i ON i.lead_id = l.id
                GROUP BY v.id
                ORDER BY replies DESC, meetings DESC, qualified DESC, v.weight DESC
                """,
                (since_value,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            messages = int(item["messages"] or 0)
            leads = int(item["leads"] or 0)
            item["reply_rate"] = round(
                (int(item["replies"] or 0) / messages * 100) if messages else 0.0,
                1,
            )
            item["qualified_rate"] = round(
                (int(item["qualified"] or 0) / leads * 100) if leads else 0.0,
                1,
            )
            result.append(item)
        return result

    def conversion_report(
        self, *, since: str | None = None, until: str | None = None
    ) -> dict[str, Any]:
        start = normalize_datetime(since) if since else "0001-01-01T00:00:00+00:00"
        end = normalize_datetime(until) if until else "9999-12-31T23:59:59+00:00"
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(DISTINCT l.id) AS leads,
                    COUNT(DISTINCT CASE WHEN l.score >= 65 THEN l.id END) AS qualified,
                    COUNT(DISTINCT d.lead_id) AS drafted,
                    COUNT(DISTINCT CASE WHEN i.direction = 'outbound' THEN i.lead_id END) AS contacted,
                    COUNT(DISTINCT CASE WHEN i.direction = 'inbound' THEN i.lead_id END) AS replied,
                    COUNT(DISTINCT CASE WHEN l.status = 'interested' THEN l.id END) AS interested,
                    COUNT(DISTINCT CASE WHEN l.status = 'meeting' THEN l.id END) AS meetings,
                    COUNT(DISTINCT CASE WHEN l.status = 'proposal' THEN l.id END) AS proposals,
                    COUNT(DISTINCT CASE WHEN l.status = 'won' THEN l.id END) AS won
                FROM leads l
                LEFT JOIN outreach_drafts d ON d.lead_id = l.id
                    AND d.created_at BETWEEN ? AND ?
                LEFT JOIN interactions i ON i.lead_id = l.id
                    AND i.occurred_at BETWEEN ? AND ?
                WHERE l.created_at BETWEEN ? AND ?
                """,
                (start, end, start, end, start, end),
            ).fetchone()
        assert row is not None
        counts = dict(row)
        leads = int(counts["leads"] or 0)
        contacted = int(counts["contacted"] or 0)
        replied = int(counts["replied"] or 0)
        return {
            "since": None if since is None else start,
            "until": None if until is None else end,
            "stages": counts,
            "rates": {
                "qualified_per_lead": round(
                    (int(counts["qualified"] or 0) / leads * 100) if leads else 0.0,
                    1,
                ),
                "reply_per_contacted": round(
                    (replied / contacted * 100) if contacted else 0.0, 1
                ),
                "meeting_per_reply": round(
                    (int(counts["meetings"] or 0) / replied * 100) if replied else 0.0,
                    1,
                ),
                "win_per_lead": round(
                    (int(counts["won"] or 0) / leads * 100) if leads else 0.0,
                    1,
                ),
            },
        }
