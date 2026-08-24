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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(
        item.strip() for item in raw.replace(" ", ",").split(",") if item.strip()
    )


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
    auth_mode: str = os.getenv("OLLUM_AUTH_MODE", "bearer").strip().lower()
    public_base_url: str | None = os.getenv("OLLUM_PUBLIC_BASE_URL")
    dashboard_base_url: str | None = os.getenv("OLLUM_DASHBOARD_BASE_URL")
    oidc_redirect_base_url: str | None = os.getenv("OLLUM_OIDC_REDIRECT_BASE_URL")
    mcp_resource_url: str | None = os.getenv("OLLUM_MCP_RESOURCE_URL")
    mcp_required_scopes: tuple[str, ...] = _env_csv(
        "OLLUM_MCP_REQUIRED_SCOPES", "sales:read,sales:write"
    )
    oauth_dcr_enabled: bool = _env_bool("OLLUM_OAUTH_DCR_ENABLED", False)
    oauth_storage_secret: str | None = os.getenv("OLLUM_OAUTH_STORAGE_SECRET")
    oauth_allowed_redirect_hosts: tuple[str, ...] = tuple(
        host.lower()
        for host in _env_csv("OLLUM_OAUTH_ALLOWED_REDIRECT_HOSTS", "chatgpt.com")
    )
    oauth_access_token_ttl_seconds: int = int(
        os.getenv("OLLUM_OAUTH_ACCESS_TOKEN_TTL_SECONDS", "3600")
    )
    oauth_refresh_token_ttl_seconds: int = int(
        os.getenv("OLLUM_OAUTH_REFRESH_TOKEN_TTL_SECONDS", "2592000")
    )
    oauth_authorization_code_ttl_seconds: int = int(
        os.getenv("OLLUM_OAUTH_AUTHORIZATION_CODE_TTL_SECONDS", "300")
    )
    oidc_issuer_url: str | None = os.getenv("OLLUM_OIDC_ISSUER_URL")
    oidc_audience: str | None = os.getenv("OLLUM_OIDC_AUDIENCE")
    oidc_jwks_url: str | None = os.getenv("OLLUM_OIDC_JWKS_URL")
    oidc_algorithms: tuple[str, ...] = _env_csv("OLLUM_OIDC_ALGORITHMS", "RS256")
    oidc_allowed_subjects: tuple[str, ...] = _env_csv("OLLUM_OIDC_ALLOWED_SUBJECTS")
    admin_enabled: bool = _env_bool("OLLUM_ADMIN_ENABLED", False)
    admin_oidc_client_id: str | None = os.getenv("OLLUM_ADMIN_OIDC_CLIENT_ID")
    admin_oidc_client_secret: str | None = os.getenv("OLLUM_ADMIN_OIDC_CLIENT_SECRET")
    admin_allowed_emails: tuple[str, ...] = tuple(
        email.lower() for email in _env_csv("OLLUM_ADMIN_ALLOWED_EMAILS")
    )
    admin_read_scope: str = os.getenv("OLLUM_ADMIN_READ_SCOPE", "sales:read").strip()
    admin_write_scope: str = os.getenv("OLLUM_ADMIN_WRITE_SCOPE", "sales:write").strip()
    admin_session_secret: str | None = os.getenv("OLLUM_ADMIN_SESSION_SECRET")
    admin_session_max_age_seconds: int = int(
        os.getenv("OLLUM_ADMIN_SESSION_MAX_AGE_SECONDS", "28800")
    )
    default_workspace_id: str = os.getenv(
        "OLLUM_DEFAULT_WORKSPACE_ID", "ollum-group"
    ).strip()
    default_workspace_name: str = os.getenv(
        "OLLUM_DEFAULT_WORKSPACE_NAME", "Ollum Group"
    ).strip()
    workspace_owner_emails: tuple[str, ...] = tuple(
        email.lower() for email in _env_csv("OLLUM_WORKSPACE_OWNER_EMAILS")
    )
    conversation_agent_enabled: bool = _env_bool(
        "OLLUM_CONVERSATION_AGENT_ENABLED", True
    )
    conversation_agent_poll_seconds: int = int(
        os.getenv("OLLUM_CONVERSATION_AGENT_POLL_SECONDS", "900")
    )
    conversation_agent_batch_size: int = int(
        os.getenv("OLLUM_CONVERSATION_AGENT_BATCH_SIZE", "3")
    )
    serper_api_key: str | None = os.getenv("SERPER_API_KEY")
    crm_db_path: str = os.getenv("OLLUM_CRM_DB_PATH", str(DEFAULT_CRM_DB))
    company_search_timeout: int = int(os.getenv("OLLUM_COMPANY_SEARCH_TIMEOUT", "20"))
    website_inspection_timeout: int = int(
        os.getenv("OLLUM_WEBSITE_INSPECTION_TIMEOUT", "20")
    )
    evidence_ttl_hours: int = int(os.getenv("OLLUM_EVIDENCE_TTL_HOURS", "168"))
    retry_attempts: int = int(os.getenv("OLLUM_RETRY_ATTEMPTS", "3"))
    retry_base_delay_seconds: float = float(
        os.getenv("OLLUM_RETRY_BASE_DELAY_SECONDS", "0.5")
    )
    whatsapp_db_path: str = os.getenv("WHATSAPP_MESSAGES_DB_PATH", str(DEFAULT_WA_DB))
    whatsapp_api_base_url: str = os.getenv(
        "WHATSAPP_API_BASE_URL", "http://localhost:8080/api"
    )
    allow_whatsapp_send: bool = os.getenv(
        "OLLUM_ALLOW_WHATSAPP_SEND", "false"
    ).lower() in {"1", "true", "yes", "on"}
    whatsapp_test_recipients: tuple[str, ...] = _env_csv(
        "OLLUM_WHATSAPP_TEST_RECIPIENTS"
    )
    autopilot_default_mode: str = (
        os.getenv("OLLUM_AUTOPILOT_DEFAULT_MODE", "safe").strip().lower()
    )
    autopilot_interval_minutes: int = int(
        os.getenv("OLLUM_AUTOPILOT_INTERVAL_MINUTES", "60")
    )
    autopilot_max_verticals_per_cycle: int = int(
        os.getenv("OLLUM_AUTOPILOT_MAX_VERTICALS_PER_CYCLE", "2")
    )
    autopilot_leads_per_vertical: int = int(
        os.getenv("OLLUM_AUTOPILOT_LEADS_PER_VERTICAL", "10")
    )
    autopilot_score_threshold: int = int(
        os.getenv("OLLUM_AUTOPILOT_SCORE_THRESHOLD", "65")
    )
    autopilot_min_training_leads: int = int(
        os.getenv("OLLUM_AUTOPILOT_MIN_TRAINING_LEADS", "100")
    )
    allow_autopilot_send: bool = os.getenv(
        "OLLUM_AUTOPILOT_ALLOW_SEND", "false"
    ).lower() in {"1", "true", "yes", "on"}
    google_sheets_enabled: bool = os.getenv(
        "OLLUM_GOOGLE_SHEETS_ENABLED", "false"
    ).lower() in {"1", "true", "yes", "on"}
    google_sheets_spreadsheet_id: str | None = os.getenv("OLLUM_GOOGLE_SHEETS_ID")
    google_service_account_file: str | None = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_FILE"
    ) or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")


settings = Settings()
