#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() {
  printf 'deployment error: %s\n' "$1" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die 'remote_deploy.sh must run through sudo'
[[ $# -eq 5 ]] || die 'expected deploy user, domain, release id, incoming path, and bind port'

deploy_user=$1
domain=$2
release_id=$3
incoming_relative=$4
bind_port=$5

[[ $deploy_user =~ ^[a-z_][a-z0-9_-]*$ ]] || die 'invalid deploy user'
[[ $domain =~ ^[A-Za-z0-9.-]+$ ]] || die 'invalid domain'
[[ $release_id =~ ^[A-Fa-f0-9-]+$ ]] || die 'invalid release id'
[[ $incoming_relative =~ ^\.ollum-sales-incoming/[A-Za-z0-9._-]+$ ]] || die 'invalid incoming path'
[[ $bind_port =~ ^[0-9]{2,5}$ ]] || die 'invalid bind port'

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
[[ -f $archive ]] || die 'release archive is missing'
[[ -f $incoming_env ]] || die 'production environment file is missing'

cleanup_incoming_secret() {
  rm -f -- "$incoming_env"
}
trap cleanup_incoming_secret EXIT

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

release_dir="$deploy_root/releases/$release_id"
[[ ! -e $release_dir ]] || die "release already exists: $release_id"

if tar -tzf "$archive" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  die 'release archive contains an unsafe path'
fi

mkdir -m 0750 "$release_dir"
tar -xzf "$archive" -C "$release_dir"
chown -R "$deploy_user:$deploy_group" "$release_dir"

install -m 0600 -o "$deploy_user" -g "$deploy_group" \
  "$incoming_env" "$deploy_root/shared/.env"
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
while IFS= read -r image; do
  if image_id=$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null); then
    prior_ollum_image_ids+=("$image_id")
  fi
done < <(docker compose config --images)

reuse_images=false
if [[ -n $previous_release ]]; then
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
if [[ $reuse_images == true ]]; then
  (( available_kb >= 1024 * 1024 )) \
    || die 'at least 1 GiB of free disk space is required when reusing images'
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
      'import bs4, mcp, pydantic, requests, scrapegraphai, uvicorn' \
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
  if [[ -n $previous_release && -f $previous_release/docker-compose.yml ]]; then
    printf 'Restoring previous release %s\n' "$(basename "$previous_release")" >&2
    cd "$previous_release"
    docker compose build
    docker compose up -d --remove-orphans
    ln -sfn "$previous_release" "$deploy_root/current"
  fi
}

if ! docker compose up -d --remove-orphans; then
  restore_previous
  die 'docker compose up failed'
fi

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

nginx_backup=''
if [[ -f $nginx_available ]]; then
  nginx_backup=$(mktemp)
  cp --preserve=mode,ownership,timestamps "$nginx_available" "$nginx_backup"
fi
nginx_candidate=$(mktemp)
sed \
  -e "s/__OLLUM_DOMAIN__/$domain/g" \
  -e "s/__OLLUM_MCP_BIND_PORT__/$bind_port/g" \
  "$nginx_template" > "$nginx_candidate"
install -m 0644 "$nginx_candidate" "$nginx_available"
rm -f "$nginx_candidate"
ln -sfn "$nginx_available" "$nginx_enabled"

if ! nginx -t; then
  if [[ -n $nginx_backup ]]; then
    install -m 0644 "$nginx_backup" "$nginx_available"
  else
    rm -f "$nginx_enabled" "$nginx_available"
  fi
  rm -f "$nginx_backup"
  restore_previous
  die 'Nginx configuration validation failed'
fi
rm -f "$nginx_backup"
systemctl reload nginx

certbot --nginx \
  --non-interactive \
  --agree-tos \
  --register-unsafely-without-email \
  --redirect \
  --keep-until-expiring \
  -d "$domain"

curl -fsS --max-time 20 \
  --resolve "$domain:443:127.0.0.1" \
  "https://$domain/health" >/dev/null \
  || die 'public HTTPS health check failed'

unauthorized_status=$(curl -sS --max-time 20 \
  --resolve "$domain:443:127.0.0.1" \
  -o /dev/null -w '%{http_code}' "https://$domain/mcp")
[[ $unauthorized_status == 401 ]] || die "unauthenticated MCP check returned HTTP $unauthorized_status"

mcp_token=$(sed -n 's/^OLLUM_MCP_BEARER_TOKEN=//p' "$deploy_root/shared/.env" | head -1)
[[ -n $mcp_token ]] || die 'MCP bearer token is missing from production environment'
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
[[ $authenticated_status == 200 ]] || die "authenticated MCP check returned HTTP $authenticated_status"

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
rm -f -- "$archive"
rmdir "$incoming_dir" 2>/dev/null || true
printf 'Deployment completed: release=%s domain=%s\n' "$release_id" "$domain"
