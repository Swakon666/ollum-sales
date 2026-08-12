# Ollum Sales runtime

The skill package is the reusable ChatGPT workflow and source bundle.

For live operations, the runtime architecture is:

ChatGPT / Skill -> Ollum Sales MCP -> ScrapeGraphAI and WhatsApp adapter -> WhatsApp Go bridge -> WhatsApp account

The MCP service exposes these intended tools:

- `ollum_status`
- `analyze_website`
- `whatsapp_search_contacts`
- `whatsapp_list_chats`
- `whatsapp_list_messages`
- `whatsapp_get_last_interaction`
- `whatsapp_send_message`

`whatsapp_send_message` is intentionally gated by `OLLUM_ALLOW_WHATSAPP_SEND`.
