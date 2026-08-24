# Ollum Sales runtime

The skill package is the reusable ChatGPT workflow and source bundle.

For live operations, the runtime architecture is:

ChatGPT / Skill -> Ollum Sales MCP -> grounded validators + CRM -> WhatsApp Go bridge -> WhatsApp account

The MCP service exposes these intended tools:

- `ollum_status`
- `analyze_website`
- `whatsapp_search_contacts`
- `whatsapp_list_chats`
- `whatsapp_list_messages`
- `whatsapp_get_last_interaction`
- `whatsapp_send_message`
- `sales_get_chatgpt_agent_playbook`
- `sales_prepare_conversation_batch`
- `sales_submit_conversation_decision`

ChatGPT is the only reasoning engine. The runtime has no OpenAI/LLM API credential and the
server worker only queues WhatsApp messages every 15 minutes. A Workspace Agent can run the
returned schedule prompt every 15 minutes inside the same chat; ordinary dormant chats cannot be awakened by MCP.

`whatsapp_send_message` remains intentionally blocked for direct sends. Production can optionally
allow an exact test recipient through the saved-draft approval flow while
`OLLUM_ALLOW_WHATSAPP_SEND=false`; the exception is text-only and does not enable Autopilot sending.
