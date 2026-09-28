const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const PRIORITY = {
  urgent: { label: "Срочно", order: 0 },
  high: { label: "Высокий", order: 1 },
  normal: { label: "Обычный", order: 2 },
  low: { label: "Низкий", order: 3 },
};
const STATUS = {
  new: "Новая",
  in_progress: "В работе",
  waiting: "Ожидает",
  done: "Завершена",
};
const state = {
  requests: [],
  chairmanRequests: [],
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
    throw new Error("Сервис временно недоступен. Попробуйте ещё раз.");
  }
  if (!response.ok) throw new Error(payload.error || "Не удалось выполнить запрос");
  return payload;
}

function demoRequests() {
  const examples = [
    ["Протечка стояка на 4 этаже", "Инженерные сети", "ул. Садовая, 18", "urgent", "in_progress", 0, "В квартире на четвёртом этаже обнаружена протечка стояка. Требуется осмотр и устранение."],
    ["Не работает пассажирский лифт", "Лифт", "пр. Мира, 42", "urgent", "new", 1, "Лифт остановился между этажами. Нужна проверка оборудования."],
    ["Освещение в подъезде № 2", "Освещение", "ул. Лесная, 7", "high", "new", 0, "Не включается свет на лестничной площадке второго подъезда."],
    ["Проверка отопления в доме", "Инженерные сети", "ул. Садовая, 18", "high", "in_progress", 2, "Жители сообщают о низкой температуре радиаторов."],
    ["Уборка придомовой территории", "Уборка", "ул. Парковая, 6", "normal", "waiting", 4, "Необходимо убрать листву у входной группы."],
    ["Замена ламп на лестнице", "Освещение", "пр. Мира, 42", "normal", "new", 7, "Перегорели лампы между третьим и четвёртым этажами."],
    ["Ремонт входной двери", "Благоустройство", "ул. Тихая, 12", "normal", "in_progress", 12, "Доводчик двери не удерживает створку."],
    ["Проверка вентиляции", "Инженерные сети", "ул. Лесная, 7", "normal", "waiting", -2, "Проверить тягу в вентиляционном канале."],
    ["Вывоз крупного мусора", "Уборка", "ул. Парковая, 6", "low", "done", -5, "У входа в контейнерную площадку складированы крупные предметы."],
    ["Шум в электрощите", "Инженерные сети", "ул. Тихая, 12", "high", "new", null, "При включении освещения слышен необычный шум в электрощите."],
    ["Покраска ограждения", "Благоустройство", "ул. Садовая, 18", "low", "new", null, "Обновить покрытие ограждения у детской площадки."],
    ["Не закрывается окно в холле", "Благоустройство", "пр. Мира, 42", "normal", "new", null, "Окно на первом этаже остаётся открытым после проветривания."],
  ];
  return examples.map((entry, index) => ({
    id: 1042 + index,
    title: entry[0],
    category: entry[1],
    address: entry[2],
    priority: entry[3],
    status: entry[4],
    scheduled_for: entry[5] === null ? null : offsetDate(entry[5]),
    description: entry[6],
    created_at: new Date(Date.now() - (index + 1) * 6 * 3600000).toISOString(),
    assignee: null,
  }));
}

function updateToday() {
  const today = new Date();
  const todayIso = localISO(today);
  $("#overview-date-label").textContent = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" }).format(today);
  $("#today-day").textContent = String(today.getDate()).padStart(2, "0");
  $("#today-month").textContent = new Intl.DateTimeFormat("ru-RU", { month: "long" }).format(today).toUpperCase();
  $("#today-weekday").textContent = new Intl.DateTimeFormat("ru-RU", { weekday: "long" }).format(today);
  const todayCount = state.requests.filter((request) => request.scheduled_for === todayIso).length;
  $("#today-scheduled").textContent = countLabel(todayCount, "заявка", "заявки", "заявок") + " на сегодня";
}

