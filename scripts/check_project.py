from __future__ import annotations

import tomllib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
required = [
    root / "app" / "server.py",
    root / "app" / "conversation_agent.py",
    root / "app" / "whatsapp_service.py",
    root / "upstream" / "Scrapegraph-ai" / "scrapegraphai" / "__init__.py",
    root / "upstream" / "whatsapp-mcp" / "whatsapp-bridge" / "main.go",
    root / "upstream" / "whatsapp-mcp" / "whatsapp-mcp-server" / "whatsapp.py",
    root / "docker-compose.yml",
    root / ".github" / "workflows" / "deploy.yml",
    root / "DEPLOY.md",
    root / "deploy" / "nginx" / "ollum-sales.conf",
    root / "deploy" / "remote_deploy.sh",
    root / "deploy" / "remote_rollback.sh",
]
missing = [str(p.relative_to(root)) for p in required if not p.exists()]
if missing:
    raise SystemExit(f"Missing required paths: {missing}")
with (root / "pyproject.toml").open("rb") as f:
    project = tomllib.load(f)["project"]
print(f"OK: {project['name']} {project['version']}")
