// Shared helpers. Text from repos and commands is always shown with textContent, never as HTML.
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    showError(data.error || { message: `Request failed (${response.status}).`, details: "" });
    throw new Error("request failed");
  }
  hideError();
  return data;
}

function showError(error) {
  document.getElementById("error-message").textContent = error.message;
  document.getElementById("error-details").textContent = error.details || "";
  document.getElementById("error").hidden = false;
}

function hideError() {
  document.getElementById("error").hidden = true;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
