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
import sys
import types

from starlette.testclient import TestClient

package = types.ModuleType("scrapegraphai")
graphs = types.ModuleType("scrapegraphai.graphs")
class SmartScraperGraph:
    pass
graphs.SmartScraperGraph = SmartScraperGraph
package.graphs = graphs
sys.modules["scrapegraphai"] = package
sys.modules["scrapegraphai.graphs"] = graphs

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
    assert payload["tool_count"] == 47
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
    assert payload["save_reply_meta"]["securitySchemes"][0]["scopes"] == [
        "sales:write"
    ]