function updateMetrics() {
  const attention = state.requests.filter((request) => ["urgent", "high"].includes(request.priority) && request.status !== "done").length;
  $("#metric-total").textContent = numberLabel(state.requests.length);
  $("#metric-urgent").textContent = numberLabel(attention);
  $("#overview-attention").textContent = numberLabel(attention);
  $("#metric-scheduled").textContent = numberLabel(state.requests.filter((request) => request.scheduled_for).length);
  $("#profile-open-count").textContent = numberLabel(state.requests.filter((request) => request.status !== "done").length);
  $("#profile-done-count").textContent = numberLabel(state.requests.filter((request) => request.status === "done").length);
  $("#quick-all-count").textContent = state.requests.length;
  $("#quick-urgent-count").textContent = state.requests.filter((request) => request.priority === "urgent").length;
  $("#quick-undated-count").textContent = state.requests.filter((request) => !request.scheduled_for).length;
  updateToday();
}

function updateCategories() {
  const select = $("#category-filter");
  const selected = select.value;
  const categories = [...new Set(state.requests.map((request) => request.category).filter(Boolean))].sort((a, b) => a.localeCompare(b, "ru"));
  select.innerHTML = '<option value="">Все категории</option>' + categories.map((category) => '<option value="' + escapeHtml(category) + '">' + escapeHtml(category) + "</option>").join("");
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
      && (!current.status || request.status === current.status)
      && (!current.priority || request.priority === current.priority)
      && (!current.category || request.category === current.category)
      && (!current.date || (current.date === "scheduled" ? Boolean(request.scheduled_for) : !request.scheduled_for));
  }).sort((a, b) => {
    const rank = (PRIORITY[a.priority]?.order ?? 4) - (PRIORITY[b.priority]?.order ?? 4);
    if (rank) return rank;
    if (!a.scheduled_for && !b.scheduled_for) return b.created_at.localeCompare(a.created_at);
    if (!a.scheduled_for) return 1;
    if (!b.scheduled_for) return -1;
    return a.scheduled_for.localeCompare(b.scheduled_for) || b.created_at.localeCompare(a.created_at);
  });
}

function priorityOptions(selected) {
  return Object.entries(PRIORITY).map(([value, config]) => '<option value="' + value + '"' + (selected === value ? " selected" : "") + ">" + config.label + "</option>").join("");
}

function emptyMarkup(title, text) {
  return '<div class="empty-state"><span class="empty-symbol" aria-hidden="true">↗</span><h3>' + escapeHtml(title) + '</h3><p>' + escapeHtml(text) + "</p></div>";
}

function rowMarkup(request) {
  const status = STATUS[request.status] || request.status;
  const id = escapeHtml(request.id);
  const code = escapeHtml(requestCode(request.id));
  return '<div class="request-row" data-id="' + id + '">'
    + '<div class="priority-cell"><span class="priority-mark priority-' + escapeHtml(request.priority) + '"></span><select class="priority-select priority-' + escapeHtml(request.priority) + '" data-priority-id="' + id + '" aria-label="Приоритет заявки № ' + code + '">' + priorityOptions(request.priority) + '</select></div>'
    + '<button type="button" class="request-open" data-open="' + id + '"><span class="request-number"><span class="ticket-code">№ ' + code + '</span><span class="ticket-category">' + escapeHtml(request.category) + '</span></span><strong>' + escapeHtml(request.title) + '</strong></button>'
    + '<div class="address-cell">' + escapeHtml(request.address) + '</div>'
    + '<div class="date-cell">' + (request.scheduled_for ? escapeHtml(dateLabel(request.scheduled_for)) : '<span class="muted">Не назначена</span>') + '</div>'
    + '<div class="status-cell"><span class="status-dot status-' + escapeHtml(request.status) + '"></span>' + escapeHtml(status) + '</div>'
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
    .filter((request) => ["urgent", "high"].includes(request.priority) && request.status !== "done")
    .sort((a, b) => (PRIORITY[a.priority]?.order ?? 4) - (PRIORITY[b.priority]?.order ?? 4)
      || String(a.scheduled_for || "9999").localeCompare(String(b.scheduled_for || "9999")))
    .slice(0, 3);
  $("#overview-focus").innerHTML = focus.length ? focus.map((request, index) =>
    '<button type="button" class="focus-item" data-open="' + escapeHtml(request.id) + '">'
    + '<span class="focus-index">' + String(index + 1).padStart(2, "0") + '</span>'
    + '<span class="focus-content"><span class="focus-meta">№ ' + escapeHtml(requestCode(request.id)) + ' / ' + escapeHtml(request.address) + '</span><strong>' + escapeHtml(request.title) + '</strong><span class="focus-status"><i class="priority-mark priority-' + escapeHtml(request.priority) + '"></i>' + escapeHtml(PRIORITY[request.priority]?.label || request.priority) + ' · ' + escapeHtml(STATUS[request.status] || request.status) + '</span></span>'
    + '<span class="focus-arrow" aria-hidden="true">↗</span></button>'
  ).join("") : emptyMarkup(state.requests.length ? "Внимание не требуется" : "Заявок пока нет", state.requests.length ? "Срочных и высокоприоритетных заявок сейчас нет." : "Новые обращения появятся здесь после создания председателем.");
}

