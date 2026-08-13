from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .candidate_quality import assess_company_candidate
from .company_search import search_company_websites
from .crm import SalesCRM
from .google_sheets import GoogleSheetsSync
from .website_inspector import inspect_website
from .whatsapp_service import send_message


class SettingsLike(Protocol):
    serper_api_key: str | None
    company_search_timeout: int
    website_inspection_timeout: int
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


def draft_message(lead: dict[str, Any]) -> str:
    analysis = lead.get("analysis") or {}
    problem = next(iter(analysis.get("website_problems") or []), "").rstrip(".")
    service = next(
        iter(analysis.get("recommended_ollum_services") or []),
        "структурированный цифровой сценарий заявки",
    )
    return (
        f"Здравствуйте! Проверили сайт «{lead['company_name']}»: {problem}. "
        f"Ollum Group может сделать {service}. Это поможет получать более полные "
        "обращения и упростить первый шаг клиента. Показать короткую схему решения?"
    )


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
            *(contacts.get("phones") or []),
            *(contacts.get("emails") or []),
        ]
        return str(candidates[0]).strip() if candidates else None

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
        return self.crm.save_outreach_draft(
            lead["id"],
            channel=channel,
            recipient=recipient,
            message=draft_message(lead),
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

    def run_cycle(self, *, force: bool = False) -> dict[str, Any]:
        cycle = self.crm.begin_autopilot_cycle(force=force)
        if cycle is None:
            return {
                "success": False,
                "blocked": True,
                "message": "Autopilot is stopped, not due yet, or another cycle holds the lock.",
                "status": self.status(),
            }
        state = self.crm.get_autopilot_state()
        metrics: dict[str, Any] = {
            "campaigns_created": 0,
            "candidates_seen": 0,
            "candidates_rejected": 0,
            "duplicates_skipped": 0,
            "rejection_reasons": {},
            "leads_found": 0,
            "analyzed": 0,
            "qualified": 0,
            "drafts_created": 0,
            "due_followups": 0,
            "send_requests": {},
            "vertical_errors": [],
        }
        try:
            verticals = self._select_verticals(int(state["max_verticals_per_cycle"]))
            self.crm.set_cycle_verticals(
                cycle["id"], [item["id"] for item in verticals]
            )
            for vertical in verticals:
                target_count = min(
                    int(vertical["daily_target"]), int(state["leads_per_vertical"])
                )
                campaign = self.crm.create_campaign(
                    f"Autopilot — {vertical['name']} — {datetime.now(UTC).date().isoformat()}",
                    industry=vertical["name"],
                    location=vertical["region"],
                    search_query=vertical.get("search_query"),
                    target_count=target_count,
                    status="discovering",
                )
                self.crm.register_autopilot_campaign(
                    cycle_id=cycle["id"],
                    campaign_id=campaign["id"],
                    vertical_id=vertical["id"],
                )
                metrics["campaigns_created"] += 1
                try:
                    discovery = self.discoverer(
                        vertical["name"],
                        vertical["region"],
                        limit=min(50, max(target_count, target_count * 4)),
                        extra_query=vertical.get("search_query"),
                        serper_api_key=self.settings.serper_api_key,
                        timeout=self.settings.company_search_timeout,
                    )
                    leads: list[tuple[dict[str, Any], dict[str, Any]]] = []
                    for index, result in enumerate(
                        discovery.get("results") or [], start=1
                    ):
                        if len(leads) >= target_count:
                            break
                        metrics["candidates_seen"] += 1
                        try:
                            if self.crm.find_lead_by_website_url(result["website_url"]):
                                metrics["duplicates_skipped"] += 1
                                continue
                            snapshot = self.inspector(
                                result["website_url"],
                                timeout=self.settings.website_inspection_timeout,
                            )
                            assessment = assess_company_candidate(result, snapshot)
                            if not assessment["accepted"]:
                                reason = str(
                                    assessment["reason"] or "candidate_rejected"
                                )
                                metrics["candidates_rejected"] += 1
                                reasons = metrics["rejection_reasons"]
                                reasons[reason] = int(reasons.get(reason) or 0) + 1
                                continue
                            lead = self.crm.upsert_lead(
                                result["company_name"],
                                result["website_url"],
                                industry=vertical["name"],
                                location=vertical["region"],
                                source=f"autopilot:{discovery.get('provider', 'search')}",
                                campaign_id=campaign["id"],
                                source_rank=index,
                            )
                            leads.append((lead, snapshot))
                        except Exception as exc:  # noqa: BLE001 - isolate candidate failure
                            metrics["candidates_rejected"] += 1
                            reasons = metrics["rejection_reasons"]
                            reasons["inspection_error"] = (
                                int(reasons.get("inspection_error") or 0) + 1
                            )
                            metrics["vertical_errors"].append(
                                {
                                    "vertical": vertical["name"],
                                    "website_url": str(result.get("website_url") or "")[
                                        :300
                                    ],
                                    "error": str(exc)[:300],
                                }
                            )
                    metrics["leads_found"] += len(leads)
                    self.crm.set_campaign_status(
                        campaign["id"], "analyzing" if leads else "paused"
                    )
                    threshold = max(
                        int(vertical["min_score"]), int(state["score_threshold"])
                    )
                    for lead, snapshot in leads:
                        try:
                            analysis = grounded_analysis(lead, snapshot)
                            self.crm.save_analysis(lead["id"], analysis)
                            scored = self.crm.score_lead(
                                lead["id"],
                                rationale=analysis["score_reason"],
                                qualify_at=threshold,
                            )
                            metrics["analyzed"] += 1
                            if int(scored.get("score") or 0) >= threshold:
                                metrics["qualified"] += 1
                            if self._create_draft_if_needed(
                                scored,
                                threshold=threshold,
                            ):
                                metrics["drafts_created"] += 1
                        except Exception as exc:  # noqa: BLE001 - isolate one lead failure
                            metrics["vertical_errors"].append(
                                {
                                    "vertical": vertical["name"],
                                    "lead_id": lead["id"],
                                    "error": str(exc)[:300],
                                }
                            )
                    self.crm.set_campaign_status(
                        campaign["id"], "ready" if leads else "paused"
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one vertical failure
                    self.crm.set_campaign_status(campaign["id"], "paused")
                    metrics["vertical_errors"].append(
                        {"vertical": vertical["name"], "error": str(exc)[:300]}
                    )

            metrics["due_followups"] = len(self.crm.list_due_followups(limit=200))
            metrics["send_requests"] = self._process_send_requests(mode=cycle["mode"])
            sheets_result = self.sheets.sync()
            metrics["google_sheets"] = {
                "success": bool(sheets_result.get("success")),
                "blocked": bool(sheets_result.get("blocked")),
            }
            completed = self.crm.complete_autopilot_cycle(cycle["id"], metrics=metrics)
            return {"success": True, "cycle": completed}
        except Exception as exc:  # noqa: BLE001 - persist cycle failure before returning
            failed = self.crm.complete_autopilot_cycle(
                cycle["id"], metrics=metrics, error=str(exc)[:1000]
            )
            return {"success": False, "cycle": failed}
