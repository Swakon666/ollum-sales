from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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
    "won",
    "lost",
    "archived",
}
DRAFT_STATUSES = {"draft", "approved", "sending", "sent", "cancelled", "failed"}
FOLLOWUP_STATUSES = {"pending", "completed", "cancelled"}


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
                    last_analyzed_at TEXT
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

                CREATE INDEX IF NOT EXISTS idx_campaign_leads_campaign ON campaign_leads(campaign_id);
                CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score DESC);
                CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
                CREATE INDEX IF NOT EXISTS idx_drafts_lead ON outreach_drafts(lead_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_interactions_lead ON interactions(lead_id, occurred_at DESC);
                CREATE INDEX IF NOT EXISTS idx_followups_due ON followups(status, due_at);
                PRAGMA user_version = 1;
                """
            )

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
        return result

    @staticmethod
    def _draft_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

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
    ) -> dict[str, Any]:
        website_url = canonical_company_url(website_url)
        name = company_name.strip() or urlsplit(website_url).hostname or website_url
        timestamp = utc_now()
        lead_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO leads (
                    id, company_name, website_url, industry, location, source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(website_url) DO UPDATE SET
                    company_name = CASE WHEN excluded.company_name <> '' THEN excluded.company_name ELSE leads.company_name END,
                    industry = COALESCE(excluded.industry, leads.industry),
                    location = COALESCE(excluded.location, leads.location),
                    updated_at = excluded.updated_at
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
            row = connection.execute(
                "SELECT id FROM leads WHERE website_url = ?", (website_url,)
            ).fetchone()
            assert row is not None
            lead_id = row["id"]
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
        contacts = (
            analysis.get("contacts")
            if isinstance(analysis.get("contacts"), dict)
            else {}
        )
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
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE leads SET score = ?, score_reason = ?, score_details_json = ?,
                    status = CASE WHEN status IN ('new','researching','analyzed') THEN 'qualified' ELSE status END,
                    updated_at = ?
                WHERE id = ?
                """,
                (score, reason, _json(details), utc_now(), lead_id),
            )
        return self.get_lead(lead_id)

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
