const loading = document.querySelector("#loading");
const error = document.querySelector("#error");
const profile = document.querySelector("#profile");
const registryMessage = document.querySelector("#registry-message");
const requestsMessage = document.querySelector("#requests-message");
const specialistMessage = document.querySelector("#specialist-message");
const statusLabels = {
  review: "На рассмотрении", in_progress: "В процессе выполнения",
  done: "Исполнено", rejected: "Отклонена",
};
const priorityLabels = { emergency: "Авария", urgent: "Срочно", today: "На сегодня", planned: "Запланировано" };
const specialtyLabels = { plumber: "Сантехник", electrician: "Электрик" };
const weekdayLabels = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const timezoneLabels = {
  "Europe/Kaliningrad": "Калининград, UTC+2", "Europe/Moscow": "Москва, UTC+3",
  "Europe/Samara": "Самара, UTC+4", "Asia/Yekaterinburg": "Екатеринбург, UTC+5",
  "Asia/Omsk": "Омск, UTC+6", "Asia/Krasnoyarsk": "Красноярск, UTC+7",
  "Asia/Irkutsk": "Иркутск, UTC+8", "Asia/Yakutsk": "Якутск, UTC+9",
  "Asia/Vladivostok": "Владивосток, UTC+10", "Asia/Magadan": "Магадан, UTC+11",
  "Asia/Kamchatka": "Камчатка, UTC+12",
};
const pageSize = 5;
let currentProfile;
let pendingDigest;
let residents = [];
let residentPage = 0;
let requests = [];
let requestSpecialists = [];
let requestPage = 0;
let photoUrls = [];
let photoUrlsByPosition = new Map();
let failedPhotoPositions = new Set();
let viewerPosition = 0;
let viewerCount = 0;
let photoReturnFocus = null;
let activeRequestId = null;

const node = (tag, className, value) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value !== undefined) element.textContent = value;
  return element;
};

const showError = (message) => {
  loading.classList.add("hidden");
  profile.classList.add("hidden");
  error.classList.remove("hidden");
  document.querySelector("#error-message").textContent = message;
};

const api = async (path, options = {}) => {
  const response = await fetch(path, {
    ...options,
    headers: { Authorization: `tma ${window.WebApp.initData}`, ...options.headers },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Запрос не выполнен");
  return data;
};

const displayDate = (value) => new Date(value).toLocaleString("ru-RU");
const shortName = (fullName) => {
  const parts = String(fullName || "").trim().split(/\s+/).filter(Boolean);
  if (parts.length < 2) return parts[0] || "—";
  return `${parts[0]} ${parts.slice(1).map((part) => `${part[0].toLocaleUpperCase("ru-RU")}.`).join(" ")}`;
};
const timeLabel = (hour) => `${String(hour).padStart(2, "0")}:00`;
const visitLabel = (request) => `${request.visit_date} · ${timeLabel(request.visit_hour)}–${timeLabel(request.visit_hour + 1)} (${timezoneLabels[request.visit_timezone] || request.visit_timezone})`;
const hourSelect = (from, to, value) => {
  const select = node("select");
  for (let hour = from; hour <= to; hour++) {
    const option = node("option", "", timeLabel(hour));
    option.value = String(hour);
    select.append(option);
  }
  select.value = String(value);
  return select;
};
const hoursEditor = (hours) => {
  const wrapper = node("div", "hours-editor");
  wrapper.append(node("p", "muted", "Часовые интервалы по местному времени ТСЖ. Отключённый день — выходной."));
  const rows = [];
  for (let weekday = 0; weekday < 7; weekday++) {
    const existing = hours.find((item) => item.weekday === weekday);
    const row = node("div", "hours-row");
    const label = node("label", "hours-day");
    const enabled = node("input");
    enabled.type = "checkbox";
    enabled.checked = Boolean(existing);
    label.append(enabled, node("span", "", weekdayLabels[weekday]));
    const start = hourSelect(0, 23, existing?.start_hour ?? 9);
    const end = hourSelect(1, 24, existing?.end_hour ?? 18);
    const sync = () => { start.disabled = !enabled.checked; end.disabled = !enabled.checked; };
    enabled.addEventListener("change", sync);
    sync();
    row.append(label, start, node("span", "hours-dash", "—"), end);
    wrapper.append(row);
    rows.push({ weekday, enabled, start, end });
  }
  return { element: wrapper, value: () => rows.filter((item) => item.enabled.checked).map((item) => ({
    weekday: item.weekday, start_hour: Number(item.start.value), end_hour: Number(item.end.value),
  })) };
};
const executorLabel = (request) => request.specialist_id
  ? `${shortName(request.specialist_name)} — ${specialtyLabels[request.specialty] || "Специалист"}`
  : `${shortName(request.chairman_name)} — Председатель`;

const groupByUnit = (entries) => {
  const groups = new Map();
  for (const entry of entries) {
    const unit = String(entry.unit || "").trim().replace(/\s+/g, " ");
    const key = unit.toLocaleLowerCase("ru-RU");
    if (!groups.has(key)) groups.set(key, { unit, owners: [] });
    groups.get(key).owners.push(entry);
  }
  return [...groups.values()];
};

const ownerCountLabel = (count) => {
  const lastTwo = count % 100;
  const last = count % 10;
  const noun = lastTwo >= 11 && lastTwo <= 14 ? "собственников"
    : last === 1 ? "собственник" : last >= 2 && last <= 4 ? "собственника" : "собственников";
  return `${count} ${noun}`;
};

const residentDetails = (resident) => {
  const details = node("details");
  details.append(node("summary", "", "Площадь, доля и право"));
  details.append(node("p", "muted", `${resident.area} м² · доля ${resident.share} · ${resident.ownership}`));
  return details;
};

const unitCard = (group, preview = false) => {
  const card = node("div", "list-item unit-card");
  const heading = node("div", "unit-heading");
  heading.append(node("strong", "", `Помещение ${group.unit}`));
  heading.append(node("span", "unit-count", ownerCountLabel(group.owners.length)));
  card.append(heading);
  for (const resident of group.owners) {
    const owner = node("div", "unit-owner");
    const nameLine = node("div", "owner-heading");
    nameLine.append(node("strong", "", resident.full_name));
    if (!preview) {
      nameLine.append(node("span", resident.linked ? "login-state login-state-linked" : "login-state login-state-pending",
        resident.linked ? "Авторизован" : "Ожидает входа"));
    }
    owner.append(nameLine);
    if (!preview) {
      const codeLine = node("div", "code-line");
      codeLine.append(node("code", "resident-code", resident.code));
      const copy = node("button", "secondary", "Скопировать код");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(resident.code);
          copy.textContent = "Скопировано";
        } catch {
          registryMessage.textContent = "Не удалось скопировать код. Его можно выделить вручную.";
        }
      });
      codeLine.append(copy);
      owner.append(codeLine);
    }
    owner.append(residentDetails(resident));
    card.append(owner);
  }
  return card;
};

