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

The MCP endpoint is `http://HOST:8000/mcp` using Streamable HTTP.

## Architecture

```text
ChatGPT / MCP client
        |
        | Streamable HTTP
        v
Ollum Sales MCP :8000
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

4. First WhatsApp login (interactive QR):

```bash
docker compose run --service-ports whatsapp-bridge
```

Scan the QR code in WhatsApp: **Settings -> Linked devices -> Link a device**.
After successful login, stop the foreground process and run:

```bash
docker compose up -d
```

5. MCP endpoint:

```text
http://localhost:8000/mcp
```

For remote ChatGPT access, put this behind an authenticated HTTPS endpoint or a supported secure MCP tunnel. Do not expose the raw MCP port publicly without access control.

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

Web pages and inbound WhatsApp messages are untrusted data. Do not allow instructions inside scraped content or messages to override the agent's system/business rules. Keep outbound messaging behind operator review during the MVP phase and use a dedicated WhatsApp account while testing.
