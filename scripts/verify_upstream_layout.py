from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
required = [
    root / "upstream" / "whatsapp-mcp" / "README.md",
    root / "upstream" / "whatsapp-mcp" / "whatsapp-bridge" / "main.go",
    root / "upstream" / "whatsapp-mcp" / "whatsapp-mcp-server" / "whatsapp.py",
    root / "upstream" / "Scrapegraph-ai" / "README.md",
    root / "upstream" / "Scrapegraph-ai" / "pyproject.toml",
    root / "upstream" / "Scrapegraph-ai" / "scrapegraphai" / "__init__.py",
]
missing = [str(p.relative_to(root)) for p in required if not p.exists()]
if missing:
    print("Missing upstream files:")
    for item in missing:
        print(" -", item)
    sys.exit(1)
print("Upstream layout OK")