const pagination = (selector, page, total, onChange) => {
  const controls = document.querySelector(selector);
  const pageCount = Math.ceil(total / pageSize);
  controls.replaceChildren();
  controls.classList.toggle("hidden", pageCount < 2);
  if (pageCount < 2) return;
  const previous = node("button", "secondary", "Назад");
  previous.type = "button";
  previous.disabled = page === 0;
  previous.addEventListener("click", () => onChange(page - 1));
  const next = node("button", "secondary", "Далее");
  next.type = "button";
  next.disabled = page >= pageCount - 1;
  next.addEventListener("click", () => onChange(page + 1));
  controls.append(previous, node("span", "", `${page + 1} / ${pageCount}`), next);
};

const renderResidentPage = () => {
  const container = document.querySelector("#residents");
  container.replaceChildren();
  if (!residents.length) {
    document.querySelector("#registry-upload").open = true;
    container.append(node("p", "muted", "Реестр пока не загружен."));
    document.querySelector("#residents-pagination").classList.add("hidden");
    return;
  }
  const query = document.querySelector("#resident-search").value.trim().toLocaleLowerCase("ru-RU");
  const groups = groupByUnit(residents);
  const filtered = groups.filter((group) =>
    `${group.unit} ${group.owners.map((owner) => owner.full_name).join(" ")}`
      .toLocaleLowerCase("ru-RU").includes(query));
  residentPage = Math.min(residentPage, Math.max(0, Math.ceil(filtered.length / pageSize) - 1));
  const count = query ? `Найдено помещений: ${filtered.length} из ${groups.length}`
    : `Помещений: ${groups.length} · собственников: ${residents.length}`;
  container.append(node("p", "muted", `${count}. Коды обновятся ${displayDate(residents[0].code_expires_at)}.`));
  if (!filtered.length) container.append(node("p", "muted", "Совпадений нет."));
  for (const group of filtered.slice(residentPage * pageSize, (residentPage + 1) * pageSize)) {
    container.append(unitCard(group));
  }
  pagination("#residents-pagination", residentPage, filtered.length, (page) => {
    residentPage = page;
    renderResidentPage();
    document.querySelector("#resident-search").scrollIntoView({ block: "start" });
  });
};

const renderResidents = async () => {
  residents = (await api("/api/residents")).residents;
  renderResidentPage();
};

document.querySelector("#resident-search").addEventListener("input", () => {
  residentPage = 0;
  renderResidentPage();
});

