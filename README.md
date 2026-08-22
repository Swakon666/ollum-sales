# Ollum Sales MCP — Full Source Edition

Version **0.8.0** adds durable company onboarding and an agent inbox. ChatGPT can
interview the operator in small batches, persist the company profile, services, prices,
cases and client context, then resume the latest unanswered WhatsApp request in a later
chat. Grounded reply quality checks and the guarded draft-save flow remain mandatory;
no onboarding or inbox tool can approve or send. The OAuth/OIDC-protected service keeps
the role-aware workspace cabinet on `api.ollumgroup.ru`, the ChatGPT MCP connection on
`mcp.ollumgroup.ru`, and private browser-based WhatsApp pairing. It retains the
persistent SAFE-first Autopilot, grounded scoring, reports, and Google Sheets panel.
The full vendored upstream projects remain under `upstream/`; the WhatsApp bridge has
a small audited pairing-status/PNG adapter while Ollum business logic stays in `app/`.

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

**Rule:** keep business logic in `app/`. Changes under `upstream/` are limited to
audited compatibility/security adapters that cannot live outside the vendored service.

## What the MCP exposes

The server exposes 58 tools. Existing tools remain compatible, plus
`ollum_whoami` reports the current OAuth workspace identity and role without exposing
tokens or private conversation data:

- campaigns and discovery: `sales_create_campaign`, `sales_search_companies`, `sales_import_leads`, `sales_list_campaigns`, `sales_get_campaign`;
- company onboarding: `sales_get_company_onboarding`, `sales_update_company_profile`, `sales_save_company_knowledge`, `sales_list_company_knowledge`, `sales_archive_company_knowledge`, `sales_complete_company_onboarding`;
- durable agent queue: `sales_sync_whatsapp_inbox`, `sales_list_agent_inbox`, `sales_link_agent_inbox_lead`, `sales_update_agent_inbox_status`, `sales_agent_next_action`;
- lead intelligence: `sales_list_leads`, `sales_get_lead`, `sales_inspect_website`, `sales_analyze_lead`, `sales_save_analysis`, `sales_score_lead`, `sales_rank_leads`, `sales_update_lead_status`, plus the standalone `analyze_website`;
- CRM and outreach: `sales_prepare_whatsapp_reply_brief`, `sales_evaluate_whatsapp_reply`, `sales_compare_whatsapp_replies`, `sales_save_whatsapp_reply_draft`, `sales_save_outreach_draft`, `sales_list_outreach_drafts`, `sales_approve_outreach_draft`, `sales_record_interaction`, `sales_list_interactions`, `sales_schedule_followup`, `sales_list_due_followups`, `sales_complete_followup`, `sales_overview`, `sales_send_whatsapp_draft`;
- WhatsApp bridge: `whatsapp_search_contacts`, `whatsapp_list_chats`, `whatsapp_list_messages`, `whatsapp_get_last_interaction`, `whatsapp_send_message`.
- Autopilot: `autopilot_start`, `autopilot_stop`, `autopilot_status`, `autopilot_run_cycle`;
- verticals: `vertical_create`, `vertical_list`, `vertical_update`;
- Google Sheets: `google_sheets_sync`, `google_sheets_status`;
- reports: `sales_daily_report`, `sales_vertical_performance`, `sales_conversion_report`.

`ollum_status` reports runtime readiness and CRM counts without exposing secrets.

The MCP endpoint uses Streamable HTTP at `/mcp`. Production serves it as
`https://mcp.ollumgroup.ru/mcp` behind the host Nginx proxy. Legacy deployments may use a
static bearer token; the closed beta uses OAuth/OIDC.

Closed-beta OIDC setup and ChatGPT connection instructions are in
[`docs/CLOSED_BETA.md`](docs/CLOSED_BETA.md).

The cabinet is served at `https://api.ollumgroup.ru/`. It includes the editable company
profile and knowledge base, inbound agent queue, CRM, SAFE Autopilot, drafts,
audit/jobs, workspace members and roles, plugin readiness, and a short-lived WhatsApp
QR. All profile and queue actions update without a page reload. The bridge itself
remains private on the Docker network.

