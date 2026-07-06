const state = {
  token: "",
  sessionToken: "",
  authenticated: false,
  items: [],
  activeTab: "file",
  searchTimer: null,
};

const config = window.TRANSFER_ASSISTANT_CONFIG || {};
const API_BASE = String(config.apiBase || "").replace(/\/$/, "");

const elements = {
  status: document.querySelector("#connectionStatus"),
  tokenInput: document.querySelector("#tokenInput"),
  totpInput: document.querySelector("#totpInput"),
  saveToken: document.querySelector("#saveTokenButton"),
  logout: document.querySelector("#logoutButton"),
  refresh: document.querySelector("#refreshButton"),
  fileTab: document.querySelector("#fileTab"),
  textTab: document.querySelector("#textTab"),
  filePanel: document.querySelector("#filePanel"),
  textPanel: document.querySelector("#textPanel"),
  fileInput: document.querySelector("#fileInput"),
  dropZone: document.querySelector("#dropZone"),
  uploadQueue: document.querySelector("#uploadQueue"),
  textTitle: document.querySelector("#textTitleInput"),
  textInput: document.querySelector("#textInput"),
  sendText: document.querySelector("#sendTextButton"),
  search: document.querySelector("#searchInput"),
  items: document.querySelector("#items"),
  template: document.querySelector("#itemTemplate"),
};

function authHeaders(extra = {}) {
  const headers = { ...extra };
  if (state.token) {
    headers.Authorization = `Bearer ${state.token}`;
    headers["X-FTA-Token"] = state.token;
  }
  if (state.sessionToken) {
    headers["X-FTA-Session"] = state.sessionToken;
  }
  return headers;
}

function apiUrl(path) {
  return `${API_BASE}${path}`;
}

async function api(path, options = {}) {
  const response = await fetch(apiUrl(path), {
    ...options,
    credentials: "same-origin",
    headers: authHeaders(options.headers || {}),
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      message = payload.error || message;
    } catch {
      // Keep HTTP status text when the body is not JSON.
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

function setStatus(text) {
  elements.status.textContent = text;
}

function toast(message) {
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = message;
  document.body.appendChild(node);
  window.setTimeout(() => node.remove(), 1800);
}

function bytes(value) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(size >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
}

function dateLabel(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function fileInitial(item) {
  if (item.kind === "text") return "T";
  const name = item.original_name || item.title || "";
  const ext = name.includes(".") ? name.split(".").pop() : "F";
  return ext.slice(0, 3).toUpperCase();
}

function renderItems() {
  elements.items.replaceChildren();
  if (!state.authenticated) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "登录后同步";
    elements.items.appendChild(empty);
    setStatus("未登录");
    return;
  }
  if (state.items.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "暂无记录";
    elements.items.appendChild(empty);
    return;
  }
  for (const item of state.items) {
    const node = elements.template.content.firstElementChild.cloneNode(true);
    const icon = node.querySelector(".item-icon");
    const title = node.querySelector("h2");
    const meta = node.querySelector(".item-meta");
    const preview = node.querySelector(".item-preview");
    const download = node.querySelector(".download-button");
    const copy = node.querySelector(".copy-button");
    const remove = node.querySelector(".delete-button");

    icon.textContent = fileInitial(item);
    icon.classList.toggle("text", item.kind === "text");
    title.textContent = item.title || item.original_name || item.id;
    meta.textContent = `${item.kind === "text" ? "文本" : "文件"} · ${bytes(item.size_bytes)} · ${dateLabel(item.created_at)}`;
    preview.textContent = item.kind === "text" ? item.text_preview || "" : item.sha256 ? `SHA-256 ${item.sha256.slice(0, 16)}` : "";
    copy.classList.toggle("hidden", item.kind !== "text");

    download.addEventListener("click", () => downloadItem(item));
    copy.addEventListener("click", () => copyItemText(item));
    remove.addEventListener("click", () => deleteItem(item));
    elements.items.appendChild(node);
  }
}

async function refreshItems() {
  const params = new URLSearchParams();
  params.set("limit", "100");
  const q = elements.search.value.trim();
  if (q) params.set("q", q);
  try {
    const payload = await api(`/api/items?${params.toString()}`);
    state.items = payload.items || [];
    state.authenticated = true;
    setStatus("已登录");
    renderItems();
  } catch (error) {
    state.authenticated = false;
    setStatus(error.status === 401 ? "未登录" : "连接失败");
    state.items = [];
    renderItems();
    if (error.status !== 401) toast(error.message);
  }
}

async function login() {
  const token = elements.tokenInput.value.trim();
  const totp = elements.totpInput.value.trim();
  if (!token) {
    elements.tokenInput.focus();
    return;
  }
  try {
    const response = await fetch(apiUrl("/api/login"), {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ token, totp }),
    });
    if (!response.ok) {
      throw new Error(response.status === 401 ? "登录失败" : `${response.status} ${response.statusText}`);
    }
    const payload = await response.json();
    state.token = "";
    state.sessionToken = payload.session_token || "";
    state.authenticated = true;
    elements.tokenInput.value = "";
    elements.totpInput.value = "";
    toast("已登录");
    refreshItems();
  } catch (error) {
    toast(error.message);
  }
}

async function logout() {
  await fetch(apiUrl("/api/logout"), {
    method: "POST",
    credentials: "same-origin",
  });
  state.token = "";
  state.sessionToken = "";
  state.authenticated = false;
  state.items = [];
  renderItems();
}

function setTab(tab) {
  state.activeTab = tab;
  const isFile = tab === "file";
  elements.fileTab.classList.toggle("active", isFile);
  elements.textTab.classList.toggle("active", !isFile);
  elements.fileTab.setAttribute("aria-selected", String(isFile));
  elements.textTab.setAttribute("aria-selected", String(!isFile));
  elements.filePanel.classList.toggle("active", isFile);
  elements.textPanel.classList.toggle("active", !isFile);
}

function queueRow(file, status) {
  const row = document.createElement("div");
  row.className = "queue-row";
  const name = document.createElement("strong");
  name.textContent = file.name;
  const label = document.createElement("span");
  label.textContent = status;
  row.append(name, label);
  elements.uploadQueue.prepend(row);
  return label;
}

async function uploadFiles(files) {
  if (!state.authenticated) {
    toast("需要登录");
    return;
  }
  for (const file of files) {
    const label = queueRow(file, "上传中");
    try {
      const url = `/api/files?name=${encodeURIComponent(file.name)}`;
      await api(url, {
        method: "POST",
        headers: {
          "Content-Type": file.type || "application/octet-stream",
        },
        body: file,
      });
      label.textContent = "完成";
    } catch (error) {
      label.textContent = "失败";
      toast(error.message);
    }
  }
  elements.fileInput.value = "";
  refreshItems();
}

async function sendText() {
  if (!state.authenticated) {
    toast("需要登录");
    return;
  }
  const text = elements.textInput.value;
  if (!text.trim()) {
    elements.textInput.focus();
    return;
  }
  try {
    await api("/api/text", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        title: elements.textTitle.value,
        text,
      }),
    });
    elements.textTitle.value = "";
    elements.textInput.value = "";
    toast("已发送");
    refreshItems();
  } catch (error) {
    toast(error.message);
  }
}

