from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .crm import SalesCRM

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

LEADS_HEADERS = [
    "LEAD_ID",
    "Компания",
    "Сфера",
    "Сайт",
    "Score",
    "Проблема",
    "Что предлагаем",
    "Контакт",
    "Статус",
    "Последнее касание",
    "Следующее действие",
    "DRAFT_ID",
    "DRAFT_RECIPIENT",
    "DRAFT_MESSAGE",
    "APPROVE",
    "SEND",
    "UPDATED_AT",
]
CAMPAIGNS_HEADERS = [
    "CAMPAIGN_ID",
    "Сфера",
    "Регион",
    "Найдено",
    "Прошло scoring",
    "Средний score",
    "Contacted",
    "Replies",
    "Meetings",
    "Deals",
    "Конверсия reply, %",
]
OUTREACH_HEADERS = [
    "DRAFT_ID",
    "LEAD_ID",
    "Компания",
    "Дата",
    "Канал",
    "Получатель",
    "Сообщение",
    "Статус",
    "APPROVE",
    "SEND",
    "SENT_AT",
]
FOLLOWUPS_HEADERS = [
    "FOLLOWUP_ID",
    "LEAD_ID",
    "Компания",
    "Когда написать",
    "Причина",
    "Статус",
    "Создан",
]


def _load_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _cell(value: Any) -> Any:
    return "" if value is None else value


def _yes(value: Any) -> bool:
    return str(value or "").strip().upper() in {"YES", "Y", "ДА", "TRUE", "1"}


