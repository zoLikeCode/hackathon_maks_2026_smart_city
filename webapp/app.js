const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const PRIORITY = {
  emergency: { label: "Авария", order: 0 },
  urgent: { label: "Срочно", order: 1 },
  today: { label: "На сегодня", order: 2 },
  planned: { label: "Плановый", order: 3 },
};
const PRIORITY_LABELS = Object.fromEntries(
  Object.entries(PRIORITY).map(([value, config]) => [value, config.label])
);
const STATUS = {
  review: "На рассмотрении",
  in_progress: "В работе",
  done: "Исполнена",
  rejected: "Отклонена",
  new: "Новая (архив)",
  waiting: "Ожидает (архив)",
};
const LEGACY_PRIORITY = { high: "Высокий (архив)", normal: "Обычный (архив)", low: "Низкий (архив)" };
const SPECIALTY = { plumber: "Сантехник", electrician: "Электрик", other: "Другое" };
const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const CLOSED_STATUSES = new Set(["done", "rejected"]);
const isClosed = (request) => CLOSED_STATUSES.has(request.status);
const statusLabel = (value) => Object.hasOwn(STATUS, value) ? STATUS[value] : (value || "Не указан");
const priorityLabel = (value) => Object.hasOwn(PRIORITY, value) ? PRIORITY[value].label : (LEGACY_PRIORITY[value] || value || "Не назначен");
const THEME_STORAGE_KEY = "smart-city-theme";
const state = {
  requests: [],
  chairmanRequests: [],
  specialists: [],
  residents: [],
  chairmanProfile: null,
  photoUrls: new Map(),
  photoPosition: 1,
  photoRequestId: null,
  photoEpoch: 0,
  pendingRegistryDigest: null,
  view: "overview",
  chairmanView: "requests",
  session: null,
  month: new Date(new Date().getFullYear(), new Date().getMonth(), 1),
  selectedDate: "",
  activeRequestId: null,
  initData: "",
  preview: false,
};
let toastTimer;

function savedTheme() {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    return null;
  }
}

function applyTheme(theme, remember = false) {
  const resolved = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = resolved;
  const themeColor = document.querySelector('meta[name="theme-color"]');
  if (themeColor) themeColor.content = resolved === "dark" ? "#06172B" : "#f8f7fd";
  $$('[data-theme-toggle]').forEach((button) => {
    button.setAttribute("aria-label", "Тёмная тема");
    button.setAttribute("aria-pressed", String(resolved === "dark"));
    button.title = resolved === "dark" ? "Светлая тема" : "Тёмная тема";
    const icon = button.querySelector(".theme-toggle-icon");
    if (icon) icon.textContent = resolved === "dark" ? "☀" : "☾";
  });
  if (remember) {
    try { window.localStorage.setItem(THEME_STORAGE_KEY, resolved); } catch { /* WebView storage may be disabled. */ }
  }
}

function initializeTheme() {
  const preference = window.matchMedia("(prefers-color-scheme: dark)");
  applyTheme(savedTheme() || (preference.matches ? "dark" : "light"));
  preference.addEventListener?.("change", (event) => {
    if (!savedTheme()) applyTheme(event.matches ? "dark" : "light");
  });
  window.addEventListener("storage", (event) => {
    if (event.key === THEME_STORAGE_KEY) {
      applyTheme(savedTheme() || (preference.matches ? "dark" : "light"));
    }
  });
}

const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[character]));
const localISO = (date) => {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return year + "-" + month + "-" + day;
};
const offsetDate = (days) => {
  const date = new Date();
  date.setHours(12, 0, 0, 0);
  date.setDate(date.getDate() + days);
  return localISO(date);
};
const parseDay = (value) => value ? new Date(value.slice(0, 10) + "T12:00:00") : null;
const dateLabel = (value, options = { day: "numeric", month: "short" }) => {
  const parsed = parseDay(value);
  return parsed ? new Intl.DateTimeFormat("ru-RU", options).format(parsed) : "Без даты";
};
const countLabel = (count, one, few, many) => {
  const last = count % 10;
  const lastTwo = count % 100;
  return count + " " + (last === 1 && lastTwo !== 11 ? one : last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14) ? few : many);
};
const numberLabel = (value) => String(value).padStart(2, "0");
const requestCode = (id) => String(id).slice(0, 8).toUpperCase();
const requestDate = (request) => request.visit_date || request.scheduled_for || null;
const visitLabel = (request) => request.visit_date
  ? `${dateLabel(request.visit_date)}${Number.isInteger(request.visit_hour) ? ` · ${String(request.visit_hour).padStart(2, "0")}:00` : ""}`
  : request.scheduled_for ? dateLabel(request.scheduled_for) : "Дата не назначена";
function normalizeRequest(item) {
  const id = String(item.request_id || item.id || "");
  return {
    ...item,
    id,
    request_id: id,
    category: item.category || SPECIALTY[item.specialty] || item.specialty || "Другое",
    scheduled_for: requestDate(item),
    assignee: item.specialist_name || item.assignee || null,
    priority: item.priority || "",
    status: item.status || "review",
  };
}

function showError(message) {
  $("#loading").classList.add("hidden");
  $("#specialist").classList.add("hidden");
  $("#chairman").classList.add("hidden");
  $("#error-message").textContent = message;
  $("#error").classList.remove("hidden");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Authorization: "tma " + state.initData,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  let payload;
  try {
    payload = await response.json();
  } catch {
    const status = response.status ? `HTTP ${response.status}` : "без статуса";
    throw new Error(`Не удалось прочитать ответ сервиса (${status}). Попробуйте ещё раз.`);
  }
  if (!response.ok) throw new Error(payload.error || "Не удалось выполнить запрос");
  return payload;
}

function demoRequests() {
  const examples = [
    ["Протечка стояка на 4 этаже", "plumber", "ул. Садовая, 18", "emergency", "in_progress", 0, "В квартире на четвёртом этаже обнаружена протечка стояка. Требуется осмотр и устранение."],
    ["Не работает пассажирский лифт", "other", "пр. Мира, 42", "urgent", "review", 1, "Лифт остановился между этажами. Нужна проверка оборудования."],
    ["Освещение в подъезде № 2", "electrician", "ул. Лесная, 7", "today", "review", 0, "Не включается свет на лестничной площадке второго подъезда."],
    ["Проверка отопления в доме", "plumber", "ул. Садовая, 18", "urgent", "in_progress", 2, "Жители сообщают о низкой температуре радиаторов."],
    ["Уборка придомовой территории", "other", "ул. Парковая, 6", "planned", "review", 4, "Необходимо убрать листву у входной группы."],
    ["Замена ламп на лестнице", "electrician", "пр. Мира, 42", "planned", "review", 7, "Перегорели лампы между третьим и четвёртым этажами."],
    ["Ремонт входной двери", "other", "ул. Тихая, 12", "today", "in_progress", 12, "Доводчик двери не удерживает створку."],
    ["Проверка вентиляции", "other", "ул. Лесная, 7", "planned", "review", -2, "Проверить тягу в вентиляционном канале."],
    ["Вывоз крупного мусора", "other", "ул. Парковая, 6", "planned", "done", -5, "У входа в контейнерную площадку складированы крупные предметы."],
    ["Шум в электрощите", "electrician", "ул. Тихая, 12", "urgent", "review", null, "При включении освещения слышен необычный шум в электрощите."],
    ["Покраска ограждения", "other", "ул. Садовая, 18", "planned", "rejected", null, "Обновить покрытие ограждения у детской площадки."],
    ["Не закрывается окно в холле", "other", "пр. Мира, 42", "planned", "review", null, "Окно на первом этаже остаётся открытым после проветривания."],
  ];
  return examples.map((entry, index) => normalizeRequest({
    id: String(1042 + index), title: entry[0], specialty: entry[1], address: entry[2],
    priority: entry[3], status: entry[4],
    visit_date: entry[5] === null ? null : offsetDate(entry[5]),
    description: entry[6], created_at: new Date(Date.now() - (index + 1) * 6 * 3600000).toISOString(),
    assignee: null, photo_count: 0, unit: String(42 + index),
  }));
}