const photoViewer = document.querySelector("#photo-viewer");
const viewerImage = document.querySelector("#photo-viewer-image");
const viewerMessage = document.querySelector("#photo-viewer-message");
const viewerCountLabel = document.querySelector("#photo-viewer-count");

const closePhotoViewer = () => {
  photoViewer.classList.add("hidden");
  document.body.classList.remove("photo-viewer-open");
  viewerImage.removeAttribute("src");
  viewerPosition = 0;
  viewerCount = 0;
  if (photoReturnFocus?.isConnected) photoReturnFocus.focus();
  photoReturnFocus = null;
};

const showViewedPhoto = () => {
  const url = photoUrlsByPosition.get(viewerPosition);
  viewerCountLabel.textContent = `${viewerPosition} / ${viewerCount}`;
  viewerImage.classList.toggle("hidden", !url);
  if (url) viewerImage.src = url;
  else viewerImage.removeAttribute("src");
  viewerImage.alt = `Фото ${viewerPosition} из ${viewerCount} к заявке`;
  viewerMessage.textContent = url ? "" : failedPhotoPositions.has(viewerPosition)
    ? "Не удалось загрузить фотографию" : "Загружаем фотографию…";
  const onlyOne = viewerCount < 2;
  document.querySelector("#photo-viewer-prev").disabled = onlyOne;
  document.querySelector("#photo-viewer-next").disabled = onlyOne;
};

const showRelativePhoto = (step) => {
  if (viewerCount < 2) return;
  viewerPosition = ((viewerPosition - 1 + step + viewerCount) % viewerCount) + 1;
  showViewedPhoto();
};

const openPhotoViewer = (position, count, trigger) => {
  photoReturnFocus = trigger;
  viewerPosition = position;
  viewerCount = count;
  photoViewer.classList.remove("hidden");
  document.body.classList.add("photo-viewer-open");
  showViewedPhoto();
  document.querySelector("#photo-viewer-close").focus();
};

document.querySelector("#photo-viewer-close").addEventListener("click", closePhotoViewer);
document.querySelector("#photo-viewer-prev").addEventListener("click", () => showRelativePhoto(-1));
document.querySelector("#photo-viewer-next").addEventListener("click", () => showRelativePhoto(1));
photoViewer.addEventListener("click", (event) => {
  if (event.target === photoViewer) closePhotoViewer();
});
document.addEventListener("keydown", (event) => {
  if (photoViewer.classList.contains("hidden")) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closePhotoViewer();
  }
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    showRelativePhoto(-1);
  }
  if (event.key === "ArrowRight") {
    event.preventDefault();
    showRelativePhoto(1);
  }
});
let swipeStartX = null;
photoViewer.addEventListener("touchstart", (event) => {
  if (event.touches.length === 1) swipeStartX = event.touches[0].clientX;
}, { passive: true });
photoViewer.addEventListener("touchend", (event) => {
  if (swipeStartX === null || !event.changedTouches.length) return;
  const distance = event.changedTouches[0].clientX - swipeStartX;
  swipeStartX = null;
  if (Math.abs(distance) > 50) showRelativePhoto(distance < 0 ? 1 : -1);
}, { passive: true });

const releaseRequestPhotos = () => {
  closePhotoViewer();
  for (const url of photoUrls) URL.revokeObjectURL(url);
  photoUrls = [];
  photoUrlsByPosition = new Map();
  failedPhotoPositions = new Set();
};

const closeRequestDetail = () => {
  activeRequestId = null;
  releaseRequestPhotos();
  document.querySelector("#request-detail-card").classList.add("hidden");
  document.querySelector("#request-list-card").classList.remove("hidden");
  document.querySelector("#request-detail-message").textContent = "";
  document.querySelector("#page-title").textContent = "Заявки";
};

document.querySelector("#request-back").addEventListener("click", closeRequestDetail);

const renderRequestPage = () => {
  const container = document.querySelector("#requests");
  container.replaceChildren();
  if (!requests.length) {
    container.append(node("p", "muted", currentProfile.role === "owner"
      ? "Заявок пока нет. Создать заявку можно в чате с ботом."
      : "Заявок пока нет."));
    document.querySelector("#requests-pagination").classList.add("hidden");
    return;
  }
  requestPage = Math.min(requestPage, Math.max(0, Math.ceil(requests.length / pageSize) - 1));
  for (const request of requests.slice(requestPage * pageSize, (requestPage + 1) * pageSize)) {
    const row = node("button", "list-item request-row");
    row.type = "button";
    row.append(node("strong", "", request.title));
    row.append(node("span", "muted", `${request.hoa_name} · пом. ${request.unit} · ${displayDate(request.created_at)}`));
    row.append(node("span", "muted", `Исполнитель: ${executorLabel(request)}`));
    row.append(node("span", `badge status-${request.status}`, statusLabels[request.status] || request.status));
    if (request.priority) {
      row.append(node("span", `priority priority-${request.priority}`,
        priorityLabels[request.priority] || request.priority));
    } else if (currentProfile.role === "specialist" || currentProfile.role === "chairman") {
      row.append(node("span", "priority priority-unset", "Приоритет не назначен"));
    }
    row.addEventListener("click", () => openRequestDetail(request.request_id));
    container.append(row);
  }
  pagination("#requests-pagination", requestPage, requests.length, (page) => {
    requestPage = page;
    renderRequestPage();
    document.querySelector("#requests-heading").scrollIntoView({ block: "start" });
  });
};

