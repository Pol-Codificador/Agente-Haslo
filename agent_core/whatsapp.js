/* Administración de WhatsApp hecha para Vortex AI Desktop. */
(() => {
  const view = page("whatsapp");
  const access = document.getElementById("whatsappForm").closest("section");
  const contacts = document
    .getElementById("vipContactsPanelTitle")
    .closest("section");
  const qr = document.getElementById("qrBanner");
  const tabs = document.createElement("div");
  tabs.className = "provider-tabs wa-tabs";
  tabs.setAttribute("aria-label", "Opciones de WhatsApp");
  view.insertBefore(tabs, view.children[1]);
  const session = document.createElement("section");
  session.className = "panel wa-section";
  session.innerHTML = `<div class="keys-heading"><div><h3>Sesión de WhatsApp</h3><p id="waSessionSummary">Consultando conexión…</p></div><button class="btn btn-primary" id="newWhatsAppQr">Generar QR nuevo</button></div><div class="keys-table-scroll wa-table-scroll"><table class="keys-table wa-session-table"><thead><tr><th>Cuenta</th><th>Número</th><th>Conectado en</th><th>Estado</th><th>Acciones</th></tr></thead><tbody id="waSessionRows"></tbody></table></div><p id="waSessionFeedback" class="admin-feedback" role="status"></p>`;
  session.appendChild(qr);
  view.insertBefore(session, tabs.nextSibling);
  access.classList.add("wa-section");
  contacts.classList.add("wa-section");
  const sections = { session, access, contacts };
  const controls = {};
  function selectSection(id) {
    Object.entries(sections).forEach(
      ([name, section]) => (section.hidden = name !== id),
    );
    Object.entries(controls).forEach(([name, button]) =>
      button.classList.toggle("selected", name === id),
    );
  }
  for (const [id, label] of [
    ["session", "Sesión"],
    ["access", "Acceso"],
    ["contacts", "Contactos"],
  ]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.onclick = () => selectSection(id);
    tabs.appendChild(button);
    controls[id] = button;
  }
  selectSection("session");
  const dialog = document.createElement("dialog");
  dialog.className = "key-dialog";
  dialog.id = "whatsappDisconnectDialog";
  dialog.innerHTML = `<h3>Desvincular WhatsApp</h3><p id="waDisconnectDescription"></p><p class="section-description">Para conectar otra cuenta tendrás que escanear un QR nuevo.</p><div class="form-actions"><button class="btn" id="waDisconnectCancel">Cancelar</button><button class="btn btn-danger" id="waDisconnectOnly">Desvincular</button><button class="btn btn-primary" id="waDisconnectAndQr">Desvincular y generar QR</button></div>`;
  view.appendChild(dialog);
  let state = null;
  let busy = false;
  const labels = {
    connected: "Conectado",
    syncing: "Sincronizando",
    connecting: "Conectando",
    qr: "Esperando escaneo",
    disconnected: "Desconectado",
    stopped: "Detenido",
    unknown: "Sin conexión",
  };
  const feedback = (text) =>
    (document.getElementById("waSessionFeedback").textContent = text);
  function openDisconnect() {
    document.getElementById("waDisconnectDescription").textContent =
      "Se eliminará la vinculación de " +
      (state?.phone ? "+" + state.phone : "WhatsApp") +
      " en este equipo.";
    dialog.showModal();
  }
  function render() {
    const body = document.getElementById("waSessionRows");
    body.replaceChildren();
    document.getElementById("waSessionSummary").textContent = state.has_session
      ? "1 cuenta vinculada en este equipo"
      : "Vincula una cuenta escaneando el QR.";
    const row = document.createElement("tr");
    if (state.has_session || state.phone) {
      for (const text of [
        state.name || "Cuenta de WhatsApp",
        state.phone ? "+" + state.phone : "Número pendiente",
        state.device,
      ]) {
        const cell = document.createElement("td");
        cell.textContent = text;
        row.appendChild(cell);
      }
      const status = document.createElement("td");
      const badge = document.createElement("span");
      badge.className =
        "key-status " +
        (state.status === "connected"
          ? "key-status-working"
          : "key-status-limited");
      badge.textContent = labels[state.status] || "Desconectado";
      status.appendChild(badge);
      row.appendChild(status);
      const actions = document.createElement("td");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn-mini btn-danger";
      remove.textContent = "Desvincular";
      remove.disabled = busy;
      remove.onclick = openDisconnect;
      actions.appendChild(remove);
      row.appendChild(actions);
    } else {
      const cell = document.createElement("td");
      cell.colSpan = 5;
      cell.className = "keys-empty";
      cell.textContent =
        state.status === "qr"
          ? "Escanea el código de abajo para conectar tu número."
          : "Ningún número vinculado";
      row.appendChild(cell);
    }
    body.appendChild(row);
    document.getElementById("newWhatsAppQr").disabled = busy;
  }
  async function refresh() {
    try {
      state = await adminRequest("/api/whatsapp/session");
      render();
      if (!busy) {
        if (state.status === "qr") feedback("QR listo. Escanéalo desde Dispositivos vinculados en tu teléfono.");
        else if (state.status === "connected") feedback("WhatsApp conectado.");
        else if (["disconnected", "connecting"].includes(state.status)) feedback(state.detail || "Conectando con WhatsApp…");
      }
    } catch (error) {
      feedback(error.message);
    }
  }
  async function disconnect(generateQr) {
    if (busy) return;
    busy = true;
    dialog.close();
    render();
    feedback("Desvinculando la sesión…");
    try {
      const result = await adminRequest("/api/whatsapp/disconnect", "POST", {
        generate_qr: generateQr,
      });
      feedback(
        result.revoked
          ? generateQr
            ? "Sesión desvinculada. Preparando un QR nuevo…"
            : "Sesión desvinculada."
          : "Sesión eliminada de este equipo. Revisa también Dispositivos vinculados en tu teléfono.",
      );
      await refreshStatus();
      await refresh();
    } catch (error) {
      feedback(error.message);
    } finally {
      busy = false;
      await refresh();
    }
  }
  document.getElementById("newWhatsAppQr").onclick = async () => {
    await refresh();
    if (state?.has_session) {
      openDisconnect();
      return;
    }
    if (busy) return;
    busy = true;
    render();
    feedback("Generando QR…");
    try {
      await adminRequest("/api/whatsapp/restart", "POST", {});
      await refreshStatus();
    } catch (error) {
      feedback(error.message);
    } finally {
      busy = false;
      await refresh();
    }
  };
  document.getElementById("waDisconnectCancel").onclick = () => dialog.close();
  document.getElementById("waDisconnectOnly").onclick = () => disconnect(false);
  document.getElementById("waDisconnectAndQr").onclick = () => disconnect(true);
  refresh();
  setInterval(() => {
    if (location.hash === "#whatsapp" && !busy) refresh();
  }, 3000);
})();