function updateToday() {
  const today = new Date();
  const todayIso = localISO(today);
  $("#overview-date-label").textContent = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" }).format(today);
  $("#today-day").textContent = String(today.getDate()).padStart(2, "0");
  $("#today-month").textContent = new Intl.DateTimeFormat("ru-RU", { month: "long" }).format(today).toUpperCase();
  $("#today-weekday").textContent = new Intl.DateTimeFormat("ru-RU", { weekday: "long" }).format(today);
  const todayCount = state.requests.filter((request) => request.scheduled_for === todayIso && !isClosed(request)).length;
  $("#today-scheduled").textContent = countLabel(todayCount, "заявка", "заявки", "заявок") + " на сегодня";
}

function updateMetrics() {
  const attention = state.requests.filter((request) => ["emergency", "urgent"].includes(request.priority) && !isClosed(request)).length;
  const open = state.requests.filter((request) => !isClosed(request)).length;
  $("#metric-total").textContent = numberLabel(state.requests.length);
  $("#metric-urgent").textContent = numberLabel(attention);
  $("#overview-attention").textContent = numberLabel(open);
  $("#overview-lead").textContent = open
    ? countLabel(open, "активная заявка", "активные заявки", "активных заявок") + " в работе"
    : "Активных заявок сейчас нет";
  $("#metric-scheduled").textContent = numberLabel(state.requests.filter((request) => request.scheduled_for).length);
  $("#profile-open-count").textContent = numberLabel(open);
  $("#profile-done-count").textContent = numberLabel(state.requests.filter((request) => request.status === "done").length);
  const rejectedCount = $("#profile-rejected-count");
  if (rejectedCount) rejectedCount.textContent = numberLabel(state.requests.filter((request) => request.status === "rejected").length);
  $("#quick-all-count").textContent = state.requests.length;
  $("#quick-emergency-count").textContent = state.requests.filter((request) => request.priority === "emergency" && !isClosed(request)).length;
  $("#quick-urgent-count").textContent = state.requests.filter((request) => request.priority === "urgent" && !isClosed(request)).length;
  $("#quick-undated-count").textContent = state.requests.filter((request) => !request.scheduled_for).length;
  updateToday();
}

function updateCategories() {
  const select = $("#category-filter");
  const selected = select.value;
  const categories = [...new Set(state.requests.map((request) => request.category).filter(Boolean))].sort((a, b) => a.localeCompare(b, "ru"));
  select.innerHTML = '<option value="">Любая</option>' + categories.map((category) => '<option value="' + escapeHtml(category) + '">' + escapeHtml(category) + "</option>").join("");
  select.value = categories.includes(selected) ? selected : "";
}

function filters() {
  return {
    search: $("#search").value.trim().toLocaleLowerCase("ru-RU"),
    status: $("#status-filter").value,
    priority: $("#priority-filter").value,
    category: $("#category-filter").value,
    date: $("#date-filter").value,
  };
}

function filteredRequests() {
  const current = filters();
  return state.requests.filter((request) => {
    const haystack = [request.id, request.title, request.address, request.category, request.description, request.assignee].join(" ").toLocaleLowerCase("ru-RU");
    return (!current.search || haystack.includes(current.search))
      && (!current.status || (current.status === "open" ? !isClosed(request) : request.status === current.status))
      && (!current.priority || request.priority === current.priority)
      && (!current.category || request.category === current.category)
      && (!current.date || (current.date === "scheduled" ? Boolean(request.scheduled_for) : !request.scheduled_for));
  }).sort((a, b) => {
    if (isClosed(a) !== isClosed(b)) return isClosed(a) ? 1 : -1;
    const rank = (PRIORITY[a.priority]?.order ?? 4) - (PRIORITY[b.priority]?.order ?? 4);
    if (rank) return rank;
    if (!a.scheduled_for && !b.scheduled_for) return b.created_at.localeCompare(a.created_at);
    if (!a.scheduled_for) return 1;
    if (!b.scheduled_for) return -1;
    return a.scheduled_for.localeCompare(b.scheduled_for) || b.created_at.localeCompare(a.created_at);
  });
}

function priorityOptions(selected) {
  const editable = Object.entries(PRIORITY)
    .map(([value, config]) => '<option value="' + value + '"' + (selected === value ? " selected" : "") + ">" + config.label + "</option>")
    .join("");
  return editable + (!Object.hasOwn(PRIORITY, selected)
    ? '<option value="' + escapeHtml(selected) + '" selected disabled>' + escapeHtml(priorityLabel(selected)) + '</option>'
    : "");
}

function selectCurrentValue(select, value, labels) {
  select.querySelectorAll('[data-legacy-current="true"]').forEach((option) => option.remove());
  if (![...select.options].some((option) => option.value === value)) {
    const option = new Option(labels[value] || value || "Не указан", value);
    option.disabled = true;
    option.dataset.legacyCurrent = "true";
    select.add(option);
  }
  select.value = value;
}

function emptyMarkup(title, text) {
  return '<div class="empty-state"><span class="empty-symbol" aria-hidden="true">↗</span><h3>' + escapeHtml(title) + '</h3><p>' + escapeHtml(text) + "</p></div>";
}

function rowMarkup(request) {
  const status = statusLabel(request.status);
  const id = escapeHtml(request.id);
  const code = escapeHtml(requestCode(request.id));
  return '<div class="request-row" data-id="' + id + '">'
    + '<div class="priority-cell"><span class="priority-caption">ПРИОРИТЕТ</span><span class="priority-mark priority-' + escapeHtml(request.priority) + '"></span><select class="priority-select priority-' + escapeHtml(request.priority) + '" data-priority-id="' + id + '" aria-label="Приоритет заявки № ' + code + '"' + (request.read_only || state.session?.demo_access || (request.priority === "emergency" && request.status !== "done") ? ' disabled' : '') + '>' + priorityOptions(request.priority) + '</select></div>'
    + '<button type="button" class="request-open" data-open="' + id + '"><span class="request-number"><span class="ticket-code">№ ' + code + '</span><span class="ticket-category">' + escapeHtml(request.category) + '</span></span><strong>' + escapeHtml(request.title) + '</strong></button>'
    + '<div class="address-cell">' + escapeHtml(request.address) + '</div>'
    + '<div class="date-cell">' + (request.scheduled_for ? escapeHtml(dateLabel(request.scheduled_for)) : '<span class="muted">Не назначена</span>') + '</div>'
    + '<div class="status-cell"><span class="status-caption">СТАТУС</span><span class="status-dot status-' + escapeHtml(request.status) + '"></span>' + escapeHtml(status) + '</div>'
    + '<button type="button" class="row-arrow" data-open="' + id + '" aria-label="Открыть заявку № ' + code + '">↗</button>'
    + "</div>";
}

function renderList(items) {
  $("#request-list").innerHTML = items.length
    ? items.map(rowMarkup).join("")
    : emptyMarkup(state.requests.length ? "Ничего не найдено" : "Заявок пока нет", state.requests.length ? "Измените параметры поиска или сбросьте фильтры." : "Новые обращения появятся здесь после создания председателем.");
}

