import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "ollum-sales"


def _load_json(relative_path: str) -> dict:
    return json.loads((PLUGIN_ROOT / relative_path).read_text(encoding="utf-8"))


def _mcp_tool_names() -> set[str]:
    tree = ast.parse((REPO_ROOT / "app" / "server.py").read_text(encoding="utf-8"))
    tool_names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            callable_node = (
                decorator.func if isinstance(decorator, ast.Call) else decorator
            )
            if isinstance(callable_node, ast.Name) and callable_node.id in {
                "_read_tool",
                "_write_tool",
            }:
                tool_names.add(node.name)
    return tool_names


def test_codex_plugin_manifest_is_release_ready() -> None:
    manifest = _load_json(".codex-plugin/plugin.json")

    assert manifest["name"] == "ollum-sales"
    assert manifest["version"] == "0.4.0"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["developerName"] == "Ollum Group"
    assert isinstance(manifest["interface"]["defaultPrompt"], list)
    assert len(manifest["interface"]["defaultPrompt"]) <= 3


def test_codex_plugin_uses_oauth_without_legacy_bearer_override() -> None:
    server = _load_json(".mcp.json")["mcpServers"]["ollum-sales"]

    assert server["url"] == "https://mcp.ollumgroup.ru/mcp"
    assert "bearer_token_env_var" not in server
    assert server["default_tools_approval_mode"] == "prompt"


def test_codex_plugin_prompts_for_external_send_and_operational_gates() -> None:
    tools = _load_json(".mcp.json")["mcpServers"]["ollum-sales"]["tools"]
    guarded_tools = {
        "autopilot_start",
        "autopilot_stop",
        "google_sheets_sync",
        "sales_approve_outreach_draft",
        "sales_send_whatsapp_draft",
        "whatsapp_send_message",
    }

    assert tools.keys() == _mcp_tool_names()
    assert all(tools[name]["approval_mode"] == "prompt" for name in guarded_tools)
    safe_tools = tools.keys() - guarded_tools
    assert all(tools[name]["approval_mode"] == "approve" for name in safe_tools)


def test_codex_plugin_skill_preserves_chatgpt_brain_and_two_step_send() -> None:
    skill = (PLUGIN_ROOT / "skills" / "ollum-sales" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    canonical_skill = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert skill == canonical_skill
    assert "sales_prepare_conversation_batch" in skill
    assert "sales_submit_conversation_decision" in skill
    assert "The server never calls an LLM API" in skill
    assert "sales_approve_outreach_draft" in skill
    assert "sales_send_whatsapp_draft" in skill
    assert "separate explicit send confirmation" in skill
