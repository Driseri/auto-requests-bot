const REFRESH_MS = 30000;

const icons = {
  alert: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5M12 17h.01"/></svg>',
  check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m20 6-11 11-5-5"/></svg>',
  server: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16v5H4zM4 13h16v5H4z"/><path d="M7 8h.01M7 15h.01"/></svg>',
  pulse: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2-7 4 14 2-7h6"/></svg>',
  send: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m22 2-7 20-4-9-9-4 20-7Z"/><path d="M22 2 11 13"/></svg>',
  google: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12.2h-8.8"/><path d="M20 8a9 9 0 1 0 .3 8"/><path d="M20.3 16A9 9 0 0 0 21 12"/></svg>',
  queue: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/></svg>',
  file: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h5"/></svg>',
  layers: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 17l9 5 9-5"/></svg>',
  spark: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m13 2-9 12h8l-1 8 9-12h-8l1-8Z"/></svg>',
  bot: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8V4M8 4h8"/><rect x="4" y="8" width="16" height="12" rx="3"/><path d="M9 13h.01M15 13h.01M9 17h6"/></svg>',
  target: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><path d="M12 7v5l3 2"/></svg>',
  users: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/></svg>',
  compass: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m16 8-3 7-7 3 3-7 7-3Z"/><circle cx="12" cy="12" r="10"/></svg>',
};

let activeTab = "monitoring";

const qs = (selector) => document.querySelector(selector);
const qsa = (selector) => [...document.querySelectorAll(selector)];

