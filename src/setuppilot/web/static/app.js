// Shared helpers. Text from repos and commands is always shown with textContent, never as HTML.
const $ = (id) => document.getElementById(id);
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
// Only pages with their own script have labels.
const labels = JSON.parse($("page-labels")?.textContent ?? "null");
// Short details read best as a sentence under the message; long ones go in a "Show details" box.
// Provisional: 200 characters is a guess, not measured.
const SHORT_DETAILS = 200;
// The GitHub sign-in's key for this browser. It lives only here and in the header, never in a page.
const BINDING_STORAGE_KEY = "setuppilot-github-binding";
const BINDING_HEADER = "X-SetupPilot-Binding";

class RequestError extends Error {
  constructor({ message, details = "" }, status = 0) {
    super(message);
    this.details = details;
    this.status = status;
  }
}

function bindingHeaders() {
  const key = localStorage.getItem(BINDING_STORAGE_KEY);
  return key ? { [BINDING_HEADER]: key } : {};
}

function forgetBinding() {
  localStorage.removeItem(BINDING_STORAGE_KEY);
}

async function readJson(url, options = {}) {
  const sent = bindingHeaders();
  let response;
  try {
    response = await fetch(url, { ...options, headers: { "X-CSRF-Token": csrfToken, ...sent, ...options.headers } });
  } catch (error) {
    throw new RequestError({ message: "Could not reach SetupPilot. Is it still running?", details: String(error) });
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    // The server no longer takes this key: the sign-in ended, or a newer one replaced it.
    if (response.status === 401 && BINDING_HEADER in sent) forgetBinding();
    throw new RequestError(data.error || { message: `Request failed (${response.status}).` }, response.status);
  }
  return data;
}

function getJson(url) {
  return readJson(url);
}

async function postJson(url, body) {
  const data = await readJson(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  hideError();
  return data;
}

// Links built from server data are followed only when they point at github.com.
function githubUrl(url) {
  return typeof url === "string" && url.startsWith("https://github.com/") ? url : null;
}

// owner/name, as the repo_name filter shows it.
function repoNameOf(url) {
  return url.replace(/^https:\/\/github\.com\//, "");
}

function isShortDetails(details) {
  return details.length <= SHORT_DETAILS && !details.includes("\n");
}

function showError(error) {
  const box = document.getElementById("error");
  const details = error.details || "";
  const message = document.getElementById("error-message");
  // The run page sends the same problem with every update; saying it again would repeat it aloud.
  const shown = `${error.message}\n${details}`;
  if (!box.hidden && box.dataset.shown === shown) return;
  box.dataset.shown = shown;
  box.hidden = false;
  message.textContent = error.message;
  const isShort = isShortDetails(details);
  document.getElementById("error-fix").textContent = isShort ? details : "";
  document.getElementById("error-fix").hidden = !isShort || !details;
  document.getElementById("error-details").textContent = isShort ? "" : details;
  document.getElementById("error-more").hidden = isShort;
}

function hideError() {
  document.getElementById("error").hidden = true;
}

function announce(text) {
  document.getElementById("announce").textContent = text;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

// Messages mark commands with backticks; show those parts as code, still as plain text.
function withCode(text) {
  return text.split("`").map((part, index) => (index % 2 ? element("code", part) : part));
}

function setWorking(button, isWorking, workingText) {
  if (isWorking) {
    button.dataset.idleText = button.textContent;
    button.textContent = workingText;
  } else if (button.dataset.idleText) {
    button.textContent = button.dataset.idleText;
  }
  button.disabled = isWorking;
  button.classList.toggle("is-working", isWorking);
}

function clock(ms) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

// When the browser refuses to copy, select the text so the reader can copy it by hand.
async function copyText(text, shownIn) {
  try {
    await navigator.clipboard.writeText(text);
    announce("Copied.");
  } catch {
    window.getSelection().selectAllChildren(shownIn);
    announce("Copying was not allowed, so the text is selected. Press Ctrl+C, or Command+C on a Mac.");
  }
}

// Times come from the server in UTC; show them in the reader's own time zone.
const localTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

function showLocalTime(node) {
  const moment = new Date(node.dateTime);
  if (Number.isNaN(moment.getTime())) return;
  node.title = node.textContent;
  node.textContent = localTime.format(moment);
}

document.querySelectorAll("time[datetime]").forEach(showLocalTime);
document.getElementById("error-close").addEventListener("click", hideError);
