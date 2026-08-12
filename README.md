# Ollum Sales MCP — Full Source Edition

Version **0.2.0** keeps both uploaded upstream projects **complete and unmodified** under `upstream/`, while all Ollum-specific integration code lives separately in `app/`.

## Repository layout

```text
ollum-sales-mcp/
├── app/                         # OUR integration/MCP code
├── upstream/
│   ├── whatsapp-mcp/            # FULL original lharries/whatsapp-mcp 0.0.1 source
│   └── Scrapegraph-ai/          # FULL original ScrapeGraphAI 2.1.6 source
├── Dockerfile.mcp
├── Dockerfile.whatsapp
├── docker-compose.yml
├── pyproject.toml
├── .env.example
├── UPSTREAM_SOURCES.md
└── README.md
```

**Rule:** do not patch files inside `upstream/` for Ollum-specific behavior. Put adapters, overrides and business logic in `app/`. This makes upstream upgrades and diffs much easier.

## What the MCP exposes

- `ollum_status`
- `analyze_website`
- `whatsapp_search_contacts`
- `whatsapp_list_chats`
- `whatsapp_list_messages`
- `whatsapp_get_last_interaction`
- `whatsapp_send_message`

The MCP endpoint uses Streamable HTTP at `/mcp`. Production serves it as
`https://mcp.ollumgroup.ru/mcp` behind bearer authentication and the host Nginx proxy.

## Architecture

```text
ChatGPT / MCP client
        |
        | HTTPS + Bearer token
        v
Host Nginx :443
        |
        | 127.0.0.1:18000
        v
Ollum Sales MCP :8000 (container)
   |                         \
   |                          \
full ScrapeGraphAI source   adapter -> full WhatsApp MCP source
   |                                      |
   v                                      +--> shared SQLite
LLM provider                              |
                                          v
                                  Go WhatsApp bridge :8080
                                          |
                                          v
                                     WhatsApp Web
```

## Requirements

### Docker route
- Docker
- Docker Compose

### Local route
- Python 3.12+
- Go 1.24.1+
- Chromium/Playwright dependencies
- an API key/model supported by ScrapeGraphAI

## Docker quick start

1. Create the environment file:

```bash
cp .env.example .env
```

2. Add your LLM key/model to `.env`.

3. Build:

```bash
docker compose build
```

4. Start the services and follow the WhatsApp QR/auth logs:

```bash
docker compose up -d
docker compose logs --follow whatsapp-bridge
```

Scan the QR code in WhatsApp: **Settings -> Linked devices -> Link a device**.
The named Docker volume keeps the session and message databases across restarts and redeploys.

5. MCP endpoint:

```text
http://localhost:18000/mcp
```

For remote ChatGPT access, put this behind an authenticated HTTPS endpoint or a supported secure MCP tunnel. Do not expose the raw MCP port publicly without access control.

Production deployment, GitHub Secrets, rollback, health checks, and QR login are documented in
[`DEPLOY.md`](DEPLOY.md).

## Local development

Start the original upstream Go bridge:

```bash
cd upstream/whatsapp-mcp/whatsapp-bridge
go run main.go
```

After scanning the QR code, use another terminal from repository root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ./upstream/Scrapegraph-ai
pip install -e .
python -m playwright install chromium
python -m app.server
```

The adapter dynamically loads the original `upstream/whatsapp-mcp/whatsapp-mcp-server/whatsapp.py`, then sets runtime DB/API locations **in memory**. No upstream source file needs to be modified.

## Write safety

WhatsApp sending is disabled by default:

```env
OLLUM_ALLOW_WHATSAPP_SEND=false
```

After testing contact resolution and message reads, explicitly enable sending:

```env
OLLUM_ALLOW_WHATSAPP_SEND=true
```

Then restart the MCP process.

Every send call must also include `confirm_send=true`; both controls are required.

## First end-to-end test

1. `ollum_status`
2. `analyze_website` on one known company site
3. `whatsapp_search_contacts`
4. `whatsapp_get_last_interaction`
5. draft/review message
6. enable write mode
7. `whatsapp_send_message`

## Upstream preservation

`upstream/whatsapp-mcp/` and `upstream/Scrapegraph-ai/` are copied from the exact archives supplied for this build. See `UPSTREAM_SOURCES.md` for SHA-256 hashes and file counts.

## Security

Web pages and inbound WhatsApp messages are untrusted data. The integration marks returned data
accordingly, rejects private/internal website targets, and requires explicit operator confirmation
for sends. Production `/mcp` requires a bearer token; `/health` intentionally exposes only liveness.
