// Shared helpers. Text from repos and commands is always shown with textContent, never as HTML.
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
// Short details read best as a sentence under the message; long ones go in a "Show details" box.
// Provisional: 200 characters is a guess, not measured.
const SHORT_DETAILS = 200;

class RequestError extends Error {
  constructor({ message, details = "" }) {
    super(message);
    this.details = details;
  }
}

async function postJson(url, body) {
  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify(body),
    });
  } catch (error) {
    throw new RequestError({ message: "Could not reach SetupPilot. Is it still running?", details: String(error) });
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new RequestError(data.error || { message: `Request failed (${response.status}).` });
  }
  hideError();
  return data;
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

// Times come from the server in UTC; show them in the reader's own time zone.
const localTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
document.querySelectorAll("time[datetime]").forEach((node) => {
  const moment = new Date(node.dateTime);
  if (Number.isNaN(moment.getTime())) return;
  node.title = node.textContent;
  node.textContent = localTime.format(moment);
});

document.getElementById("error-close").addEventListener("click", hideError);
