const appsContainer = document.querySelector("#public-apps");
const emptyState = document.querySelector("#public-empty-state");
const storePicker = document.querySelector(".store-picker");
const storeMenu = document.querySelector("#store-menu");

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text ?? "");
  return element;
}

function appCard(app) {
  const card = node("article", "app-card public-app-card");
  const titleRow = node("div", "app-title-row");
  const icon = node("img", "app-icon");
  icon.src = app.icon_url;
  icon.alt = "";
  const title = node("div");
  title.append(node("h3", "", app.name));
  title.append(
    node(
      "p",
      "app-meta",
      `${app.bundle_identifier} · version ${app.version}${app.developer_name ? ` · ${app.developer_name}` : ""}`,
    ),
  );
  titleRow.append(icon, title);
  card.append(titleRow);
  if (app.subtitle) card.append(node("p", "app-subtitle", app.subtitle));
  if (app.description) card.append(node("p", "app-description", app.description));
  return card;
}

async function loadApps() {
  try {
    const response = await fetch("/api/apps");
    if (!response.ok) throw new Error(`Could not load apps (${response.status}).`);
    const apps = await response.json();
    appsContainer.replaceChildren(...apps.map(appCard));
    emptyState.textContent = apps.length ? "" : "No apps have been added to this source yet.";
    emptyState.hidden = apps.length > 0;
  } catch (error) {
    appsContainer.replaceChildren();
    emptyState.textContent = error.message;
    emptyState.hidden = false;
  }
}

async function loadSourceName() {
  try {
    const response = await fetch("/api/public-config");
    if (!response.ok) return;
    const settings = await response.json();
    document.querySelector("#source-name").textContent = settings.name;
    document.querySelector("#source-description").textContent =
      settings.description || "Apps in this source, ready to install.";
    document.title = `${settings.name} · IPA Catalog`;
    const encodedSource = encodeURIComponent(new URL("/source.json", window.location.href).href);
    document.querySelector("#add-to-label").textContent = settings.add_button_label;
    storePicker.hidden = settings.store_options.length === 0;
    storeMenu.replaceChildren(
      ...settings.store_options.map((option) => {
        const link = node("a", "store-option", option.name);
        link.href = `${option.scheme}://source?url=${encodedSource}`;
        return link;
      }),
    );
  } catch {
    storeMenu.replaceChildren();
  }
}

document.addEventListener("click", (event) => {
  if (!storePicker.contains(event.target)) storePicker.open = false;
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") storePicker.open = false;
});

loadApps();
loadSourceName();
