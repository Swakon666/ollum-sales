#!/usr/bin/env bash
# shellcheck shell=bash
set -Eeuo pipefail

die() {
  printf 'Prospecting reset error: %s\n' "$1" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die 'reset must run through sudo'
[[ $# -eq 5 ]] || die 'expected deploy user, three exact counts, and confirmation'

deploy_user=$1
expected_prospecting_leads=$2
expected_campaigns=$3
expected_inbox_events=$4
confirmation=$5

[[ $deploy_user =~ ^[a-z_][a-z0-9_-]*$ ]] || die 'invalid deploy user'
[[ $expected_prospecting_leads =~ ^[0-9]+$ ]] || die 'invalid prospecting lead count'
[[ $expected_campaigns =~ ^[0-9]+$ ]] || die 'invalid campaign count'
[[ $expected_inbox_events =~ ^[0-9]+$ ]] || die 'invalid inbox event count'
expected_confirmation="RESET ${expected_prospecting_leads} ${expected_campaigns} PRESERVE ${expected_inbox_events}"
[[ $confirmation == "$expected_confirmation" ]] || die 'confirmation does not match exact counts'

deploy_home=$(getent passwd "$deploy_user" | cut -d: -f6)
deploy_root="$deploy_home/ollum-sales"
current_release=$(readlink -f "$deploy_root/current")
case "$current_release" in
  "$deploy_root"/releases/*) ;;
  *) die 'current release is missing or unsafe' ;;
esac

cd "$current_release"
docker compose config --quiet
docker compose ps --status running --services | grep -Fxq 'ollum-sales-mcp' \
  || die 'ollum-sales-mcp is not running'

docker compose exec -T \
  -e RESET_EXPECTED_PROSPECTING_LEADS="$expected_prospecting_leads" \
  -e RESET_EXPECTED_CAMPAIGNS="$expected_campaigns" \
  -e RESET_EXPECTED_INBOX_EVENTS="$expected_inbox_events" \
  -e RESET_ACTOR="github-actions:${GITHUB_RUN_ID:-manual}" \
  ollum-sales-mcp python - <<'PY'
import json
import os

from app.config import settings
from app.crm import SalesCRM


def emit(name: str, value: object) -> None:
    print(
        f"{name}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
    )


expected_leads = int(os.environ["RESET_EXPECTED_PROSPECTING_LEADS"])
expected_campaigns = int(os.environ["RESET_EXPECTED_CAMPAIGNS"])
expected_inbox = int(os.environ["RESET_EXPECTED_INBOX_EVENTS"])
actor = os.environ["RESET_ACTOR"]

if settings.allow_whatsapp_send or settings.allow_autopilot_send:
    raise RuntimeError("both production send flags must remain disabled")

crm = SalesCRM(settings.crm_db_path)
preview = crm.preview_prospecting_reset()
emit("RESET_PREVIEW", preview)
if preview["prospecting_leads"] != expected_leads:
    raise RuntimeError("prospecting lead count changed before reset")
if preview["campaigns"] != expected_campaigns:
    raise RuntimeError("campaign count changed before reset")
if preview["inbox_events"] != expected_inbox:
    raise RuntimeError("inbox event count changed before reset")
if preview["autopilot_running"]:
    raise RuntimeError("Autopilot must be stopped before reset")
if preview["pending_prospecting_sends"]:
    raise RuntimeError("pending prospecting send requests exist")

result = crm.reset_prospecting_data(
    expected_prospecting_leads=expected_leads,
    expected_campaigns=expected_campaigns,
    actor=actor,
)
after = crm.preview_prospecting_reset()
onboarding = crm.get_company_onboarding_state(settings.default_workspace_id)

if after["prospecting_leads"] != 0 or after["campaigns"] != 0:
    raise RuntimeError("prospecting reset did not produce a clean pipeline")
if after["inbox_events"] != expected_inbox:
    raise RuntimeError("inbox events were not preserved")
if not onboarding["sales_ready"]:
    raise RuntimeError("company onboarding was not preserved")

emit("RESET_RESULT", result)
emit(
    "RESET_VERIFICATION",
    {
        "prospecting_leads": after["prospecting_leads"],
        "campaigns": after["campaigns"],
        "inbox_events": after["inbox_events"],
        "company_sales_ready": onboarding["sales_ready"],
        "whatsapp_send_enabled": settings.allow_whatsapp_send,
        "autopilot_send_enabled": settings.allow_autopilot_send,
        "backup_id": result["backup_id"],
        "restorable": result["restorable"],
    },
)
PY
