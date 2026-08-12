from pathlib import Path

root = Path(__file__).resolve().parents[1]
skill = root / "SKILL.md"
assert skill.is_file(), "SKILL.md must exist at the skill root"
text = skill.read_text(encoding="utf-8")
assert text.startswith("---\n"), "SKILL.md should start with YAML frontmatter"
assert "name: ollum-sales" in text
assert "description:" in text
print("OK: root SKILL.md and required metadata are present")
