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
sidebar.innerHTML =
  '<div class="admin-logo">Vortex AI Desktop<small>Administración personal</small></div><nav class="admin-nav" aria-label="Menú principal"></nav><div class="admin-footer">Espacio local · Windows<br>Tu asistente, en un solo lugar</div>';
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
  button.innerHTML = `<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="${icons[index]}"/></svg>${title}`;
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
panel(
  "settings",
  `<h3>Conexión con Gemini</h3><p class="section-description">Guarda tus claves y prueba el acceso. Las respuestas continúan en el mismo chat cuando cambia la clave.</p><span class="connection-state" id="keyState">Consultando configuración</span><form id="settingsForm"><div class="admin-form"><label class="field field-wide">Agregar claves de API de respaldo<textarea id="apiKeys" autocomplete="off" spellcheck="false" placeholder="Una clave por línea. Deja vacío para conservar las actuales."></textarea><small>Las claves nuevas se agregan a las existentes al guardar. Las claves guardadas se mantienen ocultas. Las cuotas se comparten entre claves del mismo proyecto.</small></label>${field("Modelo principal", "geminiModel")}${field("Modelos rápidos, separados por comas", "modelsFast")}${field("Modelos complejos, separados por comas", "modelsComplex")}</div><div class="form-actions"><button class="btn btn-primary">Guardar configuración</button><button type="button" class="btn" id="testGemini">Probar clave principal</button><span id="settingsFeedback" class="admin-feedback" role="status"></span></div></form>`,
);
panel(
  "whatsapp",
  `<h3>Acceso a WhatsApp</h3><p class="usage-note">Comandos disponibles · /chats para listar · /chat 1 para elegir · /nuevo Nombre para crear · /salir para volver al chat general.</p><p class="section-description">Vincula el dispositivo con el QR y configura qué número puede dar instrucciones.</p><form id="whatsappForm"><div class="admin-form">${field("Número autorizado", "allowedPhone", "tel", "Código de país y número, sin + ni espacios.")}${field("Prefijo de activación", "botPrefix", "text", "Opcional. Vacío para escribir directamente.")}</div><div class="form-actions"><button class="btn btn-primary">Guardar y aplicar conexión</button><button type="button" class="btn" onclick="restartWhatsApp()">Reconectar WhatsApp</button><span class="admin-feedback" id="whatsappFeedback" role="status"></span></div></form>`,
);
panel(
  "integrations",
  `<h3>Cuenta de Google</h3><p class="section-description">Conecta Classroom, Drive y Calendar mediante OAuth. Necesitas un cliente OAuth de escritorio con estas APIs habilitadas en Google Cloud.</p><span class="connection-state" id="googleState">Consultando conexión</span><div class="admin-form"><label class="field">Credenciales OAuth de escritorio<input id="googleFile" type="file" accept="application/json,.json"><small>Selecciona el archivo JSON descargado de Google Cloud.</small></label>${field("Correo institucional", "institutionalEmail", "email")}${field("Calendario", "calendarId", "text", "Usa primary para tu calendario principal.")}</div><div class="form-actions"><button class="btn btn-primary" type="button" id="connectGoogle">Autorizar cuenta Google</button><a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">Configurar OAuth en Google Cloud</a><span id="googleFeedback" class="admin-feedback" role="status"></span></div>`,
);
panel(
  "integrations",
  `<h3>Calendario DUTIC</h3><p class="section-description">En DUTIC abre Calendario → Exportar calendario y copia la URL de eventos.</p><form id="integrationsForm"><div class="admin-form">${field("URL del calendario iCal", "duticUrl", "url", "Deja vacío para conservar la URL guardada. El enlace se mantiene oculto.")}</div><div class="form-actions"><button class="btn btn-primary">Guardar integraciones</button><span id="integrationFeedback" class="admin-feedback" role="status"></span></div></form>`,
);

