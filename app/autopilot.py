from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .candidate_quality import assess_company_candidate
from .company_search import search_company_websites
from .crm import SalesCRM
from .data_quality import candidate_phones, retry_call
from .google_sheets import GoogleSheetsSync
from .outreach_quality import compose_grounded_first_touch, evaluate_whatsapp_message
from .website_inspector import inspect_website
from .whatsapp_service import normalize_recipient, send_message

logger = logging.getLogger(__name__)


class SettingsLike(Protocol):
    serper_api_key: str | None
    company_search_timeout: int
    website_inspection_timeout: int
    evidence_ttl_hours: int
    retry_attempts: int
    retry_base_delay_seconds: float
    autopilot_default_mode: str
    autopilot_interval_minutes: int
    autopilot_max_verticals_per_cycle: int
    autopilot_leads_per_vertical: int
    autopilot_score_threshold: int
    autopilot_min_training_leads: int
    allow_autopilot_send: bool
    allow_whatsapp_send: bool


DEFAULT_VERTICALS = [
    ("мебель", ["monday"]),
    ("вентиляция", ["monday"]),
    ("логистика", ["tuesday"]),
    ("клининг", ["tuesday"]),
    ("строительство", ["wednesday"]),
    ("производство", ["wednesday"]),
    ("стоматологии", ["thursday"]),
    ("образование", ["thursday"]),
    ("недвижимость", ["friday"]),
    ("B2B услуги", ["friday"]),
    ("автосервисы", ["saturday"]),
    ("юридические компании", ["saturday"]),
]


