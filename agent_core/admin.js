/* Interfaz de administración hecha para Vortex AI Desktop. */
let activeChatId = null;
let chatCatalog = [];
let chatSending = false;
let projectCatalog = [];
let activeProject = "all";

async function adminRequest(url, method = "GET", data) {
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok)
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : "Revisa los campos ingresados.",
    );
  return result;
}

const pages = [
  [
    "overview",
    "Resumen",
    "Actividad y estado de tu asistente",
    ["workersPanelTitle", "timelinePanelTitle"],
  ],
  [
    "chats",
    "Chats",
    "Conversaciones independientes con historial y contexto guardados",
    ["chatPanelTitle"],
  ],
  [
    "models",
    "Modelos y consumo",
    "Selección de modelos y seguimiento local de tokens",
    ["routerPanelTitle"],
  ],
  [
    "whatsapp",
    "WhatsApp",
    "Vinculación, acceso y contactos",
    ["vipContactsPanelTitle"],
  ],
  [
    "integrations",
    "Integraciones",
    "Google Classroom, Drive, Calendar y DUTIC",
    [],
  ],
  [
    "files",
    "Archivos",
    "Documentos y artefactos creados por el agente",
    ["factoryPanelTitle"],
  ],
  ["memory", "Memoria", "Notas y conocimiento guardado", ["brainPanelTitle"]],
  [
    "automation",
    "Automatizaciones",
    "Recordatorios y vigilancia de tareas",
    ["alertsPanelTitle"],
  ],
  [
    "computer",
    "Computadora",
    "Monitores y vista de la estación de trabajo",
    ["monitorPanelTitle"],
  ],
  ["settings", "Configuración", "Claves de API y preferencias del agente", []],
];
const icons = [
  "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  "M4 4h16v12H8l-4 4z",
  "M12 3v4 M12 17v4 M3 12h4 M17 12h4 M8 8h8v8H8z",
  "M5 3h14v18H5z M9 17h6",
  "M9 3v6h6V3 M9 15v6 M15 15v6 M5 9h14v6H5z",
  "M3 5h7l2 3h9v12H3z",
  "M4 4h16v16H4z M8 8h8 M8 12h8 M8 16h5",
  "M13 2 4 14h7l-1 8 10-13h-7z",
  "M3 4h18v13H3z M8 21h8 M12 17v4",
  "M4 7h16 M4 17h16 M8 4v6 M16 14v6",
];
const main = document.querySelector(".main-grid");
const sidebar = document.createElement("aside");
sidebar.className = "admin-sidebar";
sidebar.id = "mainSidebar";
sidebar.innerHTML =
  '<div class="admin-logo"><img class="brand-logo" src="/assets/vortex-ai-logo.png" alt="Vortex AI"><div class="sidebar-brand-text">Vortex AI<small>Desktop</small></div></div><nav class="admin-nav" aria-label="Menú principal"></nav><div class="admin-footer">Espacio local · Windows<br>Tu asistente, en un solo lugar</div>';
document.body.prepend(sidebar);
pages.forEach(([id, title, description, sections], index) => {
  const view = document.createElement("div");
  view.className = "admin-view";
  view.dataset.page = id;
  view.hidden = true;
  view.innerHTML = `<div class="page-intro"><h2>${title}</h2><p>${description}</p></div>`;
  sections.forEach((section) =>
    view.appendChild(document.getElementById(section).closest("section")),
  );
  main.appendChild(view);
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.page = id;
  button.innerHTML = `<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${icons[index]}"/></svg><span class="nav-label">${title}</span>`;
  button.title = title;
  button.setAttribute("aria-label", title);
  button.onclick = () => {
    location.hash = id;
  };
  sidebar.querySelector("nav").appendChild(button);
});
document.querySelectorAll(".column-stack").forEach((el) => el.remove());
const page = (id) => document.querySelector(`.admin-view[data-page="${id}"]`);
page("overview").appendChild(document.querySelector(".control-bar"));
page("whatsapp").insertBefore(
  document.getElementById("qrBanner"),
  page("whatsapp").children[1],
);
function showPage() {
  const id = pages.some((item) => item[0] === location.hash.slice(1))
    ? location.hash.slice(1)
    : "overview";
  document
    .querySelectorAll(".admin-view")
    .forEach((el) => (el.hidden = el.dataset.page !== id));
  document.querySelectorAll(".admin-nav button").forEach((el) => {
    el.classList.toggle("active", el.dataset.page === id);
    el.setAttribute("aria-current", el.dataset.page === id ? "page" : "false");
  });
}
addEventListener("hashchange", showPage);
showPage();

function panel(view, html) {
  const el = document.createElement("section");
  el.className = "panel";
  el.innerHTML = html;
  page(view).appendChild(el);
  return el;
}
function field(label, id, type = "text", help = "") {
  return `<label class="field">${label}<input id="${id}" type="${type}" autocomplete="off"><small>${help}</small></label>`;
}
const uiDialog = document.createElement("dialog");
uiDialog.className = "key-dialog ui-dialog";
uiDialog.innerHTML = `<div class="dialog-heading"><div><h3 id="uiDialogTitle"></h3><p id="uiDialogMessage"></p></div><button class="dialog-close" id="uiDialogClose" type="button" aria-label="Cerrar">×</button></div><label class="field ui-dialog-input" id="uiDialogInputWrap">Nombre<input id="uiDialogInput" type="text" maxlength="100" autocomplete="off"></label><div class="form-actions"><button class="btn" id="uiDialogCancel" type="button">Cancelar</button><button class="btn btn-primary" id="uiDialogConfirm" type="button">Aceptar</button></div>`;
document.body.appendChild(uiDialog);

