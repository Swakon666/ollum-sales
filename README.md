# Ollum Sales MCP — Full Source Edition

Version **0.3.0** adds a persistent sales-agent runtime while keeping both upstream projects **complete and unmodified** under `upstream/`. All Ollum-specific integration code remains separate in `app/`.

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

The server exposes 30 tools in four groups:

- campaigns and discovery: `sales_create_campaign`, `sales_search_companies`, `sales_import_leads`, `sales_list_campaigns`, `sales_get_campaign`;
- lead intelligence: `sales_list_leads`, `sales_get_lead`, `sales_inspect_website`, `sales_analyze_lead`, `sales_save_analysis`, `sales_score_lead`, `sales_rank_leads`, `sales_update_lead_status`, plus the standalone `analyze_website`;
- CRM and outreach: `sales_save_outreach_draft`, `sales_list_outreach_drafts`, `sales_approve_outreach_draft`, `sales_record_interaction`, `sales_list_interactions`, `sales_schedule_followup`, `sales_list_due_followups`, `sales_complete_followup`, `sales_overview`, `sales_send_whatsapp_draft`;
- WhatsApp bridge: `whatsapp_search_contacts`, `whatsapp_list_chats`, `whatsapp_list_messages`, `whatsapp_get_last_interaction`, `whatsapp_send_message`.

`ollum_status` reports runtime readiness and CRM counts without exposing secrets.

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
   |                 |                       \
   |                 |                        \
CRM SQLite volume   website evidence       full WhatsApp MCP source
                     |                       |
                     +--> ScrapeGraphAI      +--> shared WhatsApp SQLite
                          when configured    |
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
- optionally, an API key/model supported by ScrapeGraphAI for provider-side analysis
- optionally, a Serper API key for reliable server-side company discovery

## Docker quick start

1. Create the environment file:

```bash
cp .env.example .env
```

2. Optionally add an LLM key/model and `SERPER_API_KEY` to `.env`. Without an LLM key, the MCP returns bounded website evidence for Codex to analyze and persist. Without Serper, company search uses a best-effort public fallback and agents can import separately verified candidates.

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
Named Docker volumes keep both the WhatsApp session/message databases and the Ollum CRM across restarts and redeploys.

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

## Persistent sales workflow

A complete agent run is resumable:

```text
campaign -> verified companies -> website evidence -> grounded analysis
         -> deterministic score/ranking -> saved draft -> operator approval
         -> confirmed send -> interaction timeline -> scheduled follow-up
```

The CRM is stored in `ollum-sales-crm-data`. Do not remove this volume during updates or rollback.

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

Direct sends require `confirm_send=true`. The recommended workflow adds a stronger boundary: save the exact message, approve that immutable recipient/message pair, and separately confirm `sales_send_whatsapp_draft`.

## First end-to-end test

1. `ollum_status`
2. `sales_create_campaign`
3. `sales_import_leads` with one known company site
4. `sales_analyze_lead`, then `sales_save_analysis` when Codex fallback evidence is returned
5. `sales_rank_leads`
6. `sales_save_outreach_draft`
7. inspect WhatsApp context and review the exact recipient/message
8. `sales_approve_outreach_draft`
9. enable write mode and separately confirm `sales_send_whatsapp_draft`
10. verify `sales_overview` and the follow-up timeline

## Upstream preservation

`upstream/whatsapp-mcp/` and `upstream/Scrapegraph-ai/` are copied from the exact archives supplied for this build. See `UPSTREAM_SOURCES.md` for SHA-256 hashes and file counts.

## Security

Web pages and inbound WhatsApp messages are untrusted data. The integration marks returned data
accordingly, rejects private/internal website targets, and requires explicit operator confirmation
for sends. Production `/mcp` requires a bearer token; `/health` intentionally exposes only liveness.