class GoogleSheetsSync:
    """Safe Google Sheets panel with exact-draft approval and send requests."""

    tab_names = ("LEADS", "CAMPAIGNS", "OUTREACH", "FOLLOWUPS", "DASHBOARD")

    def __init__(
        self,
        crm: SalesCRM,
        *,
        enabled: bool,
        spreadsheet_id: str | None,
        service_account_file: str | None,
        service_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.crm = crm
        self.enabled = bool(enabled)
        self.spreadsheet_id = (spreadsheet_id or "").strip() or None
        self.service_account_file = (
            Path(service_account_file).expanduser() if service_account_file else None
        )
        self._service_factory = service_factory
        self._service: Any | None = None

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.spreadsheet_id
            and self.service_account_file
            and self.service_account_file.is_file()
        )

    def status(self) -> dict[str, Any]:
        sync_state = self.crm.get_google_sheets_sync_state()
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "spreadsheet_id_configured": bool(self.spreadsheet_id),
            "credentials_file_configured": bool(self.service_account_file),
            "credentials_file_exists": bool(
                self.service_account_file and self.service_account_file.is_file()
            ),
            "tabs": list(self.tab_names),
            "approval_guardrail": (
                "APPROVE confirms only the exact visible draft. SEND is a separate request "
                "and is never processed while Autopilot is in SAFE mode."
            ),
            "last_sync": sync_state,
        }

    def _build_service(self) -> Any:
        if self._service is not None:
            return self._service
        if self._service_factory is not None:
            self._service = self._service_factory()
            return self._service
        if not self.configured:
            raise RuntimeError("Google Sheets is not fully configured")
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_file(
            str(self.service_account_file), scopes=[SHEETS_SCOPE]
        )
        self._service = build(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )
        return self._service

    @staticmethod
    def parse_action_rows(
        values: list[list[Any]], headers: list[str]
    ) -> list[dict[str, Any]]:
        if not values:
            return []
        source_headers = [str(value).strip() for value in values[0]]
        indexes = {
            name: source_headers.index(name)
            for name in headers
            if name in source_headers
        }
        required = {"DRAFT_ID", "DRAFT_RECIPIENT", "DRAFT_MESSAGE"}
        if not required.issubset(indexes):
            return []
        actions: list[dict[str, Any]] = []
        for row in values[1:]:

            def value(name: str, current_row: list[Any] = row) -> Any:
                index = indexes.get(name)
                return (
                    current_row[index]
                    if index is not None and index < len(current_row)
                    else ""
                )

            if not (_yes(value("APPROVE")) or _yes(value("SEND"))):
                continue
            actions.append(
                {
                    "draft_id": str(value("DRAFT_ID")).strip(),
                    "recipient": str(value("DRAFT_RECIPIENT")).strip(),
                    "message": str(value("DRAFT_MESSAGE")),
                    "approve": _yes(value("APPROVE")),
                    "send": _yes(value("SEND")),
                }
            )
        return actions

    def _get_values(self, service: Any, range_name: str) -> list[list[Any]]:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=range_name)
            .execute()
        )
        return result.get("values", [])

    def _pull_actions(self, service: Any) -> dict[str, Any]:
        values = self._get_values(service, "LEADS!A1:Q")
        actions = self.parse_action_rows(values, LEADS_HEADERS)
        outreach_values = self._get_values(service, "OUTREACH!A1:K")
        normalized_outreach = [
            ["DRAFT_ID", "DRAFT_RECIPIENT", "DRAFT_MESSAGE", "APPROVE", "SEND"]
        ]
        for row in outreach_values[1:] if outreach_values else []:
            normalized_outreach.append(
                [
                    row[0] if len(row) > 0 else "",
                    row[5] if len(row) > 5 else "",
                    row[6] if len(row) > 6 else "",
                    row[8] if len(row) > 8 else "",
                    row[9] if len(row) > 9 else "",
                ]
            )
        outreach_actions = self.parse_action_rows(
            normalized_outreach, normalized_outreach[0]
        )
        by_id: dict[str, dict[str, Any]] = {}
        for item in [*actions, *outreach_actions]:
            draft_id = item["draft_id"]
            current = by_id.get(draft_id)
            if current is None:
                by_id[draft_id] = dict(item)
                continue
            if (
                current["recipient"] != item["recipient"]
                or current["message"] != item["message"]
            ):
                # Conflicting visible copies must fail the exact-draft check below.
                current["recipient"] = "__conflicting_visible_copy__"
                current["message"] = "__conflicting_visible_copy__"
                continue
            current["approve"] = current["approve"] or item["approve"]
            current["send"] = current["send"] or item["send"]
        approved: list[str] = []
        send_requested: list[str] = []
        rejected: list[dict[str, str]] = []
        for action in by_id.values():
            try:
                draft = self.crm.get_outreach_draft(action["draft_id"])
                if (
                    str(draft.get("recipient") or "") != action["recipient"]
                    or str(draft.get("message") or "") != action["message"]
                ):
                    raise ValueError(
                        "visible recipient/message no longer matches the CRM draft"
                    )
                if action["approve"] and draft["status"] == "draft":
                    draft = self.crm.approve_outreach_draft(draft["id"])
                    approved.append(draft["id"])
                if action["send"]:
                    if draft["status"] != "approved":
                        raise ValueError(
                            "SEND requires the exact draft to be approved first"
                        )
                    self.crm.queue_outreach_send_request(draft["id"])
                    send_requested.append(draft["id"])
            except (TypeError, ValueError) as exc:
                rejected.append({"draft_id": action["draft_id"], "reason": str(exc)})
        return {
            "approved_draft_ids": approved,
            "send_requested_draft_ids": send_requested,
            "rejected_actions": rejected,
        }

    def _snapshot(self) -> dict[str, list[list[Any]]]:
        with self.crm.connect() as connection:
            lead_rows = connection.execute(
                """
                SELECT l.*,
                    (SELECT occurred_at FROM interactions i WHERE i.lead_id = l.id
                        ORDER BY occurred_at DESC LIMIT 1) AS last_touch,
                    (SELECT due_at || ' — ' || action FROM followups f
                        WHERE f.lead_id = l.id AND f.status = 'pending'
                        ORDER BY due_at ASC LIMIT 1) AS next_action,
                    (SELECT id FROM outreach_drafts d WHERE d.lead_id = l.id
                        AND d.status IN ('draft','approved') ORDER BY created_at DESC LIMIT 1) AS draft_id,
                    (SELECT recipient FROM outreach_drafts d WHERE d.lead_id = l.id
                        AND d.status IN ('draft','approved') ORDER BY created_at DESC LIMIT 1) AS draft_recipient,
                    (SELECT message FROM outreach_drafts d WHERE d.lead_id = l.id
                        AND d.status IN ('draft','approved') ORDER BY created_at DESC LIMIT 1) AS draft_message
                FROM leads l ORDER BY COALESCE(l.score, -1) DESC, l.updated_at DESC
                """
            ).fetchall()
            campaign_rows = connection.execute(
                """
                SELECT c.id, c.industry, c.location,
                    COUNT(DISTINCT cl.lead_id) AS found,
                    COUNT(DISTINCT CASE WHEN l.score IS NOT NULL THEN l.id END) AS scored,
                    ROUND(AVG(l.score), 1) AS average_score,
                    COUNT(DISTINCT CASE WHEN l.status IN ('contacted','replied','interested','meeting','proposal','won') THEN l.id END) AS contacted,
                    COUNT(DISTINCT CASE WHEN i.direction = 'inbound' THEN i.lead_id END) AS replies,
                    COUNT(DISTINCT CASE WHEN l.status = 'meeting' THEN l.id END) AS meetings,
                    COUNT(DISTINCT CASE WHEN l.status = 'won' THEN l.id END) AS deals
                FROM campaigns c
                LEFT JOIN campaign_leads cl ON cl.campaign_id = c.id
                LEFT JOIN leads l ON l.id = cl.lead_id
                LEFT JOIN interactions i ON i.lead_id = l.id
                GROUP BY c.id ORDER BY c.created_at DESC
                """
            ).fetchall()
            draft_rows = connection.execute(
                """
                SELECT d.*, l.company_name FROM outreach_drafts d
                JOIN leads l ON l.id = d.lead_id ORDER BY d.created_at DESC
                """
            ).fetchall()
            followup_rows = connection.execute(
                """
                SELECT f.*, l.company_name FROM followups f
                JOIN leads l ON l.id = f.lead_id ORDER BY f.due_at ASC
                """
            ).fetchall()

        leads: list[list[Any]] = []
        for row in lead_rows:
            item = dict(row)
            analysis = _load_json(item.get("analysis_json"), {})
            contacts = _load_json(item.get("contacts_json"), {})
            problem = next(iter(analysis.get("website_problems") or []), "")
            offer = next(iter(analysis.get("recommended_ollum_services") or []), "")
            contact = next(
                iter(
                    [*(contacts.get("phones") or []), *(contacts.get("emails") or [])]
                ),
                "",
            )
            leads.append(
                [
                    item["id"],
                    item["company_name"],
                    item.get("industry"),
                    item["website_url"],
                    item.get("score"),
                    problem,
                    offer,
                    contact,
                    item["status"],
                    item.get("last_touch"),
                    item.get("next_action"),
                    item.get("draft_id"),
                    item.get("draft_recipient"),
                    item.get("draft_message"),
                    "",
                    "",
                    item["updated_at"],
                ]
            )

        campaigns: list[list[Any]] = []
        for row in campaign_rows:
            item = dict(row)
            contacted = int(item["contacted"] or 0)
            replies = int(item["replies"] or 0)
            campaigns.append(
                [
                    item["id"],
                    item.get("industry"),
                    item.get("location"),
                    item["found"],
                    item["scored"],
                    item["average_score"],
                    contacted,
                    replies,
                    item["meetings"],
                    item["deals"],
                    round(replies / contacted * 100, 1) if contacted else 0.0,
                ]
            )

        outreach = [
            [
                row["id"],
                row["lead_id"],
                row["company_name"],
                row["created_at"],
                row["channel"],
                row["recipient"],
                row["message"],
                row["status"],
                "",
                "",
                row["sent_at"],
            ]
            for row in draft_rows
        ]
        followups = [
            [
                row["id"],
                row["lead_id"],
                row["company_name"],
                row["due_at"],
                row["action"],
                row["status"],
                row["created_at"],
            ]
            for row in followup_rows
        ]

        daily = self.crm.daily_report()
        conversion = self.crm.conversion_report()
        performance = self.crm.vertical_performance()
        dashboard: list[list[Any]] = [
            ["OLLUM SALES", ""],
            ["Сегодня найдено", daily["leads_found"]],
            ["Проанализировано", daily["analyzed"]],
            ["Qualified", daily["qualified"]],
            ["Новых черновиков", daily["drafts_created"]],
            ["Отправлено", daily["messages_sent"]],
            ["Ответов", daily["replies"]],
            ["Встреч", daily["meetings"]],
            ["Сделок", daily["deals"]],
            ["В работе", conversion["stages"]["leads"]],
            ["", ""],
            ["Лучшие отрасли", "Reply rate"],
        ]
        dashboard.extend(
            [[item["name"], item["reply_rate"]] for item in performance[:5]]
        )

        return {
            "LEADS": [
                LEADS_HEADERS,
                *[[_cell(value) for value in row] for row in leads],
            ],
            "CAMPAIGNS": [
                CAMPAIGNS_HEADERS,
                *[[_cell(value) for value in row] for row in campaigns],
            ],
            "OUTREACH": [
                OUTREACH_HEADERS,
                *[[_cell(value) for value in row] for row in outreach],
            ],
            "FOLLOWUPS": [
                FOLLOWUPS_HEADERS,
                *[[_cell(value) for value in row] for row in followups],
            ],
            "DASHBOARD": [[_cell(value) for value in row] for row in dashboard],
        }

    def _ensure_tabs(self, service: Any) -> tuple[dict[str, int], list[int]]:
        metadata = (
            service.spreadsheets()
            .get(
                spreadsheetId=self.spreadsheet_id,
                fields="sheets.properties(sheetId,title)",
            )
            .execute()
        )
        sheet_ids = {
            sheet["properties"]["title"]: sheet["properties"]["sheetId"]
            for sheet in metadata.get("sheets", [])
        }
        missing = [name for name in self.tab_names if name not in sheet_ids]
        created_ids: list[int] = []
        if missing:
            response = (
                service.spreadsheets()
                .batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={
                        "requests": [
                            {"addSheet": {"properties": {"title": name}}}
                            for name in missing
                        ]
                    },
                )
                .execute()
            )
            for reply in response.get("replies", []):
                properties = reply.get("addSheet", {}).get("properties", {})
                if properties:
                    sheet_ids[properties["title"]] = properties["sheetId"]
                    created_ids.append(properties["sheetId"])
        return sheet_ids, created_ids

    def _format_new_tabs(
        self, service: Any, sheet_ids: dict[str, int], created_ids: list[int]
    ) -> None:
        if not created_ids:
            return
        requests: list[dict[str, Any]] = []
        for sheet_id in created_ids:
            requests.extend(
                [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheet_id,
                                "gridProperties": {"frozenRowCount": 1},
                            },
                            "fields": "gridProperties.frozenRowCount",
                        }
                    },
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": {
                                        "red": 0.10,
                                        "green": 0.22,
                                        "blue": 0.36,
                                    },
                                    "textFormat": {
                                        "foregroundColor": {
                                            "red": 1,
                                            "green": 1,
                                            "blue": 1,
                                        },
                                        "bold": True,
                                    },
                                }
                            },
                            "fields": "userEnteredFormat(backgroundColor,textFormat)",
                        }
                    },
                    {
                        "autoResizeDimensions": {
                            "dimensions": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": 0,
                                "endIndex": 20,
                            }
                        }
                    },
                ]
            )
        leads_id = sheet_ids.get("LEADS")
        if leads_id in created_ids:
            status_colors = {
                "lost": {"red": 0.96, "green": 0.70, "blue": 0.70},
                "analyzing": {"red": 1.0, "green": 0.90, "blue": 0.55},
                "contacted": {"red": 0.68, "green": 0.84, "blue": 0.96},
                "replied": {"red": 0.72, "green": 0.90, "blue": 0.72},
                "interested": {"red": 0.55, "green": 0.84, "blue": 0.55},
                "won": {"red": 0.20, "green": 0.55, "blue": 0.30},
            }
            for status, color in status_colors.items():
                requests.append(
                    {
                        "addConditionalFormatRule": {
                            "rule": {
                                "ranges": [
                                    {
                                        "sheetId": leads_id,
                                        "startRowIndex": 1,
                                        "startColumnIndex": 8,
                                        "endColumnIndex": 9,
                                    }
                                ],
                                "booleanRule": {
                                    "condition": {
                                        "type": "TEXT_EQ",
                                        "values": [{"userEnteredValue": status}],
                                    },
                                    "format": {"backgroundColor": color},
                                },
                            },
                            "index": 0,
                        }
                    }
                )
        service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests}
        ).execute()

    def sync(self) -> dict[str, Any]:
        if not self.configured:
            return {
                "success": False,
                "blocked": True,
                "message": (
                    "Google Sheets is not configured. Set the feature flag, spreadsheet ID, "
                    "and service-account file path."
                ),
                "status": self.status(),
            }
        try:
            service = self._build_service()
            sheet_ids, created_ids = self._ensure_tabs(service)
            actions = self._pull_actions(service)
            snapshot = self._snapshot()
            ranges = [f"'{name}'!A:Z" for name in self.tab_names]
            (
                service.spreadsheets()
                .values()
                .batchClear(spreadsheetId=self.spreadsheet_id, body={"ranges": ranges})
                .execute()
            )
            body = {
                "valueInputOption": "RAW",
                "data": [
                    {"range": f"'{name}'!A1", "values": values}
                    for name, values in snapshot.items()
                ],
            }
            result = (
                service.spreadsheets()
                .values()
                .batchUpdate(spreadsheetId=self.spreadsheet_id, body=body)
                .execute()
            )
            self._format_new_tabs(service, sheet_ids, created_ids)
            details = {
                "tabs": {
                    name: max(0, len(values) - 1) for name, values in snapshot.items()
                },
                "updated_cells": result.get("totalUpdatedCells", 0),
                "actions": actions,
            }
            self.crm.record_google_sheets_sync(status="success", details=details)
            return {"success": True, **details}
        except Exception as exc:
            self.crm.record_google_sheets_sync(
                status="failed", details={}, error=str(exc)[:1000]
            )
            raise
