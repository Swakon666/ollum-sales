from __future__ import annotations

import logging
import signal
import time

from .agent_inbox import sync_whatsapp_inbox
from .autopilot import AutopilotService
from .config import settings
from .conversation_agent import ConversationAgent
from .crm import SalesCRM
from .google_sheets import GoogleSheetsSync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ollum-sales-worker")
stopping = False


def _stop(_signum: int, _frame: object) -> None:
    global stopping
    stopping = True


def create_service() -> AutopilotService:
    crm = SalesCRM(settings.crm_db_path)
    crm.ensure_workspace(settings.default_workspace_id, settings.default_workspace_name)
    sheets = GoogleSheetsSync(
        crm,
        enabled=settings.google_sheets_enabled,
        spreadsheet_id=settings.google_sheets_spreadsheet_id,
        service_account_file=settings.google_service_account_file,
        retry_attempts=settings.retry_attempts,
        retry_base_delay_seconds=settings.retry_base_delay_seconds,
    )
    return AutopilotService(crm, settings, sheets)


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    service = create_service()
    conversation_agent = ConversationAgent(service.crm, settings)
    logger.info("worker started; SAFE remains the default mode")
    while not stopping:
        try:
            inbox = sync_whatsapp_inbox(
                service.crm,
                settings.default_workspace_id,
                scan_limit=100,
            )
            if inbox["new_events"]:
                logger.info(
                    "queued %s new WhatsApp inbound event(s)",
                    inbox["new_events"],
                )
            dialogue = conversation_agent.process_pending(
                settings.default_workspace_id,
                limit=settings.conversation_agent_batch_size,
            )
            if dialogue["drafts_created"] or dialogue["escalated"]:
                logger.info(
                    "conversation agent created %s draft(s), escalated %s event(s)",
                    dialogue["drafts_created"],
                    dialogue["escalated"],
                )
            state = service.status()
            if state["running"]:
                result = service.run_cycle()
                if result.get("success"):
                    logger.info("autopilot cycle completed")
                elif not result.get("blocked"):
                    logger.warning("autopilot cycle failed: %s", result)
        except Exception:
            logger.exception("worker iteration failed")
        deadline = time.monotonic() + max(
            5, int(settings.conversation_agent_poll_seconds)
        )
        while not stopping and time.monotonic() < deadline:
            time.sleep(1)
    logger.info("worker stopped")


if __name__ == "__main__":
    main()