function renderOverview() {
  const focus = state.requests
    .filter((request) => !isClosed(request))
    .sort((a, b) => (PRIORITY[a.priority]?.order ?? 4) - (PRIORITY[b.priority]?.order ?? 4)
      || String(a.scheduled_for || "9999").localeCompare(String(b.scheduled_for || "9999")))
    .slice(0, 3);
  $("#overview-focus").innerHTML = focus.length ? focus.map((request, index) =>
    '<button type="button" class="focus-item" data-open="' + escapeHtml(request.id) + '">'
    + '<span class="focus-index">' + String(index + 1).padStart(2, "0") + '</span>'
    + '<span class="focus-content"><span class="focus-meta">№ ' + escapeHtml(requestCode(request.id)) + ' · ' + escapeHtml(request.address) + '</span><strong>' + escapeHtml(request.title) + '</strong><span class="focus-status"><span class="focus-tag focus-priority priority-' + escapeHtml(request.priority) + '">' + escapeHtml(priorityLabel(request.priority)) + '</span><span class="focus-tag focus-workflow status-' + escapeHtml(request.status) + '">' + escapeHtml(statusLabel(request.status)) + '</span><span class="focus-tag focus-date">' + (request.scheduled_for ? escapeHtml(dateLabel(request.scheduled_for)) : 'Без даты') + '</span></span></span>'
    + '<span class="focus-arrow" aria-hidden="true">↗</span></button>'
  ).join("") : emptyMarkup(state.requests.length ? "Заявок в работе нет" : "Заявок пока нет", state.requests.length ? "Все текущие заявки завершены или отклонены." : "Новые обращения появятся здесь после создания председателем.");
}

function agendaMarkup(request, undated = false) {
  return '<button class="' + (undated ? "undated-item" : "agenda-item") + '" type="button" data-open="' + escapeHtml(request.id) + '">'
    + '<span class="agenda-priority priority-' + escapeHtml(request.priority) + '"></span>'
    + '<span class="agenda-body"><strong class="agenda-title">' + escapeHtml(request.title) + '</strong><span class="agenda-meta">' + escapeHtml(request.address) + '</span><span class="agenda-tags"><span class="agenda-status status-' + escapeHtml(request.status) + '">' + escapeHtml(statusLabel(request.status)) + '</span><span class="agenda-priority-label priority-' + escapeHtml(request.priority) + '">' + escapeHtml(priorityLabel(request.priority)) + '</span></span></span>'
    + '<span class="agenda-arrow" aria-hidden="true">↗</span></button>';
}

function renderAgenda(items) {
  const dayItems = items.filter((request) => request.scheduled_for === state.selectedDate);
  const date = parseDay(state.selectedDate);
  $("#agenda-date").textContent = date ? new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long" }).format(date) : "—";
  $("#agenda-list").innerHTML = dayItems.length ? dayItems.map((request) => agendaMarkup(request)).join("") : '<p class="agenda-empty">На этот день заявок нет.</p>';
  const undated = items.filter((request) => !request.scheduled_for);
  $("#undated-count").textContent = String(undated.length);
  const undatedEmptyText = state.requests.some((request) => !request.scheduled_for)
    ? "По выбранным фильтрам заявок без даты нет."
    : "Все заявки запланированы.";
  $("#undated-list").innerHTML = undated.length ? undated.map((request) => agendaMarkup(request, true)).join("") : '<p class="agenda-empty">' + undatedEmptyText + "</p>";
}

function renderCalendar(items) {
  const year = state.month.getFullYear();
  const month = state.month.getMonth();
  const monthTitle = new Intl.DateTimeFormat("ru-RU", { month: "long", year: "numeric" }).format(state.month);
  $("#calendar-title").textContent = monthTitle.charAt(0).toUpperCase() + monthTitle.slice(1);
  const monthPrefix = year + "-" + String(month + 1).padStart(2, "0") + "-";
  const monthItems = items.filter((request) => request.scheduled_for?.startsWith(monthPrefix));
  const orderedMonthItems = [...monthItems].sort((a, b) =>
    a.scheduled_for.localeCompare(b.scheduled_for) ||
    (PRIORITY[a.priority]?.order ?? 5) - (PRIORITY[b.priority]?.order ?? 5)
  );
  const monthCount = $("#month-count");
  const monthList = $("#month-list");
  if (monthCount) monthCount.textContent = countLabel(orderedMonthItems.length, "заявка", "заявки", "заявок");
  if (monthList) {
    monthList.innerHTML = orderedMonthItems.length ? orderedMonthItems.map((request) =>
      '<button type="button" class="month-request" data-open="' + escapeHtml(request.id) + '">'
      + '<span class="month-request-date">' + escapeHtml(dateLabel(request.scheduled_for)) + '</span>'
      + '<span class="month-request-main"><strong>' + escapeHtml(request.title) + '</strong><small>' + escapeHtml(request.address) + '</small></span>'
      + '<span class="month-request-tags"><span class="status-' + escapeHtml(request.status) + '">' + escapeHtml(statusLabel(request.status)) + '</span><span class="priority-' + escapeHtml(request.priority) + '">' + escapeHtml(priorityLabel(request.priority)) + '</span></span>'
      + '<span class="month-request-arrow" aria-hidden="true">↗</span></button>'
    ).join("") : '<p class="month-request-empty">На этот месяц заявок с датой выезда нет. Заявки без даты показаны выше.</p>';
  }
  const todayIso = localISO(new Date());
  const upcoming = monthItems.filter((request) => !isClosed(request) && request.scheduled_for >= todayIso);
  const monthActive = monthItems.filter((request) => !isClosed(request));
  const previewItems = (upcoming.length ? upcoming : monthActive.length ? monthActive : monthItems).sort((a, b) =>
    a.scheduled_for.localeCompare(b.scheduled_for) || (PRIORITY[a.priority]?.order ?? 4) - (PRIORITY[b.priority]?.order ?? 4)
  ).slice(0, 3);
  $("#calendar-mobile-agenda").innerHTML = '<div class="mobile-agenda-head"><span>ПО ДАТАМ</span><span>' + countLabel(monthItems.length, "заявка", "заявки", "заявок") + ' в месяце</span></div>'
    + (previewItems.length ? previewItems.map((request) => '<button type="button" class="mobile-agenda-item" data-open="' + escapeHtml(request.id) + '"><span class="mobile-agenda-date"><strong>' + escapeHtml(parseDay(request.scheduled_for).getDate()) + '</strong><small>' + escapeHtml(dateLabel(request.scheduled_for, { month: "short" })) + '</small></span><span class="mobile-agenda-body"><strong>' + escapeHtml(request.title) + '</strong><small>' + escapeHtml(request.address) + ' · ' + escapeHtml(priorityLabel(request.priority)) + (isClosed(request) ? ' · ' + escapeHtml(statusLabel(request.status)) : '') + '</small></span><span class="mobile-agenda-arrow" aria-hidden="true">↗</span></button>').join("") : '<p class="mobile-agenda-empty">Запланированных выездов пока нет.</p>');
  const firstWeekday = (new Date(year, month, 1).getDay() + 6) % 7;
  const lastDay = new Date(year, month + 1, 0).getDate();
  const cells = Math.ceil((firstWeekday + lastDay) / 7) * 7;
  const byDay = new Map();
  for (const request of items) {
    if (!request.scheduled_for) continue;
    const entries = byDay.get(request.scheduled_for) || [];
    entries.push(request);
    byDay.set(request.scheduled_for, entries);
  }
  const html = [];
  for (let index = 0; index < cells; index++) {
    const day = new Date(year, month, 1 + index - firstWeekday);
    const iso = localISO(day);
    const events = byDay.get(iso) || [];
    const outside = day.getMonth() !== month;
    const classes = ["calendar-day", outside ? "outside" : "", iso === todayIso ? "today" : "", iso === state.selectedDate ? "selected" : ""].filter(Boolean).join(" ");
    const eventsMarkup = events.slice(0, 2).map((request) => '<span class="calendar-event priority-' + escapeHtml(request.priority) + '">' + escapeHtml(request.title) + "</span>").join("");
    const more = events.length > 2 ? '<span class="calendar-more">+' + (events.length - 2) + " ещё</span>" : "";
    const count = events.length ? '<span class="day-count">' + events.length + "</span>" : "";
    html.push('<button type="button" class="' + classes + '" data-date="' + iso + '" aria-label="' + escapeHtml(dateLabel(iso, { day: "numeric", month: "long", year: "numeric" })) + ', ' + countLabel(events.length, "заявка", "заявки", "заявок") + '"><span class="day-heading"><span class="day-number">' + day.getDate() + "</span>" + count + '</span><span class="day-events">' + eventsMarkup + more + '</span><span class="day-dot' + (events.length ? " has-events" : "") + '"></span></button>');
  }
  $("#calendar-grid").innerHTML = html.join("");
  renderAgenda(items);
}

