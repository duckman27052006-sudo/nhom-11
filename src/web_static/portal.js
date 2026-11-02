"use strict";
const $ = (id) => document.getElementById(id);
let csrf = "", user = null, mode = "login", cursor = null, pollTimer = null;
let epoch = 0, maxFileSize = 0, pollBusy = false, sendBusy = false;
const formatSize = (size) => size < 1024 ? `${size} B` : size < 1024 ** 2
  ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 ** 2).toFixed(1)} MB`;

function notice(id, message, error = false) {
  const element = $(id);
  element.textContent = message;
  element.hidden = !message;
  element.classList.toggle("is-error", error);
}

async function api(path, options = {}) {
  const headers = { ...options.headers };
  if (options.method && options.method !== "GET") headers["X-CSRF-Token"] = csrf;
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && user) {
      showSession({ user: null });
      notice("auth-error", "Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại.", true);
      // Establish a new anonymous CSRF session for the next login.
      start().catch(() => {});
    }
    throw new Error(data.error || "Không thể xử lý yêu cầu. Hãy thử lại.");
  }
  return data;
}

function jsonPost(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) });
}

function showSession(data) {
  epoch += 1;
  csrf = data.csrf || "";
  user = data.user;
  maxFileSize = data.max_file_size || maxFileSize;
  clearTimeout(pollTimer);
  cursor = null;
  $("messages").replaceChildren($("chat-empty"));
  $("chat-empty").hidden = false;
  $("file-list").replaceChildren();
  $("chat-body").value = "";
  $("upload-form").reset();
  $("new-messages").hidden = true;
  $("account").hidden = !user;
  $("workspace").hidden = !user;
  $("auth").hidden = !!user;
  $("username").textContent = user || "";
  $("auth-submit").disabled = !csrf;
  notice("boot-status", "");
  notice("file-status", "");
  notice("chat-error", "");
  if (user) {
    $("upload-limit").textContent = `Tối đa ${formatSize(maxFileSize)} mỗi file.`;
    loadFiles();
    pollMessages();
  }
}

function schedulePoll(delay) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollMessages, delay);
}

function setMode(next) {
  mode = next;
  const registering = mode === "register";
  $("login-tab").setAttribute("aria-pressed", String(!registering));
  $("register-tab").setAttribute("aria-pressed", String(registering));
  $("auth-title").textContent = registering ? "Tham gia không gian chung." : "Chào mừng trở lại.";
  $("auth-copy").textContent = registering ? "Tạo tài khoản để chia sẻ và trò chuyện." : "Đăng nhập để truy cập file và phòng chat.";
  $("auth-password").autocomplete = registering ? "new-password" : "current-password";
  $("confirm-field").hidden = !registering;
  $("auth-confirm").required = registering;
  $("auth-confirm").value = "";
  $("auth-confirm").setCustomValidity("");
  notice("auth-error", "");
}
$("login-tab").addEventListener("click", () => setMode("login"));
$("register-tab").addEventListener("click", () => setMode("register"));
for (const id of ["auth-password", "auth-confirm"]) {
  $(id).addEventListener("input", () => $("auth-confirm").setCustomValidity(""));
}
$("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (mode === "register" && $("auth-password").value !== $("auth-confirm").value) {
    $("auth-confirm").setCustomValidity("Mật khẩu nhập lại chưa khớp.");
    $("auth-confirm").reportValidity();
    return;
  }
  $("auth-submit").disabled = true;
  $("login-tab").disabled = $("register-tab").disabled = true;
  notice("auth-error", "");
  try {
    const data = await jsonPost(`/api/${mode}`, { username: $("auth-name").value.trim(),
      password: $("auth-password").value });
    $("auth-form").reset();
    showSession(data);
  } catch (error) { notice("auth-error", error.message, true); }
  finally {
    $("auth-submit").disabled = !csrf;
    $("login-tab").disabled = $("register-tab").disabled = false;
  }
});
$("logout").addEventListener("click", async () => {
  $("logout").disabled = true;
  try { showSession(await jsonPost("/api/logout", {})); }
  catch (error) { notice("file-status", error.message, true); }
  finally { $("logout").disabled = false; }
});

async function loadFiles() {
  const generation = epoch;
  $("refresh-files").disabled = true;
  try {
    const data = await api("/api/files");
    if (generation !== epoch) return;
    $("file-list").replaceChildren();
    $("file-count").textContent = data.files.length;
    $("files-empty").hidden = data.files.length !== 0;
    for (const file of data.files) {
      const row = document.createElement("tr");
      const name = document.createElement("td");
      const label = document.createElement("span"); label.className = "file-name";
      label.textContent = file.name;
      const date = document.createElement("span"); date.className = "file-date";
      date.textContent = new Date(file.modified * 1000).toLocaleString("vi-VN");
      name.append(label, date);
      const size = document.createElement("td"); size.textContent = formatSize(file.size);
      const action = document.createElement("td");
      const button = document.createElement("button"); button.type = "button";
      button.textContent = "Tải về ↓";
      button.setAttribute("aria-label", `Tải về ${file.name}`);
      button.addEventListener("click", () => download(file.name, button));
      action.append(button); row.append(name, size, action); $("file-list").append(row);
    }
  } catch (error) { if (generation === epoch) notice("file-status", error.message, true); }
  finally { $("refresh-files").disabled = false; }
}
$("refresh-files").addEventListener("click", () => { notice("file-status", ""); loadFiles(); });

async function download(name, button) {
  const generation = epoch;
  button.disabled = true;
  notice("file-status", `Đang tải ${name}…`);
  try {
    const response = await fetch(`/api/files/${encodeURIComponent(name)}`, { credentials: "same-origin" });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      if (response.status === 401) await start();
      throw new Error(error.error || "Không thể tải file.");
    }
    const blob = await response.blob();
    if (generation !== epoch) return;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url; link.download = name;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    notice("file-status", `Đã tải ${name}.`);
  } catch (error) { if (generation === epoch) notice("file-status", error.message, true); }
  finally { button.disabled = false; }
}

$("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("upload-file").files[0];
  if (!file) return;
  if (file.size > maxFileSize) { notice("file-status", `File vượt giới hạn ${formatSize(maxFileSize)}.`, true); return; }
  const generation = epoch, data = new FormData(); data.append("file", file);
  $("upload-submit").disabled = true;
  notice("file-status", `Đang gửi ${file.name}…`);
  try {
    const result = await api("/api/files", { method: "POST", body: data });
    if (generation !== epoch) return;
    notice("file-status", result.filename !== file.name
      ? `Đã gửi ${file.name}. Tên lưu trên máy chủ: ${result.filename}.`
      : `Đã gửi ${file.name}.`);
    $("upload-form").reset(); await loadFiles();
  } catch (error) { if (generation === epoch) notice("file-status", error.message, true); }
  finally { $("upload-submit").disabled = false; }
});

async function pollMessages() {
  if (!user) return;
  if (pollBusy) { schedulePoll(500); return; }
  pollBusy = true;
  const generation = epoch;
  let delay = 2000;
  try {
    const data = await api(`/api/messages${cursor === null ? "" : `?after=${cursor}`}`);
    if (generation !== epoch) return;
    $("chat-state").textContent = "Đã kết nối";
    const box = $("messages");
    const follow = cursor === null || box.scrollHeight - box.scrollTop - box.clientHeight < 70;
    for (const message of data.messages) {
      if (cursor !== null && message.id <= cursor) continue;
      $("chat-empty").hidden = true;
      const item = document.createElement("article");
      item.className = `message${message.username === user ? " own" : ""}`;
      const meta = document.createElement("div"); meta.className = "message-meta";
      const author = document.createElement("strong"); author.textContent = message.username;
      const time = document.createElement("time"), date = new Date(message.created * 1000);
      time.dateTime = date.toISOString(); time.textContent = date.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" });
      time.title = date.toLocaleString("vi-VN");
      const text = document.createElement("p"); text.textContent = message.body;
      meta.append(author, time); item.append(meta, text); box.append(item); cursor = message.id;
    }
    if (cursor === null) cursor = 0;
    // Keep a bounded DOM while retaining the latest messages.
    while (box.querySelectorAll(".message").length > 300) box.querySelector(".message").remove();
    if (follow) { box.scrollTop = box.scrollHeight; $("new-messages").hidden = true; }
    else if (data.messages.length) $("new-messages").hidden = false;
    if (data.messages.length === 100) delay = 0;
  } catch {
    if (generation === epoch) $("chat-state").textContent = "Mất kết nối · đang thử lại";
    delay = 4000;
  } finally {
    pollBusy = false;
    if (user && generation === epoch) schedulePoll(delay);
  }
}
$("new-messages").addEventListener("click", () => {
  $("messages").scrollTop = $("messages").scrollHeight; $("new-messages").hidden = true;
});
$("chat-body").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault(); if (!sendBusy) $("chat-form").requestSubmit();
  }
});
$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = $("chat-body").value.trim();
  if (!body || sendBusy) return;
  const generation = epoch, original = $("chat-body").value;
  sendBusy = true; $("chat-submit").disabled = true;
  notice("chat-error", "");
  try {
    await jsonPost("/api/messages", { body });
    if (generation !== epoch) return;
    if ($("chat-body").value === original) $("chat-body").value = "";
    clearTimeout(pollTimer); pollMessages();
  } catch (error) { if (generation === epoch) notice("chat-error", error.message, true); }
  finally { sendBusy = false; $("chat-submit").disabled = false; }
});

async function start() {
  $("reconnect").hidden = true;
  $("auth-submit").disabled = true;
  try { showSession(await api("/api/session")); }
  catch (error) {
    notice("boot-status", `Không thể kết nối: ${error.message}`, true);
    $("reconnect").hidden = false;
  }
}
$("reconnect").addEventListener("click", start);
start();