function agendaMarkup(request, undated = false) {
  return '<button class="' + (undated ? "undated-item" : "agenda-item") + '" type="button" data-open="' + escapeHtml(request.id) + '">'
    + '<span class="agenda-priority priority-' + escapeHtml(request.priority) + '"></span>'
    + '<span class="agenda-body"><strong class="agenda-title">' + escapeHtml(request.title) + '</strong><span class="agenda-meta">' + escapeHtml(request.address) + '</span></span>'
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
  const firstWeekday = (new Date(year, month, 1).getDay() + 6) % 7;
  const lastDay = new Date(year, month + 1, 0).getDate();
  const cells = Math.ceil((firstWeekday + lastDay) / 7) * 7;
  const todayIso = localISO(new Date());
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
  const noOtherFilters = !current.search && !current.status && !current.category;
  const quick = noOtherFilters && current.priority === "urgent" && !current.date ? "urgent"
    : noOtherFilters && current.priority === "high" && !current.date ? "high"
      : noOtherFilters && current.date === "unscheduled" && !current.priority ? "unscheduled"
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
  if (view !== state.view && (view === "list" || view === "calendar")) {
    clearAllFilters();
    render();
  }
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
  if (!["requests", "create", "profile"].includes(view)) return;
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
}

function setFilterPanel(open) {
  $("#filter-panel").classList.toggle("hidden", !open);
  $("#filter-backdrop").classList.toggle("hidden", !open);
  $("#filter-toggle").setAttribute("aria-expanded", String(open));
  document.body.classList.toggle("filter-open", open && window.matchMedia("(max-width: 620px)").matches);
}

function showToast(message) {
  const toast = $("#action-toast");
  window.clearTimeout(toastTimer);
  toast.textContent = message;
  toast.classList.remove("hidden");
  toastTimer = window.setTimeout(() => toast.classList.add("hidden"), 3200);
}

function openRequest(id) {
  const request = state.requests.find((item) => String(item.id) === String(id));
  if (!request) return;
  state.activeRequestId = request.id;
  $("#dialog-id").textContent = "ЗАЯВКА № " + requestCode(request.id);
  $("#dialog-title").textContent = request.title;
  $("#dialog-description").textContent = request.description || "Описание не добавлено.";
  $("#dialog-address").textContent = request.address || "—";
  $("#dialog-category").textContent = request.category || "—";
  $("#dialog-status").value = request.status;
  $("#dialog-date").value = request.scheduled_for || "";
  $("#dialog-created").textContent = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" }).format(new Date(request.created_at));
  $("#dialog-priority").value = request.priority;
  $("#dialog-feedback").textContent = "";
  $("#request-dialog").showModal();
}

async function changeRequest(id, endpoint, field, value, control, successMessage) {
  const request = state.requests.find((item) => String(item.id) === String(id));
  if (!request || (request[field] ?? null) === (value ?? null)) return;
  const oldValue = request[field] ?? "";
  const restoreFocus = document.activeElement === control && !$("#request-dialog").open;
  control.disabled = true;
  try {
    if (state.preview) {
      request[field] = value;
    } else {
      const result = await api("/api/requests/" + encodeURIComponent(id) + "/" + endpoint, {
        method: "PATCH",
        body: JSON.stringify({ [field]: value }),
      });
      Object.assign(request, result.request);
    }
    render();
    if (restoreFocus) {
      const nextControl = $$("[data-priority-id]").find((item) => item.dataset.priorityId === String(id));
      (nextControl || $("#filter-toggle")).focus();
    }
    if (state.activeRequestId === request.id && $("#request-dialog").open) {
      $("#dialog-priority").value = request.priority;
      $("#dialog-status").value = request.status;
      $("#dialog-date").value = request.scheduled_for || "";
      $("#dialog-feedback").textContent = successMessage;
    } else if (field === "priority") {
      showToast("Приоритет заявки № " + requestCode(request.id) + " сохранён. Порядок очереди обновлён.");
    }
  } catch (error) {
    control.value = oldValue;
    if ($("#request-dialog").open) $("#dialog-feedback").textContent = error.message;
    else window.alert(error.message);
  } finally {
    control.disabled = false;
  }
}

