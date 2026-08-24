"use strict";

const LEAD_STATUSES = [
  "new", "researching", "analyzed", "qualified", "drafted", "approved",
  "contacted", "replied", "interested", "meeting", "proposal", "follow_up",
  "won", "lost", "archived",
];

const STATUS_LABELS = {
  new: "Новый", researching: "Исследование", analyzed: "Проверен",
  qualified: "Qualified", drafted: "Черновик", approved: "Одобрен",
  contacted: "Контакт", replied: "Ответил", interested: "Интерес",
  meeting: "Встреча", proposal: "Предложение", follow_up: "Follow-up",
  won: "Выигран", lost: "Потерян", archived: "Архив",
  running: "В работе", completed: "Завершён", failed: "Ошибка",
  queued: "В очереди", blocked: "Заблокирован", draft: "На проверке",
  sent: "Отправлен", cancelled: "Отменён", ready: "Готова",
  active: "Активна", paused: "Пауза", done: "Завершена",
  starting: "Запускается", waiting_for_qr: "Ожидает QR",
  waiting_for_scan: "Ожидает сканирования", refreshing: "Обновляет QR",
  paired: "Привязано", connected: "Подключено", not_required: "Не требуется",
  timed_out: "Время истекло", restarting: "Перезапускается",
  not_started: "Не настроено", in_progress: "Настройка",
  acknowledged: "В работе", drafted: "Черновик готов", resolved: "Закрыто",
  ignored: "Игнор", processing: "Передано ChatGPT", needs_review: "Нужен человек",
  discovery: "Знакомство", qualification: "Квалификация", objection: "Возражение",
  handoff: "Передача менеджеру", closed: "Закрыт", observe: "Наблюдение",
  on_track: "В SLA", at_risk: "SLA под риском", overdue: "SLA просрочен",
  event_not_in_review: "Повтор доступен только после проверки",
  draft_already_exists: "У обращения уже есть черновик",
  lead_not_linked: "Сначала сопоставьте лид",
  inbound_expired: "Входящее вышло за окно свежести",
  invalid_received_at: "Некорректное время входящего",
};

const VIEW_TITLES = {
  overview: "Обзор системы", company: "Профиль компании", agent: "AI-агент продаж", inbox: "Входящие обращения",
  leads: "Лиды", campaigns: "Кампании",
  drafts: "Черновики", autopilot: "Autopilot", plugin: "GPT-плагин",
  whatsapp: "Подключение WhatsApp", team: "Команда и доступ", audit: "Журнал действий",
};

const VIEW_HASHES = Object.freeze({
  overview: "#overview", company: "#company", agent: "#agent", inbox: "#inbox",
  leads: "#leads", campaigns: "#campaigns",
  drafts: "#drafts", autopilot: "#autopilot", plugin: "#plugin",
  whatsapp: "#whatsapp", team: "#team", audit: "#audit",
});

const PAGE_SIZE = 50;