function updateFilterIndicator() {
  const current = filters();
  const activeCount = ["status", "priority", "category", "date"].filter((key) => current[key]).length;
  $("#filter-count").textContent = activeCount;
  $("#filter-count").classList.toggle("hidden", activeCount === 0);
  $("#filter-toggle").classList.toggle("has-filters", activeCount > 0);
  const noOtherFilters = !current.search && !current.category;
  const quick = noOtherFilters && current.status === "open" && current.priority === "emergency" && !current.date ? "emergency"
    : noOtherFilters && current.status === "open" && current.priority === "urgent" && !current.date ? "urgent"
      : noOtherFilters && !current.status && current.date === "unscheduled" && !current.priority ? "unscheduled"
        : !current.search && !current.status && !current.priority && !current.category && !current.date ? "all" : "";
  $$("[data-quick]").forEach((button) => {
    const active = button.dataset.quick === quick;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function clearAllFilters() {
  ["search", "status-filter", "priority-filter", "category-filter", "date-filter"].forEach((id) => { $("#" + id).value = ""; });
}

function render() {
  updateMetrics();
  updateCategories();
  updateFilterIndicator();
  const items = filteredRequests();
  $("#board-count").textContent = numberLabel(items.length);
  $("#calendar-toolbar-count").textContent = numberLabel(items.length);
  $("#result-summary").textContent = "Показано " + countLabel(items.length, "заявка", "заявки", "заявок") + " из " + state.requests.length;
  renderOverview();
  renderList(items);
  renderCalendar(items);
}

function setView(view) {
  if (!["overview", "list", "calendar", "profile"].includes(view)) return;
  state.view = view;
  $("#specialist").classList.remove("view-overview", "view-list", "view-calendar", "view-profile", "calendar-active");
  $("#specialist").classList.add("view-" + view);
  $("#overview-view").classList.toggle("hidden", view !== "overview");
  $("#work-view").classList.toggle("hidden", view !== "list" && view !== "calendar");
  $("#profile-view").classList.toggle("hidden", view !== "profile");
  $("#list-view").classList.toggle("hidden", view !== "list");
  $("#calendar-view").classList.toggle("hidden", view !== "calendar");
  $("#board-heading").textContent = view === "list" ? "Заявки" : "Календарь";
  $("#board-eyebrow").textContent = view === "list" ? "ВСЕ ОБРАЩЕНИЯ / 02" : "ПЛАН ВЫЕЗДОВ / 03";
  $("#board-subtitle").textContent = view === "list" ? "Проверьте приоритет, статус и дату выезда." : "Выберите день, чтобы увидеть запланированные задачи.";
  $$("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    if (button.classList.contains("side-link") || button.closest(".mobile-nav")) {
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
  });
  setFilterPanel(false);
  window.scrollTo(0, 0);
}

function setChairmanView(view) {
  if (!["requests", "specialists", "residents", "profile"].includes(view)) return;
  if (state.session?.role === "owner" && ["specialists", "residents"].includes(view)) return;
  state.chairmanView = view;
  $$("#chairman .chairman-view").forEach((section) => section.classList.toggle("hidden", section.id !== "chairman-" + view + "-view"));
  $$("[data-chairman-view]").forEach((button) => {
    if (!button.closest(".chairman-mobile-nav")) return;
    const active = button.dataset.chairmanView === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  window.scrollTo(0, 0);
  if (view === "specialists") renderSpecialists().catch((error) => { $("#specialist-message").textContent = error.message; });
  if (view === "residents") renderResidents().catch((error) => { $("#registry-message").textContent = error.message; });
}

function setFilterPanel(open) {
  const panel = $("#filter-panel");
  const toggle = $("#filter-toggle");
  const wasOpen = !panel.classList.contains("hidden");
  const mobile = window.matchMedia("(max-width: 620px)").matches;
  const restoreFocus = wasOpen && !open && panel.contains(document.activeElement);
  panel.classList.toggle("hidden", !open);
  $("#filter-backdrop").classList.toggle("hidden", !open);
  toggle.setAttribute("aria-expanded", String(open));
  document.body.classList.toggle("filter-open", open && mobile);
  if (open && mobile) {
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", "Фильтры заявок");
    $("#filter-close").focus();
  } else {
    panel.removeAttribute("role");
    panel.removeAttribute("aria-modal");
    panel.removeAttribute("aria-label");
    if (restoreFocus) toggle.focus();
  }
}

function showToast(message) {
  const toast = $("#action-toast");
  window.clearTimeout(toastTimer);
  toast.textContent = message;
  toast.classList.remove("hidden");
  toastTimer = window.setTimeout(() => toast.classList.add("hidden"), 3200);
}

function updateSpecialNote(request) {
  const messages = [];
  if (request.read_only) messages.push("Историческая заявка доступна только для просмотра.");
  if (request.priority === "emergency" && request.status !== "done") {
    messages.push("Аварийную заявку нельзя отклонить или понизить её приоритет до исполнения.");
  }
  if (request.status === "rejected") messages.push("Отклонённая заявка остаётся в истории обращений.");
  if (request.priority === "planned") messages.push("Плановый — приоритет заявки, дата выезда показывается отдельно.");
  const note = $("#dialog-special-note");
  note.textContent = messages.join(" ");
  note.classList.toggle("hidden", messages.length === 0);
}

function activeRequest() {
  return [...state.requests, ...state.chairmanRequests].find((item) => String(item.id) === String(state.activeRequestId));
}

function closePhotoViewer() {
  const viewer = $("#photo-viewer");
  if (viewer.open) viewer.close();
}

function releaseRequestPhotos() {
  closePhotoViewer();
  state.photoEpoch += 1;
  for (const url of state.photoUrls.values()) URL.revokeObjectURL(url);
  state.photoUrls.clear();
  state.photoRequestId = null;
  $("#dialog-photos").replaceChildren();
  $("#dialog-photo-message").textContent = "";
}

function showPhoto(position) {
  const request = activeRequest();
  if (!request) return;
  const count = Math.min(3, Number(request.photo_count) || 0);
  const viewer = $("#photo-viewer");
  state.photoPosition = position;
  const image = $("#photo-viewer-image");
  const url = state.photoUrls.get(position);
  image.classList.toggle("hidden", !url);
  if (url) image.src = url;
  else image.removeAttribute("src");
  image.alt = `Фото ${position} из ${count} к заявке № ${requestCode(request.id)}`;
  $("#photo-viewer-message").textContent = url ? "" : "Загружаем фотографию…";
  $("#photo-viewer-count").textContent = `${position} / ${count}`;
  $("#photo-viewer-prev").disabled = count < 2;
  $("#photo-viewer-next").disabled = count < 2;
  if (!viewer.open) viewer.showModal();
  $("#photo-viewer-close").focus();
}

function movePhoto(step) {
  const count = Math.min(3, Number(activeRequest()?.photo_count) || 0);
  if (!count) return;
  showPhoto(((state.photoPosition - 1 + step + count) % count) + 1);
}

function loadRequestPhotos(request) {
  releaseRequestPhotos();
  const count = Math.min(3, Number(request.photo_count) || 0);
  $("#dialog-photos-section").classList.toggle("hidden", count === 0);
  $("#dialog-photo-count").textContent = String(count);
  if (!count) return;
  state.photoRequestId = request.id;
  const epoch = state.photoEpoch;
  for (let position = 1; position <= count; position += 1) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "dialog-photo-button";
    button.disabled = true;
    button.setAttribute("aria-label", `Открыть фото ${position} из ${count}`);
    button.innerHTML = `<span class="photo-placeholder" aria-hidden="true">${position}</span>`;
    button.addEventListener("click", () => showPhoto(position));
    $("#dialog-photos").append(button);
    if (state.preview) continue;
    fetch(`/api/requests/${encodeURIComponent(request.id)}/photos/${position}`, {
      headers: { Authorization: `tma ${state.initData}` },
    }).then(async (response) => {
      if (!response.ok) throw new Error("Фотография не загрузилась");
      return response.blob();
    }).then((blob) => {
      if (epoch !== state.photoEpoch || state.photoRequestId !== request.id) return;
      const url = URL.createObjectURL(blob);
      state.photoUrls.set(position, url);
      const image = document.createElement("img");
      image.src = url;
      image.alt = `Фото ${position} к заявке`;
      button.replaceChildren(image);
      button.disabled = false;
      if ($("#photo-viewer").open && state.photoPosition === position) showPhoto(position);
    }).catch(() => {
      if (epoch !== state.photoEpoch) return;
      button.replaceChildren(document.createTextNode("Не удалось загрузить"));
      $("#dialog-photo-message").textContent = "Одно из фото не загрузилось. Откройте заявку позже.";
    });
  }
}

function syncRequestDialog(request) {
  const role = state.session?.role || "specialist";
  $("#dialog-id").textContent = "ЗАЯВКА № " + requestCode(request.id);
  $("#dialog-title").textContent = request.title;
  $("#dialog-description").textContent = request.description || "Описание не добавлено.";
  $("#dialog-address").textContent = request.address || "—";
  $("#dialog-unit").textContent = request.unit || "—";
  $("#dialog-category").textContent = request.category || "—";
  $("#dialog-created").textContent = dateLabel(request.created_at, { day: "numeric", month: "long", year: "numeric" });
  $("#dialog-visit-date").textContent = visitLabel(request);
  selectCurrentValue($("#dialog-status"), request.status, STATUS);
  selectCurrentValue($("#dialog-priority"), request.priority, PRIORITY_LABELS);
  const editable = role !== "owner" && !request.read_only && !state.session?.demo_access;
  $("#dialog-edit-fields").classList.toggle("hidden", !editable);
  $("#dialog-status").querySelector('option[value="review"]').disabled = role === "specialist";
  $("#dialog-status").querySelector('option[value="rejected"]').disabled = request.priority === "emergency";
  [...$("#dialog-priority").options].forEach((option) => {
    option.disabled = request.priority === "emergency" && request.status !== "done" && option.value !== "emergency";
  });
  const assigneeField = $("#dialog-assignee-field");
  assigneeField.classList.toggle("hidden", !editable || role !== "chairman");
  if (editable && role === "chairman") {
    const choices = state.specialists.filter((person) => !request.specialty || person.specialty === request.specialty);
    const select = $("#dialog-assignee");
    select.innerHTML = '<option value="">Председатель</option>' + choices.map((person) =>
      `<option value="${escapeHtml(person.specialist_id)}">${escapeHtml(person.full_name)} · ${escapeHtml(SPECIALTY[person.specialty] || person.specialty)}</option>`
    ).join("");
    select.value = request.specialist_id || "";
    if (select.value !== (request.specialist_id || "")) select.add(new Option(request.specialist_name || "Текущий исполнитель", request.specialist_id, true, true));
  }
  const canRequestVisit = role === "specialist" && editable && ["review", "in_progress"].includes(request.status)
    && !request.emergency_busy && !["awaiting_owner", "pending", "accepted"].includes(request.visit_status);
  $("#dialog-visit-request").classList.toggle("hidden", !canRequestVisit);
  updateSpecialNote(request);
}

function openRequest(id) {
  const request = [...state.requests, ...state.chairmanRequests].find((item) => String(item.id) === String(id));
  if (!request) return;
  state.activeRequestId = request.id;
  syncRequestDialog(request);
  $("#dialog-feedback").textContent = "";
  loadRequestPhotos(request);
  if (!$("#request-dialog").open) $("#request-dialog").showModal();
}

async function refreshRequests() {
  if (state.preview) return;
  const result = await api("/api/requests");
  const items = Array.isArray(result.requests) ? result.requests.map(normalizeRequest) : [];
  if (state.session?.role === "specialist") {
    state.requests = items;
    render();
  } else {
    state.chairmanRequests = items;
    renderChairmanRequests();
  }
}

async function changeRequest(id, field, value, control, successMessage) {
  const request = [...state.requests, ...state.chairmanRequests].find((item) => String(item.id) === String(id));
  if (!request || request.read_only || request[field] === value) return;
  const oldValue = request[field] || "";
  control.disabled = true;
  try {
    if (state.preview) {
      request[field] = value;
      if (state.session?.role === "specialist") render(); else renderChairmanRequests();
    } else {
      await api(`/api/requests/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ [field]: value }) });
      await refreshRequests();
    }
    const updated = [...state.requests, ...state.chairmanRequests].find((item) => String(item.id) === String(id));
    if (updated && $("#request-dialog").open) {
      syncRequestDialog(updated);
      $("#dialog-feedback").textContent = successMessage;
    } else showToast(successMessage);
  } catch (error) {
    control.value = oldValue;
    if ($("#request-dialog").open) $("#dialog-feedback").textContent = error.message;
    else showToast(error.message);
  } finally {
    control.disabled = false;
  }
}

function renderChairmanRequests() {
  const requests = [...state.chairmanRequests].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  $("#chairman-request-list").innerHTML = requests.length ? requests.map((request) =>
    `<button type="button" class="chairman-request" data-chairman-open="${escapeHtml(request.id)}">` +
      `<span class="chairman-request-code">№ ${escapeHtml(requestCode(request.id))}</span>` +
      `<span class="chairman-request-status"><i class="status-dot status-${escapeHtml(request.status)}"></i>${escapeHtml(statusLabel(request.status))}</span>` +
      `<strong>${escapeHtml(request.title)}</strong>` +
      `<span class="chairman-request-meta">${escapeHtml(visitLabel(request))} · ${escapeHtml(priorityLabel(request.priority))}${request.photo_count ? ` · ${request.photo_count} фото` : ""}${request.read_only ? " · Архив" : ""}</span>` +
      `<span class="chairman-request-arrow" aria-hidden="true">↗</span></button>`
  ).join("") : emptyMarkup("Заявок пока нет", state.session?.role === "owner"
    ? "Создайте заявку с фотографиями в чате с ботом MAX."
    : "Заявки жителей появятся здесь после отправки в боте MAX.");
}

function renderChairman(profile, requests = []) {
  state.chairmanProfile = profile;
  state.chairmanRequests = requests.map(normalizeRequest);
  const role = state.session?.role || profile.role || "chairman";
  const owner = role === "owner";
  $("#chairman").classList.toggle("owner-mode", owner);
  $(".chairman-header-label").textContent = owner ? "СОБСТВЕННИК" : "ПРЕДСЕДАТЕЛЬ";
  $(".chairman-mobile-nav").setAttribute("aria-label", owner ? "Разделы собственника" : "Разделы председателя");
  $$("[data-chairman-only]").forEach((button) => button.classList.toggle("hidden", owner));
  $("#chairman-main-action").classList.toggle("hidden", owner);
  $("#chairman-requests-heading").innerHTML = owner ? 'Мои заявки<span class="accent">.</span>' : 'Заявки дома<span class="accent">.</span>';
  $("#chairman-requests-view .chairman-subtitle").textContent = owner
    ? "Заявки по вашим помещениям. Новую заявку с фотографиями создайте в боте MAX."
    : state.session?.demo_access ? "Демонстрационный кабинет. Создание специалистов здесь показывается без сохранения на сервере."
      : "Все обращения жителей и их текущий статус.";
  $("#chairman-specialists-view .chairman-subtitle").textContent = state.session?.demo_access
    ? "Демонстрационный режим: специалисты, созданные здесь, не сохраняются на сервере."
    : "Добавьте исполнителя по подтверждённому номеру MAX.";
  $("#chairman-name").textContent = profile.full_name || profile.display_name || "Пользователь";
  $("#chairman-hoa").textContent = profile.hoa_name || "Умный город";
  $("#chairman-address").textContent = profile.address || "—";
  $("#chairman-phone").textContent = profile.phone || "—";
  $("#chairman-protocol").textContent = profile.protocol_filename || "—";
  $("#chairman-verified").textContent = profile.verified_at
    ? new Intl.DateTimeFormat("ru-RU", { dateStyle: "long" }).format(new Date(profile.verified_at)) : "—";
  $("#chairman-chat").classList.toggle("hidden", owner || Boolean(state.session?.demo_access));
  $("#hoa-timezone").value = profile.timezone || "Europe/Moscow";
  if (!owner && !state.session?.demo_access) {
    $("#group-chat-link").value = profile.group_chat?.link || "";
    renderGroupChatState(profile.group_chat);
  }
  const memberships = $("#owner-memberships");
  memberships.classList.toggle("hidden", !owner);
  memberships.innerHTML = owner ? `<h2>Мои помещения</h2>${(profile.memberships || []).map((item) =>
    `<div class="resident-item"><strong>${escapeHtml(item.hoa_name)} · пом. ${escapeHtml(item.unit)}</strong><p>${escapeHtml(item.area)} м² · доля ${escapeHtml(item.share)} · ${escapeHtml(item.ownership)}</p></div>`
  ).join("")}` : "";
  renderChairmanRequests();
  $("#loading").classList.add("hidden");
  $("#chairman").classList.remove("hidden");
  setChairmanView("requests");
}

function renderRoleDemo(role, displayName, phone) {
  state.preview = true;
  state.session = { role, display_name: displayName, demo_access: true };
  const owner = role === "owner";
  const profile = {
    full_name: displayName || "Демонстрационный пользователь",
    hoa_name: owner ? "Дом на Садовой, 18" : "Демонстрационное ТСЖ",
    address: "ул. Садовая, 18", phone: phone || "Номер подтверждён в MAX",
    protocol_filename: owner ? "—" : "Демонстрационный профиль",
    verified_at: new Date().toISOString(),
    memberships: owner ? [{ hoa_name: "Дом на Садовой, 18", unit: "42", area: 54, share: "1/1", ownership: "Собственность" }] : [],
  };
  renderChairman(profile, demoRequests().filter((request) => request.address === profile.address));
  if (!owner) {
    state.specialists = [{ specialist_id: "demo-plumber", specialty: "plumber", full_name: "Иван Петров", phone: "+79990000000", linked: true, work_hours: [] }];
    renderSpecialists();
  }
}


function renderGroupChatState(chat) {
  $("#group-chat-state").textContent = !chat ? "Чат не указан."
    : chat.chat_id ? `Подключён чат: ${chat.title || chat.link}`
      : "Ссылка сохранена. Добавьте бота в этот чат для проверки.";
}

function hoursEditor(existing = []) {
  const element = document.createElement("div");
  element.className = "hours-editor";
  const rows = [];
  for (let weekday = 0; weekday < 7; weekday += 1) {
    const current = existing.find((item) => item.weekday === weekday);
    const row = document.createElement("div");
    row.className = "hours-row";
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = Boolean(current);
    enabled.setAttribute("aria-label", `${WEEKDAYS[weekday]} — рабочий день`);
    const day = document.createElement("label");
    day.append(enabled, document.createTextNode(WEEKDAYS[weekday]));
    const select = (value, label) => {
      const input = document.createElement("select");
      input.setAttribute("aria-label", `${WEEKDAYS[weekday]} — ${label}`);
      for (let hour = 0; hour <= 24; hour += 1) {
        const option = new Option(`${String(hour).padStart(2, "0")}:00`, String(hour));
        input.add(option);
      }
      input.value = String(value);
      return input;
    };
    const from = select(current?.start_hour ?? 9, "начало");
    const to = select(current?.end_hour ?? 18, "конец");
    const sync = () => { from.disabled = !enabled.checked; to.disabled = !enabled.checked; };
    enabled.addEventListener("change", sync);
    sync();
    row.append(day, from, document.createTextNode("—"), to);
    element.append(row);
    rows.push({ weekday, enabled, from, to });
  }
  return { element, value: () => rows.filter((row) => row.enabled.checked).map((row) => ({
    weekday: row.weekday, start_hour: Number(row.from.value), end_hour: Number(row.to.value),
  })) };
}

async function renderSpecialists() {
  if (state.session?.role !== "chairman") return;
  if (!state.preview && !state.session?.demo_access) {
    const data = await api("/api/specialists");
    state.specialists = Array.isArray(data.specialists) ? data.specialists : [];
  }
  const container = $("#specialists-list");
  container.replaceChildren();
  if (!state.specialists.length) {
    container.innerHTML = emptyMarkup("Специалистов пока нет", "Добавьте сантехника или электрика по номеру телефона.");
    return;
  }
  for (const specialist of state.specialists) {
    const card = document.createElement("article");
    card.className = "specialist-item";
    const info = document.createElement("div");
    info.className = "specialist-item-head";
    info.innerHTML = `<span class="specialist-specialty">${escapeHtml(SPECIALTY[specialist.specialty] || specialist.specialty)}</span><span class="specialist-linked">${specialist.linked ? "Номер подтверждён" : "Ожидает входа"}</span><strong>${escapeHtml(specialist.full_name)}</strong><span class="specialist-phone">${escapeHtml(specialist.phone)}</span>`;
    card.append(info);
    const edit = document.createElement("details");
    edit.className = "specialist-edit";
    edit.innerHTML = "<summary>Данные и рабочие часы <span aria-hidden=\"true\">↗</span></summary>";
    const form = document.createElement("form");
    form.className = "compact-form";
    const typeLabel = document.createElement("label");
    typeLabel.textContent = "Специальность";
    const type = document.createElement("select");
    for (const [value, title] of Object.entries(SPECIALTY).filter(([value]) => value !== "other")) type.add(new Option(title, value));
    type.value = specialist.specialty;
    typeLabel.append(type);
    const nameLabel = document.createElement("label");
    nameLabel.textContent = "ФИО";
    const name = document.createElement("input");
    name.type = "text"; name.required = true; name.maxLength = 180; name.value = specialist.full_name;
    nameLabel.append(name);
    const phoneLabel = document.createElement("label");
    phoneLabel.textContent = "Номер телефона";
    const phone = document.createElement("input");
    phone.type = "tel"; phone.required = true; phone.value = specialist.phone;
    phoneLabel.append(phone);
    const hours = hoursEditor(specialist.work_hours || []);
    const heading = document.createElement("h3");
    heading.textContent = "Рабочие часы";
    const save = document.createElement("button");
    save.type = "submit"; save.textContent = "Сохранить специалиста";
    const feedback = document.createElement("p");
    feedback.className = "form-feedback"; feedback.setAttribute("role", "status");
    form.append(typeLabel, nameLabel, phoneLabel, heading, hours.element, save, feedback);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      feedback.textContent = "Сохраняем…";
      try {
        const body = { specialty: type.value, full_name: name.value, phone: phone.value, work_hours: hours.value() };
        if (state.preview || state.session?.demo_access) Object.assign(specialist, body);
        else await api(`/api/specialists/${encodeURIComponent(specialist.specialist_id)}`, { method: "PATCH", body: JSON.stringify(body) });
        await renderSpecialists();
        $("#specialist-message").textContent = "Данные и расписание специалиста сохранены.";
      } catch (error) {
        feedback.textContent = error.message;
        save.disabled = false;
      }
    });
    edit.append(form);
    card.append(edit);
    container.append(card);
  }
}

function residentMarkup(item, preview = false) {
  const parts = [item.full_name, item.unit ? `пом. ${item.unit}` : "", item.area ? `${item.area} м²` : ""].filter(Boolean);
  return `<div class="resident-item"><strong>${escapeHtml(parts[0] || "Собственник")}</strong><p>${escapeHtml(parts.slice(1).join(" · "))}</p>${!preview && item.code ? `<div class="resident-code-row"><code>${escapeHtml(item.code)}</code><button type="button" data-copy-code="${escapeHtml(item.code)}">Скопировать код</button></div>` : ""}</div>`;
}

function renderResidentList() {
  const query = $("#resident-search").value.trim().toLocaleLowerCase("ru-RU");
  const items = state.residents.filter((item) => `${item.full_name || ""} ${item.unit || ""}`.toLocaleLowerCase("ru-RU").includes(query));
  $("#residents-list").innerHTML = items.length ? items.map((item) => residentMarkup(item)).join("")
    : emptyMarkup(query ? "Совпадений нет" : "Реестр пока пуст", query ? "Попробуйте другой запрос." : "Загрузите PDF-реестр и проверьте найденные записи.");
}

async function renderResidents() {
  if (state.session?.role !== "chairman") return;
  if (!state.preview && !state.session?.demo_access) {
    const data = await api("/api/residents");
    state.residents = Array.isArray(data.residents) ? data.residents : [];
  }
  renderResidentList();
}

function renderSpecialistIdentity(session) {
  state.session = session;
  const name = session.display_name || "Специалист";
  $("#specialist-name").textContent = name;
  $("#profile-name").textContent = name;
  $("#profile-user-id").textContent = session.user_id ? "MAX ID: " + session.user_id : "Демонстрационный режим";
  $(".user-initial").textContent = name.charAt(0).toLocaleUpperCase("ru-RU");
  $(".profile-avatar").textContent = name.charAt(0).toLocaleUpperCase("ru-RU");
}

async function loadSpecialistRequests() {
  const retry = $("#retry-specialist-requests");
  retry.disabled = true;
  retry.textContent = "Загружаем…";
  try {
    const result = await api("/api/requests");
    if (!Array.isArray(result.requests)) {
      throw new Error("Сервис вернул некорректные данные заявок. Попробуйте ещё раз.");
    }
    state.requests = result.requests.map(normalizeRequest);
    $("#specialist-request-status").classList.add("hidden");
    $("#specialist").classList.remove("requests-unavailable");
    $("#loading").classList.add("hidden");
    $("#specialist").classList.remove("hidden");
    render();
    setView(state.view);
  } catch (error) {
    state.requests = [];
    $("#specialist-request-error").textContent = error.message || "Не удалось загрузить заявки. Попробуйте ещё раз.";
    $("#specialist-request-status").classList.remove("hidden");
    $("#specialist").classList.add("requests-unavailable");
    $("#loading").classList.add("hidden");
    $("#specialist").classList.remove("hidden");
  } finally {
    retry.disabled = false;
    retry.textContent = "Повторить загрузку";
  }
}

function bindEvents() {
  $$('[data-theme-toggle]').forEach((button) => button.addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
  }));
  $("#retry-specialist-requests").addEventListener("click", loadSpecialistRequests);
  $$('[data-view]').forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $$(".brand, .mobile-brand").forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault(); setView("overview");
  }));
  $$('[data-chairman-view]').forEach((button) => button.addEventListener("click", () => setChairmanView(button.dataset.chairmanView)));
  $("#chairman-request-list").addEventListener("click", (event) => {
    const opener = event.target.closest('[data-chairman-open]');
    if (opener) openRequest(opener.dataset.chairmanOpen);
  });
  $$('[data-action]').forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.action === "open-list") setView("list");
    if (button.dataset.action === "open-calendar") setView("calendar");
  }));
  $("#overview-focus").addEventListener("click", (event) => {
    const opener = event.target.closest('[data-open]');
    if (opener) openRequest(opener.dataset.open);
  });
  ["search", "status-filter", "priority-filter", "category-filter", "date-filter"].forEach((id) => {
    $("#" + id).addEventListener(id === "search" ? "input" : "change", render);
  });
  $("#filter-toggle").addEventListener("click", () => setFilterPanel($("#filter-panel").classList.contains("hidden")));
  $("#filter-close").addEventListener("click", () => setFilterPanel(false));
  $("#filter-backdrop").addEventListener("click", () => setFilterPanel(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#filter-panel").classList.contains("hidden")) setFilterPanel(false);
    if ($("#photo-viewer").open && event.key === "ArrowLeft") { event.preventDefault(); movePhoto(-1); }
    if ($("#photo-viewer").open && event.key === "ArrowRight") { event.preventDefault(); movePhoto(1); }
    if (event.key !== "Tab" || !window.matchMedia("(max-width: 620px)").matches || $("#filter-panel").classList.contains("hidden")) return;
    const controls = $$("#filter-panel button, #filter-panel select").filter((item) => !item.disabled);
    if (!controls.length) return;
    const first = controls[0]; const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  window.matchMedia("(max-width: 620px)").addEventListener("change", () => setFilterPanel(false));
  $("#reset-filters").addEventListener("click", () => { clearAllFilters(); render(); setFilterPanel(false); });
  $$('[data-quick]').forEach((button) => button.addEventListener("click", () => {
    const quick = button.dataset.quick;
    clearAllFilters();
    if (quick === "emergency" || quick === "urgent") {
      $("#priority-filter").value = quick;
      $("#status-filter").value = "open";
    }
    if (quick === "unscheduled") $("#date-filter").value = "unscheduled";
    render(); setFilterPanel(false);
  }));
  $(".board").addEventListener("click", (event) => {
    const opener = event.target.closest('[data-open]');
    if (opener) openRequest(opener.dataset.open);
    const dayButton = event.target.closest('[data-date]');
    if (dayButton) {
      state.selectedDate = dayButton.dataset.date;
      const date = parseDay(state.selectedDate);
      if (date.getMonth() !== state.month.getMonth() || date.getFullYear() !== state.month.getFullYear()) {
        state.month = new Date(date.getFullYear(), date.getMonth(), 1);
      }
      renderCalendar(filteredRequests());
      if (window.matchMedia("(max-width: 620px)").matches) {
        $(".calendar-agenda").scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }
  });
  $(".board").addEventListener("change", (event) => {
    const control = event.target.closest('[data-priority-id]');
    if (control) changeRequest(control.dataset.priorityId, "priority", control.value, control, "Приоритет сохранён");
  });
  $("#dialog-priority").addEventListener("change", (event) => changeRequest(state.activeRequestId, "priority", event.target.value, event.target, "Приоритет сохранён"));
  $("#dialog-status").addEventListener("change", (event) => changeRequest(state.activeRequestId, "status", event.target.value, event.target, "Статус сохранён"));
  $("#dialog-assignee").addEventListener("change", (event) => changeRequest(state.activeRequestId, "specialist_id", event.target.value, event.target, "Исполнитель сохранён"));
  $("#dialog-visit-request").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await api(`/api/requests/${encodeURIComponent(state.activeRequestId)}/visit`, { method: "POST" });
      $("#dialog-feedback").textContent = "Запрос отправлен собственнику в MAX. Он выберет свободное время.";
      await refreshRequests();
      const updated = activeRequest();
      if (updated) syncRequestDialog(updated);
    } catch (error) { $("#dialog-feedback").textContent = error.message; }
    finally { button.disabled = false; }
  });
  $("#request-dialog").addEventListener("click", (event) => {
    if (event.target === $("#request-dialog")) $("#request-dialog").close();
  });
  $("#request-dialog").addEventListener("close", () => { releaseRequestPhotos(); state.activeRequestId = null; });
  $("#photo-viewer-close").addEventListener("click", closePhotoViewer);
  $("#photo-viewer-prev").addEventListener("click", () => movePhoto(-1));
  $("#photo-viewer-next").addEventListener("click", () => movePhoto(1));
  $("#photo-viewer").addEventListener("click", (event) => { if (event.target === $("#photo-viewer")) closePhotoViewer(); });
  let touchX = 0;
  $("#photo-viewer").addEventListener("touchstart", (event) => { touchX = event.changedTouches[0]?.screenX || 0; }, { passive: true });
  $("#photo-viewer").addEventListener("touchend", (event) => {
    const delta = (event.changedTouches[0]?.screenX || 0) - touchX;
    if (Math.abs(delta) > 50) movePhoto(delta < 0 ? 1 : -1);
  }, { passive: true });
  $("#calendar-prev").addEventListener("click", () => {
    state.month = new Date(state.month.getFullYear(), state.month.getMonth() - 1, 1);
    state.selectedDate = localISO(new Date(state.month.getFullYear(), state.month.getMonth(), 1));
    renderCalendar(filteredRequests());
  });
  $("#calendar-next").addEventListener("click", () => {
    state.month = new Date(state.month.getFullYear(), state.month.getMonth() + 1, 1);
    state.selectedDate = localISO(new Date(state.month.getFullYear(), state.month.getMonth(), 1));
    renderCalendar(filteredRequests());
  });
  $("#calendar-today").addEventListener("click", () => {
    const today = new Date();
    state.month = new Date(today.getFullYear(), today.getMonth(), 1);
    state.selectedDate = localISO(today);
    renderCalendar(filteredRequests());
  });
  $("#specialist-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget; const button = form.querySelector('button[type="submit"]');
    const feedback = $("#specialist-message");
    button.disabled = true; feedback.textContent = "Сохраняем специалиста…";
    try {
      const body = { specialty: $("#specialist-type").value, full_name: $("#specialist-full-name").value, phone: $("#specialist-phone").value };
      if (state.preview || state.session?.demo_access) {
        state.specialists.push({ ...body, specialist_id: `demo-${Date.now()}`, linked: false, work_hours: [] });
        feedback.textContent = "Пример специалиста добавлен только в этом просмотре.";
      } else {
        await api("/api/specialists", { method: "POST", body: JSON.stringify(body) });
        feedback.textContent = "Специалист добавлен. Попросите его выбрать роль в MAX и подтвердить номер.";
      }
      form.reset(); await renderSpecialists();
    } catch (error) { feedback.textContent = error.message; }
    finally { button.disabled = false; }
  });
  $("#timezone-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const feedback = $("#timezone-message");
    try {
      if (!state.preview && !state.session?.demo_access) await api("/api/hoa-timezone", { method: "PATCH", body: JSON.stringify({ timezone: $("#hoa-timezone").value }) });
      feedback.textContent = state.preview || state.session?.demo_access ? "Часовой пояс изменён только в просмотре." : "Часовой пояс сохранён.";
    } catch (error) { feedback.textContent = error.message; }
  });
  $("#group-chat-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const link = $("#group-chat-link").value.trim();
    const feedback = $("#group-chat-message");
    try {
      const result = await api("/api/group-chat", { method: "PATCH", body: JSON.stringify({ link }) });
      renderGroupChatState(result.group_chat);
      feedback.textContent = link ? "Ссылка сохранена." : "Ссылка удалена.";
    } catch (error) { feedback.textContent = error.message; }
  });
  $("#resident-search").addEventListener("input", renderResidentList);
  $("#residents-list").addEventListener("click", async (event) => {
    const button = event.target.closest('[data-copy-code]');
    if (!button) return;
    try { await navigator.clipboard.writeText(button.dataset.copyCode); button.textContent = "Скопировано"; }
    catch { $("#registry-message").textContent = "Код можно выделить и скопировать вручную."; }
  });
  $("#registry-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = $("#registry-file").files[0];
    if (!file) return;
    const feedback = $("#registry-message");
    feedback.textContent = "Проверяем реестр. Это может занять несколько минут…";
    state.pendingRegistryDigest = null;
    $("#registry-preview").classList.add("hidden");
    try {
      const data = await api("/api/registry", { method: "POST", headers: { "Content-Type": "application/pdf", "X-File-Name": encodeURIComponent(file.name) }, body: file });
      state.pendingRegistryDigest = data.digest;
      $("#preview-text").textContent = `Адрес: ${data.address}. Найдено собственников: ${data.resident_count}. Пропущенные помещения: ${(data.skipped_units || []).join(", ") || "нет"}. Проверьте записи перед сохранением.`;
      $("#preview-residents").innerHTML = (data.residents || []).map((item) => residentMarkup(item, true)).join("");
      $("#registry-preview").classList.remove("hidden");
      feedback.textContent = "Проверьте записи перед импортом.";
    } catch (error) { feedback.textContent = error.message; }
  });
  $("#confirm-registry").addEventListener("click", async () => {
    const file = $("#registry-file").files[0];
    if (!file || !state.pendingRegistryDigest) return;
    const feedback = $("#registry-message");
    feedback.textContent = "Сохраняем реестр…";
    try {
      const data = await api("/api/registry", { method: "POST", headers: { "Content-Type": "application/pdf", "X-File-Name": encodeURIComponent(file.name), "X-Confirm-Registry": state.pendingRegistryDigest }, body: file });
      feedback.textContent = `Сохранено собственников: ${data.imported}.`;
      state.pendingRegistryDigest = null;
      $("#registry-preview").classList.add("hidden");
      $("#registry-upload").open = false;
      await renderResidents();
    } catch (error) { feedback.textContent = error.message; }
  });
}

async function start() {
  initializeTheme();
  bindEvents();
  state.selectedDate = localISO(new Date());
  const params = new URLSearchParams(window.location.search);
  const previewMode = ["localhost", "127.0.0.1"].includes(window.location.hostname) ? params.get("preview") : null;
  state.preview = ["1", "chairman", "owner", "empty"].includes(previewMode);
  if (previewMode === "chairman" || previewMode === "owner") {
    renderRoleDemo(previewMode, previewMode === "owner" ? "Алексей Петров" : "Анна Петрова", "+7 ••• ••• 45 67");
    return;
  }
  if (state.preview) {
    state.requests = previewMode === "empty" ? [] : demoRequests();
    renderSpecialistIdentity({ role: "specialist", display_name: previewMode === "empty" ? "Пустая очередь" : "Специалист" });
    $("#loading").classList.add("hidden");
    $("#specialist").classList.remove("hidden");
    render();
    setView("overview");
    return;
  }

  const webApp = window.WebApp;
  if (!webApp?.initData) {
    showError("Откройте мини-приложение из чата с ботом в MAX.");
    return;
  }
  state.initData = webApp.initData;
  webApp.ready?.();
  webApp.expand?.();
  try {
    const session = await api("/api/session");
    state.session = session;
    if (session.role === "specialist") {
      renderSpecialistIdentity(session);
      if (session.demo_access) {
        state.preview = true;
        state.requests = demoRequests();
        $("#loading").classList.add("hidden");
        $("#specialist").classList.remove("hidden");
        render();
        setView("overview");
      } else await loadSpecialistRequests();
    } else if (session.role === "chairman") {
      if (session.demo_access) {
        renderRoleDemo("chairman", session.display_name, session.phone);
      } else {
        const [profile, requests] = await Promise.all([api("/api/profile"), api("/api/requests")]);
        const specialists = await api("/api/specialists").catch(() => ({ specialists: [] }));
        state.specialists = Array.isArray(specialists.specialists) ? specialists.specialists : [];
        renderChairman(profile, Array.isArray(requests.requests) ? requests.requests : []);
      }
    } else if (session.role === "owner") {
      if (session.demo_access) renderRoleDemo("owner", session.display_name, session.phone);
      else {
        const [profile, requests] = await Promise.all([api("/api/profile"), api("/api/requests")]);
        renderChairman(profile, Array.isArray(requests.requests) ? requests.requests : []);
      }
    } else {
      showError("Для этой роли пространство пока недоступно.");
    }
  } catch (error) {
    showError(error.message || "Не удалось открыть рабочее пространство.");
  }
}

start();