let savedConfig = {};
async function loadSettings() {
  const data = await adminRequest("/api/admin/settings");
  savedConfig = data;
  const values = {
    allowedPhone: data.values.allowed_phone_number,
    botPrefix: data.values.bot_prefix,
    institutionalEmail: data.values.institutional_email,
    calendarId: data.values.google_calendar_id,
    geminiModel: data.models.gemini_model,
    modelsFast: data.models.gemini_models_fast,
    modelsComplex: data.models.gemini_models_complex,
  };
  Object.entries(values).forEach(
    ([id, value]) => (document.getElementById(id).value = value || ""),
  );
  document.getElementById("keyState").textContent =
    `${data.key_count} clave(s) guardada(s)`;
  document.getElementById("googleState").textContent = data.google_connected
    ? "Autorización guardada"
    : data.credentials_uploaded
      ? "Credenciales listas para autorizar"
      : "Credenciales pendientes";
  document.getElementById("duticUrl").placeholder = data.dutic_configured
    ? "Calendario configurado · pega otra URL para reemplazarlo"
    : "https://...";
}
async function saveConnections(feedbackId) {
  const value = (id) => document.getElementById(id).value.trim();
  const data = {
    institutional_email: value("institutionalEmail"),
    allowed_phone_number: value("allowedPhone"),
    bot_prefix: value("botPrefix"),
    google_calendar_id: value("calendarId") || "primary",
    dutic_url: value("duticUrl"),
    gemini_model: value("geminiModel"),
    gemini_models_fast: value("modelsFast"),
    gemini_models_complex: value("modelsComplex"),
    replace_keys: document.getElementById("replaceKeys").checked,
  };
  if (value("apiKeys"))
    data.gemini_keys = value("apiKeys")
      .split(/\r?\n/)
      .map((x) => x.trim())
      .filter(Boolean);
  const result = await adminRequest("/api/admin/settings", "POST", data);
  document.getElementById("apiKeys").value = "";
  document.getElementById("duticUrl").value = "";
  if (result.restart_whatsapp)
    await adminRequest("/api/whatsapp/restart", "POST", {});
  document.getElementById(feedbackId).textContent =
    "Configuración guardada y aplicada.";
  await loadSettings();
  return result;
}
[
  ["settingsForm", "settingsFeedback"],
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
document.getElementById("testGemini").onclick = async function () {
  this.disabled = true;
  try {
    const result = await adminRequest("/api/admin/gemini/test", "POST", {});
    document.getElementById("settingsFeedback").textContent = result.message;
  } catch (err) {
    document.getElementById("settingsFeedback").textContent = err.message;
  } finally {
    this.disabled = false;
  }
};
document.getElementById("googleFile").onchange = async function () {
  try {
    const file = this.files[0];
    if (!file) return;
    if (file.size > 50000) throw new Error("El archivo es demasiado grande.");
    await adminRequest("/api/admin/google/credentials", "POST", {
      document: JSON.parse(await file.text()),
    });
    await loadSettings();
    document.getElementById("googleFeedback").textContent =
      "Credenciales guardadas.";
  } catch (err) {
    document.getElementById("googleFeedback").textContent = err.message;
  }
  this.value = "";
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
const chatTools = document.createElement("div");
chatTools.innerHTML = `<div class="chat-toolbar"><select id="projectFilter" aria-label="Proyecto"><option value="all">Todos los proyectos</option><option value="none">Sin proyecto</option></select><button class="btn" id="newProject">Nuevo proyecto</button></div><div class="chat-toolbar"><select id="chatSelect" aria-label="Conversación"></select><button class="btn btn-primary" id="newChat">Nuevo chat</button></div><details class="chat-context"><summary>Contexto y ahorro de tokens</summary><div class="admin-form">${field("Nombre de la conversación", "chatTitle")}<label class="field">Proyecto del chat<select id="chatProject"><option value="">Sin proyecto</option></select></label><label class="field">Mensajes recientes enviados al modelo<select id="contextTurns"><option value="0">Solo contexto guardado</option><option value="2">2 intercambios</option><option value="4" selected>4 intercambios</option><option value="8">8 intercambios</option><option value="20">20 intercambios</option></select></label><label class="field field-wide">Contexto guardado<textarea id="chatContext" maxlength="12000" placeholder="Objetivo, decisiones y datos que el agente debe recordar."></textarea><small>Se envía en cada turno junto con los intercambios recientes. Mantén este texto breve para ahorrar tokens.</small></label><label class="field">Importar contexto de otra conversación<select id="importChat"></select></label><label class="field">Contenido a importar<select id="importMode"><option value="context">Contexto guardado</option><option value="recent">Contexto y últimos 2 intercambios</option></select></label></div><div class="form-actions"><button class="btn" id="importContext">Importar al borrador</button><button class="btn btn-primary" id="saveContext">Guardar contexto</button><span class="admin-feedback" id="chatFeedback" role="status"></span></div></details>`;
chatSection.insertBefore(chatTools, document.getElementById("chatFeed"));
async function refreshChats() {
  [chatCatalog, projectCatalog] = await Promise.all([
    adminRequest("/api/chats"),
    adminRequest("/api/chats/projects/list"),
  ]);
  const filter = document.getElementById("projectFilter");
  filter.replaceChildren(
    new Option("Todos los proyectos", "all"),
    new Option("Sin proyecto", "none"),
  );
  const assignment = document.getElementById("chatProject");
  const assigned = assignment.value;
  assignment.replaceChildren(new Option("Sin proyecto", ""));
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
  tree.replaceChildren();
  const heading = document.createElement("div");
  heading.className = "tree-heading";
  heading.textContent = "Proyectos y chats";
  tree.appendChild(heading);
  [...projectCatalog, { id: null, title: "Sin proyecto" }].forEach(
    (project) => {
      const group = document.createElement("details");
      group.open = project.id === activeProject || !project.id;
      const summary = document.createElement("summary");
      summary.textContent = project.title;
      group.appendChild(summary);
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
          group.appendChild(button);
        });
      tree.appendChild(group);
    },
  );
}

