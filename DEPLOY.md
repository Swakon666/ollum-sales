# Ollum Sales production deployment

Production is deployed by `.github/workflows/deploy.yml`. The workflow supports a read-only
preflight, deployment, one-step rollback, and an explicit SAFE Autopilot verification. Every
production operation requires a manual workflow dispatch; merging or pushing to `main` never changes
production. A manual run defaults to preflight so an operator cannot accidentally deploy.

Production jobs run on the dedicated repository runner labelled `ollum-sales-production`. The
runner is installed as a separate system service under the deployment user and connects outbound to
GitHub over HTTPS. It exposes no public port, and the workflow does not run for pull requests.
Because the provider does not support NAT loopback, server-side TLS checks resolve the production
domain to localhost. A separate GitHub-hosted job verifies the real public HTTPS route externally.

## Architecture

```text
ChatGPT MCP client                 Browser cabinet
  | OAuth access token              | OIDC session + CSRF
  v                                 v
mcp.ollumgroup.ru              api.ollumgroup.ru
             \                  /
              existing host Nginx :443
  |
  | 127.0.0.1:18000
  v
ollum-sales-mcp :8000 ---- internal Docker network ---- whatsapp-bridge :8080
  |                  |                                     |
  |                  +-- named volume                      +-- named volume
  |                      ollum-sales-crm-data                   ollum-sales-whatsapp-data
  v
ScrapeGraphAI / LLM provider (optional)
```

The WhatsApp bridge has no published host port. The MCP container is published only on localhost;
Nginx is the sole public entry point. Both containers have a private internal network and a second
network for required outbound Internet access.

The production server already uses Nginx on ports 80/443 for other applications. The workflow adds
only `/etc/nginx/sites-available/ollum-sales` and its matching symlink. It does not stop or replace
existing Nginx sites, PM2 processes, PostgreSQL, or systemd services. Caddy is intentionally not used
because it would conflict with the existing listener.

## GitHub Secrets

Configure these repository secrets under **Settings → Secrets and variables → Actions**:

| Secret | Required | Purpose |
| --- | --- | --- |
| `OLLUM_SSH_HOST` | yes | Production SSH host |
| `OLLUM_SSH_PORT` | yes | Production SSH port |
| `OLLUM_SSH_USER` | yes | Deployment user with sudo permission |
| `OLLUM_SSH_PASSWORD` | yes | SSH and sudo password |
| `OLLUM_SSH_HOST_KEY` | yes | Verified SSH fingerprint such as `SHA256:…` |
| `OLLUM_DOMAIN` | yes | Public MCP domain (`mcp.ollumgroup.ru`) |
| `OLLUM_MCP_BEARER_TOKEN` | bearer mode only | Long random token protecting `/mcp` |
| `OLLUM_ADMIN_OIDC_CLIENT_SECRET` | OIDC cabinet | Server-side web client secret |
| `OLLUM_ADMIN_ALLOWED_EMAILS` | OIDC cabinet | Comma-separated bootstrap allowlist |
| `OLLUM_WORKSPACE_OWNER_EMAILS` | OIDC cabinet | Comma-separated owner emails |
| `OLLUM_ADMIN_SESSION_SECRET` | OIDC cabinet | At least 32 random bytes |
| `OPENAI_API_KEY` | for OpenAI | ScrapeGraphAI provider credential |
| `LLM_API_KEY` | optional | Generic provider credential override |
| `SERPER_API_KEY` | optional | Reliable server-side company discovery through Serper |
| `SCRAPEGRAPH_MODEL` | optional | Defaults to `openai/gpt-4o-mini` |
| `OLLUM_GOOGLE_SERVICE_ACCOUNT_JSON` | yes for v0.4 | Complete Google service-account JSON; transferred as a mode-`0600` file and never written to `.env` |

Configure the non-sensitive repository variables for the closed beta:

```text
OLLUM_API_DOMAIN=api.ollumgroup.ru
OLLUM_AUTH_MODE=oidc
OLLUM_PUBLIC_BASE_URL=https://mcp.ollumgroup.ru
OLLUM_DASHBOARD_BASE_URL=https://mcp.ollumgroup.ru
OLLUM_MCP_RESOURCE_URL=https://mcp.ollumgroup.ru/mcp
OLLUM_MCP_REQUIRED_SCOPES=sales:read,sales:write
OLLUM_OIDC_ISSUER_URL=https://<tenant>/
OLLUM_OIDC_AUDIENCE=<exact API audience>
OLLUM_ADMIN_ENABLED=true
OLLUM_ADMIN_OIDC_CLIENT_ID=<regular web application client id>
OLLUM_DEFAULT_WORKSPACE_ID=ollum-group
OLLUM_DEFAULT_WORKSPACE_NAME=Ollum Group
```

