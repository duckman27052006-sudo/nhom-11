const input = document.querySelector("#file-input");
const dropzone = document.querySelector("#dropzone");
const selection = document.querySelector("#selection");
const list = document.querySelector("#file-list");
const sendButton = document.querySelector("#send-button");
const clearButton = document.querySelector("#clear-files");
const statusMessage = document.querySelector("#status-message");
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const maxBytes = Number(document.querySelector('meta[name="max-file-size"]').content);
let selectedFiles = [];

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = units[0];
  for (let index = 1; value >= 1024 && index < units.length; index += 1) {
    value /= 1024;
    unit = units[index];
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${unit}`;
}

function showStatus(message, isError = false) {
  statusMessage.textContent = message;
  statusMessage.classList.toggle("error", isError);
  statusMessage.hidden = false;
}

function renderFiles() {
  list.replaceChildren();
  for (const [index, file] of selectedFiles.entries()) {
    const row = document.createElement("li");
    row.className = "file-row";

    const icon = document.createElement("span");
    icon.className = "file-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = file.name.split(".").pop().slice(0, 4) || "FILE";

    const meta = document.createElement("span");
    meta.className = "file-meta";
    const name = document.createElement("span");
    name.className = "file-name";
    name.textContent = file.name;
    const size = document.createElement("span");
    size.className = "file-size";
    size.textContent = formatSize(file.size);
    meta.append(name, size);

    const state = document.createElement("span");
    state.className = "file-state";
    state.dataset.index = String(index);
    state.textContent = file.size > maxBytes ? "QUÁ CỠ" : "CHỜ GỬI";
    if (file.size > maxBytes) state.classList.add("error");

    row.append(icon, meta, state);
    list.append(row);
  }
  selection.hidden = selectedFiles.length === 0;
  dropzone.hidden = selectedFiles.length > 0;
}

function addFiles(files) {
  const available = Array.from(files);
  if (!available.length) return;
  selectedFiles = [...selectedFiles, ...available];
  statusMessage.hidden = true;
  renderFiles();
}

input.addEventListener("change", (event) => {
  addFiles(event.target.files);
  input.value = "";
});

dropzone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    input.click();
  }
});

for (const eventName of ["dragenter", "dragover"]) {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.add("is-dragging");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-dragging");
  });
}
dropzone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));

clearButton.addEventListener("click", () => {
  selectedFiles = [];
  statusMessage.hidden = true;
  renderFiles();
});

sendButton.addEventListener("click", async () => {
  const tooLarge = selectedFiles.some((file) => file.size > maxBytes);
  if (tooLarge) {
    showStatus(`Bỏ các tệp vượt giới hạn ${formatSize(maxBytes)} rồi thử lại.`, true);
    return;
  }
  sendButton.disabled = true;
  let succeeded = 0;
  let failed = 0;
  const retryFiles = [];

  for (const [index, file] of selectedFiles.entries()) {
    const state = list.querySelector(`[data-index="${index}"]`);
    state.textContent = "ĐANG GỬI";
    state.classList.remove("error");
    const body = new FormData();
    body.append("file", file, file.name);
    try {
      const response = await fetch("/api/upload", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
        body,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Không gửi được tệp.");
      state.textContent = "ĐÃ NHẬN";
      succeeded += 1;
    } catch (error) {
      state.textContent = "LỖI";
      state.classList.add("error");
      state.title = error.message;
      retryFiles.push(file);
      failed += 1;
    }
  }

  sendButton.disabled = false;
  selectedFiles = retryFiles;
  if (failed === 0) {
    showStatus(`Đã gửi thành công ${succeeded} tệp.`);
    window.setTimeout(() => {
      statusMessage.hidden = true;
      renderFiles();
    }, 3500);
  } else {
    showStatus(`Đã gửi ${succeeded} tệp; ${failed} tệp lỗi. Di chuột lên trạng thái lỗi để xem chi tiết.`, true);
    renderFiles();
  }
});