function showUiDialog({
  title,
  message = "",
  input = false,
  value = "",
  confirmLabel = "Aceptar",
  danger = false,
  cancel = true,
}) {
  return new Promise((resolve) => {
    const inputWrap = document.getElementById("uiDialogInputWrap");
    const inputControl = document.getElementById("uiDialogInput");
    const cancelButton = document.getElementById("uiDialogCancel");
    const confirmButton = document.getElementById("uiDialogConfirm");
    let settled = false;
    document.getElementById("uiDialogTitle").textContent = title;
    document.getElementById("uiDialogMessage").textContent = message;
    inputWrap.hidden = !input;
    inputControl.value = value;
    cancelButton.hidden = !cancel;
    confirmButton.textContent = confirmLabel;
    confirmButton.className = danger ? "btn btn-danger" : "btn btn-primary";
    const finish = (result) => {
      if (settled) return;
      settled = true;
      uiDialog.close();
      resolve(result);
    };
    confirmButton.onclick = () => finish(input ? inputControl.value.trim() : true);
    cancelButton.onclick = () => finish(null);
    document.getElementById("uiDialogClose").onclick = () => finish(null);
    uiDialog.oncancel = (event) => {
      event.preventDefault();
      finish(null);
    };
    uiDialog.onclick = (event) => {
      if (event.target === uiDialog) finish(null);
    };
    inputControl.onkeydown = (event) => {
      if (event.key === "Enter") finish(inputControl.value.trim());
    };
    uiDialog.showModal();
    if (input) requestAnimationFrame(() => inputControl.select());
  });
}