Also configure `OLLUM_GOOGLE_SHEETS_ID` with the target spreadsheet ID. Production
deployment forces both `OLLUM_ALLOW_WHATSAPP_SEND=false` and
`OLLUM_AUTOPILOT_ALLOW_SEND=false`; changing a repository secret cannot enable sends.

Never commit or print any of these values. The workflow passes the SSH password through `sshpass`
environment input and streams the sudo password over SSH. The generated production `.env` is copied
over encrypted SSH with mode `0600`; its contents are never printed.

In the closed beta, configure the ChatGPT connection with:

```text
URL: https://mcp.ollumgroup.ru/mcp
Authentication: OAuth
```

Legacy bearer deployments remain supported, but the browser cabinet intentionally requires OIDC.
The OIDC provider must allow the callback derived from `OLLUM_DASHBOARD_BASE_URL`.
The current closed-beta client uses `https://mcp.ollumgroup.ru/auth/callback`; the API
hostname remains a valid cabinet/API entry point, while authentication completes on the
canonical MCP hostname. After `https://api.ollumgroup.ru/auth/callback` is registered in
the same OIDC client, the dashboard variable can be switched to the API hostname without
a code change.

## Deployment

Merging or pushing to `main` does not deploy. Every production operation is manual:

1. Open **Actions → Production deployment → Run workflow**.
2. Choose `preflight` to inspect the server without changing it.
3. Choose `deploy` to deploy the selected Git ref.
4. After deployment, choose `verify-autopilot` once. It stops only the Ollum Sales worker, starts
   Autopilot in SAFE mode at a 45-minute interval, verifies that pending send requests remain
   untouched, runs one cycle, restarts the worker, and checks persistence. Both send flags remain
   disabled and no draft is approved or sent by the verification.

The deployment performs these operations:

1. pins and verifies the SSH host key;
2. runs the read-only server preflight;
3. copies a source archive and mode-`0600` production environment over SSH;
4. installs Docker Engine and the Compose plugin only when absent;
5. builds and starts the three Ollum Sales Compose services without touching unrelated services;
6. waits for the local MCP health endpoint;
7. installs a dedicated Nginx vhost and validates the entire Nginx configuration before reload;
8. obtains or reuses one Let's Encrypt certificate for MCP and API hostnames;
9. checks both `/health` endpoints, OAuth metadata/challenge, the cabinet redirect, and
   unauthenticated `/api/v1/session` rejection.

Releases are stored below:

```text
/home/<ssh-user>/ollum-sales/releases/<commit-run-attempt>/
/home/<ssh-user>/ollum-sales/current -> active release
/home/<ssh-user>/ollum-sales/previous -> previous release
/home/<ssh-user>/ollum-sales/shared/.env
```

## Health and MCP checks

Public liveness:

```bash
curl --fail https://mcp.ollumgroup.ru/health
```

Expected response:

```json
{"status":"ok","service":"ollum-sales-mcp"}
```

An unauthenticated MCP request must return HTTP `401` with OAuth resource metadata. MCP
clients must supply a valid OIDC access token and scopes.
The local backend check, run on the server, is:

```bash
curl --fail http://127.0.0.1:18000/health
```

## WhatsApp QR login

The primary closed-beta flow is the authenticated page
`https://api.ollumgroup.ru/` → **WhatsApp**. The private bridge keeps the raw pairing
value only in memory; the backend returns a no-store PNG to an authorized session.
The bridge has no public port.

For recovery, an operator can still inspect the private service over SSH:

```bash
cd /home/<ssh-user>/ollum-sales/current
sudo docker compose logs --follow --tail=200 whatsapp-bridge
```

In WhatsApp, open **Settings → Linked devices → Link a device** and scan the QR code. The bridge
allows three minutes per attempt; `restart: unless-stopped` starts a fresh attempt if it expires.

Authentication and message databases live in `ollum-sales-whatsapp-data`. Campaigns, leads,
analysis, scores, drafts, interactions, and follow-ups live in `ollum-sales-crm-data`. Do not delete
either named volume. They survive container recreation, release changes, and server restarts.

The bridge health status remains `starting` until WhatsApp authentication succeeds. This does not
prevent the MCP health endpoint from running; WhatsApp operations become available after pairing.

The production workflow also provides `connect-whatsapp`, which recreates only the bridge and keeps
fresh QR batches rotating for up to ten minutes. The persistent WhatsApp volume is preserved and the
script refuses to run unless `OLLUM_ALLOW_WHATSAPP_SEND=false`.

## Fast WhatsApp-only update