const requestFact = (container, label, value) => {
  const line = node("div");
  line.append(node("dt", "", label), node("dd", "", value || "—"));
  container.append(line);
};

const updateRequest = async (request, change) => {
  const feedback = document.querySelector("#request-detail-message");
  feedback.textContent = "Сохраняем…";
  try {
    const result = await api(`/api/requests/${request.request_id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(change),
    });
    Object.assign(request, change);
    if (result.request) {
      request.status = result.request.status;
      request.specialist_id = result.request.specialist_id;
      request.specialist_name = result.request.specialist_name;
      request.target = result.request.specialist_id ? "specialist" : "chairman";
      if (result.request.specialist_id) {
        const specialist = requestSpecialists.find((item) => item.specialist_id === result.request.specialist_id);
        if (specialist) request.specialty = specialist.specialty;
      }
    }
    request.updated_at = new Date().toISOString();
    renderRequestPage();
    renderRequestDetail(request);
    feedback.textContent = result.warnings?.length
      ? `Изменения сохранены. ${result.warnings.join(" ")}` : "Изменения сохранены.";
    return true;
  } catch (problem) {
    feedback.textContent = problem.message;
    return false;
  }
};

const appendRequestControls = (content, request) => {
  if (currentProfile.role !== "specialist" && currentProfile.role !== "chairman") return;
  const form = node("form", "request-edit-form");
  form.append(node("h4", "", "Управление заявкой"));

  const statusLabel = node("label", "", "Статус");
  statusLabel.htmlFor = "request-status";
  const statusSelect = node("select");
  statusSelect.id = "request-status";
  let availableStatuses = currentProfile.role === "chairman"
    ? Object.keys(statusLabels)
    : request.status === "review" ? ["review", "in_progress", "rejected"]
      : request.status === "in_progress" ? ["in_progress", "done"] : [request.status];
  if (request.priority === "emergency") {
    availableStatuses = availableStatuses.filter((value) => value !== "rejected");
  }
  for (const value of availableStatuses) {
    const option = node("option", "", statusLabels[value] || value);
    option.value = value;
    statusSelect.append(option);
  }
  statusSelect.value = request.status;
  statusSelect.disabled = availableStatuses.length === 1;
  form.append(statusLabel, statusSelect);

  const priorityLabel = node("label", "", "Приоритет");
  priorityLabel.htmlFor = "request-priority";
  const prioritySelect = node("select");
  prioritySelect.id = "request-priority";
  const placeholder = node("option", "", "Не назначен");
  placeholder.value = "";
  placeholder.disabled = Boolean(request.priority);
  prioritySelect.append(placeholder);
  for (const [value, label] of Object.entries(priorityLabels)) {
    const option = node("option", "", label);
    option.value = value;
    prioritySelect.append(option);
  }
  prioritySelect.value = request.priority || "";
  form.append(priorityLabel, prioritySelect);
  if (request.priority === "emergency" && request.status !== "done") {
    form.append(node("p", "muted", "Приоритет аварии сохраняется до исполнения заявки. До этого свободные слоты исполнителя закрыты."));
  }

  let specialistSelect;
  if (currentProfile.role === "chairman") {
    const specialistLabel = node("label", "", "Исполнитель");
    specialistLabel.htmlFor = "request-specialist";
    specialistSelect = node("select");
    specialistSelect.id = "request-specialist";
    const chairman = node("option", "", `${shortName(currentProfile.full_name)} — Председатель`);
    chairman.value = "";
    specialistSelect.append(chairman);
    for (const specialist of requestSpecialists.filter((item) => item.linked)) {
      const option = node("option", "",
        `${shortName(specialist.full_name)} — ${specialtyLabels[specialist.specialty] || specialist.specialty}`);
      option.value = specialist.specialist_id;
      specialistSelect.append(option);
    }
    if (request.specialist_id && !requestSpecialists.some((item) =>
      item.specialist_id === request.specialist_id && item.linked)) {
      const current = node("option", "", "Текущий исполнитель не авторизован");
      current.value = request.specialist_id;
      current.hidden = true;
      specialistSelect.append(current);
    }
    specialistSelect.value = request.specialist_id || "";
    form.append(specialistLabel, specialistSelect);
  }

  const save = node("button", "", "Сохранить изменения");
  save.type = "submit";
  save.disabled = true;
  form.append(save);
  const changes = () => {
    const result = {};
    if (statusSelect.value !== request.status) result.status = statusSelect.value;
    if (prioritySelect.value !== (request.priority || "")) result.priority = prioritySelect.value;
    if (specialistSelect && specialistSelect.value !== (request.specialist_id || "")) {
      result.specialist_id = specialistSelect.value;
    }
    return result;
  };
  const refreshSave = () => {
    save.disabled = Object.keys(changes()).length === 0;
    document.querySelector("#request-detail-message").textContent = "";
  };
  statusSelect.addEventListener("change", refreshSave);
  prioritySelect.addEventListener("change", refreshSave);
  specialistSelect?.addEventListener("change", refreshSave);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const pending = changes();
    if (!Object.keys(pending).length) return;
    save.disabled = true;
    if (!await updateRequest(request, pending)) save.disabled = false;
  });
  content.append(form);
};

const appendVisitControls = (content, request) => {
  const panel = node("section", "visit-panel");
  panel.append(node("h4", "", "Доступ в квартиру"));
  const labels = {
    awaiting_owner: "Собственник выбирает время визита в чате MAX",
    pending: "Ожидается ответ собственника по ранее предложенному времени",
    accepted: "Собственник подтвердил визит",
    declined: "Собственник отказался от визита",
    cancelled: "Запрос доступа отменён",
  };
  if (request.visit_date) {
    panel.append(node("p", "muted", `${labels[request.visit_status] || "Предложение"}: ${visitLabel(request)}`));
  } else {
    panel.append(node("p", "muted", labels[request.visit_status] || "Запрос доступа ещё не отправлен."));
  }
  if (currentProfile.role === "specialist" && request.emergency_busy) {
    panel.append(node("p", "feedback", "Пока вы устраняете аварию, новые часы для визитов недоступны. Они откроются после исполнения аварийной заявки."));
    content.append(panel);
    return;
  }
  if (currentProfile.role !== "specialist" || !["review", "in_progress"].includes(request.status)
      || request.visit_status === "accepted") {
    content.append(panel);
    return;
  }
  const form = node("form", "visit-form");
  const feedback = node("p", "feedback");
  const submit = node("button", "", request.visit_status === "awaiting_owner"
    ? "Отправить запрос повторно" : "Запросить доступ в квартиру");
  submit.type = "submit";
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    feedback.textContent = "Отправляем запрос собственнику…";
    try {
      await api(`/api/requests/${request.request_id}/visit`, {
        method: "POST",
      });
      requests = (await api("/api/requests")).requests;
      renderRequestDetail(requests.find((item) => item.request_id === request.request_id));
      document.querySelector("#request-detail-message").textContent = "Запрос отправлен собственнику. Он выберет свободный час в чате MAX.";
    } catch (problem) {
      feedback.textContent = problem.message;
      submit.disabled = false;
    }
  });
  form.append(submit, feedback);
  panel.append(form);
  content.append(panel);
};

const renderRequestDetail = (request) => {
  releaseRequestPhotos();
  const content = document.querySelector("#request-detail");
  content.replaceChildren();
  content.append(node("h3", "", request.title));
  const facts = node("dl", "request-facts");
  requestFact(facts, "Статус", statusLabels[request.status] || request.status);
  requestFact(facts, "Приоритет", priorityLabels[request.priority] || "Не назначен");
  requestFact(facts, "ТСЖ", request.hoa_name);
  requestFact(facts, "Адрес", request.address);
  requestFact(facts, "Помещение", request.unit);
  requestFact(facts, "Собственник", request.full_name);
  requestFact(facts, "Исполнитель", executorLabel(request));
  requestFact(facts, "Специализация", specialtyLabels[request.specialty] || "Другое");
  requestFact(facts, "Создана", displayDate(request.created_at));
  requestFact(facts, "Обновлена", displayDate(request.updated_at));
  content.append(facts);
  content.append(node("h4", "", "Текст собственника"));
  content.append(node("p", "request-description", request.description));

  if (request.photo_count > 0) {
    content.append(node("h4", "", `Фотографии (${request.photo_count})`));
    const photos = node("div", "request-photos");
    content.append(photos);
    for (let position = 1; position <= request.photo_count; position++) {
      const openButton = node("button", "request-photo-button");
      openButton.type = "button";
      openButton.disabled = true;
      openButton.setAttribute("aria-label", `Открыть фото ${position} из ${request.photo_count}`);
      const picture = node("img");
      picture.alt = `Фото ${position} к заявке`;
      openButton.append(picture);
      openButton.addEventListener("click", () => openPhotoViewer(position, request.photo_count, openButton));
      photos.append(openButton);
      fetch(`/api/requests/${request.request_id}/photos/${position}`, {
        headers: { Authorization: `tma ${window.WebApp.initData}` },
      }).then(async (response) => {
        if (!response.ok) throw new Error("Не удалось загрузить фотографии");
        const blob = await response.blob();
        if (activeRequestId !== request.request_id || !photos.isConnected) return;
        const url = URL.createObjectURL(blob);
        photoUrls.push(url);
        photoUrlsByPosition.set(position, url);
        picture.src = url;
        openButton.disabled = false;
        if (!photoViewer.classList.contains("hidden") && viewerPosition === position) showViewedPhoto();
      }).catch((problem) => {
        if (activeRequestId === request.request_id && photos.isConnected) {
          failedPhotoPositions.add(position);
          openButton.remove();
          if (!photoViewer.classList.contains("hidden") && viewerPosition === position) showViewedPhoto();
          document.querySelector("#request-detail-message").textContent = problem.message;
        }
      });
    }
  }
  appendRequestControls(content, request);
  appendVisitControls(content, request);
};

const openRequestDetail = (requestId) => {
  const request = requests.find((item) => item.request_id === requestId);
  if (!request) return;
  activeRequestId = requestId;
  document.querySelector("#request-list-card").classList.add("hidden");
  document.querySelector("#request-detail-card").classList.remove("hidden");
  document.querySelector("#request-detail-message").textContent = "";
  document.querySelector("#page-title").textContent = "Заявка";
  renderRequestDetail(request);
  window.scrollTo({ top: 0, behavior: "auto" });
};

const renderRequests = async () => {
  if (currentProfile.role === "chairman") {
    const [requestData, specialistData] = await Promise.all([
      api("/api/requests"), api("/api/specialists"),
    ]);
    requests = requestData.requests;
    requestSpecialists = specialistData.specialists;
  } else {
    requests = (await api("/api/requests")).requests;
    requestSpecialists = [];
  }
  renderRequestPage();
};

const renderSpecialists = async () => {
  const specialists = (await api("/api/specialists")).specialists;
  document.querySelector("#hoa-timezone").value = currentProfile.timezone || "Europe/Moscow";
  const container = document.querySelector("#specialists");
  container.replaceChildren();
  const chairman = node("div", "list-item");
  const chairmanHeading = node("div", "owner-heading");
  chairmanHeading.append(node("strong", "", currentProfile.full_name));
  chairmanHeading.append(node("span", "login-state login-state-linked", "Авторизован"));
  chairman.append(chairmanHeading, node("p", "muted", "Председатель ТСЖ · может исполнять заявки"));
  container.append(chairman);
  if (!specialists.length) {
    container.append(node("p", "muted", "Других специалистов пока нет."));
    return;
  }
  for (const specialist of specialists) {
    const card = node("div", "list-item");
    const heading = node("div", "owner-heading");
    heading.append(node("strong", "", specialist.full_name));
    heading.append(node("span", specialist.linked ? "login-state login-state-linked" : "login-state login-state-pending",
      specialist.linked ? "Авторизован" : "Ожидает входа"));
    card.append(heading);
    card.append(node("p", "muted", `${specialtyLabels[specialist.specialty] || specialist.specialty} · ${specialist.phone}`));
    const edit = node("details", "specialist-edit");
    edit.append(node("summary", "", "Редактировать специалиста и часы работы"));
    const form = node("form");
    const typeLabel = node("label", "", "Специальность");
    const type = node("select");
    for (const [value, title] of Object.entries(specialtyLabels)) {
      const option = node("option", "", title);
      option.value = value;
      type.append(option);
    }
    type.value = specialist.specialty;
    typeLabel.append(type);
    const nameLabel = node("label", "", "ФИО");
    const name = node("input");
    name.type = "text";
    name.maxLength = 180;
    name.required = true;
    name.value = specialist.full_name;
    nameLabel.append(name);
    const phoneLabel = node("label", "", "Номер телефона");
    const phone = node("input");
    phone.type = "tel";
    phone.required = true;
    phone.value = specialist.phone;
    phoneLabel.append(phone);
    const hours = hoursEditor(specialist.work_hours || []);
    const save = node("button", "", "Сохранить специалиста");
    save.type = "submit";
    const feedback = node("p", "feedback");
    form.append(typeLabel, nameLabel, phoneLabel, node("h4", "", "Рабочие часы"), hours.element, save, feedback);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      feedback.textContent = "Сохраняем…";
      try {
        const result = await api(`/api/specialists/${specialist.specialist_id}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ specialty: type.value, full_name: name.value,
            phone: phone.value, work_hours: hours.value() }),
        });
        await renderSpecialists();
        specialistMessage.textContent = result.specialist.linked
          ? "Данные и расписание сохранены."
          : "Данные сохранены. Для нового номера специалисту нужно повторно подтвердить вход в MAX.";
      } catch (problem) {
        feedback.textContent = problem.message;
        save.disabled = false;
      }
    });
    edit.append(form);
    card.append(edit);
    container.append(card);
  }
};