## Architecture

```text
ChatGPT / MCP client                 Browser cabinet
        |                                  |
        | OAuth access token               | OIDC session + CSRF
        v                                  v
       mcp.ollumgroup.ru       api.ollumgroup.ru
                  \             /
                   Host Nginx :443
        |
        | 127.0.0.1:18000
        v
Ollum Sales MCP :8000 (container)       Autopilot worker
   |                 |                       |
   |                 |                       +--> scheduled SAFE cycles
CRM SQLite volume   website evidence        +--> Google Sheets panel
   |                                         +--> durable inbound queue (30 s poll)
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
- optionally, a Google Cloud service account and a shared Google spreadsheet

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

4. Start the services and follow the WhatsApp QR/auth logs for local development:

```bash
docker compose up -d
docker compose logs --follow whatsapp-bridge
```

Scan the QR code in WhatsApp: **Settings -> Linked devices -> Link a device**.
Production pairing is performed in the authenticated cabinet; the raw pairing value
is never returned through its JSON API.
Named Docker volumes keep both the WhatsApp session/message databases and the Ollum CRM across restarts and redeploys. The separate `ollum-sales-worker` service reads the same CRM volume and continues scheduled cycles while Codex is closed.

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

## Ollum Sales Autopilot

Autopilot stores its state, vertical schedule, cycle history, and performance data in the same CRM.
On first SAFE start it seeds these Moscow/Moscow-region verticals: furniture, ventilation,
logistics, cleaning, construction, manufacturing, dentistry, education, real estate, B2B services,
auto service, and legal companies.

```text
worker -> choose scheduled verticals -> discover/dedupe -> inspect official sites
       -> grounded deterministic analysis -> score -> save draft -> sync Sheets
