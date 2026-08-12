from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WA_DB = (
    REPO_ROOT
    / "upstream"
    / "whatsapp-mcp"
    / "whatsapp-bridge"
    / "store"
    / "messages.db"
)
DEFAULT_CRM_DB = REPO_ROOT / "data" / "ollum-sales.db"


@dataclass(frozen=True)
class Settings:
    mcp_host: str = os.getenv("MCP_HOST", "0.0.0.0")
    mcp_port: int = int(os.getenv("MCP_PORT", "8000"))
    mcp_require_auth: bool = os.getenv("OLLUM_MCP_REQUIRE_AUTH", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    mcp_bearer_token: str | None = os.getenv("OLLUM_MCP_BEARER_TOKEN")
    scrapegraph_model: str = os.getenv("SCRAPEGRAPH_MODEL", "openai/gpt-4o-mini")
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    llm_api_key: str | None = os.getenv("LLM_API_KEY")
    serper_api_key: str | None = os.getenv("SERPER_API_KEY")
    crm_db_path: str = os.getenv("OLLUM_CRM_DB_PATH", str(DEFAULT_CRM_DB))
    company_search_timeout: int = int(os.getenv("OLLUM_COMPANY_SEARCH_TIMEOUT", "20"))
    website_inspection_timeout: int = int(
        os.getenv("OLLUM_WEBSITE_INSPECTION_TIMEOUT", "20")
    )
    whatsapp_db_path: str = os.getenv("WHATSAPP_MESSAGES_DB_PATH", str(DEFAULT_WA_DB))
    whatsapp_api_base_url: str = os.getenv(
        "WHATSAPP_API_BASE_URL", "http://localhost:8080/api"
    )
    allow_whatsapp_send: bool = os.getenv(
        "OLLUM_ALLOW_WHATSAPP_SEND", "false"
    ).lower() in {"1", "true", "yes", "on"}


settings = Settings()