const activateScreen = async (name) => {
  if (!currentProfile) return;
  if (name === "residents" && currentProfile.role !== "chairman") return;
  if (name === "specialists" && currentProfile.role !== "chairman") return;
  closeRequestDetail();
  for (const screen of document.querySelectorAll(".app-screen")) {
    screen.classList.toggle("hidden", screen.id !== `screen-${name}`);
  }
  for (const button of document.querySelectorAll(".nav-button")) {
    const active = button.dataset.screen === name;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  const titles = { profile: "Профиль", residents: "Жители", specialists: "Специалисты", requests: "Заявки" };
  document.querySelector("#page-title").textContent = titles[name];
  window.scrollTo({ top: 0, behavior: "auto" });
  try {
    if (name === "residents") await renderResidents();
    if (name === "specialists") await renderSpecialists();
    if (name === "requests") await renderRequests();
  } catch (problem) {
    const target = name === "residents" ? registryMessage
      : name === "specialists" ? specialistMessage : requestsMessage;
    target.textContent = problem.message || "Не удалось загрузить данные";
  }
};

document.querySelector("#app-nav").addEventListener("click", (event) => {
  const button = event.target.closest(".nav-button");
  if (button) activateScreen(button.dataset.screen);
});

const renderProfile = (data) => {
  currentProfile = data;
  document.querySelector("#role").textContent = data.role === "chairman" ? "Председатель ТСЖ"
    : data.role === "specialist" ? specialtyLabels[data.specialty] || "Специалист" : "Собственник";
  document.querySelector("#full-name").textContent = data.full_name;
  document.querySelector("#hoa-name").textContent = data.hoa_name;
  if (data.role === "chairman") {
    document.querySelector("#app-nav").classList.add("has-specialists");
    document.querySelector("#nav-specialists").classList.remove("hidden");
    document.querySelector("#chairman-details").classList.remove("hidden");
    document.querySelector("#chairman-chat").classList.remove("hidden");
    document.querySelector("#group-chat-link").value = data.group_chat?.link || "";
    renderGroupChatState(data.group_chat);
    document.querySelector("#address").textContent = data.address;
    document.querySelector("#phone").textContent = data.phone;
    document.querySelector("#protocol").textContent = data.protocol_filename;
    document.querySelector("#verified-at").textContent = displayDate(data.verified_at);
    document.querySelector("#requests-heading").textContent = "Заявки жителей";
  } else if (data.role === "specialist") {
    document.querySelector("#app-nav").classList.add("two-column");
    document.querySelector("#nav-middle").classList.add("hidden");
    document.querySelector("#specialist-details").classList.remove("hidden");
    document.querySelector("#requests-heading").textContent = "Назначенные заявки";
    const container = document.querySelector("#specialist-memberships");
    container.replaceChildren();
    for (const item of data.memberships) {
      const card = node("div", "list-item");
      card.append(node("strong", "", item.hoa_name));
      card.append(node("p", "muted", `${specialtyLabels[item.specialty] || item.specialty} · ${item.address}`));
      const hours = item.work_hours || [];
      card.append(node("h4", "", "Рабочие часы"));
      card.append(node("p", "muted", hours.length
        ? hours.map((range) => `${weekdayLabels[range.weekday]} ${timeLabel(range.start_hour)}–${timeLabel(range.end_hour)}`).join(" · ")
        : "Председатель ещё не назначил рабочие часы."));
      container.append(card);
    }
  } else {
    document.querySelector("#app-nav").classList.add("two-column");
    document.querySelector("#nav-middle").classList.add("hidden");
    document.querySelector("#owner-details").classList.remove("hidden");
    document.querySelector("#requests-heading").textContent = "Мои заявки";
    const memberships = document.querySelector("#memberships");
    for (const item of data.memberships) {
      const row = node("div", "list-item");
      row.append(node("strong", "", `${item.hoa_name} · пом. ${item.unit}`));
      row.append(node("p", "muted", `${item.area} м² · доля ${item.share} · ${item.ownership}`));
      memberships.append(row);
    }
  }
  loading.classList.add("hidden");
  error.classList.add("hidden");
  profile.classList.remove("hidden");
};

const renderGroupChatState = (chat) => {
  document.querySelector("#group-chat-state").textContent = !chat
    ? "Чат не указан. Бот будет выходить из групповых чатов."
    : chat.chat_id
      ? `Подключён чат: ${chat.title || chat.link}`
      : "Ссылка сохранена. Добавьте бота в этот чат для проверки.";
};

document.querySelector("#group-chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const link = document.querySelector("#group-chat-link").value.trim();
  const feedback = document.querySelector("#group-chat-message");
  const save = event.currentTarget.querySelector("button");
  save.disabled = true;
  feedback.textContent = "Сохраняем ссылку…";
  try {
    const result = await api("/api/group-chat", {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ link }),
    });
    currentProfile.group_chat = result.group_chat;
    renderGroupChatState(result.group_chat);
    feedback.textContent = link ? "Ссылка сохранена." : "Ссылка удалена. Бот покинет прежний чат.";
  } catch (problem) {
    feedback.textContent = problem.message;
  } finally {
    save.disabled = false;
  }
});