For bridge-only changes, cross-compile the static Linux binary on the operator computer instead of
rebuilding MCP and worker on production:

```powershell
.\scripts\build_whatsapp_prebuilt.ps1
```

Upload the resulting ELF binary as a private GitHub release asset, then run the production workflow in
`deploy-whatsapp-prebuilt` mode with the release tag, asset name, and SHA-256 from the generated JSON
metadata. The workflow verifies the binary before and after transfer, builds only a tiny overlay layer
on the existing bridge image, and recreates only `whatsapp-bridge` with `--no-deps --no-build`. It
preserves the named volume, keeps WhatsApp sending disabled, and restores the previous bridge image if
the new container fails to start.

## Logs and status

```bash
cd /home/<ssh-user>/ollum-sales/current
sudo docker compose ps
sudo docker compose logs --tail=200 ollum-sales-mcp
sudo docker compose logs --tail=200 ollum-sales-worker
sudo docker compose logs --tail=200 whatsapp-bridge
```

Do not paste `.env`, container environment output, authentication databases, or full WhatsApp logs
into tickets or chat messages.

## Restart and update

Restart only this project:

```bash
cd /home/<ssh-user>/ollum-sales/current
sudo docker compose restart
```

Restart one service:

```bash
sudo docker compose restart ollum-sales-mcp
sudo docker compose restart whatsapp-bridge
```

For an update, merge the reviewed commit, then separately run the workflow in `deploy` mode.
Do not run `docker compose down --volumes`; that would remove persistent WhatsApp and CRM state.

## Rollback

Open the production workflow and run it manually with mode `rollback`. The workflow rebuilds and
starts the release referenced by `previous`, verifies local and public health, then swaps the
`current` and `previous` links. The named WhatsApp volume and shared production environment remain
unchanged.

Rollback is unavailable before at least two successful releases exist.

## Troubleshooting

- **SSH host key mismatch:** stop. Verify the new fingerprint through a trusted server-console path,
  then update `OLLUM_SSH_HOST_KEY`. Never bypass host-key validation.
- **Sudo failure:** the SSH user must be allowed to install Docker packages, manage Docker, write the
  dedicated Nginx site, reload Nginx, and run Certbot. Do not weaken sudo policy globally.
- **Low disk:** normal builds require 1.5 GiB free. With 384 MiB to 1.5 GiB and an existing verified
  MCP image, deployment creates a small application overlay after checking required dependencies.
  Releases with an unchanged runtime fingerprint reuse the verified images with at least 256 MiB
  free and do not rebuild them.
  `diagnose-space` is read-only. `recover-space` requires an exact reclaimable Ollum cache record ID
  and failed release ID, then removes dangling BuildKit cache only after an operator has verified the
  diagnostic inventory belongs to the failed Ollum build. It does not delete images, containers,
  volumes, or unrelated files. After a verified deployment, the workflow removes only superseded
  Ollum Sales images that no container still uses.
- **Nginx configuration conflict:** the workflow refuses to overwrite an unmanaged
  `/etc/nginx/sites-available/ollum-sales` or unrelated enabled-site symlink.
- **HTTP 502:** check `sudo docker compose ps` and MCP logs, then verify localhost port `18000`.
- **HTTP 401 on `/mcp`:** expected without `Authorization: Bearer …`; check the client header.
- **No LLM key:** `sales_analyze_lead` returns bounded website evidence in `codex_fallback` mode;
  Codex can ground an analysis in that evidence and save it with `sales_save_analysis`. Configure an
  LLM key only when provider-side ScrapeGraphAI analysis is required.
- **Company discovery is empty or noisy:** configure `SERPER_API_KEY`, or verify candidates through
  agent research and persist them with `sales_import_leads`.
- **WhatsApp bridge not healthy:** follow the QR/auth logs and pair the device. A timed-out QR attempt
  is restarted automatically.
- **WhatsApp sends blocked:** expected in this production profile. The deployment workflow pins
  `OLLUM_ALLOW_WHATSAPP_SEND=false` and `OLLUM_AUTOPILOT_ALLOW_SEND=false`.
- **Google Sheets not configured:** create/share the spreadsheet, mount the service-account JSON
  read-only into both Python services, and set `OLLUM_GOOGLE_SHEETS_ENABLED`,
  `OLLUM_GOOGLE_SHEETS_ID`, and `GOOGLE_SERVICE_ACCOUNT_FILE`. Never put the JSON key in `.env`.
- **Autopilot does not run:** inspect `autopilot_status`, then check the worker logs. SAFE is the
  default. Non-SAFE modes remain blocked until the explicit flags, confirmation, and minimum lead
  history are all present.
