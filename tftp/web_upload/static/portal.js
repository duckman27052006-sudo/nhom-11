"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const SESSION_ERROR = "Phiên đăng nhập đã hết hạn. Hãy đăng nhập lại.";
  const EMPTY_FILES = ["Chưa có tệp nào", "Gửi tệp đầu tiên để bắt đầu chia sẻ."];
  const EMPTY_CHAT = ["Chưa có tin nhắn", "Bắt đầu bằng một lời chào!"];

  const state = {
    csrf: "",
    user: null,
    id: null,
    role: null,
    canModerate: false,
    maxFileSize: 0,
    chat: { muted: false, muted_until: null, mute_reason: "" },
    files: [],
    messages: [],
    search: "",
    mode: "login",
    generation: 0,
    chatVersion: 0,
    serverEpoch: null,
    cursor: null,
    pollTimer: null,
    pollBusy: false,
    stream: null,
    streamGeneration: 0,
    streamVersion: 0,
    sendBusy: false,
    uploadBusy: false,
    progressTimer: null,
    adminTimer: null,
    admin: null,
    restrictionTimer: null,
  };

  /* ---------------------------------------------------------------- helpers */

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function icon(name, className = "ico") {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", className);
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#" + name);
    svg.append(use);
    return svg;
  }

  function formatSize(size) {
    if (!size) return "0 B";
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
    return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }

  const AVATAR_TONES = ["", "avatar--leaf", "avatar--gold", "avatar--coral", "avatar--aqua"];

  function hashCode(value) {
    let hash = 0;
    for (const char of String(value || "?")) hash = (hash * 31 + char.codePointAt(0)) % 100003;
    return hash;
  }

  function avatarTone(name) {
    return AVATAR_TONES[hashCode(name) % AVATAR_TONES.length];
  }

  function initials(name) {
    const parts = String(name || "")
      .split(/[_\s.-]+/)
      .filter(Boolean)
      .slice(0, 2);
    const text = parts.map((part) => part[0]).join("").toUpperCase();
    return text || "?";
  }

  function relativeTime(seconds) {
    if (!seconds) return "trước đây";
    const diff = Math.max(0, Date.now() / 1000 - Number(seconds));
    if (diff < 60) return "vừa xong";
    if (diff < 3600) return `${Math.floor(diff / 60)} phút trước`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} giờ trước`;
    if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} ngày trước`;
    return new Date(Number(seconds) * 1000).toLocaleDateString("vi-VN");
  }

  function remainingText(until) {
    if (!until) return "";
    const diff = Number(until) - Date.now() / 1000;
    if (diff <= 0) return "ít phút";
    const minutes = Math.ceil(diff / 60);
    if (minutes < 60) return `${minutes} phút`;
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    if (hours < 24) return rest ? `${hours} giờ ${rest} phút` : `${hours} giờ`;
    const days = Math.floor(hours / 24);
    const left = hours % 24;
    return left ? `${days} ngày ${left} giờ` : `${days} ngày`;
  }

  function dateText(seconds) {
    if (!seconds) return "trước đây";
    return new Date(Number(seconds) * 1000).toLocaleDateString("vi-VN");
  }

  function toast(message, tone = "ok") {
    const stack = $("toasts");
    while (stack.children.length >= 4) stack.firstElementChild.remove();
    const box = el("div", `toast toast--${tone}`);
    box.append(icon(tone === "ok" ? "i-check" : "i-warning"));
    box.append(el("p", "", message));
    stack.append(box);
    setTimeout(() => {
      box.classList.add("is-leaving");
      setTimeout(() => box.remove(), 240);
    }, 4600);
  }

  function notice(id, message, error = false) {
    const node = $(id);
    node.textContent = message;
    node.hidden = !message;
    node.classList.toggle("is-error", Boolean(error));
  }

  function setEmptyMessage(node, title, copy) {
    const strong = node.querySelector("strong");
    if (strong) strong.textContent = title;
    const last = node.lastElementChild;
    if (last && last !== strong) last.textContent = copy;
  }

  /* -------------------------------------------------------------------- api */

  async function api(path, options = {}) {
    const method = options.method || "GET";
    const headers = { ...(options.headers || {}) };
    if (method !== "GET" && method !== "HEAD") headers["X-CSRF-Token"] = state.csrf;
    let response;
    try {
      response = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
    } catch {
      throw new Error("Không kết nối được tới máy chủ. Kiểm tra VPN/Wi-Fi rồi thử lại.");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.error || "Không thể xử lý yêu cầu. Hãy thử lại.");
      error.status = response.status;
      if (response.status === 401 && state.user) {
        await recoverSession();
      }
      throw error;
    }
    return data;
  }

  const jsonOptions = (method, body) => ({
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  async function recoverSession() {
    clearTimeout(state.pollTimer);
    closeStream();
    clearInterval(state.adminTimer);
    clearInterval(state.restrictionTimer);
    state.user = null;
    state.canModerate = false;
    toast(SESSION_ERROR, "warn");
    try {
      applySession(await api("/api/session"));
    } catch {
      notice("boot-status", "Không thể kết nối máy chủ LAN.", true);
    }
  }

  /* ---------------------------------------------------------------- session */

  function updateAccount() {
    $("username").textContent = state.user || "";
    $("username").title = state.user || "";
    const avatar = $("user-avatar");
    avatar.textContent = initials(state.user);
    avatar.className = "avatar " + avatarTone(state.user);
    const role = $("user-role");
    role.textContent = state.canModerate ? "Quản trị" : "Thành viên";
    role.classList.toggle("is-admin", state.canModerate);
    const greeting = $("greeting");
    const hour = new Date().getHours();
    const salutation = hour < 11 ? "Chào buổi sáng" : hour < 18 ? "Chào buổi chiều" : "Chào buổi tối";
    const name = el("span", "", state.user + ".");
    greeting.replaceChildren(document.createTextNode(`${salutation}, `), name);
    $("workspace-date").textContent =
      "KHÔNG GIAN CHUNG · " +
      new Date().toLocaleDateString("vi-VN", {
        weekday: "long",
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
      }).toUpperCase();
  }

  function applySession(data) {
    state.generation += 1;
    state.chatVersion += 1;
    clearTimeout(state.pollTimer);
    closeStream();
    clearInterval(state.adminTimer);
    clearInterval(state.restrictionTimer);
    state.pollBusy = false;
    state.sendBusy = false;
    state.uploadBusy = false;
    state.csrf = data.csrf || "";
    state.user = data.user || null;
    state.id = data.id ?? null;
    state.role = data.role || null;
    state.canModerate = Boolean(data.can_moderate);
    state.maxFileSize = data.max_file_size || state.maxFileSize || 0;
    state.chat = data.chat || { muted: false, muted_until: null, mute_reason: "" };
    state.files = [];
    state.messages = [];
    state.cursor = null;
    state.serverEpoch = null;
    state.admin = null;
    state.search = "";
    $("search").value = "";

    $("file-list").replaceChildren();
    $("members").replaceChildren();
    $("actions-log").replaceChildren();
    $("messages").replaceChildren($("chat-empty"));
    $("chat-empty").hidden = false;
    $("new-messages").hidden = true;
    $("upload-form").reset();
    $("upload-name").textContent = "Chưa chọn tệp nào";
    $("chat-form").reset();
    $("chat-body").value = "";
    updateChatCounter();
    setProgress(null);
    notice("file-status", "");
    notice("chat-error", "");
    notice("auth-error", "");
    notice("boot-status", "");
    $("auth-submit").disabled = !state.csrf;

    $("account").hidden = !state.user;
    $("workspace").hidden = !state.user;
    $("auth").hidden = Boolean(state.user);
    for (const node of document.querySelectorAll(".workspace-only")) node.hidden = !state.user;
    $("nav-admin").hidden = !state.canModerate;
    $("admin").hidden = !state.canModerate;
    $("stat-files").textContent = "0";
    $("stat-messages").textContent = "0";
    $("stat-status").textContent = "chờ";
    $("chat-state").textContent = "Đang kết nối";
    $("chat-state").classList.remove("is-off");
    resetEmptyState();

    if (!state.user) {
      $("username").textContent = "";
      $("chat-body").disabled = true;
      $("chat-submit").disabled = true;
      $("file-count").textContent = "0";
      return;
    }

    updateAccount();
    $("upload-limit").textContent = `Tối đa ${formatSize(state.maxFileSize)} mỗi tệp.`;
    applyChatState(state.chat, state.canModerate);
    renderRestriction();
    state.restrictionTimer = setInterval(renderRestriction, 30000);
    loadFiles();
    startChatFeed();
    if (state.canModerate) {
      startAdmin();
      loadAdmin();
    }
  }

  function resetEmptyState() {
    setEmptyMessage($("files-empty"), EMPTY_FILES[0], EMPTY_FILES[1]);
    setEmptyMessage($("chat-empty"), EMPTY_CHAT[0], EMPTY_CHAT[1]);
    $("files-empty").hidden = state.files.length !== 0;
    $("chat-empty").hidden = state.messages.length !== 0;
  }

  function startAdmin() {
    clearInterval(state.adminTimer);
    state.adminTimer = setInterval(() => {
      if (!document.hidden && state.user && state.canModerate) loadAdmin({ silent: true });
    }, 20000);
  }

  /* ------------------------------------------------------------------ auth */

  function setMode(next) {
    state.mode = next;
    const registering = next === "register";
    $("login-tab").setAttribute("aria-pressed", String(!registering));
    $("register-tab").setAttribute("aria-pressed", String(registering));
    $("auth-title").textContent = registering ? "Tham gia không gian chung" : "Chào mừng trở lại";
    $("auth-copy").textContent = registering
      ? "Tạo tài khoản để gửi tệp và trò chuyện cùng mọi người."
      : "Đăng nhập để truy cập tệp và phòng chat.";
    $("auth-password").autocomplete = registering ? "new-password" : "current-password";
    $("confirm-field").hidden = !registering;
    $("auth-confirm").required = registering;
    $("auth-confirm").value = "";
    $("auth-confirm").setCustomValidity("");
    notice("auth-error", "");
  }

  /* ----------------------------------------------------------------- files */

  function fileKind(name) {
    const extension = String(name).split(".").pop().toLowerCase();
    if (["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico"].includes(extension)) return "img";
    if (["zip", "rar", "7z", "tar", "gz", "xz", "bz2"].includes(extension)) return "archive";
    if (["mp4", "mkv", "mov", "avi", "webm", "mp3", "wav", "flac", "ogg"].includes(extension)) return "media";
    if (["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv"].includes(extension)) return "doc";
    return "";
  }

  function fileRow(file) {
    const row = el("tr");
    row.dataset.search = `${file.name} ${file.owner || ""}`.toLowerCase();

    const cell = el("td");
    const wrap = el("div", "file-cell");
    const badge = el("span", "file-icon " + (fileKind(file.name) ? `file-icon--${fileKind(file.name)}` : ""));
    badge.append(icon("i-doc"));
    const copy = el("div");
    copy.append(el("span", "file-name", file.name));
    const origin = file.owner ? `Người tải: ${file.owner}` : "Chưa ghi nhận người tải";
    copy.append(el("span", "file-date",
      `${new Date(file.modified * 1000).toLocaleString("vi-VN")} · ${origin}`));
    wrap.append(badge, copy);
    cell.append(wrap);

    const size = el("td", "", formatSize(file.size));

    const action = el("td");
    const actions = el("div", "file-actions");
    const download = el("button", "gel gel--glass gel--sm");
    download.type = "button";
    download.setAttribute("aria-label", `Tải về ${file.name}`);
    download.append(icon("i-download"));
    download.append(el("span", "", "Tải về"));
    download.addEventListener("click", () => downloadFile(file.name, download));
    actions.append(download);
    if (file.can_delete) {
      const remove = el("button", "gel gel--danger gel--sm");
      remove.type = "button";
      remove.setAttribute("aria-label", `Xóa ${file.name}`);
      remove.append(icon("i-trash"));
      remove.append(el("span", "", "Xóa"));
      remove.addEventListener("click", () => openDeleteFileModal(file));
      actions.append(remove);
    }
    action.append(actions);

    row.append(cell, size, action);
    return row;
  }

  function renderFiles() {
    $("file-list").replaceChildren();
    for (const file of state.files) $("file-list").append(fileRow(file));
    $("file-count").textContent = String(state.files.length);
    $("stat-files").textContent = String(state.files.length);
    applySearch();
  }

  async function loadFiles() {
    const generation = state.generation;
    $("refresh-files").disabled = true;
    try {
      const data = await api("/api/files");
      if (generation !== state.generation) return;
      state.files = Array.isArray(data.files) ? data.files : [];
      renderFiles();
    } catch (error) {
      if (generation === state.generation) toast(error.message, "err");
    } finally {
      $("refresh-files").disabled = false;
    }
  }

  function openDeleteFileModal(file) {
    openModal({
      eyebrow: "CHIA SẺ · XÓA TỆP",
      title: `Xóa “${file.name}”?`,
      description: "Tệp sẽ bị xóa khỏi máy chủ TFTP và mọi người sẽ không thể tải về nữa. Hành động này không thể hoàn tác.",
      confirmLabel: "Xóa tệp",
      tone: "gel--danger",
      onConfirm: async () => {
        const data = await api(`/api/files/${encodeURIComponent(file.name)}`,
          { method: "DELETE" });
        toast(data.message || "Đã xóa tệp.", "ok");
        await loadFiles();
        if (state.canModerate) loadAdmin({ silent: true });
      },
    });
  }

  async function downloadFile(name, button) {
    button.disabled = true;
    try {
      let response;
      try {
        response = await fetch(`/api/files/${encodeURIComponent(name)}`, {
          credentials: "same-origin",
        });
      } catch {
        throw new Error("Không kết nối được tới máy chủ. Kiểm tra VPN/Wi-Fi rồi thử lại.");
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        if (response.status === 401) await recoverSession();
        throw new Error(data.error || "Không thể tải tệp.");
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      toast(`Đã tải ${name}.`, "ok");
    } catch (error) {
      toast(error.message, "err");
    } finally {
      button.disabled = false;
    }
  }

  function setProgress(fraction, label = "") {
    const bar = $("progress");
    const labelNode = $("progress-label");
    if (fraction === null) {
      bar.hidden = true;
      labelNode.hidden = true;
      $("progress-fill").style.width = "0%";
      bar.setAttribute("aria-valuenow", "0");
      labelNode.textContent = "";
      return;
    }
    const percent = Math.max(0, Math.min(1, fraction));
    bar.hidden = false;
    bar.setAttribute("aria-valuenow", String(Math.round(percent * 100)));
    $("progress-fill").style.width = `${percent * 100}%`;
    if (label) {
      labelNode.hidden = false;
      labelNode.textContent = label;
    } else {
      labelNode.hidden = true;
    }
  }

  function uploadFile(file) {
    if (!file) return;
    if (state.uploadBusy) {
      toast("Đang gửi một tệp khác. Vui lòng đợi.", "warn");
      return;
    }
    if (state.maxFileSize && file.size > state.maxFileSize) {
      toast(`Tệp vượt giới hạn ${formatSize(state.maxFileSize)}.`, "err");
      return;
    }
    state.uploadBusy = true;
    $("upload-submit").disabled = true;
    $("file-status").hidden = true;
    clearTimeout(state.progressTimer);
    setProgress(0, `Đang gửi ${file.name}…`);
    const form = new FormData();
    form.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/files");
    xhr.setRequestHeader("X-CSRF-Token", state.csrf);
    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      const ratio = event.total ? event.loaded / event.total : 0;
      setProgress(ratio, `Đang gửi ${file.name}… ${Math.round(ratio * 100)}%`);
    });
    xhr.addEventListener("load", () => {
      let data = {};
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        data = {};
      }
      state.uploadBusy = false;
      $("upload-submit").disabled = false;
      if (xhr.status === 201) {
        setProgress(1, "Hoàn tất");
        const changed = data.renamed === true
          || (data.filename && data.filename !== file.name);
        const saved = changed && data.filename
          ? ` Tệp trùng tên nên máy chủ đã lưu thành ${data.filename}.`
          : "";
        toast(`Đã gửi ${file.name}.${saved}`, "ok");
        $("upload-form").reset();
        $("upload-name").textContent = "Chưa chọn tệp nào";
        loadFiles();
        state.progressTimer = setTimeout(() => setProgress(null), 1400);
      } else if (xhr.status === 401) {
        setProgress(null);
        recoverSession();
      } else {
        setProgress(null);
        toast(data.error || "Không thể gửi tệp.", "err");
      }
    });
    xhr.addEventListener("error", () => {
      state.uploadBusy = false;
      $("upload-submit").disabled = false;
      setProgress(null);
      toast("Không thể gửi tệp. Kiểm tra kết nối VPN/Wi-Fi.", "err");
    });
    xhr.send(form);
  }

  /* ------------------------------------------------------------------ chat */

  function updateChatCounter() {
    const value = $("chat-body").value;
    $("chat-counter").textContent = `${value.length}/2000`;
  }

  function nearBottom(box) {
    return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  }

  function buildMessage(message) {
    const own = message.user_id === state.id;
    const article = el("article", "msg" + (own ? " msg--own" : ""));
    article.dataset.search = `${message.username} ${message.body}`.toLowerCase();
    const avatar = el("span", "msg__avatar " + avatarTone(message.username), initials(message.username));
    avatar.setAttribute("aria-hidden", "true");
    const content = el("div", "msg__content");
    const head = el("div", "msg__head");
    head.append(el("strong", "msg__author", message.username));
    if (message.role === "admin") {
      const role = el("span", "msg__role");
      role.append(icon("i-crown"));
      role.append(document.createTextNode("QTV"));
      role.title = "Quản trị viên";
      head.append(role);
    }
    const date = new Date(message.created * 1000);
    const time = el("time", "msg__time", date.toLocaleTimeString("vi-VN", {
      hour: "2-digit",
      minute: "2-digit",
    }));
    time.dateTime = date.toISOString();
    time.title = date.toLocaleString("vi-VN");
    head.append(time);
    if (state.canModerate || own) {
      const remove = el("button", "msg__delete");
      remove.type = "button";
      remove.title = "Xóa tin nhắn";
      remove.setAttribute("aria-label", `Xóa tin nhắn của ${message.username}`);
      remove.append(icon("i-trash"));
      remove.addEventListener("click", () => deleteMessage(message.id, article));
      head.append(remove);
    }
    content.append(head, el("p", "msg__bubble", message.body));
    article.append(avatar, content);
    return article;
  }

  function applyChatState(chat, canModerate) {
    const wasMuted = state.chat.muted;
    state.chat = {
      muted: Boolean(chat && chat.muted),
      muted_until: chat && chat.muted_until ? chat.muted_until : null,
      mute_reason: (chat && chat.mute_reason) || "",
    };
    if (typeof canModerate === "boolean" && canModerate !== state.canModerate) {
      state.canModerate = canModerate;
      $("nav-admin").hidden = !canModerate;
      $("admin").hidden = !canModerate;
      $("user-role").textContent = canModerate ? "Quản trị" : "Thành viên";
      $("user-role").classList.toggle("is-admin", canModerate);
      if (canModerate) {
        startAdmin();
        loadAdmin({ silent: true });
      } else {
        clearInterval(state.adminTimer);
      }
    }
    renderRestriction();
    if (wasMuted && !state.chat.muted) toast("Quyền gửi tin nhắn của bạn đã được mở lại.", "ok");
    if (!wasMuted && state.chat.muted) toast("Bạn đã bị hạn chế gửi tin nhắn.", "warn");
  }

  function renderRestriction() {
    const muted = state.chat.muted;
    $("chat-restriction").hidden = !muted;
    $("chat-form").classList.toggle("is-locked", muted);
    $("chat-body").disabled = muted || !state.user;
    $("chat-submit").disabled = muted || state.sendBusy || !state.user;
    $("chat-body").placeholder = muted
      ? "Bạn đang bị hạn chế gửi tin nhắn…"
      : "Viết lời nhắn cho mọi người…";
    if (!muted) return;
    const parts = [];
    if (state.chat.mute_reason) parts.push(`Lý do: ${state.chat.mute_reason}.`);
    parts.push(state.chat.muted_until
      ? `Tự động mở sau ${remainingText(state.chat.muted_until)}.`
      : "Quản trị viên sẽ mở lại khi phù hợp.");
    $("restriction-text").textContent = parts.join(" ");
  }

  function closeStream() {
    if (state.stream) {
      const source = state.stream;
      state.stream = null;
      source.close();
    }
  }

  function markChatConnected() {
    $("chat-state").textContent = "Đã kết nối";
    $("chat-state").classList.remove("is-off");
    $("stat-status").textContent = "ổn định";
  }

  function consumeChat(data) {
    const box = $("messages");
    const following = state.cursor === null || nearBottom(box);
    if (data.reset) {
      state.messages = [];
      state.cursor = null;
      box.replaceChildren($("chat-empty"));
    }
    state.serverEpoch = data.epoch;
    if (data.chat) applyChatState(data.chat, data.can_moderate);
    const incoming = Array.isArray(data.messages) ? data.messages : [];
    for (const message of incoming) {
      if (state.cursor !== null && message.id <= state.cursor) continue;
      box.append(buildMessage(message));
      state.messages.push(message);
      state.cursor = message.id;
    }
    if (state.cursor === null) state.cursor = 0;
    while (state.messages.length > 300) {
      state.messages.shift();
      const first = box.querySelector(".msg");
      if (first) first.remove();
    }
    $("stat-messages").textContent = String(state.messages.length);
    $("chat-empty").hidden = state.messages.length > 0;
    markChatConnected();
    if (following) {
      box.scrollTop = box.scrollHeight;
      $("new-messages").hidden = true;
    } else if (incoming.length) {
      $("new-messages").hidden = false;
    }
    applySearch();
    return incoming.length;
  }

  function startChatFeed() {
    closeStream();
    clearTimeout(state.pollTimer);
    if (!state.user) return;
    if (typeof window.EventSource === "function") {
      openStream();
    } else {
      pollMessages();
    }
  }

  function openStream() {
    if (!state.user) return;
    if (typeof window.EventSource !== "function") {
      pollMessages();
      return;
    }
    closeStream();
    clearTimeout(state.pollTimer);
    const params = new URLSearchParams();
    if (state.cursor !== null) params.set("after", String(state.cursor));
    if (state.serverEpoch !== null) params.set("epoch", String(state.serverEpoch));
    const query = params.toString();
    const source = new EventSource(`/api/stream${query ? `?${query}` : ""}`);
    state.stream = source;
    state.streamGeneration = state.generation;
    state.streamVersion = state.chatVersion;
    const current = () => state.stream === source
      && state.generation === state.streamGeneration
      && state.chatVersion === state.streamVersion;
    source.addEventListener("open", () => {
      if (current()) markChatConnected();
    });
    source.addEventListener("messages", (event) => {
      if (!current()) return;
      let data;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }
      consumeChat(data);
    });
    source.addEventListener("chat", (event) => {
      if (!current()) return;
      let data;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }
      if (data.chat) applyChatState(data.chat, data.can_moderate);
    });
    source.addEventListener("expire", () => {
      if (state.stream !== source) return;
      closeStream();
      recoverSession();
    });
    source.addEventListener("error", () => {
      if (state.stream !== source) return;
      closeStream();
      $("chat-state").textContent = "Đang tải lại tin nhắn";
      $("chat-state").classList.add("is-off");
      pollMessages();
    });
  }

  function forceChatRefresh() {
    state.chatVersion += 1;
    state.messages = [];
    state.cursor = null;
    state.serverEpoch = null;
    const box = $("messages");
    box.replaceChildren($("chat-empty"));
    $("chat-empty").hidden = false;
    $("new-messages").hidden = true;
    closeStream();
    clearTimeout(state.pollTimer);
    state.pollBusy = false;
    if (typeof window.EventSource === "function") openStream();
    else pollMessages();
  }

  function schedulePoll(delay) {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(pollMessages, delay);
  }

  async function pollMessages() {
    if (!state.user) return;
    if (state.pollBusy) {
      schedulePoll(600);
      return;
    }
    state.pollBusy = true;
    const generation = state.generation;
    const version = state.chatVersion;
    let delay = 2200;
    try {
      const params = new URLSearchParams();
      if (state.cursor !== null) params.set("after", String(state.cursor));
      if (state.serverEpoch !== null) params.set("epoch", String(state.serverEpoch));
      const query = params.toString();
      const data = await api(`/api/messages${query ? `?${query}` : ""}`);
      if (generation !== state.generation || version !== state.chatVersion) return;
      if (consumeChat(data) === 100) delay = 0;
    } catch {
      if (generation === state.generation) {
        $("chat-state").textContent = "Mất kết nối · đang thử lại";
        $("chat-state").classList.add("is-off");
        $("stat-status").textContent = "gián đoạn";
        delay = 4500;
      }
    } finally {
      state.pollBusy = false;
      if (state.user && generation === state.generation && !state.stream) {
        schedulePoll(delay);
      }
    }
  }

  async function deleteMessage(id, node) {
    try {
      await api(`/api/messages/${id}`, { method: "DELETE" });
      node.remove();
      state.messages = state.messages.filter((message) => message.id !== id);
      $("stat-messages").textContent = String(state.messages.length);
      $("chat-empty").hidden = state.messages.length > 0;
      toast("Đã xóa tin nhắn.", "ok");
      forceChatRefresh();
      if (state.canModerate) loadAdmin({ silent: true });
    } catch (error) {
      toast(error.message, "err");
    }
  }

  async function sendMessage() {
    if (!state.user || state.sendBusy || state.chat.muted) return;
    const area = $("chat-body");
    const body = area.value.trim();
    if (!body) return;
    const original = area.value;
    state.sendBusy = true;
    $("chat-submit").disabled = true;
    notice("chat-error", "");
    try {
      await api("/api/messages", jsonOptions("POST", { body }));
      if (area.value === original) area.value = "";
      updateChatCounter();
      clearTimeout(state.pollTimer);
      pollMessages();
    } catch (error) {
      notice("chat-error", error.message, true);
    } finally {
      state.sendBusy = false;
      renderRestriction();
    }
  }

  /* ----------------------------------------------------------------- search */

  function applySearch() {
    const raw = state.search.trim();
    const term = raw.toLowerCase();
    let visibleFiles = 0;
    for (const row of $("file-list").querySelectorAll("tr")) {
      const hit = !term || (row.dataset.search || "").includes(term);
      row.classList.toggle("is-hidden", !hit);
      if (hit) visibleFiles += 1;
    }
    const filesEmpty = $("files-empty");
    if (state.files.length === 0) {
      setEmptyMessage(filesEmpty, EMPTY_FILES[0], EMPTY_FILES[1]);
      filesEmpty.hidden = false;
    } else if (term && visibleFiles === 0) {
      setEmptyMessage(filesEmpty, "Không tìm thấy tệp", `Không có tệp nào khớp “${raw}”.`);
      filesEmpty.hidden = false;
    } else {
      filesEmpty.hidden = true;
    }

    let visibleMessages = 0;
    for (const node of $("messages").querySelectorAll(".msg")) {
      const hit = !term || (node.dataset.search || "").includes(term);
      node.hidden = !hit;
      if (hit) visibleMessages += 1;
    }
    const chatEmpty = $("chat-empty");
    if (state.messages.length === 0) {
      setEmptyMessage(chatEmpty, EMPTY_CHAT[0], EMPTY_CHAT[1]);
      chatEmpty.hidden = false;
    } else if (term && visibleMessages === 0) {
      setEmptyMessage(chatEmpty, "Không tìm thấy tin nhắn", `Không có tin nhắn nào khớp “${raw}”.`);
      chatEmpty.hidden = false;
    } else {
      chatEmpty.hidden = true;
    }
  }

  /* ------------------------------------------------------------------ admin */

  function renderStats(stats) {
    if (!stats) return;
    $("stat-users").textContent = String(stats.users ?? 0);
    $("stat-admins").textContent = String(stats.admins ?? 0);
    $("stat-banned").textContent = String(stats.banned ?? 0);
    $("stat-muted").textContent = String(stats.muted ?? 0);
    $("stat-total-messages").textContent = String(stats.messages ?? 0);
  }

  function statusPills(user) {
    const wrap = el("div", "member__tags");
    if (user.banned) {
      const pill = el("span", "pill pill--danger");
      pill.append(icon("i-ban"));
      pill.append(document.createTextNode("Đã khóa"));
      wrap.append(pill);
      if (user.ban_until) wrap.append(el("span", "hint", `Mở sau ${remainingText(user.ban_until)}`));
    }
    if (user.muted) {
      const pill = el("span", "pill pill--warn");
      pill.append(icon("i-mute"));
      pill.append(document.createTextNode("Hạn chế chat"));
      wrap.append(pill);
      if (user.mute_until) wrap.append(el("span", "hint", `Mở sau ${remainingText(user.mute_until)}`));
    }
    if (!user.banned && !user.muted) {
      const pill = el("span", "pill pill--ok");
      pill.append(icon("i-check"));
      pill.append(document.createTextNode("Hoạt động"));
      wrap.append(pill);
    }
    return wrap;
  }

  function actionButton(iconName, label, handler, options = {}) {
    const variant = options.danger ? " act--danger"
      : options.gold ? " act--gold"
        : options.warn ? " act--warn"
          : options.ok ? " act--ok" : "";
    const button = el("button", "act" + variant);
    button.type = "button";
    button.title = options.hint || label;
    button.setAttribute("aria-label", label);
    button.append(icon(iconName), el("span", "act__label", options.short || label));
    if (options.disabled) button.disabled = true;
    else button.addEventListener("click", handler);
    return button;
  }

  function memberRow(user) {
    const self = user.id === state.id;
    const row = el("div", "member"
      + (user.banned ? " member--banned" : "")
      + (!user.banned && user.muted ? " member--muted" : "")
      + (self ? " member--self" : ""));

    const avatar = el("span", "avatar " + avatarTone(user.username), initials(user.username));
    avatar.setAttribute("aria-hidden", "true");

    const identity = el("div", "member__id");
    const nameLine = el("div", "member__name");
    nameLine.append(el("strong", "", user.username));
    if (user.role === "admin") {
      const badge = el("span", "badge is-admin");
      badge.append(icon("i-crown"));
      badge.append(document.createTextNode("Quản trị"));
      nameLine.append(badge);
    }
    if (self) nameLine.append(el("span", "badge", "Bạn"));
    identity.append(nameLine);
    const meta = el("div", "member__meta");
    const joined = el("span");
    joined.append(icon("i-clock"), document.createTextNode(`Tham gia ${dateText(user.created)}`));
    const count = el("span");
    count.append(icon("i-chat"), document.createTextNode(`${user.messages || 0} tin nhắn`));
    meta.append(joined, count);
    if (user.last_message) {
      const active = el("span");
      active.append(icon("i-sparkle"), document.createTextNode(relativeTime(user.last_message)));
      meta.append(active);
    }
    identity.append(meta);
    identity.append(statusPills(user));

    const actions = el("div", "member__actions");
    const blockedHint = "Hạ quyền quản trị viên trước khi khóa hoặc hạn chế chat.";
    if (self) {
      actions.append(
        actionButton("i-mute", "Không thể hạn chế chính bạn", () => {},
          { short: "Hạn chế", disabled: true, warn: true, hint: "Không thể thao tác trên chính bạn." }),
        actionButton("i-ban", "Không thể khóa chính bạn", () => {},
          { short: "Khóa", danger: true, disabled: true, hint: "Không thể thao tác trên chính bạn." }),
      );
    } else if (user.role === "admin") {
      actions.append(
        actionButton("i-crown", `Hạ quyền ${user.username}`,
          () => openRoleModal(user, false), { gold: true, short: "Hạ quyền" }),
        actionButton("i-ban", "Khóa tài khoản", () => {},
          { danger: true, disabled: true, short: "Khóa", hint: blockedHint }),
      );
    } else {
      const blocked = user.banned || user.muted;
      actions.append(
        actionButton("i-crown", `Cấp quyền quản trị cho ${user.username}`,
          () => openRoleModal(user, true),
          {
            gold: true,
            short: "Cấp quyền",
            disabled: blocked,
            hint: user.banned
              ? "Mở khóa tài khoản trước khi cấp quyền quản trị."
              : "Bỏ hạn chế chat trước khi cấp quyền quản trị.",
          }),
        actionButton(user.muted ? "i-voice" : "i-mute",
          user.muted ? `Bỏ hạn chế chat của ${user.username}` : `Hạn chế chat của ${user.username}`,
          () => (user.muted
            ? runModeration(`/api/admin/users/${user.id}/mute`, { active: false })
            : openSanctionModal(user, "mute")),
          { warn: true, short: user.muted ? "Bỏ hạn chế" : "Hạn chế" }),
        actionButton(user.banned ? "i-unlock" : "i-ban",
          user.banned ? `Mở khóa ${user.username}` : `Khóa ${user.username}`,
          () => (user.banned
            ? runModeration(`/api/admin/users/${user.id}/ban`, { active: false })
            : openSanctionModal(user, "ban")),
          user.banned ? { ok: true, short: "Mở khóa" } : { danger: true, short: "Khóa" }),
        actionButton("i-trash", `Xóa lịch sử chat của ${user.username}`,
          () => openPurgeModal(user), { danger: true, short: "Xóa lịch sử" }),
      );
    }

    row.append(avatar, identity, actions);
    return row;
  }

  function renderMembers(users) {
    const box = $("members");
    box.replaceChildren();
    if (!users || users.length === 0) {
      const empty = el("p", "empty");
      const orb = el("span", "empty__orb");
      orb.append(icon("i-users"));
      empty.append(orb, el("strong", "", "Chưa có thành viên"), el("span", "", "Tài khoản mới sẽ xuất hiện tại đây."));
      box.append(empty);
      return;
    }
    for (const user of users) box.append(memberRow(user));
  }

  const ACTION_ICONS = {
    ban: "i-ban",
    unban: "i-unlock",
    mute: "i-mute",
    unmute: "i-voice",
    role: "i-crown",
    delete: "i-trash",
    purge: "i-trash",
  };

  function renderLog(actions) {
    const list = $("actions-log");
    list.replaceChildren();
    if (!actions || actions.length === 0) {
      list.append(el("li", "log__empty", "Chưa có thao tác quản trị nào."));
      return;
    }
    for (const action of actions) {
      const item = el("li", "log__item");
      const badge = el("span", "log__icon");
      badge.append(icon(ACTION_ICONS[action.action] || "i-shield"));
      const body = el("div");
      const text = el("p", "log__text");
      text.append(el("strong", "", action.actor || "?"), document.createTextNode(" → "));
      text.append(el("strong", "", action.target || "phòng chung"), document.createTextNode(`: ${action.detail || ""}`));
      const time = el("span", "log__time", relativeTime(action.created));
      time.title = new Date(action.created * 1000).toLocaleString("vi-VN");
      body.append(text, time);
      item.append(badge, body);
      list.append(item);
    }
  }

  async function loadAdmin({ silent = false, button = null } = {}) {
    if (!state.canModerate || !state.user) return;
    if (button) button.disabled = true;
    try {
      const data = await api("/api/admin/overview");
      if (!state.user) return;
      state.admin = data;
      const me = (data.users || []).find((user) => user.id === state.id);
      if (me && me.role !== "admin") {
        state.canModerate = false;
        $("admin").hidden = true;
        $("nav-admin").hidden = true;
        clearInterval(state.adminTimer);
        toast("Bạn không còn quyền quản trị.", "warn");
        return;
      }
      renderStats(data.stats);
      renderMembers(data.users);
      renderLog(data.actions);
    } catch (error) {
      if (error.status === 403 && /quản trị/.test(error.message)) {
        state.canModerate = false;
        $("admin").hidden = true;
        $("nav-admin").hidden = true;
        clearInterval(state.adminTimer);
        if (!silent) toast(error.message, "warn");
      } else if (!silent && error.status !== 401) {
        toast(error.message, "err");
      }
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function runModeration(path, body, method = "POST") {
    try {
      const data = await api(path, {
        method,
        ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
      });
      toast(data.message || "Đã cập nhật quyền truy cập.", "ok");
      await loadAdmin({ silent: true });
    } catch (error) {
      toast(error.message, "err");
    }
  }

  async function afterPurge() {
    await loadAdmin({ silent: true });
    forceChatRefresh();
  }

  /* ------------------------------------------------------------------ modal */

  let modalCounter = 0;

  function openModal({ eyebrow, title, description, content, confirmLabel, tone = "gel--aqua", onConfirm }) {
    const root = $("modal-root");
    const previousFocus = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    root.replaceChildren();
    root.hidden = false;
    document.body.style.overflow = "hidden";
    let busy = false;
    let closed = false;

    const dialog = el("div", "modal aero window");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    const titleId = `modal-title-${++modalCounter}`;
    dialog.setAttribute("aria-labelledby", titleId);

    const bar = el("header", "window__bar");
    const badge = el("span", "window__icon window__icon--shield");
    badge.append(icon("i-shield"));
    const heading = el("div", "window__title");
    heading.append(el("p", "window__eyebrow", eyebrow || "QUẢN TRỊ"));
    const h2 = el("h2", "", title);
    h2.id = titleId;
    heading.append(h2);
    const close = el("button", "gel gel--glass gel--icon");
    close.type = "button";
    close.setAttribute("aria-label", "Đóng hộp thoại");
    close.append(icon("i-close"));
    bar.append(badge, heading, close);

    const body = el("div", "window__body modal__body");
    const copy = el("p", "modal__copy", description || "");
    body.append(copy);
    if (content) body.append(content);
    const error = el("p", "error modal__error");
    error.hidden = true;
    body.append(error);

    const foot = el("div", "modal__foot");
    const cancel = el("button", "gel gel--glass", "Hủy bỏ");
    cancel.type = "button";
    const confirm = el("button", `gel ${tone}`, confirmLabel || "Xác nhận");
    confirm.type = "button";
    foot.append(cancel, confirm);

    dialog.append(bar, body, foot);
    root.append(dialog);

    function closeModal() {
      if (closed) return;
      closed = true;
      root.hidden = true;
      root.replaceChildren();
      document.body.style.overflow = previousOverflow;
      root.removeEventListener("keydown", onKey);
      root.removeEventListener("click", onOverlay);
      if (previousFocus && typeof previousFocus.focus === "function") previousFocus.focus();
    }

    function onOverlay(event) {
      if (event.target === root && !busy) closeModal();
    }

    function onKey(event) {
      if (event.key === "Escape" && !busy) {
        event.preventDefault();
        closeModal();
        return;
      }
      if (event.key !== "Tab") return;
      const items = [...dialog.querySelectorAll(
        "button:not(:disabled), input:not(:disabled), textarea:not(:disabled), a[href]",
      )].filter((node) => node.offsetParent !== null);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    root.addEventListener("keydown", onKey);
    root.addEventListener("click", onOverlay);
    close.addEventListener("click", () => {
      if (!busy) closeModal();
    });
    cancel.addEventListener("click", () => {
      if (!busy) closeModal();
    });
    confirm.addEventListener("click", async () => {
      if (busy) return;
      busy = true;
      confirm.disabled = true;
      cancel.disabled = true;
      error.hidden = true;
      try {
        await onConfirm(dialog);
        closeModal();
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
        busy = false;
        confirm.disabled = false;
        cancel.disabled = false;
      }
    });

    setTimeout(() => {
      const firstField = dialog.querySelector("input, textarea");
      (firstField || confirm).focus();
    }, 20);
    return dialog;
  }

  function durationChoices() {
    const wrap = el("fieldset", "choices");
    wrap.setAttribute("aria-label", "Thời hạn");
    const options = [["60", "1 giờ"], ["1440", "24 giờ"], ["10080", "7 ngày"], ["0", "Vĩnh viễn"]];
    options.forEach(([value, label], index) => {
      const choice = el("label", "choice");
      const input = el("input");
      input.type = "radio";
      input.name = "duration";
      input.value = value;
      input.checked = index === 0;
      choice.append(input, el("span", "", label));
      wrap.append(choice);
    });
    return wrap;
  }

  function reasonField() {
    const wrap = el("div", "field");
    const label = el("label", "", "Lý do (tuỳ chọn)");
    label.htmlFor = "sanction-reason";
    const area = el("textarea");
    area.id = "sanction-reason";
    area.name = "reason";
    area.maxLength = 160;
    area.rows = 3;
    area.placeholder = "Ví dụ: gửi tin nhắn rác liên tục";
    const counter = el("span", "char-count", "0/160");
    area.addEventListener("input", () => {
      counter.textContent = `${area.value.length}/160`;
    });
    wrap.append(label, area, counter);
    return { wrap, area };
  }

  function openSanctionModal(user, kind) {
    const banning = kind === "ban";
    const choices = durationChoices();
    const reason = reasonField();
    const content = el("div");
    const durationLabel = el("p", "modal__copy", "Thời hạn áp dụng");
    content.append(durationLabel, choices, reason.wrap);
    openModal({
      eyebrow: banning ? "KIỂM DUYỆT · KHÓA TÀI KHOẢN" : "KIỂM DUYỆT · HẠN CHẾ CHAT",
      title: `${banning ? "Khóa" : "Hạn chế"} “${user.username}”`,
      description: banning
        ? "Tài khoản bị chặn đăng nhập và mọi phiên đang mở bị thu hồi ngay lập tức."
        : "Tài khoản vẫn đọc được phòng chat nhưng không thể gửi tin nhắn mới.",
      content,
      confirmLabel: banning ? "Khóa tài khoản" : "Hạn chế chat",
      tone: "gel--danger",
      onConfirm: async (dialog) => {
        const picked = dialog.querySelector('input[name="duration"]:checked');
        const minutes = !picked || picked.value === "0" ? null : Number(picked.value);
        const data = await api(`/api/admin/users/${user.id}/${kind}`, jsonOptions("POST", {
          active: true,
          minutes,
          reason: reason.area.value.trim(),
        }));
        toast(data.message || "Đã cập nhật quyền truy cập.", "ok");
        await loadAdmin({ silent: true });
      },
    });
  }

  function openRoleModal(user, promote) {
    openModal({
      eyebrow: "KIỂM DUYỆT · VAI TRÒ",
      title: promote ? `Cấp quyền quản trị cho “${user.username}”` : `Hạ quyền “${user.username}”`,
      description: promote
        ? "Quản trị viên có thể khóa tài khoản, hạn chế chat và xóa lịch sử trò chuyện của mọi thành viên."
        : "Tài khoản trở lại vai trò thành viên và mất toàn bộ quyền kiểm duyệt.",
      confirmLabel: promote ? "Cấp quyền" : "Hạ quyền",
      tone: promote ? "gel--gold" : "gel--danger",
      onConfirm: async () => {
        const data = await api(`/api/admin/users/${user.id}/role`, jsonOptions("POST", {
          role: promote ? "admin" : "member",
        }));
        toast(data.message || "Đã đổi vai trò.", "ok");
        await loadAdmin({ silent: true });
      },
    });
  }

  function openPurgeModal(user) {
    const count = user.messages || 0;
    openModal({
      eyebrow: "KIỂM DUYỆT · LỊCH SỬ CHAT",
      title: `Xóa lịch sử chat của “${user.username}”`,
      description: `${count} tin nhắn sẽ bị xóa vĩnh viễn khỏi phòng chat chung. Hành động này không thể hoàn tác.`,
      confirmLabel: "Xóa lịch sử",
      tone: "gel--danger",
      onConfirm: async () => {
        const data = await api(`/api/messages?scope=user&user_id=${user.id}`, { method: "DELETE" });
        toast(data.message || "Đã xóa lịch sử chat.", "ok");
        await afterPurge();
      },
    });
  }

  function openClearModal() {
    const count = state.admin && state.admin.stats ? state.admin.stats.messages : 0;
    openModal({
      eyebrow: "KIỂM DUYỆT · TOÀN BỘ PHÒNG CHAT",
      title: "Xóa toàn bộ lịch sử chat",
      description: `${count} tin nhắn trong phòng chat chung sẽ bị xóa vĩnh viễn. Hành động này không thể hoàn tác và áp dụng cho mọi thành viên.`,
      confirmLabel: "Xóa toàn bộ",
      tone: "gel--danger",
      onConfirm: async () => {
        const data = await api("/api/messages?scope=all", { method: "DELETE" });
        toast(data.message || "Đã xóa toàn bộ lịch sử chat.", "ok");
        await afterPurge();
      },
    });
  }

  /* ----------------------------------------------------------------- events */

  for (const id of ["login-tab", "register-tab"]) {
    $(id).addEventListener("click", () => setMode(id === "register-tab" ? "register" : "login"));
  }
  for (const id of ["auth-password", "auth-confirm"]) {
    $(id).addEventListener("input", () => $("auth-confirm").setCustomValidity(""));
  }

  $("auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.mode === "register" && $("auth-password").value !== $("auth-confirm").value) {
      $("auth-confirm").setCustomValidity("Mật khẩu nhập lại chưa khớp.");
      $("auth-confirm").reportValidity();
      return;
    }
    const submit = $("auth-submit");
    submit.disabled = true;
    $("login-tab").disabled = true;
    $("register-tab").disabled = true;
    notice("auth-error", "");
    try {
      const data = await api(`/api/${state.mode}`, jsonOptions("POST", {
        username: $("auth-name").value.trim(),
        password: $("auth-password").value,
      }));
      $("auth-form").reset();
      applySession(data);
    } catch (error) {
      notice("auth-error", error.message, true);
    } finally {
      submit.disabled = !state.csrf;
      $("login-tab").disabled = false;
      $("register-tab").disabled = false;
    }
  });

  $("logout").addEventListener("click", async () => {
    $("logout").disabled = true;
    try {
      const data = await api("/api/logout", jsonOptions("POST", {}));
      applySession(data);
      setMode("login");
    } catch (error) {
      toast(error.message, "err");
    } finally {
      $("logout").disabled = false;
    }
  });

  $("refresh-files").addEventListener("click", () => loadFiles());

  $("upload-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const file = $("upload-file").files[0];
    if (!file) {
      toast("Hãy chọn một tệp trước khi gửi.", "warn");
      return;
    }
    uploadFile(file);
  });

  $("choose-file").addEventListener("click", () => $("upload-file").click());

  $("upload-file").addEventListener("change", () => {
    const file = $("upload-file").files[0];
    $("upload-name").textContent = file ? `${file.name} · ${formatSize(file.size)}` : "Chưa chọn tệp nào";
  });

  const dropZone = $("drop-zone");
  for (const type of ["dragenter", "dragover"]) {
    dropZone.addEventListener(type, (event) => {
      event.preventDefault();
      dropZone.classList.add("is-dragging");
    });
  }
  dropZone.addEventListener("dragleave", (event) => {
    if (!dropZone.contains(event.relatedTarget)) dropZone.classList.remove("is-dragging");
  });
  dropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropZone.classList.remove("is-dragging");
    const file = event.dataTransfer && event.dataTransfer.files ? event.dataTransfer.files[0] : null;
    if (file) uploadFile(file);
  });

  $("chat-body").addEventListener("input", updateChatCounter);
  $("chat-body").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!state.sendBusy) $("chat-form").requestSubmit();
    }
  });
  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage();
  });
  $("new-messages").addEventListener("click", () => {
    $("messages").scrollTop = $("messages").scrollHeight;
    $("new-messages").hidden = true;
  });

  $("search").addEventListener("input", () => {
    state.search = $("search").value;
    applySearch();
  });

  $("admin-refresh").addEventListener("click", () => loadAdmin({ button: $("admin-refresh") }));
  $("clear-chat").addEventListener("click", () => {
    if (!state.canModerate) return;
    openClearModal();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.user && state.canModerate) loadAdmin({ silent: true });
  });

  async function start() {
    $("reconnect").hidden = true;
    $("auth-submit").disabled = true;
    try {
      const data = await api("/api/session");
      applySession(data);
    } catch (error) {
      notice("boot-status", `Không thể kết nối máy chủ LAN: ${error.message}`, true);
      $("reconnect").hidden = false;
    }
  }
  $("reconnect").addEventListener("click", () => start());

  start();
})();
