from __future__ import annotations

from pathlib import Path

from app.production_check import _verify_safe_send_guardrail

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class FakeCRM:
    def __init__(self) -> None:
        self.requests = [
            {
                "id": "request-1",
                "draft_id": "draft-1",
                "status": "pending",
            }
        ]

    def list_pending_send_requests(self, *, limit: int) -> list[dict[str, str]]:
        assert limit == 200
        return [dict(item) for item in self.requests]


class FakeAutopilot:
    def _process_send_requests(self, *, mode: str) -> dict[str, object]:
        assert mode == "safe"
        return {
            "pending": 1,
            "sent": 0,
            "failed": 0,
            "blocked": True,
        }


def test_production_send_guardrail_is_read_only() -> None:
    crm = FakeCRM()

    result = _verify_safe_send_guardrail(crm, FakeAutopilot())  # type: ignore[arg-type]

    assert result["pending_requests"] == 1
    assert result["pending_requests_unchanged"] is True
    assert crm.requests[0]["status"] == "pending"


def test_production_deployment_requires_manual_dispatch() -> None:
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    trigger_block = workflow.split("permissions:", 1)[0]

    assert "\n  workflow_dispatch:" in trigger_block
    assert "\n  push:" not in trigger_block
    assert "github.event_name == 'push'" not in workflow
    assert "DEPLOY_MODE: ${{ inputs.mode }}" in workflow


def test_direct_whatsapp_tool_cannot_bypass_persistent_draft_flow() -> None:
    server = (REPOSITORY_ROOT / "app" / "server.py").read_text(encoding="utf-8")
    direct_tool = server.split("def whatsapp_send_message(", 1)[1].split("\n@_", 1)[0]

    assert "Direct WhatsApp sending is disabled" in direct_tool
    assert "return send_message(" not in direct_tool


def test_ranking_defaults_to_fresh_evidence_only() -> None:
    server = (REPOSITORY_ROOT / "app" / "server.py").read_text(encoding="utf-8")
    rank_tool = server.split("def sales_rank_leads(", 1)[1].split("\n@_", 1)[0]

    assert "include_stale: bool = False" in rank_tool
    assert "fresh_evidence_only=not include_stale" in rank_tool


def test_nginx_template_hides_version_and_sets_security_headers() -> None:
    nginx = (REPOSITORY_ROOT / "deploy" / "nginx" / "ollum-sales.conf").read_text(
        encoding="utf-8"
    )

    assert "server_tokens off;" in nginx
    assert "Strict-Transport-Security" in nginx
    assert "X-Content-Type-Options" in nginx
    assert "Referrer-Policy" in nginx
    assert "location ^~ /.well-known/" in nginx
    assert "location ^~ /api/admin/" in nginx
    assert "location ^~ /api/v1/" in nginx
    assert "server_name __OLLUM_DOMAIN__ __OLLUM_API_DOMAIN__;" in nginx
    assert "location = /" in nginx


def test_closed_beta_admin_exposes_no_send_endpoint_or_flag_toggle() -> None:
    admin = (REPOSITORY_ROOT / "app" / "admin.py").read_text(encoding="utf-8")

    assert '"send_controls_exposed": False' in admin
    assert "/send" not in admin
    assert "allow_whatsapp_send =" not in admin
    assert "allow_autopilot_send =" not in admin


def test_closed_beta_admin_requires_oidc_at_startup() -> None:
    server = (REPOSITORY_ROOT / "app" / "server.py").read_text(encoding="utf-8")

    assert 'settings.auth_mode != "oidc"' in server
    assert "The closed-beta admin requires OLLUM_AUTH_MODE=oidc" in server


def test_repository_dependency_automation_covers_all_manifests() -> None:
    dependabot = (REPOSITORY_ROOT / ".github" / "dependabot.yml").read_text(
        encoding="utf-8"
    )

    for ecosystem in ("pip", "gomod", "docker", "github-actions"):
        assert f"package-ecosystem: {ecosystem}" in dependabot


def test_prebuilt_deploy_uses_archive_digest_as_cross_version_trust_anchor() -> None:
    deploy = (REPOSITORY_ROOT / "deploy" / "remote_deploy.sh").read_text(
        encoding="utf-8"
    )
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )

    assert "expected_prebuilt_archive_sha256=${8:-}" in deploy
    assert "api_domain=${9:-$domain}" in deploy
    assert "sha256sum --check --strict" in deploy
    assert "verified archive SHA-256 remains the trust anchor" in deploy
    assert 'rm -f -- "$incoming_env" "$incoming_google_credentials"' in deploy
    assert (
        'rm -f -- "$incoming_env" "$incoming_google_credentials" '
        '"$prebuilt_image_archive"'
    ) not in deploy
    assert "'$PREBUILT_IMAGE_TAG' '$PREBUILT_IMAGE_ID' '$PREBUILT_SHA256'" in workflow
    assert "OLLUM_DASHBOARD_BASE_URL" in workflow
    assert "https://$api_domain/api/v1/session" in workflow


def test_failed_deploy_restores_shared_configuration_and_nginx() -> None:
    deploy = (REPOSITORY_ROOT / "deploy" / "remote_deploy.sh").read_text(
        encoding="utf-8"
    )

    assert "restore_shared_configuration" in deploy
    assert "rollback_unexpected_error" in deploy
    assert "nginx_changed=true" in deploy
    assert "deployment_committed=true" in deploy


def test_whatsapp_bridge_logs_no_private_message_data() -> None:
    bridge = (
        REPOSITORY_ROOT / "upstream" / "whatsapp-mcp" / "whatsapp-bridge" / "main.go"
    ).read_text(encoding="utf-8")

    forbidden_log_fragments = (
        'fmt.Printf("[%s] %s %s:',
        "Attempting to download media for message",
        "Successfully downloaded %s media to %s",
        'logger.Infof("Message content:',
        'logger.Infof("Stored message:',
        'logger.Infof("Using existing chat name for %s:',
    )
    for fragment in forbidden_log_fragments:
        assert fragment not in bridge

    assert "Never place private message content" in bridge
    assert "Successfully downloaded media (%d bytes)" in bridge
