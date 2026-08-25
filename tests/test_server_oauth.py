from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_real_server_exposes_oauth_metadata_and_challenges_mcp(tmp_path) -> None:
    code = r"""
import json

from starlette.testclient import TestClient

import app.server as server

with TestClient(server.app, base_url="https://sales.example") as client:
    metadata = client.get("/.well-known/oauth-protected-resource/mcp")
    unauthorized = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    status_tool = server.mcp._tool_manager._tools["ollum_status"]
    send_tool = server.mcp._tool_manager._tools["whatsapp_send_message"]
    evaluate_reply_tool = server.mcp._tool_manager._tools["sales_evaluate_whatsapp_reply"]
    compare_replies_tool = server.mcp._tool_manager._tools["sales_compare_whatsapp_replies"]
    save_reply_tool = server.mcp._tool_manager._tools["sales_save_whatsapp_reply_draft"]
    onboarding_tool = server.mcp._tool_manager._tools["sales_get_company_onboarding"]
    update_profile_tool = server.mcp._tool_manager._tools["sales_update_company_profile"]
    archive_knowledge_tool = server.mcp._tool_manager._tools["sales_archive_company_knowledge"]
    link_inbox_tool = server.mcp._tool_manager._tools["sales_link_agent_inbox_lead"]
    retry_inbox_tool = server.mcp._tool_manager._tools["sales_retry_agent_inbox_event"]
    next_action_tool = server.mcp._tool_manager._tools["sales_agent_next_action"]
    coordination_tool = server.mcp._tool_manager._tools["sales_get_agent_coordination"]
    playbook_tool = server.mcp._tool_manager._tools["sales_get_chatgpt_agent_playbook"]
    prepare_batch_tool = server.mcp._tool_manager._tools["sales_prepare_conversation_batch"]
    prepare_persisted_tool = server.mcp._tool_manager._tools[
        "sales_prepare_persisted_conversation"
    ]
    submit_decision_tool = server.mcp._tool_manager._tools["sales_submit_conversation_decision"]
    print(json.dumps({
        "metadata_status": metadata.status_code,
        "metadata": metadata.json(),
        "unauthorized_status": unauthorized.status_code,
        "challenge": unauthorized.headers.get("www-authenticate"),
        "tool_count": len(server.mcp._tool_manager._tools),
        "status_annotations": status_tool.annotations.model_dump(by_alias=True),
        "status_meta": status_tool.meta,
        "send_annotations": send_tool.annotations.model_dump(by_alias=True),
        "send_meta": send_tool.meta,
        "evaluate_reply_annotations": evaluate_reply_tool.annotations.model_dump(by_alias=True),
        "evaluate_reply_meta": evaluate_reply_tool.meta,
        "compare_replies_annotations": compare_replies_tool.annotations.model_dump(by_alias=True),
        "compare_replies_meta": compare_replies_tool.meta,
        "save_reply_annotations": save_reply_tool.annotations.model_dump(by_alias=True),
        "save_reply_meta": save_reply_tool.meta,
        "onboarding_annotations": onboarding_tool.annotations.model_dump(by_alias=True),
        "update_profile_annotations": update_profile_tool.annotations.model_dump(by_alias=True),
        "archive_knowledge_annotations": archive_knowledge_tool.annotations.model_dump(by_alias=True),
        "link_inbox_annotations": link_inbox_tool.annotations.model_dump(by_alias=True),
        "retry_inbox_annotations": retry_inbox_tool.annotations.model_dump(by_alias=True),
        "retry_inbox_meta": retry_inbox_tool.meta,
        "next_action_annotations": next_action_tool.annotations.model_dump(by_alias=True),
        "coordination_annotations": coordination_tool.annotations.model_dump(by_alias=True),
        "playbook_annotations": playbook_tool.annotations.model_dump(by_alias=True),
        "prepare_batch_annotations": prepare_batch_tool.annotations.model_dump(by_alias=True),
        "prepare_persisted_annotations": prepare_persisted_tool.annotations.model_dump(
            by_alias=True
        ),
        "prepare_persisted_meta": prepare_persisted_tool.meta,
        "submit_decision_annotations": submit_decision_tool.annotations.model_dump(by_alias=True),
    }))
"""
    env = os.environ.copy()
    env.update(
        {
            "OLLUM_AUTH_MODE": "oidc",
            "OLLUM_PUBLIC_BASE_URL": "https://sales.example",
            "OLLUM_MCP_RESOURCE_URL": "https://sales.example/mcp",
            "OLLUM_OIDC_ISSUER_URL": "https://identity.example/",
            "OLLUM_OIDC_AUDIENCE": "https://api.example",
            "OLLUM_ADMIN_ENABLED": "false",
            "OLLUM_CRM_DB_PATH": str(tmp_path / "oauth.db"),
            "PYTHONWARNINGS": "error",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload["metadata_status"] == 200
    assert payload["metadata"] == {
        "resource": "https://sales.example/mcp",
        "authorization_servers": ["https://identity.example/"],
        "scopes_supported": ["sales:read", "sales:write"],
        "bearer_methods_supported": ["header"],
    }
    assert payload["unauthorized_status"] == 401
    assert "resource_metadata=" in payload["challenge"]
    assert payload["tool_count"] == 69
    assert payload["status_annotations"]["readOnlyHint"] is True
    assert payload["status_annotations"]["destructiveHint"] is False
    assert payload["status_meta"]["securitySchemes"][0]["scopes"] == ["sales:read"]
    assert payload["send_annotations"]["readOnlyHint"] is False
    assert payload["send_annotations"]["destructiveHint"] is True
    assert payload["send_meta"]["securitySchemes"][0]["scopes"] == ["sales:write"]
    assert payload["evaluate_reply_annotations"]["readOnlyHint"] is True
    assert payload["evaluate_reply_annotations"]["openWorldHint"] is True
    assert payload["evaluate_reply_meta"]["securitySchemes"][0]["scopes"] == [
        "sales:read"
    ]
    assert payload["compare_replies_annotations"]["readOnlyHint"] is True
    assert payload["compare_replies_annotations"]["openWorldHint"] is True
    assert payload["compare_replies_meta"]["securitySchemes"][0]["scopes"] == [
        "sales:read"
    ]
    assert payload["save_reply_annotations"]["readOnlyHint"] is False
    assert payload["save_reply_annotations"]["destructiveHint"] is False
    assert payload["save_reply_annotations"]["openWorldHint"] is True
    assert payload["save_reply_meta"]["securitySchemes"][0]["scopes"] == ["sales:write"]
    assert payload["onboarding_annotations"]["readOnlyHint"] is True
    assert payload["update_profile_annotations"]["readOnlyHint"] is False
    assert payload["update_profile_annotations"]["destructiveHint"] is False
    assert payload["archive_knowledge_annotations"]["destructiveHint"] is True
    assert payload["link_inbox_annotations"]["readOnlyHint"] is False
    assert payload["link_inbox_annotations"]["destructiveHint"] is False
    assert payload["retry_inbox_annotations"]["readOnlyHint"] is False
    assert payload["retry_inbox_annotations"]["destructiveHint"] is False
    assert payload["retry_inbox_meta"]["securitySchemes"][0]["scopes"] == [
        "sales:write"
    ]
    assert payload["next_action_annotations"]["readOnlyHint"] is True
    assert payload["coordination_annotations"]["readOnlyHint"] is True
    assert payload["coordination_annotations"]["openWorldHint"] is False
    assert payload["playbook_annotations"]["readOnlyHint"] is True
    assert payload["prepare_batch_annotations"]["readOnlyHint"] is False
    assert payload["prepare_batch_annotations"]["openWorldHint"] is True
    assert payload["prepare_persisted_annotations"]["readOnlyHint"] is False
    assert payload["prepare_persisted_annotations"]["destructiveHint"] is False
    assert payload["prepare_persisted_annotations"]["openWorldHint"] is False
    assert payload["prepare_persisted_meta"]["securitySchemes"][0]["scopes"] == [
        "sales:write"
    ]
    assert payload["submit_decision_annotations"]["readOnlyHint"] is False
    assert payload["submit_decision_annotations"]["destructiveHint"] is False


def test_real_server_hosts_chatgpt_dynamic_client_registration(tmp_path) -> None:
    code = r"""
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient

import app.server as server

with TestClient(server.app, base_url="https://mcp.sales.example") as client:
    code_verifier = "v" * 64
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip("=")
    protected = client.get("/.well-known/oauth-protected-resource/mcp")
    authorization_server = client.get("/.well-known/oauth-authorization-server")
    registration = client.post(
        "/register",
        json={
            "redirect_uris": ["https://chatgpt.com/connector/oauth/integration"],
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "sales:read sales:write",
            "client_name": "Ollum Sales Integration",
        },
    )
    registered = registration.json()
    authorize = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registered.get("client_id"),
            "redirect_uri": "https://chatgpt.com/connector/oauth/integration",
            "scope": "sales:read sales:write",
            "state": "integration-state",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "resource": "https://mcp.sales.example/mcp",
        },
        follow_redirects=False,
    )
    consent = client.get(authorize.headers.get("location", ""), follow_redirects=False)
    request_id = parse_qs(urlsplit(authorize.headers["location"]).query)["request_id"][0]
    callback_url = server._mcp_auth.provider.complete_authorization(
        request_id,
        subject="auth0|integration-user",
        approved=True,
    )
    authorization_code = parse_qs(urlsplit(callback_url).query)["code"][0]
    server.crm.authorize_workspace_identity(
        workspace_id="ollum-group",
        workspace_name="Ollum Group",
        subject="auth0|integration-user",
        email="owner@example.com",
        display_name="Integration Owner",
        bootstrap_allowed=True,
        owner_emails=("owner@example.com",),
    )
    token_response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "client_secret": registered["client_secret"],
            "code": authorization_code,
            "redirect_uri": "https://chatgpt.com/connector/oauth/integration",
            "code_verifier": code_verifier,
            "resource": "https://mcp.sales.example/mcp",
        },
    )
    tokens = token_response.json()
    initialize = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {tokens.get('access_token', '')}",
            "Accept": "application/json, text/event-stream",
            "Host": "127.0.0.1:8000",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "integration", "version": "1"},
            },
        },
    )
    refreshed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registered["client_id"],
            "client_secret": registered["client_secret"],
            "refresh_token": tokens.get("refresh_token", ""),
            "scope": "sales:read",
        },
    )
    print(json.dumps({
        "protected_status": protected.status_code,
        "protected": protected.json(),
        "authorization_status": authorization_server.status_code,
        "authorization": authorization_server.json(),
        "registration_status": registration.status_code,
        "registered": registered,
        "authorize_status": authorize.status_code,
        "authorize_location": authorize.headers.get("location"),
        "consent_status": consent.status_code,
        "consent_location": consent.headers.get("location"),
        "token_status": token_response.status_code,
        "token_type": tokens.get("token_type"),
        "has_refresh_token": bool(tokens.get("refresh_token")),
        "initialize_status": initialize.status_code,
        "refresh_status": refreshed.status_code,
        "refresh_scope": refreshed.json().get("scope"),
    }))
"""
    env = os.environ.copy()
    env.update(
        {
            "OLLUM_AUTH_MODE": "oidc",
            "OLLUM_PUBLIC_BASE_URL": "https://mcp.sales.example",
            "OLLUM_DASHBOARD_BASE_URL": "https://api.sales.example",
            "OLLUM_OIDC_REDIRECT_BASE_URL": "https://mcp.sales.example",
            "OLLUM_MCP_RESOURCE_URL": "https://mcp.sales.example/mcp",
            "OLLUM_OIDC_ISSUER_URL": "https://identity.example/",
            "OLLUM_OIDC_AUDIENCE": "https://identity-api.example",
            "OLLUM_OAUTH_DCR_ENABLED": "true",
            "OLLUM_OAUTH_STORAGE_SECRET": "integration-oauth-storage-secret-48-characters-long",
            "OLLUM_OAUTH_ALLOWED_REDIRECT_HOSTS": "chatgpt.com",
            "OLLUM_ADMIN_ENABLED": "true",
            "OLLUM_ADMIN_OIDC_CLIENT_ID": "admin-client",
            "OLLUM_ADMIN_OIDC_CLIENT_SECRET": "admin-client-secret",
            "OLLUM_ADMIN_ALLOWED_EMAILS": "owner@example.com",
            "OLLUM_ADMIN_SESSION_SECRET": "integration-session-secret-with-32-bytes",
            "OLLUM_DEFAULT_WORKSPACE_ID": "ollum-group",
            "OLLUM_DEFAULT_WORKSPACE_NAME": "Ollum Group",
            "OLLUM_CRM_DB_PATH": str(tmp_path / "oauth-dcr.db"),
            "PYTHONWARNINGS": "error",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload["protected_status"] == 200
    assert payload["protected"]["authorization_servers"] == [
        "https://mcp.sales.example/"
    ]
    assert payload["authorization_status"] == 200
    assert payload["authorization"]["registration_endpoint"] == (
        "https://mcp.sales.example/register"
    )
    assert payload["authorization"]["authorization_endpoint"] == (
        "https://mcp.sales.example/authorize"
    )
    assert payload["authorization"]["token_endpoint"] == (
        "https://mcp.sales.example/token"
    )
    assert payload["registration_status"] == 201
    assert payload["registered"]["client_id"]
    assert payload["registered"]["client_secret"]
    assert payload["authorize_status"] == 302
    assert payload["authorize_location"].startswith(
        "https://api.sales.example/oauth/authorize?request_id="
    )
    assert payload["consent_status"] == 303
    assert payload["consent_location"] == "/auth/login"
    assert payload["token_status"] == 200
    assert payload["token_type"] == "Bearer"
    assert payload["has_refresh_token"] is True
    assert payload["initialize_status"] == 200
    assert payload["refresh_status"] == 200
    assert payload["refresh_scope"] == "sales:read"