document.addEventListener("pointerdown", (event) => {
  document
    .querySelectorAll(".tree-create-menu[open], .tree-actions[open]")
    .forEach((menu) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
});
panel(
  "settings",
  '<div id="keysRoot"></div><span id="settingsFeedback" role="status" class="admin-feedback"></span>',
);
panel(
  "whatsapp",
  `<h3>Acceso a WhatsApp</h3><p class="usage-note">Comandos disponibles · /chats para listar · /chat 1 para elegir · /nuevo Nombre para crear · /salir para volver al chat general.</p><p class="section-description">Vincula el dispositivo con el QR y configura qué número puede dar instrucciones.</p><form id="whatsappForm"><div class="admin-form">${field("Número autorizado", "allowedPhone", "tel", "Código de país y número, sin + ni espacios.")}${field("Prefijo de activación", "botPrefix", "text", "Opcional. Vacío para escribir directamente.")}</div><div class="form-actions"><button class="btn btn-primary">Guardar y aplicar conexión</button><button type="button" class="btn" onclick="restartWhatsApp()">Reconectar WhatsApp</button><span class="admin-feedback" id="whatsappFeedback" role="status"></span></div></form>`,
);
panel(
  "integrations",
  `<div class="integration-heading"><h3>Cuenta de Google</h3><button class="integration-help" id="googleHelp" type="button" aria-label="Ayuda para conectar Google" title="Cómo conectar Google"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M9 18h6M10 22h4M8.5 14.5A7 7 0 1 1 15.5 14.5C14.4 15.3 14 16 14 18h-4c0-2-.4-2.7-1.5-3.5Z"/></svg></button></div><p class="section-description">Conecta Classroom, Drive y Calendar mediante OAuth. Necesitas un cliente OAuth de escritorio con estas APIs habilitadas en Google Cloud.</p><div class="google-connection-card"><div><span class="connection-state" id="googleState">Consultando conexión</span><strong id="googleAccount">Sin cuenta autorizada</strong><small id="googleCredentialFile">Ningún archivo OAuth cargado</small></div><div class="google-connection-actions"><button class="btn" type="button" id="disconnectGoogle" hidden>Desconectar</button><button class="btn btn-danger" type="button" id="removeGoogle" hidden>Eliminar conexión</button></div></div><div class="admin-form"><label class="field">Credenciales OAuth de escritorio<input id="googleFile" type="file" accept="application/json,.json"><small>Selecciona el archivo JSON descargado de Google Cloud.</small></label>${field("Correo institucional", "institutionalEmail", "email")}${field("Calendario", "calendarId", "text", "Usa primary para tu calendario principal.")}</div><div class="form-actions"><button class="btn btn-primary" type="button" id="connectGoogle">Autorizar cuenta Google</button><a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">Configurar OAuth en Google Cloud</a><span id="googleFeedback" class="admin-feedback" role="status"></span></div>`,
);
panel(
  "integrations",
  `<div class="integration-heading"><h3>Calendario DUTIC</h3><button class="integration-help" id="duticHelp" type="button" aria-label="Ayuda para conectar DUTIC" title="Cómo conectar DUTIC"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M9 18h6M10 22h4M8.5 14.5A7 7 0 1 1 15.5 14.5C14.4 15.3 14 16 14 18h-4c0-2-.4-2.7-1.5-3.5Z"/></svg></button></div><p class="section-description">En DUTIC abre Calendario → Exportar calendario y copia la URL de eventos.</p><div class="dutic-connection-row"><span class="connection-state" id="duticState">Consultando conexión</span><button class="btn btn-danger" type="button" id="removeDutic" hidden>Eliminar conexión</button></div><form id="integrationsForm"><div class="admin-form">${field("URL del calendario iCal", "duticUrl", "url", "Deja vacío para conservar la URL guardada. El enlace se mantiene oculto.")}</div><div class="form-actions"><button class="btn btn-primary">Guardar integración</button><span id="integrationFeedback" class="admin-feedback" role="status"></span></div></form>`,
);

document.getElementById("googleHelp").onclick = () =>
  showUiDialog({
    title: "Cómo conectar Google",
    message: "1. Entra en console.cloud.google.com y selecciona o crea un proyecto.\n2. En APIs y servicios → Biblioteca, habilita Google Drive API, Google Calendar API y Google Classroom API.\n3. En Google Auth Platform → Branding, configura el nombre Vortex AI Desktop.\n4. En Audiencia, usa Externo y agrega tu correo como usuario de prueba si la aplicación sigue en pruebas.\n5. En Acceso a datos, agrega los permisos de Drive, Calendar y Classroom que utilizarás.\n6. En Clientes, crea un cliente OAuth de tipo Aplicación de escritorio.\n7. Descarga su archivo JSON y selecciónalo aquí.\n8. Escribe tu correo, deja Calendario como primary y pulsa Autorizar cuenta Google.\n9. Acepta los permisos en Google y regresa a Vortex. La activación de las APIs puede tardar unos minutos.",
    confirmLabel: "Entendido",
    cancel: false,
  });

document.getElementById("duticHelp").onclick = () =>
  showUiDialog({
    title: "Cómo conectar DUTIC",
    message: "1. Inicia sesión en el Aula Virtual DUTIC.\n2. Abre Calendario → Importar o exportar calendarios → Exportar calendario.\n3. En Eventos a exportar, selecciona Todos los eventos.\n4. En Periodo de tiempo, selecciona 60 días recientes y próximos.\n5. Pulsa Obtener URL del calendario; no uses Exportar, porque descargaría una copia estática.\n6. Copia la URL completa generada.\n7. Pégala en URL del calendario iCal y pulsa Guardar integración.\n\nLa URL es privada y funciona como una contraseña. No la compartas.",
    confirmLabel: "Entendido",
    cancel: false,
  });

async function loadSettings() {
  const data = await adminRequest("/api/admin/settings");
  const values = {
    allowedPhone: data.values.allowed_phone_number,
    botPrefix: data.values.bot_prefix,
    institutionalEmail: data.values.institutional_email,
    calendarId: data.values.google_calendar_id,
  };
  Object.entries(values).forEach(
    ([id, value]) => (document.getElementById(id).value = value || ""),
  );
  document.getElementById("googleState").textContent = data.google_connected
    ? "Autorizado"
    : data.credentials_uploaded
      ? "Credenciales listas para autorizar"
      : "Credenciales pendientes";
  const connection = data.google_connection || {};
  document.getElementById("googleAccount").textContent = connection.authorized
    ? connection.account || "Cuenta Google autorizada"
    : "Sin cuenta autorizada";
  document.getElementById("googleCredentialFile").textContent = connection.credentials_uploaded
    ? "Archivo OAuth · " + connection.credential_file
    : "Ningún archivo OAuth cargado";
  document.getElementById("disconnectGoogle").hidden = !connection.authorized;
  document.getElementById("removeGoogle").hidden = !connection.credentials_uploaded;
  document.getElementById("connectGoogle").textContent = connection.authorized
    ? "Volver a autorizar"
    : "Autorizar cuenta Google";
  document.getElementById("duticUrl").placeholder = data.dutic_configured
    ? "Calendario configurado · pega otra URL para reemplazarlo"
    : "https://...";
  document.getElementById("duticState").textContent = data.dutic_configured
    ? "Configurado"
    : "Sin configurar";
  document.getElementById("removeDutic").hidden = !data.dutic_configured;
}
async function saveConnections(feedbackId) {
  const value = (id) => document.getElementById(id).value.trim();
  const data = {
    institutional_email: value("institutionalEmail"),
    allowed_phone_number: value("allowedPhone"),
    bot_prefix: value("botPrefix"),
    google_calendar_id: value("calendarId") || "primary",
    dutic_url: value("duticUrl"),
  };
  const result = await adminRequest("/api/admin/settings", "POST", data);
  document.getElementById("duticUrl").value = "";
  if (result.restart_whatsapp)
    await adminRequest("/api/whatsapp/restart", "POST", {});
  document.getElementById(feedbackId).textContent =
    "Configuración guardada y aplicada.";
  await loadSettings();
  return result;
}
[
  ["whatsappForm", "whatsappFeedback"],
  ["integrationsForm", "integrationFeedback"],
].forEach(
  ([id, feedback]) =>
    (document.getElementById(id).onsubmit = async (e) => {
      e.preventDefault();
      const btn = e.submitter;
      btn.disabled = true;
      try {
        await saveConnections(feedback);
      } catch (err) {
        document.getElementById(feedback).textContent = err.message;
      } finally {
        btn.disabled = false;
      }
    }),
);
document.getElementById("googleFile").onchange = async function () {
  try {
    const file = this.files[0];
    if (!file) return;
    if (file.size > 50000) throw new Error("El archivo es demasiado grande.");
    await adminRequest("/api/admin/google/credentials", "POST", {
      document: JSON.parse(await file.text()),
      filename: file.name,
    });
    await loadSettings();
    document.getElementById("googleFeedback").textContent =
      "Credenciales guardadas.";
  } catch (err) {
    document.getElementById("googleFeedback").textContent = err.message;
  }
  this.value = "";
};
document.getElementById("disconnectGoogle").onclick = async function () {
  const approved = await showUiDialog({
    title: "Desconectar Google",
    message: "Vortex dejará de acceder a Google. El archivo OAuth se conservará para que puedas volver a autorizar después.",
    confirmLabel: "Desconectar",
    danger: true,
  });
  if (!approved) return;
  await adminRequest("/api/admin/google/disconnect", "POST", {});
  document.getElementById("googleFeedback").textContent = "Cuenta Google desconectada.";
  await loadSettings();
};
document.getElementById("removeGoogle").onclick = async function () {
  const approved = await showUiDialog({
    title: "Eliminar conexión Google",
    message: "Se eliminarán de este equipo el token autorizado y el archivo OAuth guardado. Para conectarte otra vez tendrás que cargar el JSON.",
    confirmLabel: "Eliminar conexión",
    danger: true,
  });
  if (!approved) return;
  await adminRequest("/api/admin/google/connection", "DELETE");
  document.getElementById("googleFeedback").textContent = "Conexión Google eliminada.";
  await loadSettings();
};
document.getElementById("removeDutic").onclick = async function () {
  const approved = await showUiDialog({
    title: "Eliminar conexión DUTIC",
    message: "Se borrará de este equipo la URL privada del calendario iCal. Vortex dejará de consultar tus eventos de DUTIC.",
    confirmLabel: "Eliminar conexión",
    danger: true,
  });
  if (!approved) return;
  await adminRequest("/api/admin/dutic", "DELETE");
  document.getElementById("integrationFeedback").textContent =
    "Conexión DUTIC eliminada.";
  document.getElementById("duticUrl").value = "";
  await loadSettings();
};
document.getElementById("connectGoogle").onclick = async function () {
  this.disabled = true;
  try {
    await saveConnections("integrationFeedback");
    const data = await adminRequest("/api/admin/google/connect", "POST", {});
    document.getElementById("googleFeedback").textContent = data.message;
    const timer = setInterval(async () => {
      try {
        const state = await adminRequest("/api/admin/settings");
        document.getElementById("googleFeedback").textContent =
          state.google_auth.message;
        if (state.google_auth.status !== "pending") {
          clearInterval(timer);
          this.disabled = false;
          await loadSettings();
        }
      } catch {
        clearInterval(timer);
        this.disabled = false;
      }
    }, 2000);
  } catch (err) {
    document.getElementById("googleFeedback").textContent = err.message;
    this.disabled = false;
  }
};
loadSettings().catch(
  (err) =>
    (document.getElementById("settingsFeedback").textContent = err.message),
);

const chatSection = document
  .getElementById("chatPanelTitle")
  .closest("section");
const projectFolder = document.createElement("p");
projectFolder.id = "projectFolder";
projectFolder.className = "project-folder";
projectFolder.hidden = true;
chatSection.querySelector(".panel-header").after(projectFolder);
const chatTools = document.createElement("div");
chatTools.innerHTML = `<div class="chat-toolbar chat-admin-controls" hidden><select id="projectFilter" aria-label="Proyecto"><option value="all">Todos los proyectos</option><option value="none">Chats libres</option></select><button class="btn" id="newProject">Nuevo proyecto</button></div><div class="chat-toolbar chat-conversation-controls" hidden><select id="chatSelect" aria-label="Conversación"></select><button class="btn chat-context-button" id="openChatContext" type="button" aria-haspopup="dialog">Contexto</button></div><span class="admin-feedback" id="chatFeedback" role="status"></span><dialog class="key-dialog chat-context-dialog" id="chatContextDialog" aria-labelledby="chatContextTitle"><div class="dialog-heading"><div><h3 id="chatContextTitle">Contexto y ahorro de tokens</h3><p>Configura lo que la IA recuerda en esta conversación.</p></div><button class="dialog-close" id="closeChatContext" type="button" aria-label="Cerrar">×</button></div><div class="admin-form">${field("Nombre de la conversación", "chatTitle")}<label class="field">Proyecto del chat<select id="chatProject"><option value="">Chats libres</option></select></label><label class="field">Mensajes recientes enviados al modelo<select id="contextTurns"><option value="0">Solo contexto guardado</option><option value="2">2 intercambios</option><option value="4" selected>4 intercambios</option><option value="8">8 intercambios</option><option value="20">20 intercambios</option></select></label><label class="field field-wide">Contexto guardado<textarea id="chatContext" maxlength="12000" placeholder="Objetivo, decisiones y datos que el agente debe recordar."></textarea><small>Se envía en cada turno junto con los intercambios recientes. Mantén este texto breve para ahorrar tokens.</small></label><label class="field">Importar contexto de otra conversación<select id="importChat"></select></label><label class="field">Contenido a importar<select id="importMode"><option value="context">Contexto guardado</option><option value="recent">Contexto y últimos 2 intercambios</option></select></label></div><div class="form-actions"><button class="btn" id="importContext" type="button">Importar al borrador</button><button class="btn" id="cancelChatContext" type="button">Cancelar</button><button class="btn btn-primary" id="saveContext" type="button">Guardar contexto</button></div></dialog>`;
chatTools.className = "chat-management";
chatSection.classList.add("chat-workspace");
const newChatButton = document.createElement("button");
newChatButton.id = "newChat";
newChatButton.type = "button";
newChatButton.className = "btn btn-primary chat-new-icon";
newChatButton.title = "Nuevo chat";
newChatButton.setAttribute("aria-label", "Nuevo chat");
newChatButton.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';
newChatButton.hidden = true;
chatSection.querySelector(".panel-header .panel-meta").remove();
chatTools.appendChild(newChatButton);
chatSection.insertBefore(chatTools, document.getElementById("chatFeed"));
chatSection
  .querySelector(".panel-header")
  .appendChild(document.getElementById("openChatContext"));
const chatContextDialog = document.getElementById("chatContextDialog");
document.getElementById("openChatContext").onclick = () =>
  chatContextDialog.showModal();
document.getElementById("closeChatContext").onclick = () =>
  chatContextDialog.close();
document.getElementById("cancelChatContext").onclick = () =>
  chatContextDialog.close();
chatContextDialog.onclick = (event) => {
  if (event.target === chatContextDialog) chatContextDialog.close();
};
const quickPrompts = chatSection.querySelector(".quick-prompts");
const quickMenu = document.createElement("details");
quickMenu.className = "chat-quick-menu";
quickMenu.innerHTML = "<summary>Accesos rápidos</summary>";
quickPrompts.before(quickMenu);
quickMenu.appendChild(quickPrompts);

async function refreshChats(catalog = null) {
  const data = catalog || (await adminRequest("/api/chats/projects/catalog"));
  chatCatalog = data.chats;
  projectCatalog = data.projects;
  const filter = document.getElementById("projectFilter");
  filter.replaceChildren(
    new Option("Todos los proyectos", "all"),
    new Option("Chats libres", "none"),
  );
  const assignment = document.getElementById("chatProject");
  const assigned = assignment.value;
  assignment.replaceChildren(new Option("Chats libres", ""));
  projectCatalog.forEach((project) => {
    filter.add(new Option(project.title + " · " + project.chats, project.id));
    assignment.add(new Option(project.title, project.id));
  });
  filter.value = activeProject;
  assignment.value = assigned;
  const select = document.getElementById("chatSelect");
  select.replaceChildren();
  document.getElementById("importChat").replaceChildren();
  chatCatalog.forEach((chat) => {
    const project = projectCatalog.find((p) => p.id === chat.project_id);
    const label = (project ? project.title + " / " : "") + chat.title;
    if (
      activeProject === "all" ||
      (activeProject === "none" && !chat.project_id) ||
      chat.project_id === activeProject
    )
      select.add(new Option(label, chat.id));
    if (chat.id !== activeChatId)
      document.getElementById("importChat").add(new Option(label, chat.id));
  });
  select.value = activeChatId;
  renderProjectTree();
  if (select.selectedIndex < 0 && select.options.length)
    select.selectedIndex = 0;
}
function renderProjectTree() {
  let tree = document.getElementById("projectTree");
  if (!tree) {
    tree = document.createElement("div");
    tree.id = "projectTree";
    tree.className = "project-tree";
    sidebar.insertBefore(tree, sidebar.querySelector(".admin-footer"));
  }
  const openGroups = new Map(
    [...tree.querySelectorAll("details")].map((group) => [
      group.dataset.project,
      group.open,
    ]),
  );
  tree.replaceChildren();
  const heading = document.createElement("div");
  heading.className = "tree-heading";
  const headingLabel = document.createElement("span");
  headingLabel.textContent = "Proyectos y chats";
  const createMenu = document.createElement("details");
  createMenu.className = "tree-create-menu";
  const createToggle = document.createElement("summary");
  createToggle.textContent = "+";
  createToggle.title = "Crear proyecto o chat";
  createToggle.setAttribute("aria-label", createToggle.title);
  const createPopup = document.createElement("div");
  createPopup.className = "tree-popup";
  const projectButton = document.createElement("button");
  projectButton.type = "button";
  projectButton.textContent = "Nuevo proyecto";
  projectButton.onclick = () => {
    createMenu.open = false;
    document.getElementById("newProject").click();
  };
  const freeChatButton = document.createElement("button");
  freeChatButton.type = "button";
  freeChatButton.textContent = "Nuevo chat libre";
  freeChatButton.onclick = () => {
    createMenu.open = false;
    activeProject = "none";
    document.getElementById("newChat").click();
  };
  createPopup.append(projectButton, freeChatButton);
  createMenu.append(createToggle, createPopup);
  heading.append(headingLabel, createMenu);
  tree.appendChild(heading);
  [...projectCatalog, { id: null, title: "Chats libres" }].forEach(
    (project) => {
      const group = document.createElement("details");
      group.dataset.project = project.id || "none";
      group.open = openGroups.has(group.dataset.project)
        ? openGroups.get(group.dataset.project)
        : true;
      const summary = document.createElement("summary");
      summary.textContent = project.title;
      group.appendChild(summary);
      const addChat = document.createElement("button");
      addChat.type = "button";
      addChat.className = "tree-project-add";
      addChat.textContent = "+";
      addChat.title = "Nuevo chat en " + project.title;
      addChat.setAttribute("aria-label", addChat.title);
      addChat.onclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
        activeProject = project.id || "none";
        document.getElementById("newChat").click();
      };
      group.appendChild(addChat);
      if (project.id) group.appendChild(treeActions("project", project));
      chatCatalog
        .filter((chat) => chat.project_id === project.id)
        .forEach((chat) => {
          const button = document.createElement("button");
          button.textContent = chat.title;
          button.classList.toggle("active", chat.id === activeChatId);
          button.onclick = async () => {
            if (chatSending) return;
            activeProject = project.id || "none";
            location.hash = "chats";
            try {
              await openChat(chat.id);
            } catch (error) {
              document.getElementById("chatFeedback").textContent =
                error.message;
            }
          };
          const row = document.createElement("div");
          row.className = "tree-chat-row";
          row.append(button, treeActions("chat", chat));
          group.appendChild(row);
        });
      tree.appendChild(group);
    },
  );
}

