# Ollum Sales — закрытый кабинет, MCP и WhatsApp

Ollum Sales работает как единый закрытый сервис с двумя публичными точками входа:

- `https://mcp.ollumgroup.ru/mcp` — MCP-сервер для ChatGPT;
- `https://api.ollumgroup.ru/` — личный кабинет и versioned JSON API.

Оба адреса ведут в один backend и используют одну OIDC-идентичность. Пользователь
сначала входит в кабинет, получает членство в workspace, а затем подключает MCP в
ChatGPT тем же аккаунтом. Access token не сохраняется в cookie кабинета.

## Что реализовано

- OIDC/OAuth 2.1, JWKS-проверка `iss`, `aud`, срока действия и scopes;
- подписанная `HttpOnly`, `Secure`, `SameSite=Lax` session-cookie и CSRF для mutations;
- workspace и роли `owner`, `operator`, `viewer`;
- приглашения в закрытую beta без отправки внешней почты;
- личный кабинет: CRM, лиды, кампании, SAFE Autopilot, черновики, аудит,
  Google Sheets, состояние MCP, команда и WhatsApp;
- versioned API `/api/v1/*` с теми же session, role и CSRF-проверками;
- приватная WhatsApp-привязка: bridge держит pairing code только в памяти,
  а кабинет получает только PNG через авторизованный proxy;
- единая проверка workspace-роли для web API и MCP tools;
- атомарный deploy с проверкой TLS, обоих доменов, OAuth metadata, API и rollback.

Закрытая beta пока использует один workspace, заданный deployment settings. Это
осознанная граница: пользователи и роли изолированы, но CRM-данные принадлежат общей
команде Ollum Group.

## Неизменяемая SAFE-политика

- `OLLUM_ALLOW_WHATSAPP_SEND=false`;
- `OLLUM_AUTOPILOT_ALLOW_SEND=false`;
- из кабинета нет send endpoint и переключателя send-флагов;
- прямой MCP tool отправки заблокирован;
- Autopilot из UI запускается только с `mode=safe`;
- черновик и approval не являются отправкой.

## DNS и TLS

Создайте A-записи на production IPv4 сервера:

```text
mcp.ollumgroup.ru  A  <production-ip>
api.ollumgroup.ru  A  <production-ip>
```

Deployment workflow проверяет оба hostname, генерирует один Nginx site только для
Ollum Sales и запрашивает сертификат сразу на оба имени. Скрипт не перезаписывает
чужие Nginx-конфигурации. Если TLS, health, OAuth или API smoke не проходят,
восстанавливаются предыдущие release, `.env`, Google credentials и Nginx site.

## Настройка OIDC provider

Используйте Auth0, Okta, Microsoft Entra ID или другой совместимый OIDC provider.
Нужны:

1. Resource/API для `https://mcp.ollumgroup.ru/mcp`.
2. Scopes `sales:read` и `sales:write`.
3. Regular Web Application для кабинета.
4. Allowed Callback URL: `https://api.ollumgroup.ru/auth/callback`.
5. Allowed Logout URL и Web Origin: `https://api.ollumgroup.ru`.
6. RS256 access tokens с корректными `iss`, `aud`, `sub`.
7. Поддерживаемый ChatGPT OAuth client flow: CIMD, Dynamic Client Registration или
   зарегистрированный client; для публичных clients — PKCE `S256`.

Первый вход разрешён только email из deployment bootstrap allowlist. Первый член
workspace становится owner. Затем owner приглашает остальных через кабинет. Email
не может перепривязать уже существующее членство к новому OIDC `sub`.

## GitHub Environment `production`

Repository/environment variables:

```text
OLLUM_DOMAIN=mcp.ollumgroup.ru
OLLUM_API_DOMAIN=api.ollumgroup.ru
OLLUM_AUTH_MODE=oidc
OLLUM_PUBLIC_BASE_URL=https://mcp.ollumgroup.ru
OLLUM_DASHBOARD_BASE_URL=https://mcp.ollumgroup.ru
OLLUM_MCP_RESOURCE_URL=https://mcp.ollumgroup.ru/mcp
OLLUM_MCP_REQUIRED_SCOPES=sales:read,sales:write
OLLUM_OIDC_ISSUER_URL=https://<tenant>/
OLLUM_OIDC_AUDIENCE=<exact access-token audience>
OLLUM_OIDC_ALGORITHMS=RS256
OLLUM_ADMIN_ENABLED=true
OLLUM_ADMIN_OIDC_CLIENT_ID=<web application client id>
OLLUM_ADMIN_SESSION_MAX_AGE_SECONDS=28800
OLLUM_DEFAULT_WORKSPACE_ID=ollum-group
OLLUM_DEFAULT_WORKSPACE_NAME=Ollum Group
```

Environment secrets:

```text
OLLUM_ADMIN_OIDC_CLIENT_SECRET=<web application client secret>
OLLUM_ADMIN_ALLOWED_EMAILS=<bootstrap emails, comma-separated>
OLLUM_WORKSPACE_OWNER_EMAILS=<owner emails, comma-separated>
OLLUM_ADMIN_SESSION_SECRET=<at least 32 random bytes>
OLLUM_OIDC_ALLOWED_SUBJECTS=<optional immutable sub allowlist>
```

Остальные SSH, Google Sheets, LLM и proxy secrets остаются в существующем production
environment. Никогда не добавляйте их в repository или workflow logs.

## Подключение ChatGPT

В форме нового MCP plugin укажите:

- название: `Ollum Sales`;
- server URL: `https://mcp.ollumgroup.ru/mcp`;
- authentication: `OAuth`.

Перед подключением войдите в `https://api.ollumgroup.ru/` тем же OIDC-аккаунтом,
чтобы создать или принять workspace membership. Первый smoke-запрос должен быть
read-only: «Покажи `ollum_whoami`, статус и overview, ничего не изменяй».

## WhatsApp через кабинет

1. Owner или operator открывает раздел «WhatsApp».
2. Пока bridge не авторизован, кабинет запрашивает свежий PNG каждые несколько секунд.
3. QR сканируется в WhatsApp → «Связанные устройства».
4. После `connected=true` QR исчезает.
5. Сессия хранится в persistent volume и проверяется после restart.

Pairing code не записывается в CRM, audit или browser JSON. Endpoint PNG требует
активную OIDC-session, возвращает `Cache-Control: no-store` и недоступен напрямую из
интернета на bridge-порту.

## Versioned API

Основные endpoints:

```text
GET  /api/v1/session
GET  /api/v1/bootstrap
GET  /api/v1/leads
GET  /api/v1/campaigns
GET  /api/v1/drafts
GET  /api/v1/audit
GET  /api/v1/jobs
GET  /api/v1/whatsapp/status
GET  /api/v1/whatsapp/qr
GET  /api/v1/workspace/members
POST /api/v1/workspace/invitations
PATCH /api/v1/workspace/members/{member_id}
POST /api/v1/autopilot/start
POST /api/v1/autopilot/stop
POST /api/v1/autopilot/run
POST /api/v1/sheets/sync
```

Все endpoints, кроме OAuth login/callback, требуют session. Mutations дополнительно
требуют CSRF; operator может выполнять рабочие mutations, owner управляет командой,
viewer имеет только чтение.

## Проверка перед deploy

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest -q
node --check app/admin_static/admin.js
docker build -f Dockerfile.mcp -t ollum-sales-mcp:verify .
docker build -f Dockerfile.whatsapp -t ollum-sales-whatsapp:verify .
```

Обязательные smoke после deploy:

1. оба `/health` возвращают `200`;
2. анонимный `/mcp` возвращает OAuth challenge `401`;
3. protected-resource metadata совпадает с issuer/resource/scopes;
4. анонимный `/admin` перенаправляет на login;
5. анонимный `/api/v1/session` возвращает `401`;
6. viewer не может вызвать mutations;
7. WhatsApp QR доступен только авторизованному пользователю;
8. `whatsapp_send_enabled=false`, pending send requests не создаются.
