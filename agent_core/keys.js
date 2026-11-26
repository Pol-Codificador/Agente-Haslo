/* Gestión visual de proveedores hecha para Vortex AI Desktop. */
(() => {
  const root = document.getElementById("keysRoot");
  let rows = [];
  let provider = "all";
  let currentPage = 0;
  const testing = new Set();
  const statusNames = {
    untested: "Sin comprobar",
    working: "Funciona",
    invalid: "Rechazada",
    forbidden: "Sin acceso",
    limited: "Sin cuota",
    unavailable: "Saturado",
    offline: "Sin conexión",
    error: "Error",
  };
  root.innerHTML = `<div class="keys-heading"><div><h3>Claves de API</h3><p>Gemini y GLM · modelos organizados automáticamente</p></div><button class="btn btn-primary" id="addApiKey">＋ Agregar clave</button></div>
    <div class="keys-toolbar"><div class="provider-tabs" role="group" aria-label="Filtrar proveedor"><button data-provider="all" class="selected">Todas</button><button data-provider="gemini">Gemini</button><button data-provider="glm">GLM · Z.ai</button></div><span id="keyCount" class="panel-meta"></span></div>
    <div class="keys-table-scroll"><table class="keys-table"><thead><tr><th>Nombre</th><th>Proveedor</th><th>API key</th><th>Estado</th><th>Modelos</th><th>Acciones</th></tr></thead><tbody id="keyRows"></tbody></table></div>
    <div class="keys-footer"><span id="keyFeedback" role="status"></span><div><button class="btn btn-mini" id="keysPrevious" aria-label="Página anterior">←</button><span id="keysPage"></span><button class="btn btn-mini" id="keysNext" aria-label="Página siguiente">→</button></div></div>
    <p class="keys-note">Las claves se guardan cifradas para tu usuario de Windows. Comprobar realiza una consulta breve y consume tokens.</p>
    <dialog id="keyDialog" class="key-dialog"><form id="keyForm"><div class="keys-heading"><h3 id="keyDialogTitle">Agregar clave</h3><button type="button" class="btn btn-mini" id="closeKeyDialog" aria-label="Cerrar">✕</button></div><div class="key-dialog-fields"><label class="field">Proveedor<select id="keyProvider"><option value="gemini">Google Gemini</option><option value="glm">GLM · Z.ai</option></select></label><label class="field">Nombre<input id="keyLabel" required maxlength="80" placeholder="Por ejemplo, Cuenta personal"></label><label class="field">API key<div class="secret-entry"><input id="keySecret" type="password" autocomplete="new-password" spellcheck="false" maxlength="2000"><button type="button" class="btn btn-mini" id="toggleKeyEntry" aria-label="Mostrar clave ingresada">◉</button></div><small id="secretHint"></small></label><a id="getProviderKey" href="https://aistudio.google.com/app/apikey" target="_blank" rel="noopener">Obtener una clave de Gemini ↗</a></div><div id="keyDialogError" role="alert"></div><div class="form-actions"><button type="button" class="btn" id="cancelKeyDialog">Cancelar</button><button class="btn btn-primary" id="saveKey">Guardar y comprobar</button></div></form></dialog>
    <dialog id="keyDeleteDialog" class="key-dialog"><h3>Eliminar clave</h3><p id="deleteKeyMessage"></p><div class="form-actions"><button class="btn" id="cancelKeyDelete">Cancelar</button><button class="btn btn-danger" id="confirmKeyDelete">Eliminar</button></div></dialog>
    <dialog id="keyModelsDialog" class="key-dialog"><div class="keys-heading"><h3>Modelos detectados</h3><button class="btn btn-mini" id="closeKeyModels" aria-label="Cerrar modelos">✕</button></div><p class="section-description">La clasificación rápida o compleja se asigna por familia de modelo. El acceso y la cuota pueden variar.</p><div id="detectedModels"></div></dialog>`;

  const feedback = (text) =>
    (document.getElementById("keyFeedback").textContent = text);
  function button(text, label, action, className = "btn btn-mini") {
    const control = document.createElement("button");
    control.type = "button";
    control.className = className;
    control.textContent = text;
    control.setAttribute("aria-label", label);
    control.title = label;
    control.onclick = action;
    return control;
  }
  function cell(row, text) {
    const td = document.createElement("td");
    if (text !== undefined) td.textContent = text;
    row.appendChild(td);
    return td;
  }
  function classification(name) {
    return /pro|hydrogen|thinking|glm-5|glm-4\.7/i.test(name)
      ? "Complejo"
      : "Rápido";
  }
  function showModels(row) {
    const container = document.getElementById("detectedModels");
    container.replaceChildren();
    row.models.forEach((model) => {
      const entry = document.createElement("div");
      entry.className = "detected-model";
      const name = document.createElement("span");
      name.textContent = model;
      const badge = document.createElement("span");
      badge.className = "badge badge-cyan";
      badge.textContent = classification(model);
      entry.append(name, badge);
      container.appendChild(entry);
    });
    document.getElementById("keyModelsDialog").showModal();
  }
  function render() {
    const pageSize = window.innerWidth < 1100 ? 3 : 5;
    const filtered = rows.filter(
      (row) => provider === "all" || row.provider === provider,
    );
    const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
    currentPage = Math.min(currentPage, totalPages - 1);
    const body = document.getElementById("keyRows");
    body.replaceChildren();
    for (const row of filtered.slice(currentPage * pageSize, currentPage * pageSize + pageSize)) {
      const tr = document.createElement("tr");
      cell(tr, row.label);
      cell(tr, row.provider === "gemini" ? "Gemini" : "GLM · Z.ai");
      const secretCell = cell(tr);
      const secret = document.createElement("input");
      secret.readOnly = true;
      secret.className = "masked-key";
      secret.value = "••••••••" + row.suffix;
      secret.setAttribute("aria-label", "Clave de " + row.label);
      let shown = false;
      const reveal = button("◉", "Mostrar clave de " + row.label, async () => {
        try {
          if (shown) {
            secret.value = "••••••••" + row.suffix;
            shown = false;
            reveal.title = "Mostrar clave";
            return;
          }
          const result = await adminRequest(
            "/api/keys/" + row.id + "/reveal",
            "POST",
            {},
          );
          secret.value = result.secret;
          shown = true;
          reveal.title = "Ocultar clave";
        } catch (error) {
          feedback(error.message);
        }
      });
      const copy = button("⧉", "Copiar clave de " + row.label, async () => {
        try {
          const result = await adminRequest(
            "/api/keys/" + row.id + "/reveal",
            "POST",
            {},
          );
          await navigator.clipboard.writeText(result.secret);
          feedback("Clave copiada.");
        } catch (error) {
          feedback("No se pudo copiar. Usa Mostrar y copia el campo.");
        }
      });
      const secretGroup = document.createElement("div");
      secretGroup.className = "key-secret-group";
      secretGroup.append(secret, reveal, copy);
      secretCell.appendChild(secretGroup);
      const state = cell(tr);
      const badge = document.createElement("span");
      badge.className = "key-status key-status-" + row.status;
      badge.textContent = testing.has(row.id)
        ? "Comprobando…"
        : statusNames[row.status] || "Sin comprobar";
      badge.title = row.message || "";
      state.appendChild(badge);
      const models = cell(tr);
      if (row.models.length)
        models.appendChild(
          button(
            String(row.models.length),
            "Ver modelos de " + row.label,
            () => showModels(row),
            "model-count",
          ),
        );
      else models.textContent = "—";
      const actions = cell(tr);
      const group = document.createElement("div");
      group.className = "key-actions";
      const check = button("Comprobar", "Comprobar " + row.label, () =>
        test(row.id),
      );
      check.disabled = testing.has(row.id);
      group.append(
        check,
        button("✎", "Editar " + row.label, () => openEditor(row)),
        button("⌫", "Eliminar " + row.label, () => confirmDelete(row)),
      );
      actions.appendChild(group);
      body.appendChild(tr);
    }
    if (!filtered.length) {
      const tr = document.createElement("tr");
      const td = cell(tr, "Agrega una clave para conectar tus modelos.");
      td.colSpan = 6;
      td.className = "keys-empty";
      body.appendChild(tr);
    }
    document.getElementById("keyCount").textContent =
      filtered.length + " clave(s)";
    document.getElementById("keysPage").textContent =
      currentPage + 1 + " / " + totalPages;
    document.getElementById("keysPrevious").disabled = currentPage === 0;
    document.getElementById("keysNext").disabled =
      currentPage >= totalPages - 1;
    root
      .querySelectorAll("[data-provider]")
      .forEach((tab) =>
        tab.classList.toggle("selected", tab.dataset.provider === provider),
      );
  }
  async function refresh() {
    rows = await adminRequest("/api/keys");
    render();
  }
  async function test(id) {
    if (testing.has(id)) return;
    testing.add(id);
    render();
    feedback("Realizando una consulta de prueba…");
    try {
      const result = await adminRequest(
        "/api/keys/" + id + "/test",
        "POST",
        {},
      );
      feedback(result.message);
      await refresh();
      await refreshStatus();
    } catch (error) {
      feedback(error.message);
    } finally {
      testing.delete(id);
      render();
    }
  }
  let editing = null;
  function providerLink() {
    const glm = document.getElementById("keyProvider").value === "glm";
    const link = document.getElementById("getProviderKey");
    link.href = glm
      ? "https://z.ai/manage-apikey/apikey-list"
      : "https://aistudio.google.com/app/apikey";
    link.textContent = glm
      ? "Obtener una clave estándar de Z.ai ↗"
      : "Obtener una clave de Gemini ↗";
  }
  function openEditor(row = null) {
    editing = row?.id || null;
    document.getElementById("keyDialogTitle").textContent = editing
      ? "Editar clave"
      : "Agregar clave";
    document.getElementById("keyProvider").value =
      row?.provider || (provider === "glm" ? "glm" : "gemini");
    document.getElementById("keyLabel").value = row?.label || "";
    const secret = document.getElementById("keySecret");
    secret.value = "";
    secret.type = "password";
    secret.required = !editing;
    document.getElementById("secretHint").textContent = editing
      ? "Deja vacío para conservar la clave actual."
      : "La clave se guardará cifrada en esta computadora.";
    document.getElementById("keyDialogError").textContent = "";
    providerLink();
    document.getElementById("keyDialog").showModal();
  }
  function confirmDelete(row) {
    const dialog = document.getElementById("keyDeleteDialog");
    document.getElementById("deleteKeyMessage").textContent =
      '¿Eliminar "' + row.label + '" de Vortex?';
    document.getElementById("confirmKeyDelete").onclick = async () => {
      try {
        await adminRequest("/api/keys/" + row.id, "DELETE");
        dialog.close();
        await refresh();
        feedback("Clave eliminada.");
        await refreshStatus();
      } catch (error) {
        feedback(error.message);
      }
    };
    dialog.showModal();
  }
  document.getElementById("keyForm").onsubmit = async (event) => {
    event.preventDefault();
    const save = document.getElementById("saveKey");
    save.disabled = true;
    try {
      const payload = {
        provider: document.getElementById("keyProvider").value,
        label: document.getElementById("keyLabel").value.trim(),
        secret: document.getElementById("keySecret").value.trim(),
      };
      const result = await adminRequest(
        "/api/keys" + (editing ? "/" + editing : ""),
        editing ? "PUT" : "POST",
        payload,
      );
      const id = editing || result.id;
      document.getElementById("keySecret").value = "";
      document.getElementById("keyDialog").close();
      await refresh();
      await test(id);
    } catch (error) {
      document.getElementById("keyDialogError").textContent = error.message;
    } finally {
      save.disabled = false;
    }
  };
  document.getElementById("keyProvider").onchange = providerLink;
  document.getElementById("toggleKeyEntry").onclick = () => {
    const input = document.getElementById("keySecret");
    input.type = input.type === "password" ? "text" : "password";
  };
  document.getElementById("addApiKey").onclick = () => openEditor();
  for (const id of ["closeKeyDialog", "cancelKeyDialog"])
    document.getElementById(id).onclick = () => {
      document.getElementById("keySecret").value = "";
      document.getElementById("keyDialog").close();
    };
  document.getElementById("cancelKeyDelete").onclick = () =>
    document.getElementById("keyDeleteDialog").close();
  document.getElementById("closeKeyModels").onclick = () =>
    document.getElementById("keyModelsDialog").close();
  document.getElementById("keysPrevious").onclick = () => {
    currentPage--;
    render();
  };
  document.getElementById("keysNext").onclick = () => {
    currentPage++;
    render();
  };
  root.querySelectorAll("[data-provider]").forEach(
    (tab) =>
      (tab.onclick = () => {
        provider = tab.dataset.provider;
        currentPage = 0;
        render();
      }),
  );
  refresh().catch((error) => feedback(error.message));
})();