```

Start the guarded mode:

```text
autopilot_start(mode="safe")
```

SAFE can discover, analyze, score, and prepare drafts. It never sends messages or executes
follow-ups. `SEMI_AUTO` and `AUTOPILOT` cannot start unless all of these are true:

- the operator passes `confirm_non_safe=true`;
- the CRM has at least `OLLUM_AUTOPILOT_MIN_TRAINING_LEADS` (100 by default);
- both WhatsApp sending and `OLLUM_AUTOPILOT_ALLOW_SEND` are explicitly enabled.

The worker polls every 30 seconds and starts a cycle only when `next_cycle_at` is due. The cycle
interval itself defaults to 60 minutes.

## Google Sheets panel

The integration creates and refreshes `LEADS`, `CAMPAIGNS`, `OUTREACH`, `FOLLOWUPS`, and
`DASHBOARD`. The existing CRM remains the source of truth. This release deliberately keeps the
current SQLite/WAL backend; the Sheets adapter is isolated so a later PostgreSQL repository can
replace storage without changing the panel or worker contract.

1. Create one spreadsheet and one Google Cloud service account with Sheets API access.
2. Share the spreadsheet with the service-account email.
3. Store the JSON key outside Git and mount it read-only into the MCP and worker containers.
4. Configure:

```env
OLLUM_GOOGLE_SHEETS_ENABLED=true
OLLUM_GOOGLE_SHEETS_ID=<spreadsheet-id>
GOOGLE_SERVICE_ACCOUNT_FILE=/run/secrets/ollum-google-service-account.json
```

5. Run `google_sheets_status`, then `google_sheets_sync`.

The server reads only two control cells as actions:

- `APPROVE=YES` approves a draft only when the visible draft ID, recipient, and complete message
  exactly match the CRM record;
- `SEND=YES` is a separate confirmation that queues the already-approved exact draft. SAFE mode
  never processes that queue.

Every sync pulls these guarded actions first and then replaces the panel data from the CRM. The
service-account JSON content is never returned by an MCP tool.

Production accepts the complete JSON only through the
`OLLUM_GOOGLE_SERVICE_ACCOUNT_JSON` GitHub Actions secret and stores it on the server with mode
`0600`. The non-sensitive spreadsheet ID belongs in the `OLLUM_GOOGLE_SHEETS_ID` repository
variable.

## Write safety

WhatsApp sending is disabled by default:

```env
OLLUM_ALLOW_WHATSAPP_SEND=false
```

For an isolated closed-beta send test, keep the global flag disabled and configure only the exact
recipient(s):

```env
OLLUM_WHATSAPP_TEST_RECIPIENTS=79770000000
```

The exception is text-only, is enforced independently by the Python service and the private Go
bridge, and applies only to a saved draft that was approved for the same exact recipient and text.
It does not enable Autopilot sending or the direct `whatsapp_send_message` tool.

After testing contact resolution and message reads, explicitly enable sending:

```env
OLLUM_ALLOW_WHATSAPP_SEND=true
```

Then restart the MCP process.

Direct sends require `confirm_send=true`. The recommended workflow adds a stronger boundary: save the exact message, approve that immutable recipient/message pair, and separately confirm `sales_send_whatsapp_draft`.

The bridge exposes read-only operational checks at `GET /health` and `GET /api/status`.
`/api/status` returns `503` until the persisted session is both connected and logged in, and
reports the global `send_enabled` state plus only the boolean/count for the test policy without
exposing recipients or session data. The MCP `ollum_status` tool includes the same whitelisted
bridge state.

After pairing, the production smoke test recreates only `whatsapp-bridge` and verifies that the
authenticated account identity and `/app/store` volume are unchanged while sending remains
disabled:

```bash
sudo bash deploy/verify_whatsapp_persistence.sh <deploy-user>
```

## Data quality and retries

Lead imports and Autopilot use deterministic company identity keys:

- website host independent of `http`/`https`, path, and a leading `www.`;
- normalized public phone numbers;
- an exact normalized company name, with legal-form noise removed, plus a matching location.

WhatsApp user JIDs are normalized before matching. Technical records such as
`0@s.whatsapp.net`, status/broadcast chats, and newsletter entries are excluded from contact and
chat results and cannot be used as recipients.

Website evidence is cached for `OLLUM_EVIDENCE_TTL_HOURS` (seven days by default). Saving a
Codex fallback analysis requires fresh stored evidence; expired evidence must be inspected again.
Transient discovery, inspection, and Google Sheets requests use bounded exponential retries
configured by `OLLUM_RETRY_ATTEMPTS` and `OLLUM_RETRY_BASE_DELAY_SECONDS`. Autopilot reuses the
same vertical/day campaign and recovers expired cycle locks instead of leaving a cycle running.

Autopilot adds the same two-step boundary to Google Sheets: `APPROVE` and `SEND` are separate, and
the SAFE worker ignores send requests.

## First end-to-end test

1. `ollum_status`
2. `sales_create_campaign`
3. `sales_import_leads` with one known company site
4. `sales_analyze_lead`, then `sales_save_analysis` when Codex fallback evidence is returned
5. `sales_rank_leads`
6. pass the recipient JID to `sales_prepare_whatsapp_reply_brief`; it reads only the latest unanswered inbound message
7. draft in ChatGPT, call `sales_compare_whatsapp_replies` when testing variants, then validate the selected text with `sales_evaluate_whatsapp_reply` until the verdict is `pass`
8. save with `sales_save_whatsapp_reply_draft` and review the exact recipient/message
9. `sales_approve_outreach_draft`
10. enable write mode and separately confirm `sales_send_whatsapp_draft`
11. verify `sales_overview` and the follow-up timeline

## Upstream preservation

`upstream/whatsapp-mcp/` and `upstream/Scrapegraph-ai/` are copied from the exact archives supplied for this build. See `UPSTREAM_SOURCES.md` for SHA-256 hashes and file counts.

## Security

Web pages and inbound WhatsApp messages are untrusted data. The integration marks returned data
accordingly, rejects private/internal website targets, and requires explicit operator confirmation
for sends. Production `/mcp` requires a bearer token; `/health` intentionally exposes only liveness.