async function openChat(id) {
  if (chatSending) return;
  const chat = await adminRequest("/api/chats/" + id);
  activeChatId = id;
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
    appendChatBubble(m.role, m.content, meta.duration_ms ? meta.duration_ms + " ms" : "");
  });
  try {
    localStorage.setItem("vortex-chat", id);
  } catch {}
  await refreshChats();
  document.getElementById("chatProject").value = chat.project_id || "";
}
document.getElementById("newChat").onclick = async () => {
  try {
    const data = await adminRequest("/api/chats", "POST", {
      project_id: projectCatalog.some((p) => p.id === activeProject)
        ? activeProject
        : null,
    });
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
      saved = (localStorage.getItem("vortex-chat") || localStorage.getItem("unsa-chat"));
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
  const title = prompt("Nombre del proyecto");
  if (!title?.trim()) return;
  try {
    const project = await adminRequest("/api/chats/projects/create", "POST", {
      title: title.trim(),
    });
    activeProject = project.id;
    await refreshChats();
    clearChatSelection();
    document.getElementById("chatFeedback").textContent =
      "Proyecto creado. Pulsa Nuevo chat para agregar una conversación.";
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

const renameProjectButton = document.createElement('button');
renameProjectButton.type = 'button';
renameProjectButton.className = 'btn';
renameProjectButton.textContent = 'Renombrar proyecto';
document.getElementById('newProject').after(renameProjectButton);
renameProjectButton.onclick = async () => {
 const project = projectCatalog.find(item => item.id === activeProject);
 if (!project) { document.getElementById('chatFeedback').textContent = 'Selecciona un proyecto para cambiar su nombre.'; return; }
 const title = prompt('Nombre del proyecto', project.title);
 if (!title?.trim()) return;
 try { await adminRequest('/api/chats/projects/' + project.id, 'PUT', {title:title.trim()}); await refreshChats(); }
 catch (error) { document.getElementById('chatFeedback').textContent = error.message; }
};
const replaceKeysLabel = document.createElement('label');
replaceKeysLabel.className = 'field field-wide';
const replaceKeysControl = document.createElement('input');
replaceKeysControl.type = 'checkbox';
replaceKeysControl.id = 'replaceKeys';
replaceKeysControl.style.width = 'auto';
replaceKeysLabel.append(replaceKeysControl, 'Reemplazar las claves guardadas por la lista ingresada');
document.getElementById('apiKeys').closest('label').after(replaceKeysLabel);

let lastSyncedMessage = null;
setInterval(async () => {
  if (!activeChatId || chatSending || location.hash !== '#chats') return;
  const chatId = activeChatId;
  try {
    const chat = await adminRequest('/api/chats/' + chatId);
    if (activeChatId !== chatId || chatSending) return;
    const latest = chat.messages.at(-1)?.id || 0;
    if (lastSyncedMessage === chatId + ':' + latest) return;
    document.getElementById('chatFeed').replaceChildren();
    chat.messages.forEach(message => {
      let metadata = {};
      try { metadata = JSON.parse(message.meta); } catch {}
      const image = metadata.images?.[0]?.split(/[\\/]/).pop();
      appendChatBubble(message.role, message.content, metadata.duration_ms ? metadata.duration_ms + ' ms' : '', image ? '/api/preview-image?name=' + encodeURIComponent(image) : '');
    });
    lastSyncedMessage = chatId + ':' + latest;
  } catch {}
}, 5000);