const state = {
  data: null,
  leads: [],
  campaigns: [],
  drafts: [],
  workspaceMembers: [],
  workspaceInvitations: [],
  companyKnowledge: [],
  inbox: [],
  conversationSessions: [],
  profileDirty: false,
  agentDirty: false,
  currentDraft: null,
  csrf: "",
  loading: false,
  pages: { leads: 1, campaigns: 1, drafts: 1, audit: 1 },
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDate(value, fallback = "—") {
  if (!value) return fallback;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return fallback;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function formatMinutes(value) {
  const minutes = Math.max(0, Number(value) || 0);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} ч ${rest} мин` : `${hours} ч`;
}

function label(value) {
  return STATUS_LABELS[value] || String(value || "—").replaceAll("_", " ");
}

function statusClass(value) {
  if (["completed", "ready", "active", "qualified", "approved", "won", "sent", "resolved"].includes(value)) return "is-good";
  if (["failed", "lost", "cancelled", "needs_review", "overdue"].includes(value)) return "is-bad";
  if (["queued", "running", "paused", "blocked", "draft", "drafted", "acknowledged", "researching", "processing", "at_risk"].includes(value)) return "is-warn";
  return "";
}

function statusPill(value) {
  return `<span class="status-pill ${statusClass(value)}">${escapeHtml(label(value))}</span>`;
}

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("is-error", error);
  toast.classList.add("is-visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("is-visible"), 3600);
}

function setLoading(loading) {
  state.loading = loading;
  $("#loading-line").classList.toggle("is-loading", loading);
  $("#refresh-button").disabled = loading;
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if (method !== "GET" && method !== "HEAD") headers.set("X-CSRF-Token", state.csrf);
  const response = await fetch(path, {
    ...options,
    method,
    headers,
    credentials: "same-origin",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (response.status === 401) {
    window.location.assign("/auth/login");
    throw new Error("Требуется авторизация");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

async function refreshAll({ quiet = false, force = false } = {}) {
  if (state.loading && !force) return;
  if (!quiet) setLoading(true);
  try {
    const bootstrap = await api("/api/v1/bootstrap");
    state.data = bootstrap;
    state.csrf = bootstrap.user.csrf || "";
    const [leads, campaigns, drafts, workspace, knowledge, inbox, sessions] = await Promise.all([
      api("/api/v1/leads?limit=200"),
      api("/api/v1/campaigns"),
      api("/api/v1/drafts"),
      api("/api/v1/workspace/members"),
      api("/api/v1/company/knowledge?limit=500"),
      api("/api/v1/agent/inbox?limit=200"),
      api("/api/v1/conversation-agent/sessions?limit=200"),
    ]);
    state.leads = leads;
    state.campaigns = campaigns;
    state.drafts = drafts;
    state.workspaceMembers = workspace.members || [];
    state.workspaceInvitations = workspace.invitations || [];
    state.companyKnowledge = knowledge || [];
    state.inbox = inbox || [];
    state.conversationSessions = sessions || [];
    renderAll();
  } catch (error) {
    showToast(error.message || "Не удалось обновить данные", true);
  } finally {
    if (!quiet) setLoading(false);
  }
}

function setMobileNavigation(open) {
  const sidebar = $(".sidebar");
  const toggle = $("#mobile-nav-toggle");
  const workspace = $("#main-content");
  const expanded = Boolean(open);
  sidebar.classList.toggle("is-open", expanded);
  toggle.setAttribute("aria-expanded", String(expanded));
  toggle.setAttribute("aria-label", expanded ? "Закрыть меню" : "Открыть меню");
  workspace.inert = expanded;
  document.body.classList.toggle("nav-open", expanded);
  if (expanded) requestAnimationFrame(() => $(".nav-item.is-active")?.focus());
}

function navigate(view) {
  const next = VIEW_TITLES[view] ? view : "overview";
  setMobileNavigation(false);
  $$(".view").forEach((panel) => panel.classList.toggle("is-visible", panel.dataset.panel === next));
  $$(".nav-item").forEach((item) => {
    const active = item.dataset.view === next;
    item.classList.toggle("is-active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  $("#page-title").textContent = VIEW_TITLES[next];
  if (window.location.hash !== VIEW_HASHES[next]) history.replaceState(null, "", VIEW_HASHES[next]);
  window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  if (state.data) renderView(next);
}

function pageItems(items, key) {
  const pageCount = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  const page = Math.min(Math.max(1, state.pages[key] || 1), pageCount);
  state.pages[key] = page;
  const start = (page - 1) * PAGE_SIZE;
  return { items: items.slice(start, start + PAGE_SIZE), page, pageCount, start };
}

function renderPagination(key, total, page, pageCount, start) {
  const container = $(`#${key}-pagination`);
  if (!container) return;
  if (total <= PAGE_SIZE) {
    container.replaceChildren();
    return;
  }
  const end = Math.min(total, start + PAGE_SIZE);
  container.innerHTML = `
    <span class="pagination-status">${start + 1}–${end} из ${total}</span>
    <button class="secondary-button page-button" type="button" data-page-key="${key}" data-page="${page - 1}" aria-label="Предыдущая страница" ${page === 1 ? "disabled" : ""}>←</button>
    <button class="secondary-button page-button" type="button" data-page-key="${key}" data-page="${page + 1}" aria-label="Следующая страница" ${page === pageCount ? "disabled" : ""}>→</button>`;
}

function renderIdentity() {
  const user = state.data.user;
  $("#user-name").textContent = user.name || "Оператор";
  $("#user-email").textContent = [user.email, user.role].filter(Boolean).join(" · ") || "—";
  $("#user-avatar").textContent = String(user.name || user.email || "OG")
    .split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase();

  const online = Boolean(state.data.whatsapp.reachable && state.data.whatsapp.logged_in);
  $("#connection-dot").classList.toggle("is-online", online);
  const waitingForQR = Boolean(state.data.whatsapp_pairing?.needs_pairing);
  $("#connection-label").textContent = online ? "Сервисы онлайн" : (waitingForQR ? "Нужен QR" : "Требует внимания");
  $("#connection-meta").textContent = online ? "WhatsApp read-only" : (waitingForQR ? "Откройте раздел WhatsApp" : "Проверьте интеграции");

  const safe = state.data.safety.safe_mode && !state.data.safety.whatsapp_send_enabled && !state.data.safety.autopilot_send_enabled;
  $("#safe-badge").classList.toggle("is-danger", !safe);
  $("#safe-badge").lastChild.textContent = safe ? "SAFE" : "CHECK";
  const canWrite = Boolean(user.capabilities?.write);
  $$('[data-action], #new-campaign-button, .lead-status-select, .toggle[data-vertical-id]').forEach((control) => {
    control.disabled = !canWrite;
  });
}

function renderMetrics() {
  const overview = state.data.overview;
  const metrics = [
    ["Всего лидов", overview.lead_count ?? state.data.crm.leads, "В постоянной CRM"],
    ["Qualified", overview.by_status?.qualified || 0, "Готовы к персонализации"],
    ["Черновики", state.data.crm.outreach_drafts || 0, "Ничего не отправлено автоматически"],
    ["Средний score", Number(overview.average_score || 0).toFixed(1), `Максимум ${overview.top_score || 0}`],
  ];
  $("#metric-grid").innerHTML = metrics.map(([name, value, note]) => `
    <article class="metric-card"><span class="metric-label">${escapeHtml(name)}</span><strong class="metric-value">${escapeHtml(value)}</strong><span class="metric-note">${escapeHtml(note)}</span></article>
  `).join("");
}

function renderFunnel() {
  const statuses = state.data.overview.by_status || {};
  const rows = ["new", "analyzed", "qualified", "drafted", "approved", "contacted", "replied", "won"];
  const max = Math.max(1, ...rows.map((item) => statuses[item] || 0));
  $("#funnel").innerHTML = rows.map((item) => {
    const count = statuses[item] || 0;
    return `<div class="funnel-row"><span>${escapeHtml(label(item))}</span><progress class="funnel-progress" max="${max}" value="${count}" aria-label="${escapeHtml(label(item))}: ${count}"></progress><strong class="funnel-value">${count}</strong></div>`;
  }).join("");
  $("#updated-at").textContent = `Обновлено ${new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(new Date())}`;
}

function renderSafety() {
  const safety = state.data.safety;
  const items = [
    ["Autopilot в SAFE", safety.safe_mode],
    ["WhatsApp send выключен", !safety.whatsapp_send_enabled],
    ["Autopilot send выключен", !safety.autopilot_send_enabled],
    ["Отправка скрыта из панели", !safety.send_controls_exposed],
    ["Очередь отправки пуста", (state.data.autopilot.pending_send_requests || 0) === 0],
  ];
  $("#safety-list").innerHTML = items.map(([name, good]) => `<div class="safety-item"><span>${escapeHtml(name)}</span><strong class="check ${good ? "" : "is-off"}">${good ? "✓" : "!"}</strong></div>`).join("");
}

function renderTopLeads() {
  const leads = state.data.top_leads || [];
  $("#top-leads").innerHTML = leads.length ? leads.map((lead) => `
    <div class="compact-row"><div><strong>${escapeHtml(lead.company_name)}</strong><small>${escapeHtml([lead.industry, lead.location, label(lead.status)].filter(Boolean).join(" · "))}</small></div><span class="score">${escapeHtml(lead.score ?? "—")}</span></div>
  `).join("") : `<div class="compact-row"><small>Лиды пока не найдены</small></div>`;
}

function renderJobs() {
  const jobs = state.data.jobs || [];
  $("#recent-jobs").innerHTML = jobs.length ? jobs.slice(0, 6).map((job) => `
    <div class="compact-row"><div><strong>${escapeHtml(job.name)}</strong><small>${formatDate(job.created_at)}</small></div>${statusPill(job.status)}</div>
  `).join("") : `<div class="compact-row"><small>Нет фоновых операций</small></div>`;
}

function evidenceStatus(lead) {
  if (!lead.evidence_expires_at) return ["Нет", "is-bad"];
  return new Date(lead.evidence_expires_at) > new Date() ? ["Актуально", "is-good"] : ["Устарело", "is-warn"];
}

function renderLeadFilters() {
  const select = $("#lead-status-filter");
  if (select.options.length === 1) {
    LEAD_STATUSES.forEach((status) => select.add(new Option(label(status), status)));
  }
}

function renderLeads() {
  renderLeadFilters();
  const query = $("#lead-search").value.trim().toLowerCase();
  const status = $("#lead-status-filter").value;
  const minScore = Number($("#lead-score-filter").value || 0);
  const filtered = state.leads.filter((lead) => {
    const haystack = [lead.company_name, lead.industry, lead.location, lead.website_url].join(" ").toLowerCase();
    return (!query || haystack.includes(query)) && (!status || lead.status === status) && Number(lead.score || 0) >= minScore;
  });
  const page = pageItems(filtered, "leads");
  $("#leads-table").innerHTML = filtered.length ? page.items.map((lead) => {
    const [evidence, evidenceClass] = evidenceStatus(lead);
    const options = LEAD_STATUSES.map((item) => `<option value="${item}" ${item === lead.status ? "selected" : ""}>${escapeHtml(label(item))}</option>`).join("");
    return `<tr><td><strong>${escapeHtml(lead.company_name)}</strong><small>${escapeHtml([lead.industry, lead.location].filter(Boolean).join(" · ") || lead.website_url)}</small></td><td><select class="lead-status-select" aria-label="Статус лида ${escapeHtml(lead.company_name)}" data-lead-id="${escapeHtml(lead.id)}" data-current-status="${escapeHtml(lead.status)}">${options}</select></td><td><span class="score">${escapeHtml(lead.score ?? "—")}</span></td><td><span class="status-pill ${evidenceClass}">${evidence}</span></td><td>${formatDate(lead.updated_at)}</td></tr>`;
  }).join("") : `<tr><td class="empty-row" colspan="5">По выбранным фильтрам лидов нет</td></tr>`;
  renderPagination("leads", filtered.length, page.page, page.pageCount, page.start);
}

function renderCampaigns() {
  const page = pageItems(state.campaigns, "campaigns");
  $("#campaign-grid").innerHTML = state.campaigns.length ? page.items.map((campaign) => `
    <article class="card campaign-card"><div>${statusPill(campaign.status)}<h3>${escapeHtml(campaign.name)}</h3><div class="campaign-meta">${escapeHtml([campaign.industry, campaign.location].filter(Boolean).join(" · ") || "Без сегмента")}</div></div><div class="campaign-stats"><div><strong>${escapeHtml(campaign.lead_count || 0)}</strong><small>лидов</small></div><small>${formatDate(campaign.created_at)}</small></div></article>
  `).join("") : `<article class="card"><p class="muted">Кампаний пока нет.</p></article>`;
  renderPagination("campaigns", state.campaigns.length, page.page, page.pageCount, page.start);
}

function renderDrafts() {
  const status = $("#draft-status-filter").value;
  const drafts = state.drafts.filter((draft) => !status || draft.status === status);
  const page = pageItems(drafts, "drafts");
  $("#drafts-table").innerHTML = drafts.length ? page.items.map((draft) => `
    <tr><td><strong>${escapeHtml(draft.channel)}</strong><small>${escapeHtml(draft.recipient || "Получатель не задан")}</small></td><td><strong>${escapeHtml(String(draft.message || "").slice(0, 92))}${String(draft.message || "").length > 92 ? "…" : ""}</strong><small>Fingerprint ${escapeHtml(String(draft.fingerprint || "").slice(0, 12))}</small></td><td>${statusPill(draft.status)}</td><td>${formatDate(draft.created_at)}</td><td>${draft.status === "draft" ? `<button class="secondary-button review-draft" data-draft-id="${escapeHtml(draft.id)}">Проверить</button>` : ""}</td></tr>
  `).join("") : `<tr><td class="empty-row" colspan="5">Черновиков с таким статусом нет</td></tr>`;
  renderPagination("drafts", drafts.length, page.page, page.pageCount, page.start);
}

function renderAutopilot() {
  const autopilot = state.data.autopilot;
  const rows = [
    ["Состояние", autopilot.running ? "Запущен" : "Остановлен"],
    ["Режим", String(autopilot.mode || "—").toUpperCase()],
    ["Интервал", `${autopilot.interval_minutes || 0} мин`],
    ["Вертикалей / цикл", autopilot.max_verticals_per_cycle || 0],
    ["Лидов / вертикаль", autopilot.leads_per_vertical || 0],
    ["Порог score", autopilot.score_threshold || 0],
    ["Следующий цикл", formatDate(autopilot.next_cycle_at)],
  ];
  $("#autopilot-details").innerHTML = rows.map(([name, value]) => `<div><dt>${escapeHtml(name)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");

  $("#vertical-list").innerHTML = (state.data.verticals || []).map((vertical) => `
    <div class="vertical-item"><header><div><strong>${escapeHtml(vertical.name)}</strong><small>${escapeHtml(vertical.region)} · score ${escapeHtml(vertical.min_score)}+ · вес ${escapeHtml(vertical.weight)}</small></div><button class="toggle ${vertical.enabled ? "is-on" : ""}" aria-label="${vertical.enabled ? "Отключить" : "Включить"} ${escapeHtml(vertical.name)}" data-vertical-id="${escapeHtml(vertical.id)}" data-enabled="${vertical.enabled ? "true" : "false"}"></button></header></div>
  `).join("");

  const cycles = state.data.cycles || [];
  $("#cycles-table").innerHTML = cycles.length ? cycles.map((cycle) => `<tr><td>${formatDate(cycle.started_at)}</td><td>${escapeHtml(String(cycle.mode || "").toUpperCase())}</td><td>${statusPill(cycle.status)}</td><td>${escapeHtml((cycle.selected_verticals || []).length)}</td><td>${escapeHtml(cycle.error || "—")}</td></tr>`).join("") : `<tr><td class="empty-row" colspan="5">История циклов пуста</td></tr>`;
}

function renderWhatsApp() {
  const bridge = state.data.whatsapp || {};
  const pairing = state.data.whatsapp_pairing || {};
  const ready = Boolean(bridge.reachable && bridge.logged_in && bridge.ready);
  const badge = $("#whatsapp-state");
  badge.textContent = ready ? "Подключён" : (pairing.needs_pairing ? "Ожидает QR" : "Недоступен");
  badge.classList.toggle("is-ready", ready);
  const rows = [
    ["Bridge", bridge.reachable ? "Доступен" : "Недоступен"],
    ["WhatsApp", bridge.logged_in ? "Авторизован" : "Не авторизован"],
    ["Соединение", bridge.connected ? "Подключено" : "Отключено"],
    ["Аккаунт", bridge.account_jid || "—"],
    ["Pairing", label(pairing.state || "unknown")],
    ["Отправка", bridge.send_enabled ? "Включена" : "Отключена (SAFE)"],
  ];
  $("#whatsapp-details").innerHTML = rows.map(([name, value]) => `<div><dt>${escapeHtml(name)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");

  const image = $("#whatsapp-qr");
  const placeholder = $("#whatsapp-qr-empty");
  const showQR = Boolean(pairing.needs_pairing && pairing.has_qr);
  image.hidden = !showQR;
  placeholder.hidden = showQR;
  if (showQR) {
    const generation = String(pairing.generation || 0);
    if (image.dataset.generation !== generation) {
      image.dataset.generation = generation;
      image.src = `/api/v1/whatsapp/qr?generation=${encodeURIComponent(generation)}`;
    }
  } else {
    const message = placeholder.querySelector("strong");
    const hint = placeholder.querySelector("span");
    if (ready) {
      message.textContent = "Устройство подключено";
      hint.textContent = "Повторная привязка не требуется.";
    } else if (pairing.needs_pairing) {
      message.textContent = "Готовим новый QR";
      hint.textContent = "Код обновится автоматически через несколько секунд.";
    } else {
      message.textContent = "Bridge недоступен";
      hint.textContent = "Проверьте состояние сервиса и повторите попытку.";
    }
  }
}

function knowledgeText(content) {
  if (typeof content === "string") return content;
  if (!content || typeof content !== "object") return "—";
  if (typeof content.details === "string") return content.details;
  return JSON.stringify(content, null, 2);
}

function renderCompany() {
  const onboarding = state.data.company_onboarding || {};
  const profile = onboarding.profile || {};
  const readiness = $("#onboarding-readiness");
  const percent = Number(onboarding.completion_percent || 0);
  readiness.textContent = onboarding.onboarding_status === "ready" ? "Готово к работе" : `Заполнено ${percent}%`;
  readiness.classList.toggle("is-ready", onboarding.onboarding_status === "ready");
  $("#onboarding-progress-bar").style.width = `${Math.max(0, Math.min(percent, 100))}%`;
  $("#company-profile-updated").textContent = formatDate(profile.updated_at, "Ещё не сохранено");

  const form = $("#company-profile-form");
  if (!state.profileDirty && !form.contains(document.activeElement)) {
    ["company_name", "website_url", "industry", "geography", "target_customer", "positioning", "sales_process", "primary_goal", "tone_of_voice", "constraints"].forEach((name) => {
      const field = form.elements.namedItem(name);
      if (field) field.value = profile[name] || "";
    });
  }

  const questions = onboarding.next_questions || [];
  $("#onboarding-questions").innerHTML = questions.length
    ? questions.map((item, index) => `<div class="question-item"><span>${String(index + 1).padStart(2, "0")}</span><p>${escapeHtml(item.prompt)}</p></div>`).join("")
    : `<div class="empty-state"><strong>Интервью завершено</strong><span>ChatGPT продолжит уточнять данные, только если для задачи не хватает фактов.</span></div>`;

  $("#knowledge-count").textContent = `${state.companyKnowledge.length} записей`;
  $("#knowledge-grid").innerHTML = state.companyKnowledge.length
    ? state.companyKnowledge.map((item) => {
      const details = knowledgeText(item.content);
      return `<article class="knowledge-item"><div><span class="eyebrow">${escapeHtml(label(item.category))}</span><h4>${escapeHtml(item.title)}</h4><p>${escapeHtml(details.slice(0, 420))}${details.length > 420 ? "…" : ""}</p></div><button class="text-button archive-knowledge" data-item-id="${escapeHtml(item.id)}">В архив</button></article>`;
    }).join("")
    : `<div class="empty-state"><strong>База знаний пока пуста</strong><span>Добавьте услуги и цены здесь или пройдите интервью в ChatGPT.</span></div>`;
}

function renderConversationAgent() {
  const status = state.data.conversation_agent || {};
  const settings = status.settings || {};
  const summary = status.summary || {};
  const inbox = summary.inbox || {};
  const runtime = status.runtime || {};
  const badge = $("#agent-runtime-state");
  badge.textContent = !runtime.ready
    ? "Playbook выключен"
    : (runtime.health === "degraded" ? "MCP готов · требуется внимание" : "ChatGPT / MCP готов");
  badge.classList.toggle("is-ready", Boolean(runtime.ready));

  const metrics = [
    ["Ждут ChatGPT", Number(inbox.new || 0) + Number(inbox.processing_active ?? inbox.processing ?? 0)],
    ["Черновики", inbox.drafted || 0],
    ["Нужен человек", inbox.needs_review || 0],
    ["SLA просрочено", inbox.sla_overdue || 0],
  ];
  $("#agent-metrics").innerHTML = metrics.map(([name, value]) => `<article class="metric-card"><span>${escapeHtml(name)}</span><strong>${Number(value)}</strong></article>`).join("");

  const form = $("#conversation-agent-form");
  if (!state.agentDirty && !form.contains(document.activeElement)) {
    ["niche", "objective", "tone", "instructions", "autonomy_mode", "confidence_threshold", "max_context_messages", "max_reply_chars", "max_inbound_age_hours", "response_sla_minutes"].forEach((name) => {
      const field = form.elements.namedItem(name);
      if (field) field.value = settings[name] ?? "";
    });
    ["qualification_questions", "forbidden_topics", "escalation_rules"].forEach((name) => {
      const field = form.elements.namedItem(name);
      if (field) field.value = Array.isArray(settings[name]) ? settings[name].join("\n") : "";
    });
    form.elements.namedItem("enabled").checked = Boolean(settings.enabled);
    form.elements.namedItem("auto_create_inbound_leads").checked = Boolean(settings.auto_create_inbound_leads);
  }

  const runtimeRows = [
    ["Мозг", "ChatGPT через MCP"],
    ["Режим", label(settings.autonomy_mode || "draft")],
    ["Ниша", settings.niche === "auto" ? "Определяется по контексту" : (settings.niche || "—")],
    ["Память компании", runtime.company_ready ? "Готова" : "Можно дополнять в чате"],
    ["Синхронизация WhatsApp", "Каждые 15 минут"],
    ["Окно свежести", `${settings.max_inbound_age_hours || 168} ч`],
    ["SLA ответа", `${settings.response_sla_minutes || 60} мин`],
    ["Самое долгое открытое", formatMinutes(inbox.oldest_open_minutes || 0)],
    ["Lease очереди", Number(inbox.processing_expired || 0) ? `Просрочено: ${Number(inbox.processing_expired)}` : "Норма"],
    ["LLM API-ключ", "Не используется"],
    ["Отправка", "Только после отдельного одобрения"],
  ];
  $("#agent-runtime-details").innerHTML = runtimeRows.map(([name, value]) => `<div><dt>${escapeHtml(name)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");

  const stage = $("#agent-stage-filter").value;
  const sessions = state.conversationSessions.filter((item) => !stage || item.stage === stage);
  $("#agent-sessions-table").innerHTML = sessions.length
    ? sessions.map((item) => {
      const lead = state.leads.find((candidate) => candidate.id === item.lead_id);
      const rawChat = String(item.external_chat_id || "").split("@", 1)[0].replace(/\D/g, "");
      const chatLabel = lead?.company_name || (rawChat ? `WhatsApp ••••${rawChat.slice(-4)}` : "Диалог");
      const context = String(item.summary || "Контекст формируется");
      const nextAction = item.escalation_status === "required"
        ? `Передать человеку: ${item.escalation_reason || "нужна проверка"}`
        : (item.next_action || item.unanswered_question || "Ожидать ответ");
      return `<tr><td><strong>${escapeHtml(chatLabel)}</strong><small>${escapeHtml(`${item.turn_count || 0} сообщений обработано`)}</small></td><td>${statusPill(item.stage)}</td><td>${escapeHtml(item.intent || "—")}</td><td class="message-preview">${escapeHtml(context.slice(0, 240))}${context.length > 240 ? "…" : ""}</td><td class="message-preview">${escapeHtml(String(nextAction).slice(0, 220))}</td><td>${formatDate(item.updated_at)}</td></tr>`;
    }).join("")
    : `<tr><td class="empty-row" colspan="6">Активных диалогов с таким этапом пока нет</td></tr>`;
}

function renderInbox() {
  const summary = state.data.agent_inbox || {};
  const metrics = [
    ["Новые", summary.new || 0],
    ["SLA просрочено", summary.sla_overdue || 0],
    ["Самое долгое", formatMinutes(summary.oldest_open_minutes || 0)],
    ["Нужен человек", summary.needs_review || 0],
  ];
  $("#inbox-metrics").innerHTML = metrics.map(([name, value]) => `<article class="metric-card"><span>${escapeHtml(name)}</span><strong>${escapeHtml(String(value))}</strong></article>`).join("");
  const filter = $("#inbox-status-filter").value;
  const events = state.inbox.filter((item) => !filter || item.status === filter);
  $("#inbox-table").innerHTML = events.length
    ? events.map((item) => {
      const message = String(item.message_text || "");
      const leadControl = item.lead_id
        ? `<code>${escapeHtml(String(item.lead_id).slice(0, 8))}</code>`
        : `<select class="inbox-lead-select" data-event-id="${escapeHtml(item.id)}" aria-label="Сопоставить входящее с лидом"><option value="">Выбрать лид…</option>${state.leads.map((lead) => `<option value="${escapeHtml(lead.id)}">${escapeHtml(lead.company_name)}</option>`).join("")}</select>`;
      const actions = [];
      if (item.status === "new") actions.push(`<button class="text-button inbox-status-action" data-event-id="${escapeHtml(item.id)}" data-status="acknowledged">В работу</button>`);
      if (item.status === "needs_review") {
        const retryTitle = item.retryable ? "Вернуть в очередь ChatGPT" : label(item.retry_block_reason);
        actions.push(`<button class="text-button inbox-retry-action" data-event-id="${escapeHtml(item.id)}" title="${escapeHtml(retryTitle)}" ${item.retryable ? "" : "disabled"}>Повторить</button>`);
        actions.push(`<button class="text-button inbox-status-action" data-event-id="${escapeHtml(item.id)}" data-status="ignored">Игнор</button>`);
      }
      if (!["resolved", "ignored"].includes(item.status)) actions.push(`<button class="text-button inbox-status-action" data-event-id="${escapeHtml(item.id)}" data-status="resolved">Закрыть</button>`);
      const decision = item.decision || {};
      const decisionMeta = decision.intent ? `<small>${escapeHtml(decision.intent)} · ${escapeHtml(decision.confidence ?? "—")}%</small>` : "";
      const errorMeta = item.agent_error ? `<small class="inbox-error">${escapeHtml(String(item.agent_error).slice(0, 240))}</small>` : "";
      const sla = item.sla_state === "closed" ? "" : `<small>${statusPill(item.sla_state)} · ${escapeHtml(formatMinutes(item.age_minutes))}</small>`;
      return `<tr><td data-label="Получено">${escapeHtml(formatDate(item.received_at))}${sla}</td><td data-label="Контакт"><strong>${escapeHtml(item.sender_label || item.chat_jid)}</strong><small>${escapeHtml(item.chat_jid)}</small></td><td data-label="Сообщение" class="message-preview">${escapeHtml(message.slice(0, 320))}${message.length > 320 ? "…" : ""}${decisionMeta}${errorMeta}</td><td data-label="Лид">${leadControl}</td><td data-label="Статус">${statusPill(item.status)}</td><td data-label="Действие"><div class="inbox-actions">${actions.join("")}</div></td></tr>`;
    }).join("")
    : `<tr class="empty-inbox-row"><td colspan="6"><div class="empty-state"><strong>Нет входящих с таким статусом</strong><span>Фоновая синхронизация проверяет личные чаты каждые 15 минут.</span></div></td></tr>`;
}

function renderTeam() {
  const user = state.data.user;
  const workspace = state.data.workspace || {};
  const canManage = Boolean(user.capabilities?.manage_members);
  $("#workspace-name").textContent = workspace.name || user.workspace_name || "Workspace";
  $("#workspace-role").textContent = String(user.role || "viewer").toUpperCase();
  $("#invite-card").hidden = !canManage;

  $("#members-table").innerHTML = state.workspaceMembers.length ? state.workspaceMembers.map((member) => {
    const roleControl = canManage && member.id !== user.member_id
      ? `<select class="role-select member-role-select" aria-label="Роль участника ${escapeHtml(member.display_name || member.email)}" data-member-id="${escapeHtml(member.id)}" data-current-role="${escapeHtml(member.role)}"><option value="viewer" ${member.role === "viewer" ? "selected" : ""}>Viewer</option><option value="operator" ${member.role === "operator" ? "selected" : ""}>Operator</option><option value="owner" ${member.role === "owner" ? "selected" : ""}>Owner</option></select>`
      : statusPill(member.role);
    return `<tr><td><strong>${escapeHtml(member.display_name || member.email)}</strong><small>${escapeHtml(member.email)}</small></td><td>${roleControl}</td><td>${statusPill(member.status)}</td><td>${formatDate(member.last_login_at)}</td></tr>`;
  }).join("") : `<tr><td class="empty-row" colspan="4">Участники ещё не зарегистрированы</td></tr>`;

  $("#invitations-table").innerHTML = canManage && state.workspaceInvitations.length ? state.workspaceInvitations.map((invitation) => `<tr><td><strong>${escapeHtml(invitation.email)}</strong></td><td>${statusPill(invitation.role)}</td><td>${formatDate(invitation.created_at)}</td><td>${formatDate(invitation.expires_at)}</td></tr>`).join("") : `<tr><td class="empty-row" colspan="4">Нет активных приглашений</td></tr>`;
}

async function refreshWhatsApp({ quiet = false } = {}) {
  try {
    const payload = await api("/api/v1/whatsapp/status");
    state.data.whatsapp = payload.bridge || {};
    state.data.whatsapp_pairing = payload.pairing || {};
    renderWhatsApp();
    renderIdentity();
  } catch (error) {
    if (!quiet) showToast(error.message, true);
  }
}

async function inviteMember(event) {
  event.preventDefault();
  const form = $("#invite-form");
  const values = Object.fromEntries(new FormData(form));
  try {
    await api("/api/v1/workspace/invitations", { method: "POST", body: values });
    form.reset();
    showToast("Приглашение создано. Автоматическая отправка письма не выполнялась.");
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderPlugin() {
  const plugin = state.data.plugin;
  const readiness = $("#plugin-readiness");
  readiness.textContent = plugin.ready ? "Готов к подключению" : "Нужна конфигурация";
  readiness.classList.toggle("is-ready", plugin.ready);
  const fields = [
    ["Название", plugin.name], ["Описание", plugin.description],
    ["Мозг агента", plugin.brain],
    ["Синхронизация входящих", `Каждые ${plugin.server_sync_interval_minutes} минут`],
    ["Расписание ChatGPT", plugin.recommended_chatgpt_schedule === "hourly_in_chat" ? "Каждый час в этом чате" : plugin.recommended_chatgpt_schedule],
    ["Промпт для расписания", plugin.scheduled_prompt],
    ["URL-адрес сервера", plugin.server_url], ["Аутентификация", plugin.authentication],
    ["Личный кабинет", plugin.dashboard_url],
    ["Authorization server", plugin.authorization_server],
    ["Protected resource metadata", plugin.protected_resource_metadata],
  ];
  $("#plugin-fields").innerHTML = fields.map(([name, value]) => `<div class="copy-field"><label>${escapeHtml(name)}</label><div class="copy-row"><code translate="no">${escapeHtml(value || "Не настроено")}</code><button class="icon-button copy-value" data-copy="${escapeHtml(value || "")}" aria-label="Копировать">⧉</button></div></div>`).join("");
  const names = {
    https_resource_url: "Публичный HTTPS /mcp", oidc_mode: "Режим OIDC",
    issuer_configured: "OIDC issuer", audience_configured: "Audience API",
    admin_client_configured: "Web OAuth client", beta_allowlist_configured: "Allowlist тестировщиков",
    session_secret_configured: "Секрет сессии",
  };
  $("#plugin-checks").innerHTML = Object.entries(plugin.checks).map(([key, good]) => `<div class="safety-item"><span>${escapeHtml(names[key] || key)}</span><strong class="check ${good ? "" : "is-off"}">${good ? "✓" : "!"}</strong></div>`).join("");
}

function renderAudit() {
  const events = state.data.audit || [];
  const page = pageItems(events, "audit");
  $("#audit-table").innerHTML = events.length ? page.items.map((event) => `<tr><td>${formatDate(event.created_at)}</td><td><strong>${escapeHtml(event.actor)}</strong></td><td>${escapeHtml(event.action)}</td><td><small>${escapeHtml([event.target_type, event.target_id].filter(Boolean).join(" · ") || "—")}</small></td><td>${statusPill(event.outcome)}</td></tr>`).join("") : `<tr><td class="empty-row" colspan="5">Журнал пока пуст</td></tr>`;
  renderPagination("audit", events.length, page.page, page.pageCount, page.start);
}

function renderView(view) {
  if (view === "overview") {
    renderMetrics(); renderFunnel(); renderSafety(); renderTopLeads(); renderJobs();
  } else if (view === "company") renderCompany();
  else if (view === "agent") renderConversationAgent();
  else if (view === "inbox") renderInbox();
  else if (view === "leads") renderLeads();
  else if (view === "campaigns") renderCampaigns();
  else if (view === "drafts") renderDrafts();
  else if (view === "autopilot") renderAutopilot();
  else if (view === "whatsapp") renderWhatsApp();
  else if (view === "team") renderTeam();
  else if (view === "plugin") renderPlugin();
  else if (view === "audit") renderAudit();
  const canWrite = Boolean(state.data.user.capabilities?.write);
  $$('[data-action], #new-campaign-button, .lead-status-select, .toggle[data-vertical-id], .review-draft, #company-profile-form input, #company-profile-form textarea, #company-profile-form button, #conversation-agent-form input, #conversation-agent-form textarea, #conversation-agent-form select, #conversation-agent-form button, #process-conversations-button, #knowledge-form input, #knowledge-form textarea, #knowledge-form select, #knowledge-form button, #sync-inbox-button, .inbox-status-action, .archive-knowledge').forEach((control) => {
    control.disabled = !canWrite;
  });
}

function renderAll() {
  renderIdentity();
  renderCompany();
  renderConversationAgent();
  renderInbox();
  renderView($(".view.is-visible")?.dataset.panel || "overview");
}

async function performAction(action) {
  const actions = {
    "run-cycle": ["/api/v1/autopilot/run", {}, "SAFE-цикл поставлен в очередь"],
    "sync-sheets": ["/api/v1/sheets/sync", {}, "Синхронизация поставлена в очередь"],
    "start-autopilot": ["/api/v1/autopilot/start", { mode: "safe" }, "Autopilot запущен в SAFE"],
    "stop-autopilot": ["/api/v1/autopilot/stop", {}, "Autopilot остановлен"],
  };
  const config = actions[action];
  if (!config) return;
  if (action === "stop-autopilot" && !window.confirm("Остановить расписание Autopilot? Данные сохранятся.")) return;
  setLoading(true);
  try {
    await api(config[0], { method: "POST", body: config[1] });
    showToast(config[2]);
    await refreshAll({ quiet: true, force: true });
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setLoading(false);
  }
}

function openDraft(draftId) {
  const draft = state.drafts.find((item) => item.id === draftId);
  if (!draft) return;
  state.currentDraft = draft;
  $("#draft-recipient").value = draft.recipient || "";
  $("#draft-message").value = draft.message || "";
  $("#draft-confirmation").value = "";
  $("#draft-dialog").showModal();
}

async function approveCurrentDraft(event) {
  event.preventDefault();
  const draft = state.currentDraft;
  if (!draft) return;
  try {
    await api(`/api/v1/drafts/${encodeURIComponent(draft.id)}/approve`, {
      method: "POST",
      body: {
        fingerprint: draft.fingerprint,
        recipient: draft.recipient,
        message: draft.message,
        confirmation: $("#draft-confirmation").value,
      },
    });
    $("#draft-dialog").close();
    showToast("Точный черновик одобрен. Отправка не выполнялась.");
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function createCampaign(event) {
  event.preventDefault();
  const form = $("#campaign-form");
  const values = Object.fromEntries(new FormData(form));
  values.target_count = Number(values.target_count);
  try {
    await api("/api/v1/campaigns", { method: "POST", body: values });
    $("#campaign-dialog").close();
    form.reset();
    showToast("Кампания создана");
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function saveCompanyProfile(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const body = Object.fromEntries(new FormData(form).entries());
  try {
    await api("/api/v1/company/profile", { method: "PATCH", body });
    state.profileDirty = false;
    showToast("Профиль сохранён и доступен ChatGPT");
    await refreshAll({ quiet: true, force: true });
  } catch (error) {
    showToast(error.message, true);
  }
}

function linesFromField(form, name) {
  return String(form.elements.namedItem(name)?.value || "")
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
}

async function saveConversationAgent(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const body = {
    enabled: form.elements.namedItem("enabled").checked,
    auto_create_inbound_leads: form.elements.namedItem("auto_create_inbound_leads").checked,
    autonomy_mode: form.elements.namedItem("autonomy_mode").value,
    niche: form.elements.namedItem("niche").value.trim() || "auto",
    objective: form.elements.namedItem("objective").value.trim(),
    tone: form.elements.namedItem("tone").value.trim(),
    instructions: form.elements.namedItem("instructions").value.trim(),
    qualification_questions: linesFromField(form, "qualification_questions"),
    forbidden_topics: linesFromField(form, "forbidden_topics"),
    escalation_rules: linesFromField(form, "escalation_rules"),
    confidence_threshold: Number(form.elements.namedItem("confidence_threshold").value),
    max_context_messages: Number(form.elements.namedItem("max_context_messages").value),
    max_reply_chars: Number(form.elements.namedItem("max_reply_chars").value),
    max_inbound_age_hours: Number(form.elements.namedItem("max_inbound_age_hours").value),
    response_sla_minutes: Number(form.elements.namedItem("response_sla_minutes").value),
  };
  try {
    state.data.conversation_agent = await api("/api/v1/conversation-agent/settings", { method: "PATCH", body });
    state.agentDirty = false;
    renderConversationAgent();
    showToast("Playbook сохранён. ChatGPT применит его в следующем MCP-цикле.");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function processConversations() {
  const button = $("#process-conversations-button");
  button.disabled = true;
  try {
    const result = await api("/api/v1/conversation-agent/process", { method: "POST", body: { scan_limit: 100 } });
    showToast(`Очередь синхронизирована: новых обращений ${Number(result.new_events || 0)}. Обработка выполняется в ChatGPT.`);
    await refreshAll({ quiet: true, force: true });
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = !Boolean(state.data?.user?.capabilities?.write);
  }
}

async function saveCompanyKnowledge(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form).entries());
  try {
    await api("/api/v1/company/knowledge", {
      method: "POST",
      body: {
        category: values.category,
        title: values.title,
        content: { details: values.content },
        source_type: "dashboard",
      },
    });
    form.reset();
    showToast("Факт добавлен в память агента");
    await refreshAll({ quiet: true, force: true });
  } catch (error) {
    showToast(error.message, true);
  }
}

async function archiveCompanyKnowledge(itemId) {
  if (!window.confirm("Переместить этот факт в архив? Он перестанет использоваться агентом.")) return;
  try {
    await api(`/api/v1/company/knowledge/${encodeURIComponent(itemId)}`, { method: "DELETE" });
    showToast("Факт перемещён в архив");
    await refreshAll({ quiet: true, force: true });
  } catch (error) {
    showToast(error.message, true);
  }
}

async function syncAgentInbox() {
  try {
    const result = await api("/api/v1/agent/inbox/sync", { method: "POST", body: { scan_limit: 100 } });
    showToast(result.new_events ? `Новых обращений: ${result.new_events}` : "Новых обращений нет");
    await refreshAll({ quiet: true, force: true });
  } catch (error) {
    showToast(error.message, true);
  }
}

async function updateInboxStatus(eventId, status) {
  try {
    await api(`/api/v1/agent/inbox/${encodeURIComponent(eventId)}`, { method: "PATCH", body: { status } });
    showToast("Статус обращения обновлён");
    await refreshAll({ quiet: true, force: true });
  } catch (error) {
    showToast(error.message, true);
  }
}

async function retryInboxEvent(eventId) {
  if (!window.confirm("Вернуть это обращение в очередь ChatGPT? Попытки будут сброшены, отправка останется выключена.")) return;
  try {
    await api(`/api/v1/agent/inbox/${encodeURIComponent(eventId)}/retry`, { method: "POST", body: { confirm_retry: true } });
    showToast("Обращение возвращено в очередь ChatGPT");
    await refreshAll({ quiet: true, force: true });
  } catch (error) {
    showToast(error.message, true);
  }
}

function bindEvents() {
  $("#logout-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const csrf = state.data?.user?.csrf || "";
    const response = await fetch("/auth/logout", {
      method: "POST",
      headers: { "X-CSRF-Token": csrf },
      credentials: "same-origin",
      redirect: "manual",
    });
    if (response.status === 403) {
      showToast("Сессия изменилась. Обновите страницу.", true);
      return;
    }
    window.location.assign("/auth/login");
  });

  $("#mobile-nav-toggle").addEventListener("click", () => {
    setMobileNavigation(!$(".sidebar").classList.contains("is-open"));
  });
  $(".brand").addEventListener("click", (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate("overview");
    $("#main-content").focus({ preventScroll: true });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $(".sidebar").classList.contains("is-open")) {
      setMobileNavigation(false);
      $("#mobile-nav-toggle").focus();
    }
  });
  $$(".nav-item").forEach((link) => link.addEventListener("click", (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(link.dataset.view);
    $("#main-content").focus({ preventScroll: true });
  }));
  $$('[data-view-link]').forEach((button) => button.addEventListener("click", () => navigate(button.dataset.viewLink)));
  $("#refresh-button").addEventListener("click", () => refreshAll());
  $("#refresh-whatsapp-button").addEventListener("click", () => refreshWhatsApp());
  $$('[data-action]').forEach((button) => button.addEventListener("click", () => performAction(button.dataset.action)));
  ["#lead-search", "#lead-status-filter", "#lead-score-filter"].forEach((selector) => $(selector).addEventListener("input", () => {
    state.pages.leads = 1;
    renderLeads();
  }));
  $("#draft-status-filter").addEventListener("change", () => {
    state.pages.drafts = 1;
    renderDrafts();
  });
  $("#new-campaign-button").addEventListener("click", () => $("#campaign-dialog").showModal());
  $("#approve-draft-button").addEventListener("click", approveCurrentDraft);
  $("#create-campaign-button").addEventListener("click", createCampaign);
  $("#invite-form").addEventListener("submit", inviteMember);
  $("#company-profile-form").addEventListener("input", () => { state.profileDirty = true; });
  $("#company-profile-form").addEventListener("submit", saveCompanyProfile);
  $("#conversation-agent-form").addEventListener("input", () => { state.agentDirty = true; });
  $("#conversation-agent-form").addEventListener("submit", saveConversationAgent);
  $("#process-conversations-button").addEventListener("click", processConversations);
  $("#agent-stage-filter").addEventListener("change", renderConversationAgent);
  $("#knowledge-form").addEventListener("submit", saveCompanyKnowledge);
  $("#inbox-status-filter").addEventListener("change", renderInbox);
  $("#sync-inbox-button").addEventListener("click", syncAgentInbox);
  $("#whatsapp-qr").addEventListener("error", () => {
    $("#whatsapp-qr").hidden = true;
    $("#whatsapp-qr-empty").hidden = false;
  });

  document.addEventListener("click", async (event) => {
    const pageButton = event.target.closest(".page-button[data-page-key][data-page]");
    if (pageButton && !pageButton.disabled) {
      const key = pageButton.dataset.pageKey;
      state.pages[key] = Number(pageButton.dataset.page);
      const renderers = { leads: renderLeads, campaigns: renderCampaigns, drafts: renderDrafts, audit: renderAudit };
      renderers[key]?.();
      $(`#${key}-pagination`)?.scrollIntoView({ block: "nearest" });
      return;
    }

    const review = event.target.closest(".review-draft");
    if (review) openDraft(review.dataset.draftId);

    const inboxAction = event.target.closest(".inbox-status-action");
    if (inboxAction) {
      await updateInboxStatus(inboxAction.dataset.eventId, inboxAction.dataset.status);
      return;
    }

    const inboxRetry = event.target.closest(".inbox-retry-action");
    if (inboxRetry && !inboxRetry.disabled) {
      await retryInboxEvent(inboxRetry.dataset.eventId);
      return;
    }

    const archiveKnowledge = event.target.closest(".archive-knowledge");
    if (archiveKnowledge) {
      await archiveCompanyKnowledge(archiveKnowledge.dataset.itemId);
      return;
    }

    const copy = event.target.closest(".copy-value");
    if (copy && copy.dataset.copy) {
      try { await navigator.clipboard.writeText(copy.dataset.copy); showToast("Скопировано"); }
      catch { showToast("Не удалось скопировать", true); }
    }

    const toggle = event.target.closest(".toggle[data-vertical-id]");
    if (toggle) {
      const enabled = toggle.dataset.enabled !== "true";
      try {
        await api(`/api/v1/verticals/${encodeURIComponent(toggle.dataset.verticalId)}`, { method: "PATCH", body: { enabled } });
        showToast(enabled ? "Вертикаль включена" : "Вертикаль отключена");
        await refreshAll();
      } catch (error) { showToast(error.message, true); }
    }
  });

  document.addEventListener("change", async (event) => {
    const inboxLead = event.target.closest(".inbox-lead-select");
    if (inboxLead) {
      if (!inboxLead.value) return;
      try {
        const updated = await api(`/api/v1/agent/inbox/${encodeURIComponent(inboxLead.dataset.eventId)}`, { method: "PATCH", body: { lead_id: inboxLead.value } });
        state.inbox = state.inbox.map((item) => item.id === updated.id ? updated : item);
        showToast("Входящее сопоставлено с лидом");
        renderInbox();
      } catch (error) {
        inboxLead.value = "";
        showToast(error.message, true);
      }
      return;
    }
    const memberRole = event.target.closest(".member-role-select");
    if (memberRole) {
      const previous = memberRole.dataset.currentRole;
      if (!window.confirm(`Изменить роль участника на «${memberRole.value}»?`)) {
        memberRole.value = previous;
        return;
      }
      try {
        await api(`/api/v1/workspace/members/${encodeURIComponent(memberRole.dataset.memberId)}`, { method: "PATCH", body: { role: memberRole.value } });
        showToast("Роль участника обновлена");
        await refreshAll();
      } catch (error) {
        memberRole.value = previous;
        showToast(error.message, true);
      }
      return;
    }
    const select = event.target.closest(".lead-status-select");
    if (!select) return;
    const previous = select.dataset.currentStatus;
    if (!window.confirm(`Изменить статус на «${label(select.value)}»?`)) {
      select.value = previous;
      return;
    }
    try {
      await api(`/api/v1/leads/${encodeURIComponent(select.dataset.leadId)}`, { method: "PATCH", body: { status: select.value } });
      showToast("Статус лида обновлён");
      await refreshAll();
    } catch (error) {
      select.value = previous;
      showToast(error.message, true);
    }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  const initialView = Object.entries(VIEW_HASHES).find(([, hash]) => hash === window.location.hash)?.[0] || "overview";
  navigate(initialView);
  await refreshAll();
  window.setInterval(() => {
    if (!document.hidden && !state.loading) refreshAll({ quiet: true });
  }, 30000);
  window.setInterval(() => {
    if (!document.hidden && state.data) refreshWhatsApp({ quiet: true });
  }, 5000);
});