def grounded_analysis(lead: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Create bounded deterministic analysis from explicit website-inspector fields."""
    text = str(snapshot.get("visible_text") or "")
    lower = text.lower()
    forms = int((snapshot.get("forms") or {}).get("count") or 0)
    mobile = bool(snapshot.get("mobile_viewport"))
    contacts = snapshot.get("contacts") or {}
    social_links = list(contacts.get("social_links") or [])
    messenger_links = [
        link
        for link in social_links
        if any(
            marker in str(link).lower()
            for marker in ("t.me", "max.ru", "wa.me", "whatsapp")
        )
    ]
    catalog_markers = sum(
        lower.count(marker)
        for marker in ("каталог", "продукц", "услуг", "прайс", "цены", "товар")
    )
    substantial_catalog = len(text) >= 1800 and catalog_markers >= 4
    industry = str(lead.get("industry") or "").lower()

    strengths = [f"Официальный сайт доступен: {lead['website_url']}"]
    if mobile:
        strengths.append("На странице задан mobile viewport.")
    if forms:
        strengths.append(f"На проверенной странице обнаружено форм: {forms}.")
    if substantial_catalog:
        strengths.append(
            "На странице подтверждён разветвлённый каталог услуг или продукции."
        )
    if messenger_links:
        strengths.append("На странице есть публичный вход в мессенджер.")

    problems: list[str] = []
    if not mobile:
        problems.append(
            "На странице не задан mobile viewport — это конкретный риск для мобильного сценария."
        )
    if forms == 0:
        problems.append(
            "На проверенной странице не обнаружена структурированная форма заявки."
        )
    if substantial_catalog and forms == 0:
        problems.append(
            "Большой каталог не сопровождается подтверждённым пошаговым подбором или конфигуратором."
        )
    if not messenger_links:
        problems.append(
            "Публичный вход в Telegram, MAX или WhatsApp на проверенной странице не обнаружен."
        )
    if not problems:
        problems.append(
            "Критическая проблема не подтверждена; глубину конверсии и автоматизации нужно проверять отдельно."
        )

    if any(marker in industry for marker in ("мебел", "производ", "вентил")):
        services = [
            "web-конфигуратор и структурированный запрос расчёта",
            "AI-помощник по подтверждённому каталогу",
            "Telegram/MAX-бот для сбора параметров заявки",
        ]
        opportunities = [
            "Собирать параметры, количество, размеры и файлы до первого ответа менеджера.",
            "Маршрутизировать полный запрос ответственному специалисту.",
        ]
    elif any(marker in industry for marker in ("стомат", "медицин", "клиник")):
        services = [
            "Telegram/MAX-бот для записи",
            "web-сценарий выбора направления услуги",
            "автоматизация маршрутизации и не-клинических напоминаний",
        ]
        opportunities = [
            "Собирать понятный запрос на запись без автоматизации медицинских решений.",
            "Передавать обращение в подходящий сценарий записи.",
        ]
    elif any(
        marker in industry for marker in ("логист", "перевоз", "клининг", "строит")
    ):
        services = [
            "калькулятор и web-бриф заявки",
            "Telegram/MAX-бот квалификации",
            "автоматизация подтверждения и маршрутизации обращения",
        ]
        opportunities = [
            "Собирать обязательные параметры объекта или услуги до обратного звонка.",
            "Сократить число первичных уточнений перед расчётом.",
        ]
    else:
        services = [
            "мобильный конверсионный сценарий сайта",
            "Telegram/MAX-бот квалификации",
            "AI-помощник по подтверждённым материалам компании",
        ]
        opportunities = [
            "Сделать первый цифровой шаг понятнее и структурировать обращение.",
            "Собирать данные заявки в едином формате.",
        ]

    fit = 82
    need = min(95, 35 + len(problems) * 14)
    score = round(fit * 0.35 + need * 0.30 + 50 * 0.20 + 40 * 0.15)
    return {
        "company_name": lead["company_name"],
        "industry": lead.get("industry"),
        "location": lead.get("location"),
        "summary": (
            f"Проведена ограниченная проверка официального сайта {lead['website_url']}. "
            "Выводы основаны только на доступной странице и технических полях снимка."
        ),
        "contacts": {
            "phones": list(contacts.get("phones") or []),
            "emails": list(contacts.get("emails") or []),
            "messengers": messenger_links,
            "social_links": social_links,
        },
        "website_strengths": strengths,
        "website_problems": problems,
        "detected_tools": list(snapshot.get("technologies") or []),
        "opportunities": opportunities,
        "recommended_ollum_services": services,
        "outreach_angles": [
            f"Начать разговор с проверенного наблюдения: {problems[0]}",
            f"Предложить первый ограниченный этап: {services[0]}.",
        ],
        "lead_score": score,
        "score_reason": (
            f"Детерминированная оценка по видимым сигналам: fit={fit}, need={need}, "
            "budget proxy=50, timing=40. Реальный бюджет и сроки покупки неизвестны."
        ),
    }


def draft_message(lead: dict[str, Any]) -> str | None:
    """Build a first-touch message only when concrete evidence and a service exist."""
    return compose_grounded_first_touch(lead)


class AutopilotService:
    def __init__(
        self,
        crm: SalesCRM,
        settings: SettingsLike,
        sheets: GoogleSheetsSync,
        *,
        discoverer: Any = search_company_websites,
        inspector: Any = inspect_website,
        sender: Any = send_message,
    ) -> None:
        self.crm = crm
        self.settings = settings
        self.sheets = sheets
        self.discoverer = discoverer
        self.inspector = inspector
        self.sender = sender

    def ensure_default_verticals(self) -> list[dict[str, Any]]:
        existing = self.crm.list_verticals(limit=500)
        if existing:
            return existing
        for name, days in DEFAULT_VERTICALS:
            self.crm.create_vertical(
                name,
                region="Москва и Московская область",
                search_query=(
                    "средний локальный бизнес, не федеральная корпорация; потенциал сайта, "
                    "Telegram/MAX-бота, web-app или AI-интеграции"
                ),
                days=days,
                daily_target=self.settings.autopilot_leads_per_vertical,
                min_score=self.settings.autopilot_score_threshold,
            )
        return self.crm.list_verticals(limit=500)

    def status(self) -> dict[str, Any]:
        state = self.crm.get_autopilot_state()
        stats = self.crm.stats()
        return {
            **state,
            "vertical_count": len(self.crm.list_verticals(enabled=True, limit=500)),
            "training_leads": stats["leads"],
            "minimum_training_leads_for_non_safe": self.settings.autopilot_min_training_leads,
            "non_safe_send_flag": self.settings.allow_autopilot_send,
            "whatsapp_send_flag": self.settings.allow_whatsapp_send,
            "pending_send_requests": len(
                self.crm.list_pending_send_requests(limit=200)
            ),
            "google_sheets": self.sheets.status(),
            "safe_behavior": (
                "SAFE may discover, analyze, score, and prepare drafts. It never sends or "
                "executes follow-ups."
            ),
        }

    def start(
        self,
        *,
        mode: str | None = None,
        interval_minutes: int | None = None,
        max_verticals_per_cycle: int | None = None,
        leads_per_vertical: int | None = None,
        score_threshold: int | None = None,
        confirm_non_safe: bool = False,
    ) -> dict[str, Any]:
        selected_mode = (mode or self.settings.autopilot_default_mode).strip().lower()
        if selected_mode != "safe":
            blockers: list[str] = []
            if not confirm_non_safe:
                blockers.append("explicit confirm_non_safe=true is required")
            if self.crm.stats()["leads"] < self.settings.autopilot_min_training_leads:
                blockers.append(
                    f"at least {self.settings.autopilot_min_training_leads} training leads are required"
                )
            if not self.settings.allow_autopilot_send:
                blockers.append("OLLUM_AUTOPILOT_ALLOW_SEND is disabled")
            if not self.settings.allow_whatsapp_send:
                blockers.append("OLLUM_ALLOW_WHATSAPP_SEND is disabled")
            if blockers:
                return {
                    "success": False,
                    "blocked": True,
                    "mode": selected_mode,
                    "reasons": blockers,
                    "status": self.status(),
                }
        self.ensure_default_verticals()
        state = self.crm.start_autopilot(
            mode=selected_mode,
            interval_minutes=interval_minutes
            or self.settings.autopilot_interval_minutes,
            max_verticals_per_cycle=max_verticals_per_cycle
            or self.settings.autopilot_max_verticals_per_cycle,
            leads_per_vertical=leads_per_vertical
            or self.settings.autopilot_leads_per_vertical,
            score_threshold=score_threshold
            if score_threshold is not None
            else self.settings.autopilot_score_threshold,
        )
        return {"success": True, "status": state}

    def stop(self) -> dict[str, Any]:
        return {"success": True, "status": self.crm.stop_autopilot()}

    def _select_verticals(self, limit: int) -> list[dict[str, Any]]:
        # Moscow currently uses a fixed UTC+3 offset; avoiding system tzdata keeps
        # the worker portable in minimal Windows and container runtimes.
        weekday = (datetime.now(UTC) + timedelta(hours=3)).strftime("%A").lower()
        verticals = self.crm.list_verticals(enabled=True, limit=500)
        scheduled = [
            item for item in verticals if not item["days"] or weekday in item["days"]
        ]
        candidates = scheduled or verticals
        performance = {item["id"]: item for item in self.crm.vertical_performance()}

        def priority(item: dict[str, Any]) -> tuple[float, str]:
            stats = performance.get(item["id"], {})
            reply_boost = float(stats.get("reply_rate") or 0) / 100 * 2
            qualified_boost = float(stats.get("qualified_rate") or 0) / 100 * 0.4
            effective = float(item["weight"]) * (1 + reply_boost + qualified_boost)
            return (-effective, str(item.get("last_selected_at") or ""))

        return sorted(candidates, key=priority)[: max(1, limit)]

    @staticmethod
    def _recipient(lead: dict[str, Any]) -> str | None:
        contacts = lead.get("contacts") or {}
        candidates = [
            *(contacts.get("messengers") or []),
            *(contacts.get("phones") or []),
            *(contacts.get("emails") or []),
        ]
        for candidate in candidates:
            value = str(candidate).strip()
            if not value:
                continue
            if "@" in value and not value.endswith(
                ("@s.whatsapp.net", "@g.us", "@lid")
            ):
                return value
            try:
                return normalize_recipient(value)
            except ValueError:
                continue
        return None

    def _create_draft_if_needed(
        self, lead: dict[str, Any], *, threshold: int
    ) -> dict[str, Any] | None:
        if int(lead.get("score") or 0) < threshold:
            return None
        if self.crm.list_outreach_drafts(lead_id=lead["id"], limit=1):
            return None
        recipient = self._recipient(lead)
        if not recipient:
            return None
        channel = "email" if "@" in recipient else "whatsapp"
        message = draft_message(lead)
        if not message:
            return None
        if channel == "whatsapp":
            quality = evaluate_whatsapp_message(lead, message, mode="first_touch")
            if quality["verdict"] != "pass":
                logger.warning(
                    "Skipped unsafe WhatsApp draft for lead %s: %s",
                    lead["id"],
                    quality["issues"],
                )
                return None
        return self.crm.save_outreach_draft(
            lead["id"],
            channel=channel,
            recipient=recipient,
            message=message,
        )

    def _process_send_requests(self, *, mode: str) -> dict[str, Any]:
        requests = self.crm.list_pending_send_requests(limit=50)
        if mode == "safe":
            return {"pending": len(requests), "sent": 0, "failed": 0, "blocked": True}
        if not (
            self.settings.allow_autopilot_send and self.settings.allow_whatsapp_send
        ):
            return {"pending": len(requests), "sent": 0, "failed": 0, "blocked": True}
        sent = 0
        failed = 0
        for request in requests:
            if (
                request["channel"] != "whatsapp"
                or request["draft_status"] != "approved"
            ):
                self.crm.complete_send_request(
                    request["id"],
                    success=False,
                    error="draft is not approved WhatsApp outreach",
                )
                failed += 1
                continue
            claimed = self.crm.claim_outreach_draft_for_send(request["draft_id"])
            if claimed is None:
                self.crm.complete_send_request(
                    request["id"], success=False, error="draft send claim unavailable"
                )
                failed += 1
                continue
            try:
                result = self.sender(claimed["recipient"], claimed["message"])
                success = bool(result.get("success")) and not bool(
                    result.get("blocked")
                )
                if result.get("blocked"):
                    self.crm.release_outreach_send_claim(claimed["id"])
                else:
                    self.crm.mark_outreach_sent(claimed["id"], success=success)
                if success:
                    self.crm.record_interaction(
                        claimed["lead_id"],
                        channel="whatsapp",
                        direction="outbound",
                        content=claimed["message"],
                        status="sent",
                    )
                    sent += 1
                else:
                    failed += 1
                self.crm.complete_send_request(
                    request["id"],
                    success=success,
                    error=None if success else "WhatsApp bridge did not confirm send",
                )
            except Exception as exc:  # noqa: BLE001 - isolate one queued send failure
                try:
                    self.crm.mark_outreach_sent(claimed["id"], success=False)
                finally:
                    self.crm.complete_send_request(
                        request["id"], success=False, error=str(exc)[:1000]
                    )
                failed += 1
        return {
            "pending": len(requests),
            "sent": sent,
            "failed": failed,
            "blocked": False,
        }

    @staticmethod
    def _new_cycle_metrics() -> dict[str, Any]:
        return {
            "campaigns_created": 0,
            "campaigns_reused": 0,
            "candidates_seen": 0,
            "candidates_rejected": 0,
            "duplicates_skipped": 0,
            "stale_evidence_refreshed": 0,
            "rejection_reasons": {},
            "leads_found": 0,
            "analyzed": 0,
            "qualified": 0,
            "drafts_created": 0,
            "due_followups": 0,
            "send_requests": {},
            "vertical_errors": [],
            "retry_count": 0,
        }

    def _retry_operation(
        self,
        operation: Any,
        metrics: dict[str, Any],
        *,
        operation_name: str,
    ) -> Any:
        def on_retry(attempt: int, exc: Exception) -> None:
            metrics["retry_count"] = int(metrics["retry_count"]) + 1
            logger.warning(
                "Autopilot operation retry",
                extra={
                    "operation": operation_name,
                    "attempt": attempt,
                    "error": str(exc)[:300],
                },
            )

        return retry_call(
            operation,
            attempts=int(getattr(self.settings, "retry_attempts", 3)),
            base_delay_seconds=float(
                getattr(self.settings, "retry_base_delay_seconds", 0.5)
            ),
            on_retry=on_retry,
        )

    @staticmethod
    def _record_rejection(metrics: dict[str, Any], reason: str) -> None:
        metrics["candidates_rejected"] += 1
        reasons = metrics["rejection_reasons"]
        reasons[reason] = int(reasons.get(reason) or 0) + 1

    @staticmethod
    def _record_vertical_error(
        metrics: dict[str, Any],
        vertical: dict[str, Any],
        exc: Exception,
        **context: Any,
    ) -> None:
        metrics["vertical_errors"].append(
            {
                "vertical": vertical["name"],
                **context,
                "error": str(exc)[:300],
            }
        )

    def _inspect_candidate(
        self,
        *,
        result: dict[str, Any],
        index: int,
        provider: str,
        vertical: dict[str, Any],
        campaign_id: str,
        metrics: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        phones = candidate_phones(result)
        existing = self.crm.find_duplicate_lead(
            company_name=str(result.get("company_name") or ""),
            website_url=result["website_url"],
            phones=phones,
            location=vertical["region"],
        )
        if existing:
            previous_evidence = self.crm.get_inspection(
                existing["id"], allow_stale=True
            )
            if not previous_evidence or previous_evidence["fresh"]:
                metrics["duplicates_skipped"] += 1
                return None
            metrics["stale_evidence_refreshed"] += 1

        snapshot = self._retry_operation(
            lambda: self.inspector(
                result["website_url"],
                timeout=self.settings.website_inspection_timeout,
            ),
            metrics,
            operation_name="website_inspection",
        )
        assessment = assess_company_candidate(result, snapshot)
        if not assessment["accepted"]:
            self._record_rejection(
                metrics, str(assessment["reason"] or "candidate_rejected")
            )
            return None

        lead = self.crm.upsert_lead(
            result["company_name"],
            result["website_url"],
            industry=vertical["name"],
            location=vertical["region"],
            source=f"autopilot:{provider}",
            campaign_id=campaign_id,
            source_rank=index,
            phones=phones,
        )
        self.crm.save_inspection(
            lead["id"],
            snapshot,
            ttl_hours=int(getattr(self.settings, "evidence_ttl_hours", 168)),
        )
        return lead, snapshot

    def _collect_vertical_leads(
        self,
        *,
        discovery: dict[str, Any],
        vertical: dict[str, Any],
        campaign_id: str,
        target_count: int,
        metrics: dict[str, Any],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        leads: list[tuple[dict[str, Any], dict[str, Any]]] = []
        provider = str(discovery.get("provider") or "search")
        for index, result in enumerate(discovery.get("results") or [], start=1):
            if len(leads) >= target_count:
                break
            metrics["candidates_seen"] += 1
            try:
                accepted = self._inspect_candidate(
                    result=result,
                    index=index,
                    provider=provider,
                    vertical=vertical,
                    campaign_id=campaign_id,
                    metrics=metrics,
                )
                if accepted:
                    leads.append(accepted)
            except Exception as exc:  # noqa: BLE001 - isolate candidate failure
                self._record_rejection(metrics, "inspection_error")
                self._record_vertical_error(
                    metrics,
                    vertical,
                    exc,
                    website_url=str(result.get("website_url") or "")[:300],
                )
                logger.warning(
                    "Autopilot candidate rejected after an error",
                    extra={"vertical": vertical["name"], "error": str(exc)[:300]},
                )
        return leads

    def _analyze_vertical_leads(
        self,
        *,
        leads: list[tuple[dict[str, Any], dict[str, Any]]],
        vertical: dict[str, Any],
        threshold: int,
        metrics: dict[str, Any],
    ) -> None:
        for lead, snapshot in leads:
            try:
                analysis = grounded_analysis(lead, snapshot)
                saved = self.crm.save_analysis(lead["id"], analysis)
                scored = self.crm.score_lead(
                    saved["id"],
                    rationale=analysis["score_reason"],
                    qualify_at=threshold,
                )
                metrics["analyzed"] += 1
                if int(scored.get("score") or 0) >= threshold:
                    metrics["qualified"] += 1
                if self._create_draft_if_needed(scored, threshold=threshold):
                    metrics["drafts_created"] += 1
            except Exception as exc:  # noqa: BLE001 - isolate one lead failure
                self._record_vertical_error(metrics, vertical, exc, lead_id=lead["id"])
                logger.warning(
                    "Autopilot lead analysis failed",
                    extra={"vertical": vertical["name"], "lead_id": lead["id"]},
                )

    def _process_vertical(
        self,
        *,
        cycle_id: str,
        state: dict[str, Any],
        vertical: dict[str, Any],
        metrics: dict[str, Any],
    ) -> None:
        target_count = min(
            int(vertical["daily_target"]), int(state["leads_per_vertical"])
        )
        campaign, campaign_created = self.crm.get_or_create_campaign(
            f"Autopilot — {vertical['name']} — {datetime.now(UTC).date().isoformat()}",
            industry=vertical["name"],
            location=vertical["region"],
            search_query=vertical.get("search_query"),
            target_count=target_count,
            status="discovering",
        )
        self.crm.register_autopilot_campaign(
            cycle_id=cycle_id,
            campaign_id=campaign["id"],
            vertical_id=vertical["id"],
        )
        metric_key = "campaigns_created" if campaign_created else "campaigns_reused"
        metrics[metric_key] += 1

        try:
            discovery = self._retry_operation(
                lambda: self.discoverer(
                    vertical["name"],
                    vertical["region"],
                    limit=min(50, max(target_count, target_count * 4)),
                    extra_query=vertical.get("search_query"),
                    serper_api_key=self.settings.serper_api_key,
                    timeout=self.settings.company_search_timeout,
                ),
                metrics,
                operation_name="company_discovery",
            )
            leads = self._collect_vertical_leads(
                discovery=discovery,
                vertical=vertical,
                campaign_id=campaign["id"],
                target_count=target_count,
                metrics=metrics,
            )
            metrics["leads_found"] += len(leads)
            self.crm.set_campaign_status(
                campaign["id"], "analyzing" if leads else "paused"
            )
            threshold = max(int(vertical["min_score"]), int(state["score_threshold"]))
            self._analyze_vertical_leads(
                leads=leads,
                vertical=vertical,
                threshold=threshold,
                metrics=metrics,
            )
            self.crm.set_campaign_status(campaign["id"], "ready" if leads else "paused")
        except Exception as exc:
            self.crm.set_campaign_status(campaign["id"], "paused")
            self._record_vertical_error(metrics, vertical, exc)
            logger.exception(
                "Autopilot vertical failed",
                extra={"cycle_id": cycle_id, "vertical": vertical["name"]},
            )

    def _finalize_cycle_work(
        self, cycle: dict[str, Any], metrics: dict[str, Any]
    ) -> None:
        metrics["due_followups"] = len(self.crm.list_due_followups(limit=200))
        metrics["send_requests"] = self._process_send_requests(mode=cycle["mode"])
        sheets_result = self.sheets.sync()
        metrics["google_sheets"] = {
            "success": bool(sheets_result.get("success")),
            "blocked": bool(sheets_result.get("blocked")),
        }

    def run_cycle(self, *, force: bool = False) -> dict[str, Any]:
        cycle = self.crm.begin_autopilot_cycle(force=force)
        if cycle is None:
            return {
                "success": False,
                "blocked": True,
                "message": "Autopilot is stopped, not due yet, or another cycle holds the lock.",
                "status": self.status(),
            }

        metrics = self._new_cycle_metrics()
        logger.info(
            "Autopilot cycle started",
            extra={"cycle_id": cycle["id"], "mode": cycle["mode"], "forced": force},
        )
        try:
            state = self.crm.get_autopilot_state()
            verticals = self._select_verticals(int(state["max_verticals_per_cycle"]))
            self.crm.set_cycle_verticals(
                cycle["id"], [item["id"] for item in verticals]
            )
            for vertical in verticals:
                self._process_vertical(
                    cycle_id=cycle["id"],
                    state=state,
                    vertical=vertical,
                    metrics=metrics,
                )

            self._finalize_cycle_work(cycle, metrics)
            completed = self.crm.complete_autopilot_cycle(cycle["id"], metrics=metrics)
            logger.info(
                "Autopilot cycle completed",
                extra={
                    "cycle_id": cycle["id"],
                    "verticals": len(verticals),
                    "leads_found": metrics["leads_found"],
                    "analyzed": metrics["analyzed"],
                    "qualified": metrics["qualified"],
                    "errors": len(metrics["vertical_errors"]),
                },
            )
            return {"success": True, "cycle": completed}
        except Exception as exc:
            logger.exception("Autopilot cycle failed", extra={"cycle_id": cycle["id"]})
            failed = self.crm.complete_autopilot_cycle(
                cycle["id"], metrics=metrics, error=str(exc)[:1000]
            )
            return {"success": False, "cycle": failed}
