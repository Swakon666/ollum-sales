#!/usr/bin/env bash
set -u

section() {
  printf '\n== %s ==\n' "$1"
}

section identity
id
uname -a
if [[ -r /etc/os-release ]]; then
  grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release
fi

section capacity
printf 'cpu_online=%s\n' "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo unknown)"
if [[ -r /proc/loadavg ]]; then
  printf 'loadavg=%s\n' "$(cut -d' ' -f1-3 /proc/loadavg)"
fi
df -hT /
df -ih /
free -h 2>/dev/null || true

section privileges
if sudo -n true 2>/dev/null; then
  echo 'passwordless_sudo=yes'
else
  echo 'passwordless_sudo=no'
fi

section runtimes
command -v git >/dev/null && git --version || echo 'git=absent'
command -v docker >/dev/null && docker --version || echo 'docker=absent'
if command -v docker >/dev/null; then
  docker compose version 2>/dev/null || echo 'docker_compose=absent-or-unavailable'
fi
command -v nginx >/dev/null && nginx -v 2>&1 || echo 'nginx=absent'
command -v certbot >/dev/null && certbot --version 2>&1 || echo 'certbot=absent'

section existing_services
for unit in docker nginx; do
  if command -v systemctl >/dev/null; then
    printf '%s=' "$unit"
    systemctl is-active "$unit" 2>/dev/null || true
  fi
done

section containers
deploy_root="$HOME/ollum-sales"
current_release=""
docker_cmd=()
if command -v docker >/dev/null; then
  if docker info >/dev/null 2>&1; then
    docker_cmd=(docker)
  elif sudo -n docker info >/dev/null 2>&1; then
    docker_cmd=(sudo -n docker)
  fi
fi
if [[ -L "$deploy_root/current" ]]; then
  current_release="$(readlink -f "$deploy_root/current" 2>/dev/null || true)"
fi
case "$current_release" in
  "$deploy_root"/releases/*)
    printf 'current_release=%s\n' "$(basename "$current_release")"
    if ((${#docker_cmd[@]})); then
      (
        cd "$current_release" &&
          "${docker_cmd[@]}" compose config --quiet &&
          "${docker_cmd[@]}" compose ps
      ) || true
    else
      echo 'ollum_compose_access=unavailable'
    fi
    ;;
  "") echo 'current_release=absent' ;;
  *) echo 'current_release=unsafe-target' ;;
esac

section target
if [[ -e "$deploy_root" ]]; then
  echo 'target_exists=yes'
  ls -ld "$deploy_root"
else
  echo 'target_exists=no'
fi