function treeActions(kind, item) {
  const menu = document.createElement("details");
  menu.className = "tree-actions";
  const toggle = document.createElement("summary");
  toggle.textContent = "⋯";
  toggle.title = "Opciones de " + item.title;
  toggle.setAttribute("aria-label", toggle.title);
  const popup = document.createElement("div");
  popup.className = "tree-popup";
  for (const [action, label] of [["rename", "Renombrar"], ["delete", "Eliminar"]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.className = action === "delete" ? "tree-delete" : "";
    button.onclick = async (event) => {
      event.stopPropagation();
      menu.open = false;
      if (chatSending) return;
      try {
        const url = "/api/chats/" + (kind === "project" ? "projects/" : "") + item.id;
        if (action === "rename") {
          const title = await showUiDialog({
            title: kind === "project" ? "Renombrar proyecto" : "Renombrar chat",
            message: "Escribe el nuevo nombre.",
            input: true,
            value: item.title,
            confirmLabel: "Guardar",
          });
          if (!title?.trim()) return;
          const payload = kind === "chat" ? await adminRequest(url) : {};
          await adminRequest(url, "PUT", {...payload, title: title.trim()});
          if (kind === "chat" && activeChatId === item.id) document.getElementById("chatTitle").value = title.trim();
        } else {
          const description = kind === "project"
            ? 'Se eliminarán el proyecto «' + item.title + '» y todos sus chats e historiales. Los archivos de la carpeta se conservarán.'
            : 'Se eliminará el chat «' + item.title + '» y todo su historial.';
          const approved = await showUiDialog({
            title: kind === "project" ? "Eliminar proyecto" : "Eliminar chat",
            message: description + " Esta acción no se puede deshacer.",
            confirmLabel: "Eliminar",
            danger: true,
          });
          if (!approved) return;
          await adminRequest(url, "DELETE");
          if (kind === "project" && activeProject === item.id) activeProject = "all";
        }
        await refreshChats();
        if (!chatCatalog.some(chat => chat.id === activeChatId)) {
          activeChatId = null;
          if (chatCatalog.length) {
            activeProject = "all";
            await openChat(chatCatalog[0].id);
          } else clearChatSelection();
        }
      } catch (error) {
        document.getElementById("chatFeedback").textContent = error.message;
      }
    };
    popup.appendChild(button);
  }
  menu.append(toggle, popup);
  return menu;
}

async function openChat(id) {
  if (chatSending) return;
  const chat = await adminRequest("/api/chats/" + id);
  activeChatId = id;
  const folder = document.getElementById("projectFolder");
  folder.textContent = chat.folder_path
    ? "Carpeta de trabajo · " + chat.folder_path
    : "";
  folder.hidden = !chat.folder_path;
  document.getElementById("chatProject").value = chat.project_id || "";
  document.getElementById("chatTitle").value = chat.title;
  document.getElementById("chatContext").value = chat.context;
  document.getElementById("contextTurns").value = String(chat.turns);
  document.getElementById("btnSendChat").disabled = false;
  document.getElementById("chatFeed").replaceChildren();
  chat.messages.forEach((m) => {
    let meta = {};
    try {
      meta = JSON.parse(m.meta);
    } catch {}
    appendChatBubble(
      m.role,
      m.content,
      meta.duration_ms ? meta.duration_ms + " ms" : "",
    );
  });
  if (!chat.messages.length) renderEmptyChat();
  try {
    localStorage.setItem("vortex-chat", id);
  } catch {}
  await refreshChats();
  document.getElementById("chatProject").value = chat.project_id || "";
}

function renderEmptyChat() {
  const feed = document.getElementById("chatFeed");
  if (feed.children.length) return;
  const empty = document.createElement("div");
  empty.className = "chat-empty";
  empty.innerHTML =
    "<strong>Empieza una conversación</strong>Escribe una instrucción abajo. Este chat conservará su propio historial y contexto.";
  feed.appendChild(empty);
}
document.getElementById("newChat").onclick = async () => {
  try {
    const data = await adminRequest("/api/chats", "POST", {
      project_id: projectCatalog.some((p) => p.id === activeProject)
        ? activeProject
        : null,
    });
    if (!projectCatalog.some((p) => p.id === activeProject)) activeProject = "none";
    await openChat(data.id);
  } catch (err) {
    document.getElementById("chatFeedback").textContent = err.message;
  }
};
document.getElementById("chatSelect").onchange = async function () {
  try {
    await openChat(this.value);
  } catch (err) {
    document.getElementById("chatFeedback").textContent = err.message;
  }
};
document.getElementById("saveContext").onclick = async () => {
  try {
    await adminRequest("/api/chats/" + activeChatId, "PUT", {
      title: document.getElementById("chatTitle").value || "Nuevo chat",
      context: document.getElementById("chatContext").value,
      turns: Number(document.getElementById("contextTurns").value),
      project_id: document.getElementById("chatProject").value || null,
    });
    await refreshChats();
    document.getElementById("chatFeedback").textContent = "Contexto guardado.";
    chatContextDialog.close();
  } catch (err) {
    document.getElementById("chatFeedback").textContent = err.message;
  }
};
document.getElementById("importContext").onclick = async () => {
  try {
    const id = document.getElementById("importChat").value;
    if (!id)
      throw new Error("Crea otra conversación para importar su contexto.");
    const source = await adminRequest("/api/chats/" + id);
    let text = source.context;
    if (document.getElementById("importMode").value === "recent")
      text +=
        "\n" +
        source.messages
          .slice(-4)
          .map((m) => m.role + ": " + m.content.slice(0, 2000))
          .join("\n");
    const input = document.getElementById("chatContext");
    input.value = (
      input.value +
      "\n\nContexto importado de " +
      source.title +
      ":\n" +
      text
    )
      .trim()
      .slice(0, 12000);
    document.getElementById("chatFeedback").textContent =
      "Borrador importado. Revísalo y guarda el contexto.";
  } catch (err) {
    document.getElementById("chatFeedback").textContent = err.message;
  }
};
(async () => {
  try {
    await refreshChats();
    let saved;
    try {
      saved =
        localStorage.getItem("vortex-chat") ||
        localStorage.getItem("unsa-chat");
    } catch {}
    if (!chatCatalog.length) {
      const chat = await adminRequest("/api/chats", "POST", {});
      await openChat(chat.id);
    } else
      await openChat(
        chatCatalog.some((c) => c.id === saved) ? saved : chatCatalog[0].id,
      );
  } catch (err) {
    document.getElementById("chatFeedback").textContent = err.message;
  }
})();

document.getElementById("newProject").onclick = async () => {
  const title = await showUiDialog({
    title: "Nuevo proyecto",
    message: "Ponle un nombre para crear su carpeta y su primer chat.",
    input: true,
    confirmLabel: "Crear proyecto",
  });
  if (!title?.trim()) return;
  try {
    const project = await adminRequest("/api/chats/projects/create", "POST", {
      title: title.trim(),
    });
    activeProject = project.id;
    await refreshChats();
    await openChat(project.chat_id);
    document.getElementById("chatFeedback").textContent =
      "Proyecto y chat listos.";
  } catch (err) {
    document.getElementById("chatFeedback").textContent = err.message;
  }
};
document.getElementById("projectFilter").onchange = async function () {
  activeProject = this.value;
  await refreshChats();
  const first = document.getElementById("chatSelect").value;
  if (first) await openChat(first);
  else clearChatSelection();
};

function clearChatSelection() {
  activeChatId = null;
  document.getElementById("btnSendChat").disabled = true;
  document.getElementById("chatFeed").replaceChildren();
  renderEmptyChat();
  document.getElementById("chatTitle").value = "";
  document.getElementById("chatContext").value = "";
  document.getElementById("chatFeedback").textContent =
    "Este proyecto todavía no tiene chats. Pulsa Nuevo chat.";
}

const summarizeButton = document.createElement("button");
summarizeButton.type = "button";
summarizeButton.className = "btn";
summarizeButton.textContent = "Preparar resumen con IA";
document.getElementById("saveContext").before(summarizeButton);
summarizeButton.onclick = async function () {
  if (!activeChatId) return;
  this.disabled = true;
  document.getElementById("chatFeedback").textContent =
    "Preparando resumen. Esta acción consume tokens.";
  try {
    const result = await adminRequest(
      "/api/chats/" + activeChatId + "/summary",
      "POST",
      {},
    );
    document.getElementById("chatContext").value = result.summary;
    document.getElementById("chatFeedback").textContent =
      "Resumen preparado. Revísalo y pulsa Guardar contexto.";
  } catch (error) {
    document.getElementById("chatFeedback").textContent = error.message;
  } finally {
    this.disabled = false;
  }
};

const renameProjectButton = document.createElement("button");
renameProjectButton.type = "button";
renameProjectButton.className = "btn";
renameProjectButton.textContent = "Renombrar proyecto";
document.getElementById("newProject").after(renameProjectButton);
renameProjectButton.onclick = async () => {
  const project = projectCatalog.find((item) => item.id === activeProject);
  if (!project) {
    document.getElementById("chatFeedback").textContent =
      "Selecciona un proyecto para cambiar su nombre.";
    return;
  }
  const title = await showUiDialog({
    title: "Renombrar proyecto",
    message: "Escribe el nuevo nombre del proyecto.",
    input: true,
    value: project.title,
    confirmLabel: "Guardar",
  });
  if (!title?.trim()) return;
  try {
    await adminRequest("/api/chats/projects/" + project.id, "PUT", {
      title: title.trim(),
    });
    await refreshChats();
  } catch (error) {
    document.getElementById("chatFeedback").textContent = error.message;
  }
};
let lastSyncedMessage = null;
setInterval(async () => {
  if (!activeChatId || chatSending || location.hash !== "#chats") return;
  const chatId = activeChatId;
  try {
    const chat = await adminRequest("/api/chats/" + chatId);
    if (activeChatId !== chatId || chatSending) return;
    const latest = chat.messages.at(-1)?.id || 0;
    if (lastSyncedMessage === chatId + ":" + latest) return;
    document.getElementById("chatFeed").replaceChildren();
    chat.messages.forEach((message) => {
      let metadata = {};
      try {
        metadata = JSON.parse(message.meta);
      } catch {}
      const image = metadata.images?.[0]?.split(/[\\/]/).pop();
      appendChatBubble(
        message.role,
        message.content,
        metadata.duration_ms ? metadata.duration_ms + " ms" : "",
        image ? "/api/preview-image?name=" + encodeURIComponent(image) : "",
      );
    });
    if (!chat.messages.length) renderEmptyChat();
    lastSyncedMessage = chatId + ":" + latest;
  } catch {}
}, 5000);

const sidebarToggle = document.getElementById("sidebarToggle");
function updateSidebarToggle() {
  const collapsed =
    document.documentElement.classList.contains("sidebar-collapsed");
  sidebarToggle.setAttribute("aria-expanded", String(!collapsed));
  sidebarToggle.setAttribute(
    "aria-label",
    collapsed ? "Expandir menú lateral" : "Contraer menú lateral",
  );
  sidebarToggle.title = collapsed
    ? "Expandir menú lateral"
    : "Contraer menú lateral";
}
sidebarToggle.onclick = () => {
  const collapsed =
    document.documentElement.classList.toggle("sidebar-collapsed");
  try {
    localStorage.setItem("vortex-sidebar-collapsed", String(collapsed));
  } catch {}
  updateSidebarToggle();
};
updateSidebarToggle();

let catalogUpdating = false;
setInterval(async () => {
  if (catalogUpdating || chatSending) return;
  catalogUpdating = true;
  try {
    const catalog = await adminRequest("/api/chats/projects/catalog");
    if (
      JSON.stringify(catalog.chats) !== JSON.stringify(chatCatalog) ||
      JSON.stringify(catalog.projects) !== JSON.stringify(projectCatalog)
    )
      await refreshChats(catalog);
  } catch {
  } finally {
    catalogUpdating = false;
  }
}, 4000);

const webGenerationPanel = panel("settings", `<h3>Generación mediante IA web</h3><div class="admin-form"><label class="field"><span><input type="checkbox" id="allowWebGeneration"> Permitir generar archivos mediante IA web</span><small>Desactivado: genera mediante las API. Activado: permite usar IA web cuando la pidas expresamente.</small></label><label class="field">Navegador<select id="webAiBrowser"><option value="brave">Brave</option><option value="chrome">Google Chrome</option><option value="edge">Microsoft Edge</option></select><small>Usará la sesión iniciada en ese navegador.</small></label></div><div class="form-actions"><button class="btn btn-primary" id="saveWebGeneration">Guardar</button><span id="webGenerationFeedback" class="admin-feedback" role="status"></span></div>`);
async function loadWebGeneration() {
 const settings = await adminRequest("/api/admin/web-generation");
 document.getElementById("allowWebGeneration").checked = settings.enabled;
 document.getElementById("webAiBrowser").value = settings.browser;
 document.getElementById("webAiBrowser").disabled = !settings.enabled;
}
document.getElementById("allowWebGeneration").onchange = function(){ document.getElementById("webAiBrowser").disabled = !this.checked; };
document.getElementById("saveWebGeneration").onclick = async function(){
 this.disabled = true;
 try {
  await adminRequest("/api/admin/web-generation", "POST", {enabled:document.getElementById("allowWebGeneration").checked,browser:document.getElementById("webAiBrowser").value});
  document.getElementById("webGenerationFeedback").textContent = "Preferencia guardada y aplicada.";
 } catch(error){ document.getElementById("webGenerationFeedback").textContent = error.message; }
 finally {this.disabled = false;}
};
loadWebGeneration().catch(error=>document.getElementById("webGenerationFeedback").textContent=error.message);
