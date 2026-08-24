#!/usr/bin/env bash
# shellcheck shell=bash
set -Eeuo pipefail
umask 077

die() {
  if [[ ${deployment_started:-false} == true \
    && ${deployment_committed:-false} != true ]] \
    && declare -F restore_previous >/dev/null; then
    restore_previous || true
  fi
  printf 'deployment error: %s\n' "$1" >&2
  exit 1
}

deployment_started=false
deployment_committed=false
shared_env_backup=''
shared_google_credentials_backup=''
nginx_backup=''
nginx_changed=false

rollback_unexpected_error() {
  local status=$?
  if [[ $deployment_started == true && $deployment_committed != true ]] \
    && declare -F restore_previous >/dev/null; then
    restore_previous || true
  fi
  exit "$status"
}
trap rollback_unexpected_error ERR

[[ $EUID -eq 0 ]] || die 'remote_deploy.sh must run through sudo'
[[ $# -eq 5 || $# -eq 7 || $# -eq 8 || $# -eq 9 ]] \
  || die 'expected deploy user, domain, release id, incoming path, bind port, and optional prebuilt image metadata'

deploy_user=$1
domain=$2
release_id=$3
incoming_relative=$4
bind_port=$5
prebuilt_image_tag=${6:-}
expected_prebuilt_image_id=${7:-}
expected_prebuilt_archive_sha256=${8:-}
api_domain=${9:-$domain}

[[ $deploy_user =~ ^[a-z_][a-z0-9_-]*$ ]] || die 'invalid deploy user'
[[ $domain =~ ^[A-Za-z0-9.-]+$ ]] || die 'invalid domain'
[[ $api_domain =~ ^[A-Za-z0-9.-]+$ ]] || die 'invalid API domain'
[[ $release_id =~ ^[A-Fa-f0-9-]+$ ]] || die 'invalid release id'
[[ $incoming_relative =~ ^\.ollum-sales-incoming/[A-Za-z0-9._-]+$ ]] || die 'invalid incoming path'
[[ $bind_port =~ ^[0-9]{2,5}$ ]] || die 'invalid bind port'
if [[ -n $prebuilt_image_tag || -n $expected_prebuilt_image_id \
  || -n $expected_prebuilt_archive_sha256 ]]; then
  [[ $prebuilt_image_tag =~ ^[A-Za-z0-9._-]+$ ]] || die 'invalid prebuilt image tag'
  [[ $expected_prebuilt_image_id =~ ^sha256:[A-Fa-f0-9]{64}$ ]] \
    || die 'invalid expected prebuilt image ID'
  if [[ -n $expected_prebuilt_archive_sha256 ]]; then
    [[ $expected_prebuilt_archive_sha256 =~ ^[A-Fa-f0-9]{64}$ ]] \
      || die 'invalid expected prebuilt archive SHA-256'
  fi
fi

deploy_home=$(getent passwd "$deploy_user" | cut -d: -f6)
deploy_group=$(id -gn "$deploy_user")
[[ -n $deploy_home && -d $deploy_home ]] || die 'deploy user home was not found'

deploy_root="$deploy_home/ollum-sales"
incoming_dir="$deploy_home/$incoming_relative"
case "$incoming_dir" in
  "$deploy_home"/.ollum-sales-incoming/*) ;;
  *) die 'incoming path escaped the deploy user home' ;;
esac

archive="$incoming_dir/release.tgz"
incoming_env="$incoming_dir/production.env"
incoming_google_credentials="$incoming_dir/google-service-account.json"
prebuilt_image_archive="$incoming_dir/ollum-sales-mcp-image.tar.gz"
[[ -f $archive ]] || die 'release archive is missing'
[[ -f $incoming_env ]] || die 'production environment file is missing'
[[ -f $incoming_google_credentials ]] || die 'Google service-account credential is missing'
if [[ -n $prebuilt_image_tag ]]; then
  [[ -f $prebuilt_image_archive ]] || die 'prebuilt MCP image archive is missing'
fi

cleanup_incoming_credentials() {
  rm -f -- "$incoming_env" "$incoming_google_credentials"
  rm -f -- \
    "$shared_env_backup" \
    "$shared_google_credentials_backup" \
    "$nginx_backup"
}
trap cleanup_incoming_credentials EXIT

command -v nginx >/dev/null || die 'existing Nginx installation is required on this server'
command -v certbot >/dev/null || die 'Certbot is required on this server'

install_docker() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  . /etc/os-release
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  install_docker
fi

if ss -H -ltn "sport = :$bind_port" 2>/dev/null | grep -q .; then
  if ! docker ps --format '{{.Names}} {{.Ports}}' \
    | grep -Eq "^ollum-sales-.*127\\.0\\.0\\.1:${bind_port}->8000/tcp"; then
    die "localhost port $bind_port is already owned by another service"
  fi
fi

install -d -m 0750 -o "$deploy_user" -g "$deploy_group" "$deploy_root"
install -d -m 0750 -o "$deploy_user" -g "$deploy_group" \
  "$deploy_root/releases" "$deploy_root/shared"
install -d -m 0700 -o "$deploy_user" -g "$deploy_group" \
  "$deploy_root/shared/secrets"

shared_env="$deploy_root/shared/.env"
shared_google_credentials="$deploy_root/shared/secrets/ollum-google-service-account.json"

install_runtime_google_credentials() {
  local source_file=$1
  [[ -f $source_file ]] || die 'Google service-account credential source is missing'
  install -m 0400 -o 10001 -g 10001 \
    "$source_file" "$shared_google_credentials"
  [[ $(stat -c '%u:%g:%a' "$shared_google_credentials") == '10001:10001:400' ]] \
    || die 'Google service-account credential permissions are unsafe'
}

if [[ -f $shared_env ]]; then
  shared_env_backup=$(mktemp)
  cp --preserve=mode,ownership,timestamps "$shared_env" "$shared_env_backup"
fi
if [[ -f $shared_google_credentials ]]; then
  shared_google_credentials_backup=$(mktemp)
  cp --preserve=mode,ownership,timestamps \
    "$shared_google_credentials" "$shared_google_credentials_backup"
fi

restore_shared_configuration() {
  if [[ -n $shared_env_backup && -f $shared_env_backup ]]; then
    install -m 0600 -o "$deploy_user" -g "$deploy_group" \
      "$shared_env_backup" "$shared_env"
  else
    rm -f -- "$shared_env"
  fi
  if [[ -n $shared_google_credentials_backup \
    && -f $shared_google_credentials_backup ]]; then
    install_runtime_google_credentials "$shared_google_credentials_backup"
  else
    rm -f -- "$shared_google_credentials"
  fi
}

release_dir="$deploy_root/releases/$release_id"
[[ ! -e $release_dir ]] || die "release already exists: $release_id"

if tar -tzf "$archive" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  die 'release archive contains an unsafe path'
fi

mkdir -m 0750 "$release_dir"
tar -xzf "$archive" -C "$release_dir"
chown -R "$deploy_user:$deploy_group" "$release_dir"

install -m 0600 -o "$deploy_user" -g "$deploy_group" \
  "$incoming_env" "$shared_env"
install_runtime_google_credentials "$incoming_google_credentials"
ln -sfn ../../shared/.env "$release_dir/.env"
chown -h "$deploy_user:$deploy_group" "$release_dir/.env"

previous_release=''
if [[ -L $deploy_root/current ]]; then
  previous_release=$(readlink -f "$deploy_root/current")
  case "$previous_release" in
    "$deploy_root"/releases/*) ;;
    *) die 'current release symlink points outside the release directory' ;;
  esac
fi

build_fingerprint() {
  local source_dir=$1
  (
    cd "$source_dir"
    find \
      .dockerignore \
      Dockerfile.mcp \
      Dockerfile.whatsapp \
      docker-compose.yml \
      pyproject.toml \
      app \
      upstream \
      -type f -print0 \
      | LC_ALL=C sort -z \
      | xargs -0 sha256sum
  ) | sha256sum | awk '{print $1}'
}

cd "$release_dir"
docker compose config --quiet

declare -a prior_ollum_image_ids=()
previous_mcp_image_id=''
while IFS= read -r image; do
  if image_id=$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null); then
    prior_ollum_image_ids+=("$image_id")
    if [[ $image == *ollum-sales-mcp* ]]; then
      previous_mcp_image_id=$image_id
    fi
  fi
done < <(docker compose config --images)

prebuilt_images=false
if [[ -n $prebuilt_image_tag ]]; then
  gzip -t "$prebuilt_image_archive" || die 'prebuilt MCP image archive is invalid'
  if [[ -n $expected_prebuilt_archive_sha256 ]]; then
    printf '%s  %s\n' \
      "$expected_prebuilt_archive_sha256" "$prebuilt_image_archive" \
      | sha256sum --check --strict \
      || die 'prebuilt MCP image archive SHA-256 does not match the verified artifact'
  fi
  gzip -dc "$prebuilt_image_archive" | docker load
  loaded_image="ollum-sales-ollum-sales-mcp:$prebuilt_image_tag"
  loaded_image_id=$(docker image inspect --format '{{.Id}}' "$loaded_image" 2>/dev/null) \
    || die 'expected prebuilt MCP image tag was not loaded'
  if [[ $loaded_image_id != "$expected_prebuilt_image_id" ]]; then
    if [[ -z $expected_prebuilt_archive_sha256 ]]; then
      die 'loaded MCP image ID does not match the verified artifact'
    fi
    printf '%s\n' \
      'Docker normalized the loaded image ID; the verified archive SHA-256 remains the trust anchor.'
  fi
  mapfile -t runtime_images < <(
    docker compose config --images | grep -E 'ollum-sales-(mcp|worker)$'
  )
  [[ ${#runtime_images[@]} -eq 2 ]] \
    || die 'could not identify both MCP and worker image names'
  for runtime_image in "${runtime_images[@]}"; do
    docker tag "$loaded_image" "$runtime_image"
  done
  while IFS= read -r image; do
    docker image inspect "$image" >/dev/null 2>&1 \
      || die "required prebuilt deployment image is unavailable: $image"
  done < <(docker compose config --images)
  prebuilt_images=true
  printf 'Loaded verified prebuilt MCP image %s\n' "$loaded_image_id"
fi

reuse_images=false
if [[ $prebuilt_images == false && -n $previous_release ]]; then
  current_fingerprint=$(build_fingerprint "$release_dir")
  previous_fingerprint=$(build_fingerprint "$previous_release")
  if [[ $current_fingerprint == "$previous_fingerprint" ]]; then
    reuse_images=true
    while IFS= read -r image; do
      if ! docker image inspect "$image" >/dev/null 2>&1; then
        reuse_images=false
        break
      fi
    done < <(docker compose config --images)
  fi
fi

available_kb=$(df -Pk / | awk 'NR==2 {print $4}')
if [[ $prebuilt_images == true ]]; then
  (( available_kb >= 512 * 1024 )) \
    || die 'at least 512 MiB of free disk space is required for a prebuilt deployment'
  printf 'Using verified prebuilt Ollum Sales MCP and worker image\n'
elif [[ $reuse_images == true ]]; then
  (( available_kb >= 256 * 1024 )) \
    || die 'at least 256 MiB of free disk space is required when reusing images'
  printf 'Reusing unchanged Ollum Sales images from release %s\n' \
    "$(basename "$previous_release")"
else
  if (( available_kb >= 1536 * 1024 )); then
    docker compose build
  else
    (( available_kb >= 384 * 1024 )) \
      || die 'at least 384 MiB of free disk space is required for an overlay build'
    mcp_image=$(docker compose config --images | grep 'ollum-sales-mcp' | head -1)
    [[ -n $mcp_image ]] || die 'could not identify the existing MCP image'
    docker image inspect "$mcp_image" >/dev/null 2>&1 \
      || die 'an existing MCP image is required for a low-disk overlay build'
    [[ -f deploy/Dockerfile.mcp-overlay ]] \
      || die 'low-disk MCP overlay Dockerfile is missing'
    docker run --rm --entrypoint python "$mcp_image" -c \
      'import bs4, mcp, pydantic, requests, uvicorn' \
      || die 'existing MCP image does not contain the required runtime dependencies'
    docker build \
      --file deploy/Dockerfile.mcp-overlay \
      --build-arg "OLLUM_BASE_IMAGE=$mcp_image" \
      --tag "$mcp_image" \
      .
    printf 'Built a low-disk MCP application overlay from %s\n' "$mcp_image"
  fi
fi

restore_previous() {
  deployment_started=false
  restore_shared_configuration
  if [[ $nginx_changed == true ]]; then
    if [[ -n $nginx_backup && -f $nginx_backup ]]; then
      install -m 0644 "$nginx_backup" "$nginx_available"
      ln -sfn "$nginx_available" "$nginx_enabled"
    else
      rm -f -- "$nginx_enabled" "$nginx_available"
    fi
    if nginx -t >/dev/null 2>&1; then
      systemctl reload nginx || true
    fi
    nginx_changed=false
  fi
  if [[ -n $previous_release && -f $previous_release/docker-compose.yml ]]; then
    printf 'Restoring previous release %s\n' "$(basename "$previous_release")" >&2
    if [[ -n $previous_mcp_image_id ]]; then
      docker tag "$previous_mcp_image_id" ollum-sales-ollum-sales-mcp:latest
    fi
    cd "$previous_release"
    docker compose up -d --remove-orphans
    ln -sfn "$previous_release" "$deploy_root/current"
  elif [[ -f $release_dir/docker-compose.yml ]]; then
    cd "$release_dir"
    docker compose down --remove-orphans || true
  fi
}

ensure_runtime_volume_ownership() {
  local runtime_image
  runtime_image=$(docker compose config --images | grep 'ollum-sales-mcp' | head -1)
  [[ -n $runtime_image ]] || die 'could not identify the MCP image for volume migration'
  docker run --rm \
    --user 0:0 \
    --entrypoint /bin/sh \
    --volume ollum-sales-crm-data:/data/crm \
    --volume ollum-sales-whatsapp-data:/data/whatsapp \
    "$runtime_image" \
    -ec 'chown -R 10001:10001 /data/crm /data/whatsapp' \
    || die 'could not migrate persistent volume ownership to the runtime user'
}

ensure_runtime_volume_ownership

if ! docker compose up -d --remove-orphans --force-recreate; then
  restore_previous
  die 'docker compose up failed'
fi
deployment_started=true

healthy=false
for _attempt in $(seq 1 60); do
  if curl -fsS --max-time 5 "http://127.0.0.1:$bind_port/health" >/dev/null; then
    healthy=true
    break
  fi
  sleep 5
done

if [[ $healthy != true ]]; then
  docker compose ps >&2 || true
  docker compose logs --tail=100 ollum-sales-mcp >&2 || true
  restore_previous
  die 'local MCP health check failed'
fi

if [[ -n $previous_release ]]; then
  ln -sfn "$previous_release" "$deploy_root/previous"
fi
ln -sfn "$release_dir" "$deploy_root/current"
chown -h "$deploy_user:$deploy_group" "$deploy_root/current" "$deploy_root/previous" 2>/dev/null || true

nginx_available=/etc/nginx/sites-available/ollum-sales
nginx_enabled=/etc/nginx/sites-enabled/ollum-sales
nginx_template="$release_dir/deploy/nginx/ollum-sales.conf"
[[ -f $nginx_template ]] || die 'Nginx template is missing from the release'

if [[ -e $nginx_available ]] && ! grep -q '^# Managed by the Ollum Sales deployment workflow\.$' "$nginx_available"; then
  restore_previous
  die 'refusing to overwrite an unmanaged Nginx configuration'
fi
if [[ -L $nginx_enabled && $(readlink -f "$nginx_enabled") != "$nginx_available" ]]; then
  restore_previous
  die 'refusing to replace an unrelated enabled Nginx site'
fi

if [[ -f $nginx_available ]]; then
  nginx_backup=$(mktemp)
  cp --preserve=mode,ownership,timestamps "$nginx_available" "$nginx_backup"
fi
nginx_candidate=$(mktemp)
sed \
  -e "s/__OLLUM_DOMAIN__/$domain/g" \
  -e "s/__OLLUM_API_DOMAIN__/$api_domain/g" \
  -e "s/__OLLUM_MCP_BIND_PORT__/$bind_port/g" \
  "$nginx_template" > "$nginx_candidate"
install -m 0644 "$nginx_candidate" "$nginx_available"
nginx_changed=true
rm -f "$nginx_candidate"
ln -sfn "$nginx_available" "$nginx_enabled"

if ! nginx -t; then
  restore_previous
  die 'Nginx configuration validation failed'
fi
systemctl reload nginx

certbot_domains=(-d "$domain")
if [[ $api_domain != "$domain" ]]; then
  certbot_domains+=(-d "$api_domain")
fi
if ! certbot --nginx \
  --non-interactive \
  --agree-tos \
  --register-unsafely-without-email \
  --redirect \
  --expand \
  --keep-until-expiring \
  "${certbot_domains[@]}"; then
  restore_previous
  die 'TLS certificate provisioning failed'
fi

curl -fsS --max-time 20 \
  --resolve "$domain:443:127.0.0.1" \
  "https://$domain/health" >/dev/null \
  || die 'public HTTPS health check failed'

unauthorized_status=$(curl -sS --max-time 20 \
  --resolve "$domain:443:127.0.0.1" \
  -o /dev/null -w '%{http_code}' "https://$domain/mcp")
[[ $unauthorized_status == 401 ]] || die "unauthenticated MCP check returned HTTP $unauthorized_status"

api_health_status=$(curl -sS --max-time 20 \
  --resolve "$api_domain:443:127.0.0.1" \
  -o /dev/null -w '%{http_code}' "https://$api_domain/health")
[[ $api_health_status == 200 ]] \
  || die "API-domain health check returned HTTP $api_health_status"

admin_enabled=$(sed -n 's/^OLLUM_ADMIN_ENABLED=//p' \
  "$deploy_root/shared/.env" | tail -1)
if [[ $admin_enabled == true ]]; then
  admin_status=$(curl -sS --max-time 20 \
    --resolve "$api_domain:443:127.0.0.1" \
    -o /dev/null -w '%{http_code}' "https://$api_domain/admin")
  [[ $admin_status == 303 || $admin_status == 307 ]] \
    || die "unauthenticated dashboard check returned HTTP $admin_status"

  session_status=$(curl -sS --max-time 20 \
    --resolve "$api_domain:443:127.0.0.1" \
    -o /dev/null -w '%{http_code}' "https://$api_domain/api/v1/session")
  [[ $session_status == 401 ]] \
    || die "unauthenticated versioned API check returned HTTP $session_status"
fi

auth_mode=$(sed -n 's/^OLLUM_AUTH_MODE=//p' "$deploy_root/shared/.env" | tail -1)
case "$auth_mode" in
  oidc)
    expected_resource=$(sed -n 's/^OLLUM_MCP_RESOURCE_URL=//p' \
      "$deploy_root/shared/.env" | tail -1)
    expected_issuer=$(sed -n 's/^OLLUM_OIDC_ISSUER_URL=//p' \
      "$deploy_root/shared/.env" | tail -1)
    [[ -n $expected_resource && -n $expected_issuer ]] \
      || die 'OIDC resource or issuer is missing from production environment'

    metadata_response=$(mktemp)
    metadata_status=$(curl -sS --max-time 20 \
      --resolve "$domain:443:127.0.0.1" \
      -o "$metadata_response" \
      -w '%{http_code}' \
      "https://$domain/.well-known/oauth-protected-resource/mcp")
    [[ $metadata_status == 200 ]] \
      || die "OAuth protected-resource metadata returned HTTP $metadata_status"
    if ! python3 - "$metadata_response" "$expected_resource" "$expected_issuer" <<'PY'
import json
import sys

path, expected_resource, expected_issuer = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    metadata = json.load(handle)
if metadata.get("resource") != expected_resource:
    raise SystemExit(1)
if expected_issuer not in metadata.get("authorization_servers", []):
    raise SystemExit(1)
if not {"sales:read", "sales:write"}.issubset(
    set(metadata.get("scopes_supported", []))
):
    raise SystemExit(1)
PY
    then
      rm -f "$metadata_response"
      die 'OAuth protected-resource metadata is inconsistent with production settings'
    fi
    rm -f "$metadata_response"

    challenge_headers=$(mktemp)
    challenge_status=$(curl -sS --max-time 20 \
      --resolve "$domain:443:127.0.0.1" \
      -D "$challenge_headers" \
      -o /dev/null \
      -w '%{http_code}' \
      "https://$domain/mcp")
    if [[ $challenge_status != 401 ]] \
      || ! grep -Eqi '^www-authenticate:.*resource_metadata=' "$challenge_headers"; then
      rm -f "$challenge_headers"
      die 'OIDC MCP challenge is missing resource_metadata'
    fi
    rm -f "$challenge_headers"
    ;;
  bearer)
    mcp_token=$(sed -n 's/^OLLUM_MCP_BEARER_TOKEN=//p' \
      "$deploy_root/shared/.env" | head -1)
    [[ -n $mcp_token ]] \
      || die 'MCP bearer token is missing from production environment'
    mcp_response=$(mktemp)
    authenticated_status=$(curl -sS --max-time 30 \
      --resolve "$domain:443:127.0.0.1" \
      -o "$mcp_response" \
      -w '%{http_code}' \
      -H "Authorization: Bearer $mcp_token" \
      -H 'Content-Type: application/json' \
      -H 'Accept: application/json, text/event-stream' \
      --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"deploy-check","version":"1"}}}' \
      "https://$domain/mcp")
    rm -f "$mcp_response"
    unset mcp_token
    [[ $authenticated_status == 200 ]] \
      || die "authenticated MCP check returned HTTP $authenticated_status"
    ;;
  *)
    die "unsupported production authentication mode: $auth_mode"
    ;;
esac

mapfile -t active_ollum_image_ids < <(
  while IFS= read -r image; do
    docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true
  done < <(docker compose config --images)
)
for prior_image_id in "${prior_ollum_image_ids[@]}"; do
  if printf '%s\n' "${active_ollum_image_ids[@]}" | grep -Fxq "$prior_image_id"; then
    continue
  fi
  if [[ -z $(docker ps -aq --filter "ancestor=$prior_image_id") ]]; then
    docker image rm "$prior_image_id" >/dev/null 2>&1 || true
  fi
done

docker compose ps
deployment_committed=true
nginx_changed=false
rm -f -- "$archive" "$prebuilt_image_archive"
rmdir "$incoming_dir" 2>/dev/null || true
printf 'Deployment completed: release=%s domain=%s api_domain=%s\n' \
  "$release_id" "$domain" "$api_domain"