async function copyItemText(item) {
  try {
    const payload = await api(`/api/items/${item.id}`);
    await navigator.clipboard.writeText(payload.item.text_content || "");
    toast("已复制");
  } catch (error) {
    toast(error.message);
  }
}

async function downloadItem(item) {
  try {
    const response = await fetch(apiUrl(`/api/items/${item.id}/download`), {
      credentials: "same-origin",
      headers: authHeaders(),
    });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = item.kind === "text" ? `${item.title || "note"}.txt` : item.original_name || item.title || item.id;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    toast(error.message);
  }
}

async function deleteItem(item) {
  if (!window.confirm("删除这条记录？")) return;
  try {
    await api(`/api/items/${item.id}`, { method: "DELETE" });
    state.items = state.items.filter((entry) => entry.id !== item.id);
    renderItems();
  } catch (error) {
    toast(error.message);
  }
}

elements.saveToken.addEventListener("click", login);
elements.logout.addEventListener("click", logout);

elements.refresh.addEventListener("click", refreshItems);
elements.fileTab.addEventListener("click", () => setTab("file"));
elements.textTab.addEventListener("click", () => setTab("text"));
elements.fileInput.addEventListener("change", () => uploadFiles([...elements.fileInput.files]));
elements.sendText.addEventListener("click", sendText);
elements.search.addEventListener("input", () => {
  window.clearTimeout(state.searchTimer);
  state.searchTimer = window.setTimeout(refreshItems, 250);
});

for (const eventName of ["dragenter", "dragover"]) {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.add("dragging");
  });
}

for (const eventName of ["dragleave", "drop"]) {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.remove("dragging");
  });
}

elements.dropZone.addEventListener("drop", (event) => {
  const files = [...event.dataTransfer.files];
  if (files.length) uploadFiles(files);
});

renderItems();
refreshItems();
