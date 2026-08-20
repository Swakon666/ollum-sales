#!/usr/bin/env bash
set -euo pipefail

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die 'rollback_whatsapp.sh must run through sudo'
[[ $# -eq 1 ]] || die 'expected deploy user'

deploy_user=$1
[[ $deploy_user =~ ^[a-z_][a-z0-9_-]*$ ]] || die 'invalid deploy user'
deploy_home=$(getent passwd "$deploy_user" | cut -d: -f6)
[[ -n $deploy_home && -d $deploy_home ]] || die 'deploy user home was not found'

deploy_root="$deploy_home/ollum-sales"
current_release=$(readlink -f "$deploy_root/current")
case "$current_release" in
  "$deploy_root"/releases/*) ;;
  *) die 'current release is unavailable or points outside the release directory' ;;
esac

send_flag=$(grep -E '^OLLUM_ALLOW_WHATSAPP_SEND=' "$deploy_root/shared/.env" | tail -1 | cut -d= -f2- || true)
[[ $send_flag == false ]] || die 'OLLUM_ALLOW_WHATSAPP_SEND must remain false'

cd "$current_release"
docker compose config --quiet
compose_image=$(docker compose config --images | grep -E 'ollum-sales.*whatsapp-bridge' | head -1)
[[ -n $compose_image ]] || die 'could not identify the WhatsApp bridge image name'
docker image inspect ollum-sales-whatsapp-bridge:previous >/dev/null 2>&1 \
  || die 'previous WhatsApp bridge image is unavailable'

docker image tag ollum-sales-whatsapp-bridge:previous "$compose_image"
docker compose up -d --no-deps --force-recreate --no-build whatsapp-bridge
sleep 5

container_id=$(docker compose ps -q whatsapp-bridge)
[[ -n $container_id ]] || die 'WhatsApp bridge container is missing after rollback'
container_status=$(docker inspect --format '{{.State.Status}}' "$container_id")
[[ $container_status == running ]] || die "WhatsApp bridge rollback failed: $container_status"

printf 'WhatsApp bridge rolled back independently; persistent volume preserved and sending remains disabled.\n'
