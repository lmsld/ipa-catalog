const loginForm = document.querySelector("#login-form");
const tokenField = document.querySelector("#admin-token");
const loginPanel = document.querySelector("#login-panel");
const adminContent = document.querySelector("#admin-content");
const loginMessage = document.querySelector("#login-message");
const addForm = document.querySelector("#add-form");
const formMessage = document.querySelector("#form-message");
const sourceSettingsForm = document.querySelector("#source-settings-form");
const sourceSettingsMessage = document.querySelector("#source-settings-message");
const storeOptionsContainer = document.querySelector("#store-options");
const appsContainer = document.querySelector("#managed-apps");
const emptyState = document.querySelector("#manage-empty-state");

tokenField.value = sessionStorage.getItem("ipa-catalog-admin-token") || "";

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text ?? "");
  return element;
}

function setStatus(element, message, failed = false) {
  element.textContent = message;
  element.classList.toggle("error", failed);
  element.classList.toggle("failed", failed);
}

function addStoreOption(option = { name: "", scheme: "" }) {
  const row = node("div", "store-option-row");
  for (const [field, labelText, placeholder] of [
    ["name", "Option name", "SideStore"],
    ["scheme", "URL scheme", "sidestore"],
  ]) {
    const label = node("label", "", labelText);
    const input = node("input");
    input.type = "text";
    input.dataset.field = field;
    input.value = option[field] || "";
    input.placeholder = placeholder;
    input.maxLength = field === "name" ? 60 : 32;
    if (field === "scheme") {
      input.required = true;
      input.pattern = "[A-Za-z][A-Za-z0-9+.\\-]*";
      input.title = "Use an app URL scheme, such as sidestore.";
      input.setAttribute("aria-label", `${labelText} for ${option.name || "new option"}`);
    } else {
      input.required = true;
      input.setAttribute("aria-label", labelText);
    }
    label.append(input);
    row.append(label);
  }
  const remove = node("button", "button secondary remove-option", "Remove");
  remove.type = "button";
  remove.addEventListener("click", () => {
    row.remove();
  });
  row.append(remove);
  storeOptionsContainer.append(row);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${tokenField.value.trim()}`);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch {
      // An upstream proxy may return an HTML login page instead of JSON.
    }
    if (response.status === 401) lockAdmin();
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function lockAdmin() {
  sessionStorage.removeItem("ipa-catalog-admin-token");
  adminContent.hidden = true;
  loginPanel.hidden = false;
}

function appCard(app) {
  const card = node("details", "app-card admin-app-card");
  const summary = node("summary", "admin-app-summary");
  const titleRow = node("div", "app-title-row");
  const icon = node("img", "app-icon");
  icon.src = app.icon_url_override || `/icons/${encodeURIComponent(app.id)}`;
  icon.alt = "";
  const title = node("div");
  title.append(node("h3", "", app.name));
  const version = app.release_tag
    ? `${app.release_tag} · ${app.version} · ${app.release_asset_name}`
    : app.version;
  title.append(node("p", "app-meta", `${app.bundle_identifier} · ${version}`));
  titleRow.append(icon, title);
  summary.append(titleRow);
  const summaryStatus = node("span", `status${app.last_error ? " failed" : ""}`);
  setStatus(
    summaryStatus,
    app.last_error || (app.last_checked ? `Last checked ${new Date(app.last_checked).toLocaleString()}` : "Not checked yet"),
    Boolean(app.last_error),
  );
  summary.append(summaryStatus);
  card.append(summary);

  const fields = node("div", "app-fields");
  const definitions = [
    ["name", "App name", "text"],
    ["source_url", app.source_type === "github" ? "GitHub repository" : "Direct IPA URL", "url"],
    ["developer_name", "Developer name", "text"],
    ["poll_interval_hours", "Check frequency (hours)", "number"],
    ["description", "Description", "textarea"],
    ["subtitle", "Subtitle / variant", "text"],
    ["version_description", "Latest version notes", "textarea"],
    ["icon_url", "Custom icon URL (optional)", "url"],
  ];
  for (const [key, labelText, type] of definitions) {
    const wide = ["description", "version_description", "icon_url_override"].includes(key);
    const label = node("label", wide ? "wide" : "");
    label.append(document.createTextNode(labelText));
    const input = type === "textarea" ? node("textarea") : node("input");
    if (type !== "textarea") input.type = type;
    input.name = key;
    input.value =
      key === "source_url" && app.source_type === "github"
        ? app.source_reference
        : key === "icon_url"
          ? app.icon_url_override ?? ""
          : app[key] ?? "";
    if (key === "poll_interval_hours") {
      input.min = "1";
      input.max = "720";
      input.step = "1";
    }
    label.append(input);
    fields.append(label);
  }
  card.append(fields);

  const content = node("div", "admin-app-content");
  content.append(fields);
  const actions = node("div", "card-actions");
  const status = node("span", `status${app.last_error ? " failed" : ""}`);
  setStatus(
    status,
    app.last_error || (app.last_checked ? `Last checked ${new Date(app.last_checked).toLocaleString()}` : "Not checked yet"),
    Boolean(app.last_error),
  );
  const save = node("button", "button primary", "Save settings");
  save.type = "button";
  save.addEventListener("click", async () => {
    const values = {};
    for (const input of fields.querySelectorAll("[name]")) values[input.name] = input.value;
    values.poll_interval_hours = Number(values.poll_interval_hours);
    save.disabled = true;
    try {
      await api(`/api/admin/apps/${encodeURIComponent(app.id)}`, {
        method: "PUT",
        body: JSON.stringify(values),
      });
      await loadApps();
    } catch (error) {
      setStatus(status, error.message, true);
    } finally {
      save.disabled = false;
    }
  });
  const check = node("button", "button secondary", "Check now");
  check.type = "button";
  check.addEventListener("click", async () => {
    check.disabled = true;
    setStatus(status, "Checking source and IPA…");
    try {
      const result = await api(`/api/admin/apps/${encodeURIComponent(app.id)}/check`, {
        method: "POST",
      });
      setStatus(status, `Checked · version ${result.version}`);
      await loadApps();
    } catch (error) {
      setStatus(status, error.message, true);
    } finally {
      check.disabled = false;
    }
  });
  const remove = node("button", "button secondary", "Remove");
  remove.type = "button";
  remove.addEventListener("click", async () => {
    if (!window.confirm(`Remove ${app.name} from this source?`)) return;
    try {
      await api(`/api/admin/apps/${encodeURIComponent(app.id)}`, { method: "DELETE" });
      await loadApps();
    } catch (error) {
      setStatus(status, error.message, true);
    }
  });
  actions.append(save, check, remove, status);
  content.append(actions);
  card.append(content);
  return card;
}

async function loadApps() {
  try {
    const apps = await api("/api/admin/apps");
    appsContainer.replaceChildren(...apps.map(appCard));
    document.querySelector("#apps-heading").textContent =
      `Apps and update checks (${apps.length})`;
    emptyState.textContent = apps.length ? "" : "No apps yet. Add an IPA or GitHub URL above.";
    emptyState.hidden = apps.length > 0;
  } catch (error) {
    if (!tokenField.value.trim()) return;
    setStatus(loginMessage, error.message, true);
  }
}

async function loadSourceSettings() {
  const response = await fetch("/api/public-config");
  if (!response.ok) throw new Error(`Could not load repository details (${response.status}).`);
  const settings = await response.json();
  for (const key of ["name", "identifier", "description", "add_button_label"]) {
    sourceSettingsForm.elements[key].value = settings[key] || "";
  }
  storeOptionsContainer.replaceChildren();
  for (const option of settings.store_options) addStoreOption(option);
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  sessionStorage.setItem("ipa-catalog-admin-token", tokenField.value.trim());
  setStatus(loginMessage, "Checking token…");
  try {
    await api("/api/admin/apps");
    loginPanel.hidden = true;
    adminContent.hidden = false;
    setStatus(loginMessage, "");
    await loadSourceSettings();
    await loadApps();
  } catch (error) {
    setStatus(loginMessage, error.message, true);
  }
});

document.querySelector("#refresh").addEventListener("click", loadApps);
document.querySelector("#add-store-option").addEventListener("click", () => {
  if (storeOptionsContainer.children.length >= 10) return;
  addStoreOption();
});

sourceSettingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = sourceSettingsForm.querySelector("button[type=submit]");
  submit.disabled = true;
  setStatus(sourceSettingsMessage, "Saving repository details…");
  const settings = Object.fromEntries(new FormData(sourceSettingsForm));
  settings.store_options = [...storeOptionsContainer.querySelectorAll(".store-option-row")].map(
    (row) => ({
      name: row.querySelector('[data-field="name"]').value.trim(),
      scheme: row.querySelector('[data-field="scheme"]').value.trim(),
    }),
  );
  try {
    const saved = await api("/api/admin/settings", {
      method: "PUT",
      body: JSON.stringify(settings),
    });
    for (const key of ["name", "identifier", "description"]) {
      sourceSettingsForm.elements[key].value = saved[key] || "";
    }
    setStatus(sourceSettingsMessage, "Repository details saved.");
  } catch (error) {
    setStatus(sourceSettingsMessage, error.message, true);
  } finally {
    submit.disabled = false;
  }
});

addForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setStatus(formMessage, "Resolving latest release and reading IPA metadata…");
  const submit = addForm.querySelector("button[type=submit]");
  submit.disabled = true;
  const values = Object.fromEntries(new FormData(addForm));
  values.poll_interval_hours = Number(values.poll_interval_hours);
  try {
    const result = await api("/api/admin/apps", {
      method: "POST",
      body: JSON.stringify(values),
    });
    addForm.reset();
    addForm.elements.poll_interval_hours.value = "24";
    setStatus(formMessage, `Added ${result.count} app${result.count === 1 ? "" : "s"}.`);
    await loadApps();
  } catch (error) {
    setStatus(formMessage, error.message, true);
  } finally {
    submit.disabled = false;
  }
});

if (tokenField.value.trim()) {
  loginForm.requestSubmit();
}
