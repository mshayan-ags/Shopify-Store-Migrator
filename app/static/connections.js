const SMW_STORAGE_KEY = "smw_connections";

function smwGetConnections() {
  try {
    const raw = localStorage.getItem(SMW_STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch (e) {
    return [];
  }
}

function smwSaveConnections(list) {
  localStorage.setItem(SMW_STORAGE_KEY, JSON.stringify(list));
}

function smwGetConnection(id) {
  return smwGetConnections().find(function (c) { return c.id === id; });
}

function smwUpsertConnection(conn) {
  const list = smwGetConnections();
  const idx = list.findIndex(function (c) { return c.id === conn.id; });
  if (idx >= 0) { list[idx] = conn; } else { list.push(conn); }
  smwSaveConnections(list);
}

function smwDeleteConnection(id) {
  smwSaveConnections(smwGetConnections().filter(function (c) { return c.id !== id; }));
}

function smwNewId() {
  return "c_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function smwConnectionsOfKind(kinds) {
  return smwGetConnections().filter(function (c) { return kinds.indexOf(c.kind) !== -1; });
}

function smwPopulateSelect(selectEl, kinds) {
  if (!selectEl) return;
  selectEl.innerHTML = "";
  smwConnectionsOfKind(kinds).forEach(function (c) {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = c.name + " (" + c.kind + ")";
    selectEl.appendChild(opt);
  });
}

function smwFillHiddenJson(selectEl, hiddenEl) {
  if (!selectEl || !hiddenEl) return;
  const conn = smwGetConnection(selectEl.value);
  hiddenEl.value = conn ? JSON.stringify(conn) : "";
}
