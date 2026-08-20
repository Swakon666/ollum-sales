from __future__ import annotations

import logging
import signal
import time

from .autopilot import AutopilotService
from .config import settings
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
    logger.info("worker started; SAFE remains the default mode")
    while not stopping:
        try:
            state = service.status()
            if state["running"]:
                result = service.run_cycle()
                if result.get("success"):
                    logger.info("autopilot cycle completed")
                elif not result.get("blocked"):
                    logger.warning("autopilot cycle failed: %s", result)
        except Exception:
            logger.exception("worker iteration failed")
        deadline = time.monotonic() + 30
        while not stopping and time.monotonic() < deadline:
            time.sleep(1)
    logger.info("worker stopped")


if __name__ == "__main__":
    main()