document.querySelector("#timezone-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const feedback = document.querySelector("#timezone-message");
  try {
    const result = await api("/api/hoa-timezone", {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ timezone: document.querySelector("#hoa-timezone").value }),
    });
    currentProfile.timezone = result.timezone;
    feedback.textContent = "Часовой пояс ТСЖ сохранён.";
  } catch (problem) {
    feedback.textContent = problem.message;
  }
});

document.querySelector("#specialist-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  specialistMessage.textContent = "Сохраняем специалиста…";
  try {
    await api("/api/specialists", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        specialty: document.querySelector("#specialist-type").value,
        full_name: document.querySelector("#specialist-name").value,
        phone: document.querySelector("#specialist-phone").value,
      }),
    });
    document.querySelector("#specialist-form").reset();
    specialistMessage.textContent = "Специалист добавлен. Попросите его выбрать роль в чате и подтвердить номер.";
    await renderSpecialists();
  } catch (problem) {
    specialistMessage.textContent = problem.message;
  }
});

document.querySelector("#registry-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = document.querySelector("#registry-file").files[0];
  if (!file) return;
  registryMessage.textContent = "GigaChat разбирает реестр. Это может занять несколько минут…";
  pendingDigest = undefined;
  document.querySelector("#registry-preview").classList.add("hidden");
  try {
    const result = await api("/api/registry", {
      method: "POST",
      headers: { "Content-Type": "application/pdf", "X-File-Name": encodeURIComponent(file.name) },
      body: file,
    });
    pendingDigest = result.digest;
    document.querySelector("#preview-text").textContent =
      `Адрес: ${result.address}. Найдено собственников: ${result.resident_count}. ` +
      `Пропущенные помещения: ${result.skipped_units.join(", ") || "нет"}. ` +
      "Проверьте каждую запись GigaChat. Предыдущий список будет обновлён.";
    const previewResidents = document.querySelector("#preview-residents");
    previewResidents.replaceChildren();
    for (const group of groupByUnit(result.residents)) {
      previewResidents.append(unitCard(group, true));
    }
    document.querySelector("#registry-preview").classList.remove("hidden");
    registryMessage.textContent = "Проверьте данные перед импортом.";
  } catch (problem) {
    registryMessage.textContent = problem.message;
  }
});

document.querySelector("#confirm-registry").addEventListener("click", async () => {
  const file = document.querySelector("#registry-file").files[0];
  if (!file || !pendingDigest) return;
  registryMessage.textContent = "Сохраняем реестр…";
  try {
    const result = await api("/api/registry", {
      method: "POST",
      headers: {
        "Content-Type": "application/pdf", "X-File-Name": encodeURIComponent(file.name),
        "X-Confirm-Registry": pendingDigest,
      },
      body: file,
    });
    registryMessage.textContent = `Сохранено собственников: ${result.imported}.`;
    document.querySelector("#registry-preview").classList.add("hidden");
    document.querySelector("#registry-upload").open = false;
    pendingDigest = undefined;
    residentPage = 0;
    await renderResidents();
  } catch (problem) {
    registryMessage.textContent = problem.message;
  }
});

const start = async () => {
  const webApp = window.WebApp;
  if (!webApp || !webApp.initData) {
    showError("Откройте мини-приложение из чата с ботом в MAX.");
    return;
  }
  webApp.ready?.();
  webApp.expand?.();
  try {
    renderProfile(await api("/api/profile"));
  } catch (problem) {
    showError(problem.message || "Не удалось загрузить профиль");
  }
};

start();
