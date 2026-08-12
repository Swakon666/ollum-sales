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
df -hT / "$HOME" 2>/dev/null || df -hT /
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
for unit in docker caddy nginx apache2 traefik; do
  if command -v systemctl >/dev/null; then
    printf '%s=' "$unit"
    systemctl is-active "$unit" 2>/dev/null || true
  fi
done

section containers
if command -v docker >/dev/null; then
  docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>&1 || true
  docker compose ls 2>&1 || true
else
  echo 'docker=absent'
fi

section listeners
if command -v ss >/dev/null; then
  ss -lntup 2>/dev/null || ss -lnt 2>/dev/null || true
fi

section target
if [[ -e "$HOME/ollum-sales" ]]; then
  echo 'target_exists=yes'
  ls -ld "$HOME/ollum-sales"
else
  echo 'target_exists=no'
fi
