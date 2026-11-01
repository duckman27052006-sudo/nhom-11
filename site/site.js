const STORAGE_KEY = "tftp-lan-upload-url";
const form = document.querySelector("#lan-form");
const input = document.querySelector("#lan-url");
const error = document.querySelector("#address-error");
const savedAddress = document.querySelector("#saved-address");
const clearButton = document.querySelector("#clear-address");

function isPrivateIpv4(hostname) {
  const octets = hostname.split(".");
  if (octets.length !== 4 || octets.some((part) => !/^\d{1,3}$/.test(part))) return false;
  const [first, second, third, fourth] = octets.map(Number);
  if ([first, second, third, fourth].some((part) => part > 255)) return false;
  return first === 10
    || first === 127
    || (first === 172 && second >= 16 && second <= 31)
    || (first === 192 && second === 168)
    || (first === 169 && second === 254);
}

function isLanAddress(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return false;

  const hostname = url.hostname.toLowerCase().replace(/\.$/, "");
  const unwrapped = hostname.replace(/^\[|\]$/g, "");
  if (isPrivateIpv4(unwrapped)) return true;
  if (unwrapped === "localhost" || unwrapped === "::1") return true;
  if (/^(fc|fd)[0-9a-f]{2}:/i.test(unwrapped) || /^fe[89ab][0-9a-f]:/i.test(unwrapped)) {
    return true;
  }
  return [".lan", ".local", ".internal", ".home.arpa"].some((suffix) =>
    hostname.endsWith(suffix)
  );
}

function setError(message) {
  error.textContent = message;
  error.hidden = false;
  input.setAttribute("aria-invalid", "true");
}

function clearError() {
  error.textContent = "";
  error.hidden = true;
  input.removeAttribute("aria-invalid");
}

try {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored && isLanAddress(stored)) {
    input.value = stored;
    savedAddress.hidden = false;
  } else if (stored) {
    localStorage.removeItem(STORAGE_KEY);
  }
} catch {
  // The form remains usable when browser storage is disabled.
}

input.addEventListener("input", clearError);

form.addEventListener("submit", (event) => {
  event.preventDefault();
  clearError();
  const destination = input.value.trim();

  if (!destination || !isLanAddress(destination)) {
    setError("Nhập URL HTTP(S) của máy chủ trong LAN, ví dụ http://192.168.1.10:8080.");
    return;
  }

  try {
    localStorage.setItem(STORAGE_KEY, destination);
  } catch {
    // Continue to the upload portal even if the browser blocks storage.
  }
  window.location.assign(new URL(destination).href);
});

clearButton.addEventListener("click", () => {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // The address is still cleared from the visible form.
  }
  input.value = "";
  savedAddress.hidden = true;
  clearError();
  input.focus();
});