function esc(value) {
  return String(value ?? "-")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function value(value, fallback = "-") {
  return value === null || value === undefined || value === "" ? fallback : value;
}

function number(value, fallback = "0") {
  return Number(value || 0).toLocaleString("ru-RU") || fallback;
}

function statusClass(status) {
  const normalized = String(status || "").toUpperCase();
  if (["OK", "HEALTHY", "RUNNING"].includes(normalized)) return "good";
  if (["CRITICAL", "FAILED", "UNHEALTHY"].includes(normalized)) return "bad";
  if (["DEGRADED", "ACTION_REQUIRED", "WARNING"].includes(normalized)) return "warn";
  return "";
}

function statusLabel(status) {
  // Backend keeps stable English enums; the UI presents them in Russian.
  const labels = {
    OK: "Система работает штатно",
    DEGRADED: "Есть отклонения",
    ACTION_REQUIRED: "Требуется внимание",
    CRITICAL: "Критическое состояние",
    UNKNOWN: "Состояние неизвестно",
  };
  return labels[String(status || "UNKNOWN").toUpperCase()] || String(status);
}

function age(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "-";
  const total = Math.max(0, Math.round(Number(seconds)));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function dateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function iconBox(name) {
  return `<span class="icon-box">${icons[name] || icons.file}</span>`;
}

function cardHeader(index, title, iconName, badge = "") {
  const indexHtml = index ? `<span class="card-index">${index}</span>` : "";
  return `
    <div class="card-header">
      <div class="card-title">${indexHtml}${iconBox(iconName)}<span>${esc(title)}</span></div>
      ${badge}
    </div>
  `;
}

function row(label, rowValue, css = "") {
  return `<div class="metric-row"><span class="metric-label">${esc(label)}</span><span class="metric-value ${css}">${rowValue}</span></div>`;
}

function pill(text, css = "") {
  return `<span class="pill ${css}">${esc(text)}</span>`;
}

function progress(label, current, max, percent) {
  const safePercent = Math.max(0, Math.min(100, Number(percent || 0)));
  return `
    <div class="stack">
      <div class="metric-row"><span class="metric-label">${esc(label)}</span><span class="metric-value">${esc(current)}${max ? ` / ${esc(max)}` : ""} ${safePercent ? `${safePercent}%` : ""}</span></div>
      <div class="bar" aria-hidden="true" style="--value:${safePercent}%"><span></span></div>
    </div>
  `;
}

function switchTab(tab) {
  activeTab = tab;
  qsa("[data-view]").forEach((view) => view.classList.toggle("is-visible", view.dataset.view === tab));
  qsa(".rail-item[data-tab]").forEach((button) => {
    const isActive = button.dataset.tab === tab;
    button.classList.toggle("is-active", isActive);
    button.setAttribute("aria-current", isActive ? "page" : "false");
  });
  qs("#page-title").textContent = tab === "applications" ? "Заявки" : "Мониторинг";
  qs("#page-subtitle").textContent = tab === "applications" ? "Проблемные и зависшие заявки" : "Alfa Auto Requests Bot";
  qs("#refresh-button").classList.toggle("hidden", tab === "applications");
  if (tab === "applications") loadApplicationReportLatest().catch(() => {});
}

function initialTabFromHash() {
  // Deep links keep the static app simple while allowing direct navigation to tabs.
  return window.location.hash === "#applications" ? "applications" : "monitoring";
}

function renderTop(snapshot) {
  const container = snapshot.container || {};
  const polling = snapshot.polling || {};
  const external = snapshot.external_health || {};
  const summaryStatus = snapshot.summary?.status || "UNKNOWN";
  const overall = qs(".overall-state");
  overall.classList.toggle("is-warning", ["DEGRADED", "ACTION_REQUIRED"].includes(summaryStatus));
  overall.classList.toggle("is-critical", summaryStatus === "CRITICAL");
  qs("#top-overall").textContent = statusLabel(summaryStatus);
  qs("#top-version").textContent = value(container.app_version || snapshot.raw?.app_version);
  qs("#top-uptime").textContent = value(container.started_at ? "см. контейнер" : "-");
  qs("#top-restarts").textContent = value(container.restart_count, 0);
  qs("#top-container").textContent = value(container.state || container.health);
  qs("#top-container").className = statusClass(container.health || container.state);
  qs("#top-heartbeat").textContent = age(polling.heartbeat_age_seconds);
  qs("#top-heartbeat").className = polling.heartbeat_age_seconds > 180 ? "warn" : "good";
  qs("#top-external").textContent = value(external.result || (external._error ? "unavailable" : "ok"));
  qs("#top-external").className = external.consecutive_failures ? "warn" : "good";
  qs("#top-updated").textContent = dateTime(snapshot.collected_at);
}

function renderSummary(snapshot) {
  const summary = snapshot.summary || {};
  const status = summary.status || "UNKNOWN";
  const css = statusClass(status);
  const problems = summary.problems || [];
  qs("#card-summary").innerHTML = `
    ${cardHeader(1, "Общий статус", status === "OK" ? "check" : "alert")}
    <div class="status-symbol">${status === "OK" ? icons.check : icons.alert}</div>
    <div class="status-word ${css}">${esc(statusLabel(status))}</div>
    <p class="status-note">${esc(problems[0] || summary.recommendations?.[0] || "Основные показатели в норме.")}</p>
    <p class="muted status-note">Последняя смена: ${dateTime(snapshot.collected_at)}</p>
  `;
}

function renderContainer(snapshot) {
  const container = snapshot.container || {};
  qs("#card-container").innerHTML = `
    ${cardHeader(2, "Контейнер", "server")}
    <div class="metric-list">
      ${row("Состояние", esc(value(container.state)), statusClass(container.state))}
      ${row("Health", esc(value(container.health)), statusClass(container.health))}
      ${row("Started at", esc(dateTime(container.started_at)))}
      ${row("Restarts", esc(value(container.restart_count, 0)))}
      ${row("OOM killed", esc(container.oom_killed ? "yes" : "no"), container.oom_killed ? "bad" : "good")}
    </div>
  `;
}

function renderVps(snapshot) {
  const vps = snapshot.vps || {};
  const mem = vps.memory?.mem || {};
  const swap = vps.memory?.swap || {};
  const disk = vps.disk || {};
  const memPercent = mem.total_mb ? Math.round((mem.used_mb / mem.total_mb) * 100) : 0;
  const swapPercent = swap.total_mb ? Math.round((swap.used_mb / swap.total_mb) * 100) : 0;
  qs("#card-vps").innerHTML = `
    ${cardHeader(3, "VPS", "pulse")}
    <div class="metric-list">
      ${row("Load 1/5/15", esc(vps.load ? `${value(vps.load["1m"])} / ${value(vps.load["5m"])} / ${value(vps.load["15m"])}` : "-"))}
      ${row("CPU cores", esc(value(vps.cpu_count)))}
      ${progress("RAM", `${value(mem.used_mb)} MB`, `${value(mem.total_mb)} MB`, memPercent)}
      ${progress("Swap", `${value(swap.used_mb)} MB`, `${value(swap.total_mb)} MB`, swapPercent)}
      ${progress("Disk /", value(disk.used), value(disk.size), disk.used_percent)}
    </div>
  `;
}

function renderPolling(snapshot) {
  const polling = snapshot.polling || {};
  qs("#card-polling").innerHTML = `
    ${cardHeader(4, "Polling", "pulse", pill("без истории"))}
    <div class="metric-list">
      ${row("Heartbeat age", esc(age(polling.heartbeat_age_seconds)), polling.heartbeat_age_seconds > polling.max_age_seconds ? "bad" : "good")}
      ${row("Iteration", esc(value(polling.iteration)))}
      ${row("Max age", esc(age(polling.max_age_seconds)))}
      ${row("Last duration", esc("нет данных"), "muted")}
      ${row("p50 / p95", esc("накопится позже"), "muted")}
    </div>
  `;
}

function renderTelegram(snapshot) {
  const q = snapshot.queues?.notification_outbox || {};
  const external = snapshot.external_health || {};
  qs("#card-telegram").innerHTML = `
    ${cardHeader("", "Telegram", "send")}
    <div class="metric-list">
      ${row("External health", esc(value(external.result || "ok")), external.consecutive_failures ? "warn" : "good")}
      ${row("Outbox state", esc(q.failed || q.sending ? "attention" : "ok"), q.failed || q.sending ? "warn" : "good")}
      ${row("Pending", esc(number(q.pending)))}
      ${row("Sending", esc(number(q.sending)))}
      ${row("Failed", esc(number(q.failed)), q.failed ? "bad" : "")}
    </div>
  `;
}

function renderGoogle(snapshot) {
  const events = snapshot.log_events || {};
  const total = Object.entries(events)
    .filter(([key]) => key.startsWith("google_"))
    .reduce((sum, [, item]) => sum + Number(item.count || 0), 0);
  qs("#card-google").innerHTML = `
    ${cardHeader("", "Google", "google", pill("окно логов"))}
    <div class="metric-list">
      ${row("External health", esc(value(snapshot.external_health?.result || "ok")), snapshot.external_health?.consecutive_failures ? "warn" : "good")}
      ${row("429 quota/rate", esc(number(events.google_429?.count)))}
      ${row("400/403 schema/rights", esc(number(events.google_400_403?.count)))}
      ${row("5xx/timeout", esc(number(events.google_transient?.count)))}
      ${row("Итого", esc(number(total)), total ? "warn" : "good")}
    </div>
  `;
}

function renderManagerFocus(snapshot) {
  const focus = snapshot.business?.manager_focus || {};
  const risks = focus.risks || [];
  qs("#card-manager-focus").innerHTML = `
    ${cardHeader("", "Что важно менеджеру", "target", pill(focus.is_clear ? "спокойно" : "требует внимания", focus.is_clear ? "good" : "warn"))}
    <div class="status-word ${focus.is_clear ? "ok" : "warn"}" style="font-size:22px;text-align:left">${esc(focus.action || "Ничего не делать")}</div>
    <div class="risk-list">
      ${
        risks.length
          ? risks.slice(0, 4).map((item) => `<div>${esc(item)}</div>`).join("")
          : "<div>Нет признаков бизнес-проблемы для команды.</div>"
      }
    </div>
  `;
}

function renderBusinessToday(snapshot) {
  const today = snapshot.business?.today || {};
  qs("#card-business-today").innerHTML = `
    ${cardHeader("", "Пилот сегодня", "queue")}
    <div class="metric-list">
      ${row("Создано заявок", esc(number(today.created)))}
      ${row("Срочных создано", esc(number(today.urgent_created)), today.urgent_created ? "warn" : "")}
      ${row("Итоговый ответ готов", esc(number(today.final_answers)), today.final_answers ? "good" : "")}
      ${row("Пачек зарегистрировано", esc(number(today.bulk_registered)))}
    </div>
  `;
}

function renderTeamLoad(snapshot) {
  const load = snapshot.business?.team_load || {};
  qs("#card-team-load").innerHTML = `
    ${cardHeader("", "Загрузка команды", "users")}
    <div class="metric-list">
      ${row("Заявки без редактора", esc(number(load.without_editor)), load.without_editor ? "warn" : "good")}
      ${row("Срочные без ответственного", esc(number(load.urgent_without_editor)), load.urgent_without_editor ? "bad" : "good")}
      ${row("Срочные без финального", esc(number(load.urgent_without_final_answer)), load.urgent_without_final_answer ? "warn" : "good")}
      ${row("Активные пачки", esc(number(load.active_bulk_batches)), load.active_bulk_batches ? "warn" : "good")}
    </div>
  `;
}

function renderDirections(snapshot) {
  const directions = snapshot.business?.directions || {};
  const rows = (directions.top || []).slice(0, 5).map((item) => `
    <div class="direction-item">
      <span class="direction-name">${esc(item.direction)}</span>
      <span class="metric-value">${esc(number(item.count))} <span class="muted">${esc(item.share_percent)}%</span></span>
    </div>
  `).join("");
  qs("#card-directions").innerHTML = `
    ${cardHeader("", "Направления", "compass")}
    <div class="direction-list">
      ${rows || '<div class="muted">Пока нет заявок по направлениям</div>'}
    </div>
  `;
}

function renderGiga(snapshot) {
  const event = snapshot.log_events?.gigachat_failed;
  qs("#card-gigachat").innerHTML = `
    ${cardHeader("", "GigaChat", "bot", pill("из логов"))}
    <div class="metric-list">
      ${row("Ошибки в окне логов", esc(number(event?.count)), event?.count ? "warn" : "good")}
      ${row("Fallback 24h", esc("нет истории"), "muted")}
      ${row("Checks 24h", esc("нет истории"), "muted")}
      ${row("Последняя ошибка", esc(event?.last_message || "-"))}
    </div>
  `;
}

function renderDashboardOutbox(snapshot) {
  const q = snapshot.queues?.dashboard_outbox || {};
  qs("#card-dashboard-outbox").innerHTML = `
    ${cardHeader("", "Dashboard outbox", "file")}
    <div class="metric-list">
      ${row("Pending", esc(number(q.pending)), q.pending ? "warn" : "good")}
      ${row("Sending", esc(number(q.sending)), q.sending ? "warn" : "")}
      ${row("Old pending groups", esc(number(q.old_pending)), q.old_pending ? "warn" : "")}
      ${row("Последняя ошибка", esc(q.last_errors?.[0]?.last_error || "-"))}
    </div>
  `;
}

function renderApplications(snapshot) {
  const app = snapshot.applications || {};
  const statusRows = (app.by_status || []).slice(0, 5).map((item) => row(item.status || "-", esc(number(item.count)))).join("");
  qs("#card-applications").innerHTML = `
    ${cardHeader("", "Заявки", "queue")}
    <div class="metric-row">
      <span class="metric-label">Создано сегодня</span>
      <span class="metric-value" style="font-size:28px">${esc(number(app.created_today))}</span>
    </div>
    <div class="metric-list" style="margin-top:14px">
      ${statusRows || row("По статусам", "-")}
      ${row("Срочные сегодня", esc(number(app.urgent_today)), app.urgent_today ? "warn" : "")}
      ${row("Не найдено / not_found", esc(number(app.not_found_total)), app.not_found_total ? "warn" : "good")}
    </div>
  `;
}

function renderBulk(snapshot) {
  const bulk = snapshot.bulk || {};
  qs("#card-bulk").innerHTML = `
    ${cardHeader("", "Массовые пачки", "layers")}
    <div class="metric-list">
      ${row("Unfinished", esc(number(bulk.unfinished)), bulk.unfinished ? "warn" : "good")}
      ${row("Registering", esc(number(bulk.registering)), bulk.registering ? "warn" : "")}
      ${row("Registered", esc(number(bulk.registered)))}
      ${row("Stale creating", esc(number(bulk.stale_creating)), bulk.stale_creating ? "bad" : "good")}
      ${row("Stale registering", esc(number(bulk.stale_registering)), bulk.stale_registering ? "bad" : "good")}
    </div>
  `;
}

function renderUrgent(snapshot) {
  const urgent = snapshot.urgent || {};
  qs("#card-urgent").innerHTML = `
    ${cardHeader("", "Срочные заявки", "spark")}
    <div class="metric-list">
      ${row("Открытые срочные", esc(number(urgent.open)), urgent.open ? "bad" : "good")}
      ${row("Без ответственного", esc(number(urgent.no_editor)), urgent.no_editor ? "warn" : "")}
      ${row("Без финального ответа", esc(number(urgent.no_final_answer)), urgent.no_final_answer ? "warn" : "")}
      ${row("Возраст старейшей", esc(age(urgent.oldest_age_seconds)), urgent.oldest_age_seconds ? "warn" : "muted")}
    </div>
  `;
}

function renderErrors(snapshot) {
  const events = Object.entries(snapshot.log_events || {}).sort((a, b) => Number(b[1].count || 0) - Number(a[1].count || 0));
  const rows = events.slice(0, 6).map(([key, item]) => `
    <tr>
      <td>${esc(key)}</td>
      <td>${esc(item.count)}</td>
      <td>${esc(item.recommendation)}</td>
      <td>${esc(item.last_message || "-")}</td>
    </tr>
  `).join("");
  qs("#card-errors").innerHTML = `
    ${cardHeader(12, "Последние ошибки", "alert", pill("окно логов"))}
    <table>
      <thead><tr><th style="width:150px">Компонент</th><th style="width:90px">Повторов</th><th style="width:260px">Действие</th><th>Сообщение</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="4" class="muted">Ошибок в последнем окне логов нет</td></tr>'}</tbody>
    </table>
  `;
}

function renderNotification(snapshot) {
  const q = snapshot.queues?.notification_outbox || {};
  qs("#card-notification").innerHTML = `
    ${cardHeader("", "Notification outbox", "send")}
    <div class="split-metric">
      <div><span>Pending</span><strong>${esc(number(q.pending))}</strong></div>
      <div><span>Sending</span><strong>${esc(number(q.sending))}</strong></div>
      <div><span>Failed</span><strong class="${q.failed ? "bad" : ""}">${esc(number(q.failed))}</strong></div>
      <div><span>Old sending</span><strong>${esc(number(q.old_sending))}</strong></div>
    </div>
    ${row("Последняя ошибка", esc(q.last_errors?.[0]?.last_error || "-"))}
  `;
}

function render(snapshot) {
  qs("#empty-state").classList.add("hidden");
  renderTop(snapshot);
  renderSummary(snapshot);
  renderContainer(snapshot);
  renderVps(snapshot);
  renderPolling(snapshot);
  renderTelegram(snapshot);
  renderGoogle(snapshot);
  renderManagerFocus(snapshot);
  renderBusinessToday(snapshot);
  renderTeamLoad(snapshot);
  renderDirections(snapshot);
  renderGiga(snapshot);
  renderDashboardOutbox(snapshot);
  renderApplications(snapshot);
  renderBulk(snapshot);
  renderUrgent(snapshot);
  renderErrors(snapshot);
  renderNotification(snapshot);
}

function applicationColumns() {
  return [
    ["ID", (item) => esc(item.application_id)],
    ["Проблема", (item) => esc(item.problem || "-")],
    ["Статус", (item) => esc(item.last_known_status || "-")],
    ["Направление", (item) => esc(item.direction || "-")],
    ["Тип", (item) => esc(item.type_label || "-")],
    ["Редактор", (item) => esc(item.last_seen_editor || "не выбран")],
    ["Финал", (item) => (item.has_final_answer ? "да" : "нет")],
    ["Лист", (item) => esc(item.sheet_name || "-")],
    ["Строка", (item) => esc(item.last_seen_row_number || "-")],
    ["Ссылка", (item) => item.row_link ? `<a href="${esc(item.row_link)}" target="_blank" rel="noreferrer">открыть</a>` : "-"],
    ["not_found", (item) => esc(number(item.not_found_count))],
    ["Последний", (item) => esc(dateTime(item.last_not_found_at))],
    ["Следующая", (item) => esc(dateTime(item.next_status_check_at))],
    ["Обновлено", (item) => esc(dateTime(item.updated_at))],
    ["Возраст", (item) => esc(age(item.problem_age_seconds))],
  ];
}

function renderTable(target, title, rows, columns, emptyText) {
  const header = columns.map(([label]) => `<th>${esc(label)}</th>`).join("");
  const body = (rows || []).map((item) => `
    <tr>${columns.map(([, getter]) => `<td>${getter(item)}</td>`).join("")}</tr>
  `).join("");
  qs(target).innerHTML = `
    ${cardHeader("", title, "file", pill(`${number((rows || []).length)} строк`))}
    <table class="wide-table">
      <thead><tr>${header}</tr></thead>
      <tbody>${body || `<tr><td colspan="${columns.length}" class="muted">${esc(emptyText)}</td></tr>`}</tbody>
    </table>
  `;
}

function renderApplicationReport(report) {
  qs("#applications-report-empty").classList.add("hidden");
  qs("#applications-report-error").classList.toggle("hidden", report.collection_status !== "failed");
  qs("#applications-report-content").classList.remove("hidden");
  qs("#applications-report-updated").textContent = dateTime(report.collected_at);
  qs("#applications-report-status").textContent = report.collection_status === "ok" ? "ok" : "failed";
  qs("#applications-report-status").className = `pill ${report.collection_status === "ok" ? "good" : "bad"}`;
  qs("#applications-report-error-text").textContent = (report.collection_errors || []).join("; ") || "-";

  const summary = report.summary || {};
  qs("#report-lost-count").textContent = number(summary.lost);
  qs("#report-urgent-count").textContent = number(summary.urgent_without_final_answer);
  qs("#report-owner-count").textContent = number(summary.without_owner);
  qs("#report-stale-count").textContent = number(summary.stale_without_movement);

  const appCols = applicationColumns();
  renderTable("#report-lost-table", "Потерялись", report.lost, appCols, "Потерянных заявок нет.");
  renderTable("#report-urgent-table", "Срочные без финального", report.urgent_without_final_answer, appCols.slice(0, 10), "Нет срочных заявок без финального ответа.");
  renderTable("#report-owner-table", "Без ответственного", report.without_owner, appCols.slice(0, 10), "Нет заявок без ответственного.");
  renderTable("#report-clarification-table", "Нужны пояснения", report.needs_clarification, appCols.slice(0, 10), "Нет заявок в статусе «Нужны пояснения».");
  renderTable("#report-stale-table", "Долго без движения", report.stale_without_movement, appCols.slice(0, 10), "Нет заявок старше 24 часов без движения.");
  renderTable("#report-bulk-table", "Проблемные пачки", report.problematic_bulk_batches, [
    ["Batch ID", (item) => esc(item.batch_id)],
    ["Состояние", (item) => esc(item.registration_state)],
    ["Локация", (item) => esc(item.location_state)],
    ["Промахов", (item) => esc(number(item.location_miss_count))],
    ["Лист", (item) => esc(item.sheet_name || "-")],
    ["Строка", (item) => esc(item.start_row || "-")],
    ["Ссылка", (item) => item.row_link ? `<a href="${esc(item.row_link)}" target="_blank" rel="noreferrer">открыть</a>` : "-"],
    ["Обновлено", (item) => esc(dateTime(item.updated_at))],
  ], "Проблемных массовых пачек нет.");
  renderTable("#report-workflows-table", "Незавершенные процессы", report.unfinished_workflows, [
    ["User ID", (item) => esc(item.telegram_user_id)],
    ["Шаг", (item) => esc(item.current_step || "-")],
    ["Отправка", (item) => esc(item.submission_state || "-")],
    ["Заявка", (item) => esc(item.application_id || "-")],
    ["Pending action", (item) => esc(item.pending_action || "-")],
    ["Active msg", (item) => esc(item.active_message_id || "-")],
    ["Обновлено", (item) => esc(dateTime(item.updated_at))],
  ], "Незавершенных пользовательских процессов нет.");
}

async function loadLatest() {
  const response = await fetch("/api/snapshot/latest", { cache: "no-store" });
  if (response.status === 404) {
    qs("#empty-state").classList.remove("hidden");
    return;
  }
  if (!response.ok) throw new Error(`latest failed: ${response.status}`);
  render(await response.json());
}

async function collect() {
  const button = qs("#refresh-button");
  button.disabled = true;
  button.innerHTML = `${icons.pulse}<span>Собираю...</span>`;
  try {
    const response = await fetch("/api/collect", { method: "POST" });
    if (!response.ok) throw new Error(`collect failed: ${response.status}`);
    const payload = await response.json();
    if (payload.snapshot) render(payload.snapshot);
  } finally {
    button.disabled = false;
    button.innerHTML = `${icons.pulse}<span>Обновить</span>`;
  }
}

async function loadApplicationReportLatest() {
  const response = await fetch("/api/applications/report/latest", { cache: "no-store" });
  if (response.status === 404) {
    qs("#applications-report-empty").classList.remove("hidden");
    qs("#applications-report-content").classList.add("hidden");
    return;
  }
  if (!response.ok) throw new Error(`application report latest failed: ${response.status}`);
  renderApplicationReport(await response.json());
}

async function collectApplicationReport() {
  const button = qs("#applications-report-button");
  button.disabled = true;
  button.innerHTML = `${icons.pulse}<span>Собираю...</span>`;
  try {
    const response = await fetch("/api/applications/report", { method: "POST" });
    if (!response.ok) throw new Error(`application report failed: ${response.status}`);
    const payload = await response.json();
    if (payload.report) renderApplicationReport(payload.report);
  } catch (error) {
    qs("#applications-report-error").classList.remove("hidden");
    qs("#applications-report-error-text").textContent = error.message;
  } finally {
    button.disabled = false;
    button.innerHTML = `${icons.pulse}<span>Получить актуальные заявки</span>`;
  }
}

qsa(".rail-item[data-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    window.location.hash = button.dataset.tab;
    switchTab(button.dataset.tab);
  });
});
qs("#refresh-button").addEventListener("click", collect);
qs("#applications-report-button").addEventListener("click", collectApplicationReport);

loadLatest().catch((error) => {
  qs("#empty-state").classList.remove("hidden");
  qs("#empty-state p").textContent = error.message;
});
switchTab(initialTabFromHash());
setInterval(() => {
  if (activeTab === "monitoring") loadLatest().catch(() => {});
}, REFRESH_MS);
