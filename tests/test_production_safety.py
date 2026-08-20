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
