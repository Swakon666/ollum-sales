#!/usr/bin/env bash
set -euo pipefail

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die 'deploy_whatsapp_prebuilt.sh must run through sudo'
[[ $# -eq 3 ]] || die 'expected deploy user, binary path, and SHA-256'

deploy_user=$1
binary=$2
expected_sha=$3

[[ $deploy_user =~ ^[a-z_][a-z0-9_-]*$ ]] || die 'invalid deploy user'
[[ $expected_sha =~ ^[A-Fa-f0-9]{64}$ ]] || die 'invalid artifact SHA-256'

deploy_home=$(getent passwd "$deploy_user" | cut -d: -f6)
[[ -n $deploy_home && -d $deploy_home ]] || die 'deploy user home was not found'

binary=$(readlink -f "$binary")
case "$binary" in
  "$deploy_home"/.ollum-sales-incoming/*) ;;
  *) die 'binary path is outside the verified incoming directory' ;;
esac
[[ -f $binary ]] || die 'prebuilt WhatsApp binary is missing'

actual_sha=$(sha256sum "$binary" | awk '{print $1}')
[[ $actual_sha == "$expected_sha" ]] || die 'prebuilt WhatsApp binary SHA-256 mismatch'
elf_machine=$(od -An -tu2 -j18 -N2 "$binary" | tr -d ' ')
[[ $elf_machine == 62 ]] || die 'prebuilt WhatsApp binary is not x86-64 ELF'

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
docker image inspect "$compose_image" >/dev/null 2>&1 || die 'current WhatsApp bridge image is unavailable'

previous_image_id=$(docker image inspect --format '{{.Id}}' "$compose_image")
docker image tag "$compose_image" ollum-sales-whatsapp-bridge:previous

build_context=$(mktemp -d)
cleanup() {
  rm -rf -- "$build_context"
}
trap cleanup EXIT
install -m 0755 "$binary" "$build_context/whatsapp-bridge"
cat > "$build_context/Dockerfile" <<'DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY --chmod=0755 whatsapp-bridge /usr/local/bin/whatsapp-bridge
DOCKERFILE

candidate_image="ollum-sales-whatsapp-bridge:candidate-$expected_sha"
docker build \
  --build-arg "BASE_IMAGE=$compose_image" \
  --tag "$candidate_image" \
  "$build_context"
candidate_image_id=$(docker image inspect --format '{{.Id}}' "$candidate_image")
docker image tag "$candidate_image" "$compose_image"

restore_previous() {
  printf 'Restoring previous WhatsApp bridge image.\n' >&2
  docker image tag ollum-sales-whatsapp-bridge:previous "$compose_image"
  docker compose up -d --no-deps --force-recreate --no-build whatsapp-bridge || true
}

if ! docker compose up -d --no-deps --force-recreate --no-build whatsapp-bridge; then
  restore_previous
  die 'WhatsApp bridge recreation failed'
fi

sleep 5
container_id=$(docker compose ps -q whatsapp-bridge)
[[ -n $container_id ]] || {
  restore_previous
  die 'WhatsApp bridge container is missing'
}
container_status=$(docker inspect --format '{{.State.Status}}' "$container_id")
[[ $container_status == running ]] || {
  docker compose logs --tail=80 whatsapp-bridge >&2 || true
  restore_previous
  die "WhatsApp bridge is not running: $container_status"
}
container_image_id=$(docker inspect --format '{{.Image}}' "$container_id")
[[ $container_image_id == "$candidate_image_id" ]] || {
  restore_previous
  die 'WhatsApp bridge container did not start from the verified overlay image'
}

rm -f -- "$binary"
rmdir "$(dirname "$binary")" 2>/dev/null || true
printf 'WhatsApp bridge updated independently; previous image=%s, persistent volume preserved, sending disabled.\n' "$previous_image_id"
