const loading = document.querySelector("#loading");
const error = document.querySelector("#error");
const profile = document.querySelector("#profile");

const showError = (message) => {
  loading.classList.add("hidden");
  profile.classList.add("hidden");
  error.classList.remove("hidden");
  document.querySelector("#error-message").textContent = message;
};

const renderProfile = (data) => {
  document.querySelector("#role").textContent = data.role;
  document.querySelector("#full-name").textContent = data.full_name;
  document.querySelector("#hoa-name").textContent = data.hoa_name;
  document.querySelector("#address").textContent = data.address;
  document.querySelector("#phone").textContent = data.phone;
  document.querySelector("#protocol").textContent = data.protocol_filename;
  document.querySelector("#verified-at").textContent = new Date(data.verified_at).toLocaleString("ru-RU");
  loading.classList.add("hidden");
  error.classList.add("hidden");
  profile.classList.remove("hidden");
};

const start = async () => {
  const webApp = window.WebApp;
  if (!webApp || !webApp.initData) {
    showError("Откройте мини-приложение из чата с ботом в MAX.");
    return;
  }
  webApp.ready?.();
  webApp.expand?.();
  try {
    const response = await fetch("/api/profile", {
      headers: { Authorization: `tma ${webApp.initData}` },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Не удалось загрузить профиль");
    renderProfile(data);
  } catch (requestError) {
    showError(requestError.message || "Не удалось загрузить профиль");
  }
};

start();