function renderChairmanRequests() {
  const requests = [...state.chairmanRequests].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  $("#chairman-request-list").innerHTML = requests.length ? requests.map((request) => {
    const code = escapeHtml(requestCode(request.id));
    return '<details class="chairman-request"><summary><span class="chairman-request-code">№ ' + code + '</span><span class="chairman-request-status"><i class="status-dot status-' + escapeHtml(request.status) + '"></i>' + escapeHtml(STATUS[request.status] || request.status) + '</span><strong>' + escapeHtml(request.title) + '</strong><span class="chairman-request-meta">' + escapeHtml(request.scheduled_for ? dateLabel(request.scheduled_for) : "Дата не назначена") + ' · ' + escapeHtml(PRIORITY[request.priority]?.label || request.priority) + '</span><span class="chairman-request-arrow" aria-hidden="true">↗</span></summary><div class="chairman-request-body"><p>' + escapeHtml(request.description || "Описание не добавлено.") + '</p><span>' + escapeHtml(request.category || "") + ' / ' + escapeHtml(request.address || "") + '</span></div></details>';
  }).join("") : emptyMarkup("Заявок пока нет", "Создайте первую заявку, чтобы передать задачу специалисту.");
}

function renderChairman(profile, requests = []) {
  state.chairmanRequests = requests;
  $("#chairman-name").textContent = profile.full_name;
  $("#chairman-hoa").textContent = profile.hoa_name;
  $("#chairman-address").textContent = profile.address;
  $("#chairman-phone").textContent = profile.phone;
  $("#chairman-protocol").textContent = profile.protocol_filename;
  $("#chairman-verified").textContent = new Intl.DateTimeFormat("ru-RU", { dateStyle: "long" }).format(new Date(profile.verified_at));
  renderChairmanRequests();
  $("#loading").classList.add("hidden");
  $("#chairman").classList.remove("hidden");
  setChairmanView("requests");
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

function bindEvents() {
  $$("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $$(".brand, .mobile-brand").forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault();
    setView("overview");
  }));
  $$("[data-chairman-view]").forEach((button) => button.addEventListener("click", () => setChairmanView(button.dataset.chairmanView)));
  $$("[data-action]").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.action === "open-list") setView("list");
    if (button.dataset.action === "open-calendar") setView("calendar");
  }));
  $("#overview-focus").addEventListener("click", (event) => {
    const opener = event.target.closest("[data-open]");
    if (opener) openRequest(opener.dataset.open);
  });
  ["search", "status-filter", "priority-filter", "category-filter", "date-filter"].forEach((id) => {
    $("#" + id).addEventListener(id === "search" ? "input" : "change", render);
  });
  $("#filter-toggle").addEventListener("click", () => {
    setFilterPanel($("#filter-panel").classList.contains("hidden"));
  });
  $("#filter-close").addEventListener("click", () => setFilterPanel(false));
  $("#filter-backdrop").addEventListener("click", () => setFilterPanel(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#filter-panel").classList.contains("hidden")) setFilterPanel(false);
  });
  window.matchMedia("(max-width: 620px)").addEventListener("change", () => setFilterPanel(false));
  $("#reset-filters").addEventListener("click", () => {
    clearAllFilters();
    render();
    setFilterPanel(false);
  });
  $$("[data-quick]").forEach((button) => button.addEventListener("click", () => {
    const quick = button.dataset.quick;
    clearAllFilters();
    if (quick === "urgent" || quick === "high") $("#priority-filter").value = quick;
    if (quick === "unscheduled") $("#date-filter").value = "unscheduled";
    render();
    setFilterPanel(false);
  }));
  $(".board").addEventListener("click", (event) => {
    const opener = event.target.closest("[data-open]");
    if (opener) openRequest(opener.dataset.open);
    const dayButton = event.target.closest("[data-date]");
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
    const control = event.target.closest("[data-priority-id]");
    if (control) changeRequest(control.dataset.priorityId, "priority", "priority", control.value, control, "Приоритет сохранён");
  });
  $("#dialog-priority").addEventListener("change", (event) => changeRequest(state.activeRequestId, "priority", "priority", event.target.value, event.target, "Приоритет сохранён"));
  $("#dialog-status").addEventListener("change", (event) => changeRequest(state.activeRequestId, "status", "status", event.target.value, event.target, "Статус сохранён"));
  $("#dialog-date").addEventListener("change", (event) => changeRequest(state.activeRequestId, "schedule", "scheduled_for", event.target.value || null, event.target, "Дата выезда сохранена"));
  $("#dialog-clear-date").addEventListener("click", (event) => changeRequest(state.activeRequestId, "schedule", "scheduled_for", null, event.currentTarget, "Дата выезда убрана"));
  $("#request-dialog").addEventListener("click", (event) => {
    if (event.target === $("#request-dialog")) $("#request-dialog").close();
  });
  $("#request-dialog").addEventListener("close", () => { state.activeRequestId = null; });
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
  $("#new-request-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type="submit"]');
    const feedback = $("#form-feedback");
    const values = Object.fromEntries(new FormData(form).entries());
    button.disabled = true;
    feedback.textContent = "Создаём заявку…";
    try {
      const result = state.preview
        ? { request: { ...values, id: String(2000 + Math.floor(Math.random() * 8000)), address: $("#chairman-address").textContent, priority: "normal", status: "new", scheduled_for: values.scheduled_for || null, created_at: new Date().toISOString() } }
        : await api("/api/requests", { method: "POST", body: JSON.stringify(values) });
      state.chairmanRequests.unshift(result.request);
      renderChairmanRequests();
      feedback.textContent = "";
      form.reset();
      setChairmanView("requests");
      showToast(state.preview
        ? "Пример: заявка № " + requestCode(result.request.id) + " создана локально."
        : "Заявка № " + requestCode(result.request.id) + " передана специалисту.");
    } catch (error) {
      feedback.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
}

async function start() {
  bindEvents();
  state.selectedDate = localISO(new Date());
  const params = new URLSearchParams(window.location.search);
  const previewMode = ["localhost", "127.0.0.1"].includes(window.location.hostname) ? params.get("preview") : null;
  state.preview = ["1", "chairman", "empty"].includes(previewMode);
  if (previewMode === "chairman") {
    $(".chairman-header>span:last-child").textContent = "ПРОФИЛЬ ПРЕДСЕДАТЕЛЯ / ПРИМЕР ДАННЫХ";
    const profile = {
      full_name: "Анна Петрова",
      hoa_name: "ТСЖ «Садовая, 18»",
      address: "ул. Садовая, 18",
      phone: "+7 ••• ••• 45 67",
      protocol_filename: "Протокол собрания.pdf",
      verified_at: new Date().toISOString(),
    };
    renderChairman(profile, demoRequests().filter((request) => request.address === profile.address));
    return;
  }
  if (state.preview) {
    state.requests = previewMode === "empty" ? [] : demoRequests();
    renderSpecialistIdentity({ display_name: previewMode === "empty" ? "Пустая очередь" : "Специалист" });
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
    if (session.role === "specialist") {
      const result = await api("/api/requests");
      state.requests = Array.isArray(result.requests) ? result.requests : [];
      renderSpecialistIdentity(session);
      $("#loading").classList.add("hidden");
      $("#specialist").classList.remove("hidden");
      render();
      setView("overview");
    } else if (session.role === "chairman") {
      const [profile, requests] = await Promise.all([api("/api/profile"), api("/api/requests")]);
      renderChairman(profile, Array.isArray(requests.requests) ? requests.requests : []);
    } else {
      showError("Для этой роли пространство пока недоступно.");
    }
  } catch (error) {
    showError(error.message || "Не удалось открыть рабочее пространство.");
  }
}

start();
