from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from .crm import SalesCRM


def _integer(value: Any) -> int:
    return int(value or 0)


def _issue(
    issues: list[dict[str, Any]],
    *,
    code: str,
    count: int,
    severity: str,
    recommendation: str,
) -> None:
    if count <= 0:
        return
    issues.append(
        {
            "code": code,
            "count": count,
            "severity": severity,
            "recommendation": recommendation,
        }
    )


def build_safe_quality_audit(
    crm: SalesCRM,
    workspace_id: str,
    *,
    whatsapp_send_enabled: bool,
    autopilot_send_enabled: bool,
) -> dict[str, Any]:
    """Return aggregate data-quality diagnostics without private message text."""

    workspace_id = str(workspace_id).strip()
    now = datetime.now(UTC)
    now_text = now.isoformat(timespec="seconds")
    processing_cutoff = now - timedelta(minutes=30)
    processing_cutoff_text = processing_cutoff.isoformat(timespec="seconds")
    with crm.connect() as connection:
        lead_row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status NOT IN ('won', 'lost', 'archived') THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN analysis_json IS NULL OR analysis_json = '{}' THEN 1 ELSE 0 END) AS missing_analysis,
                SUM(CASE WHEN inspection_json IS NULL OR inspection_json = '{}' THEN 1 ELSE 0 END) AS missing_inspection,
                SUM(CASE WHEN evidence_expires_at IS NULL OR evidence_expires_at <= ? THEN 1 ELSE 0 END) AS stale_evidence,
                SUM(CASE WHEN status IN ('analyzed', 'qualified', 'drafted', 'approved')
                    AND (evidence_expires_at IS NULL OR evidence_expires_at <= ?)
                    THEN 1 ELSE 0 END) AS progressed_without_fresh_evidence,
                SUM(CASE WHEN domain_key IS NULL OR domain_key = '' THEN 1 ELSE 0 END) AS missing_domain_key
            FROM leads
            """,
            (now_text, now_text),
        ).fetchone()
        duplicate_domain_groups = _integer(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT domain_key FROM leads
                    WHERE domain_key IS NOT NULL AND domain_key != ''
                    GROUP BY domain_key HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )
        duplicate_identity_groups = _integer(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT name_key, COALESCE(location_key, '') FROM leads
                    WHERE name_key IS NOT NULL AND name_key != ''
                    GROUP BY name_key, COALESCE(location_key, '') HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )
        inbox_status_rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM agent_inbox_events WHERE workspace_id = ? GROUP BY status
            """,
            (workspace_id,),
        ).fetchall()
        inbox_metric_row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN lead_id IS NULL THEN 1 ELSE 0 END) AS unlinked,
                SUM(CASE WHEN status = 'processing' AND updated_at <= ? THEN 1 ELSE 0 END) AS stale_processing,
                SUM(CASE WHEN status IN ('new', 'processing', 'needs_review')
                    AND agent_attempts >= 3 THEN 1 ELSE 0 END) AS retry_exhausted
            FROM agent_inbox_events WHERE workspace_id = ?
            """,
            (processing_cutoff_text, workspace_id),
        ).fetchone()
        decision_rows = connection.execute(
            """
            SELECT decision_json FROM agent_inbox_events
            WHERE workspace_id = ? AND decision_json IS NOT NULL AND decision_json != ''
            """,
            (workspace_id,),
        ).fetchall()
        repeated_message_patterns = _integer(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT lower(trim(message_text)) AS message_key
                    FROM agent_inbox_events
                    WHERE workspace_id = ? AND length(trim(message_text)) >= 3
                    GROUP BY message_key HAVING COUNT(*) > 1
                )
                """,
                (workspace_id,),
            ).fetchone()[0]
        )
        pending_send_requests = _integer(
            connection.execute(
                "SELECT COUNT(*) FROM outreach_send_requests WHERE status = 'pending'"
            ).fetchone()[0]
        )
        pending_followups = _integer(
            connection.execute(
                "SELECT COUNT(*) FROM followups WHERE status = 'pending'"
            ).fetchone()[0]
        )
        duplicate_current_drafts = _integer(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT lead_id, channel FROM outreach_drafts
                    WHERE status = 'draft'
                    GROUP BY lead_id, channel HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )
        autopilot_row = connection.execute(
            "SELECT mode, running, last_error FROM autopilot_state WHERE id = 1"
        ).fetchone()

    inbox_statuses = {
        str(row["status"] or "unknown"): _integer(row["count"])
        for row in inbox_status_rows
    }
    quality_counts: Counter[str] = Counter()
    malformed_decisions = 0
    unclassified_decisions = 0
    for row in decision_rows:
        raw_decision = row["decision_json"]
        try:
            decision = json.loads(raw_decision)
        except (TypeError, ValueError):
            malformed_decisions += 1
            continue
        if not isinstance(decision, dict):
            malformed_decisions += 1
            continue
        quality = decision.get("message_quality")
        if isinstance(quality, str) and quality:
            quality_counts[quality] += 1
        else:
            unclassified_decisions += 1

    leads = {
        "total": _integer(lead_row["total"]),
        "active": _integer(lead_row["active"]),
        "missing_analysis": _integer(lead_row["missing_analysis"]),
        "missing_inspection": _integer(lead_row["missing_inspection"]),
        "stale_evidence": _integer(lead_row["stale_evidence"]),
        "progressed_without_fresh_evidence": _integer(
            lead_row["progressed_without_fresh_evidence"]
        ),
        "missing_domain_key": _integer(lead_row["missing_domain_key"]),
        "duplicate_domain_groups": duplicate_domain_groups,
        "duplicate_identity_groups": duplicate_identity_groups,
    }
    inbox = {
        "total": _integer(inbox_metric_row["total"]),
        "statuses": dict(sorted(inbox_statuses.items())),
        "message_quality": dict(sorted(quality_counts.items())),
        "stale_processing": _integer(inbox_metric_row["stale_processing"]),
        "retry_exhausted": _integer(inbox_metric_row["retry_exhausted"]),
        "unlinked": _integer(inbox_metric_row["unlinked"]),
        "malformed_decisions": malformed_decisions,
        "unclassified_decisions": unclassified_decisions,
        "repeated_message_patterns": repeated_message_patterns,
    }
    autopilot_mode = str(autopilot_row["mode"] if autopilot_row else "unknown")
    safety_ok = (
        not whatsapp_send_enabled
        and not autopilot_send_enabled
        and autopilot_mode == "safe"
    )
    issues: list[dict[str, Any]] = []
    _issue(
        issues,
        code="unsafe_send_flags",
        count=int(not safety_ok),
        severity="critical",
        recommendation="Stop the cycle until SAFE mode and both send flags are false.",
    )
    _issue(
        issues,
        code="progressed_without_fresh_evidence",
        count=leads["progressed_without_fresh_evidence"],
        severity="warning",
        recommendation="Reinspect official company pages before scoring or outreach drafting.",
    )
    _issue(
        issues,
        code="duplicate_company_groups",
        count=duplicate_domain_groups + duplicate_identity_groups,
        severity="warning",
        recommendation="Review and merge duplicate company identities before the next import.",
    )
    _issue(
        issues,
        code="stale_processing_events",
        count=inbox["stale_processing"],
        severity="warning",
        recommendation="Requeue safely or escalate events whose ChatGPT lease expired.",
    )
    _issue(
        issues,
        code="retry_exhausted_events",
        count=inbox["retry_exhausted"],
        severity="warning",
        recommendation="Inspect the bounded event context and escalate persistent uncertainty.",
    )
    _issue(
        issues,
        code="unlinked_inbound_events",
        count=inbox["unlinked"],
        severity="warning",
        recommendation="Match the contact to one CRM lead before preparing a reply.",
    )
    _issue(
        issues,
        code="unclassified_decisions",
        count=unclassified_decisions + malformed_decisions,
        severity="info",
        recommendation="Re-evaluate legacy decisions with the current message-quality schema.",
    )
    _issue(
        issues,
        code="repeated_message_patterns",
        count=repeated_message_patterns,
        severity="info",
        recommendation="Review aggregates for duplicate imports or recurring system noise.",
    )
    _issue(
        issues,
        code="pending_send_requests",
        count=pending_send_requests,
        severity="warning",
        recommendation="Keep pending requests blocked during SAFE testing and review manually.",
    )
    _issue(
        issues,
        code="duplicate_current_drafts",
        count=duplicate_current_drafts,
        severity="info",
        recommendation="Keep only the newest grounded draft per lead and channel.",
    )

    has_warning = any(item["severity"] == "warning" for item in issues)
    return {
        "generated_at": now_text,
        "overall_status": (
            "blocked" if not safety_ok else "attention" if has_warning else "healthy"
        ),
        "safety": {
            "safe": safety_ok,
            "autopilot_mode": autopilot_mode,
            "autopilot_running": bool(autopilot_row["running"] if autopilot_row else 0),
            "whatsapp_send_enabled": bool(whatsapp_send_enabled),
            "autopilot_send_enabled": bool(autopilot_send_enabled),
            "pending_send_requests": pending_send_requests,
            "pending_followups": pending_followups,
        },
        "companies": leads,
        "inbox": inbox,
        "drafts": {"duplicate_current_groups": duplicate_current_drafts},
        "issues": issues,
        "privacy": {
            "private_message_text_included": False,
            "recipient_identifiers_included": False,
        },
        "approved": False,
        "sent": False,
        "followups_created": 0,
        "send_flags_changed": False,
    }
