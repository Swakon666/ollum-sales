#!/usr/bin/env bash
# shellcheck shell=bash
set -Eeuo pipefail

die() {
  printf 'Autopilot report error: %s\n' "$1" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die 'report must run through sudo'
[[ $# -eq 1 ]] || die 'expected deploy user'

deploy_user=$1
[[ $deploy_user =~ ^[a-z_][a-z0-9_-]*$ ]] || die 'invalid deploy user'
deploy_home=$(getent passwd "$deploy_user" | cut -d: -f6)
deploy_root="$deploy_home/ollum-sales"
current_release=$(readlink -f "$deploy_root/current")
case "$current_release" in
  "$deploy_root"/releases/*) ;;
  *) die 'current release is missing or unsafe' ;;
esac

cd "$current_release"
docker compose exec -T ollum-sales-mcp python - <<'PY'
import json

from app.production_check import (
    _build_services,
    _cycle_sheet_rows,
    _sheet_row_counts,
)


def emit(name, value):
    print(f"{name}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}")


crm, sheets, autopilot = _build_services()
with crm.connect() as connection:
    latest = connection.execute(
        """
        SELECT id FROM autopilot_cycles
        WHERE status = 'completed'
        ORDER BY completed_at DESC
        LIMIT 1
        """
    ).fetchone()
    if latest is None:
        raise RuntimeError("no completed Autopilot cycle was found")
    artifact_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM leads
        WHERE source = 'production-safe-check'
        """
    ).fetchone()[0]

if artifact_count:
    raise RuntimeError("legacy production-safe-check artifacts remain in the CRM")

cycle = crm.get_autopilot_cycle(latest["id"])
state = autopilot.status()
vertical_by_id = {item["id"]: item for item in crm.list_verticals(enabled=True, limit=500)}
selected_verticals = [
    {
        "id": vertical_id,
        "name": vertical_by_id.get(vertical_id, {}).get("name"),
        "last_selected_at": vertical_by_id.get(vertical_id, {}).get("last_selected_at"),
    }
    for vertical_id in cycle["selected_verticals"]
]
rotation = [
    {
        "id": item["id"],
        "name": item["name"],
        "last_selected_at": item["last_selected_at"],
        "selected": item["id"] in set(cycle["selected_verticals"]),
    }
    for item in vertical_by_id.values()
]

emit(
    "AUTOPILOT_CYCLE",
    {
        "id": cycle["id"],
        "mode": cycle["mode"],
        "status": cycle["status"],
        "started_at": cycle["started_at"],
        "completed_at": cycle["completed_at"],
        "verticals_processed": len(cycle["selected_verticals"]),
        "selected_verticals": selected_verticals,
    },
)
emit("AUTOPILOT_METRICS", cycle["metrics"])
emit("AUTOPILOT_CYCLE_SHEET_ROWS", _cycle_sheet_rows(crm, cycle["id"]))
emit("AUTOPILOT_SHEET_TOTALS", _sheet_row_counts(sheets))
emit(
    "AUTOPILOT_VERIFICATION_ARTIFACTS",
    {
        "remaining": artifact_count,
        "clean": artifact_count == 0,
    },
)
emit(
    "AUTOPILOT_STATE",
    {
        "running": state["running"],
        "mode": state["mode"],
        "interval_minutes": state["interval_minutes"],
        "next_cycle_at": state["next_cycle_at"],
        "whatsapp_send_flag": state["whatsapp_send_flag"],
        "non_safe_send_flag": state["non_safe_send_flag"],
        "pending_send_requests": state["pending_send_requests"],
        "google_sheets": state["google_sheets"],
    },
)
emit("AUTOPILOT_ROTATION", rotation)
PY
