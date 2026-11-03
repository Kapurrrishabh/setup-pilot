const form = document.getElementById("setup-form");
const checks = document.getElementById("preflight");
const dockerBox = form.elements.use_docker;
const FOLDER_KEY = "setuppilot.folder";
// Wait until typing pauses: each check reaches GitHub and asks Docker, which takes a moment.
// Provisional: 500 ms is a guess, not measured.
const TYPING_PAUSE_MS = 500;
let latestChecks = [];
let preflightNumber = 0;
let typingTimer = null;

function checkState(check) {
  if (check.is_ok) return "ok";
  const isNeeded = check.is_needed || (check.id === "docker" && dockerBox.checked);
  return isNeeded ? "bad" : "optional";
}

const STATE_WORDS = { ok: "Ready", bad: "Needs attention", optional: "Optional" };

function renderChecks() {
  const states = latestChecks.map(checkState);
  checks.replaceChildren(
    ...latestChecks.map((check, index) => {
      const state = states[index];
      const item = element("li", undefined, state);
      const body = element("div", undefined, "check-body");
      const title = element("span", undefined, "check-title");
      title.append(element("span", check.name, "check-name"));
      if (state === "ok") title.prepend(element("span", `${STATE_WORDS.ok}: `, "visually-hidden"));
      else {
        const badge = element("span", STATE_WORDS[state], "badge");
        badge.dataset.tone = state === "bad" ? "bad" : "neutral";
        title.append(badge);
      }
      body.append(title);
      if (check.fix) body.append(element("p", check.fix, "hint"));
      item.append(body);
      return item;
    })
  );
  const blockers = states.filter((state) => state === "bad").length;
  const extras = states.filter((state) => state === "optional").length;
  const summary = document.getElementById("preflight-summary");
  summary.className = `summary ${blockers ? "bad" : "ok"}`;
  if (blockers) {
    summary.textContent = `${blockers} ${blockers === 1 ? "thing needs" : "things need"} attention before you start.`;
  } else {
    summary.textContent = extras ? "Ready to start. Some optional extras are off." : "Everything is ready.";
  }
  const docker = latestChecks.find((check) => check.id === "docker");
  document.getElementById("docker-problem").hidden = !(dockerBox.checked && docker && !docker.is_ok);
}

async function preflight() {
  const number = ++preflightNumber;
  const folder = form.elements.dest_dir.value.trim();
  const summary = document.getElementById("preflight-summary");
  summary.className = "summary";
  summary.textContent = "Checking…";
  try {
    const response = await fetch(`/api/preflight?folder=${encodeURIComponent(folder)}`, {
      headers: { "X-CSRF-Token": csrfToken },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new RequestError(data.error || { message: `Pre-flight check failed (${response.status}).` });
    // A slow answer to an older check must not replace a newer one.
    if (number !== preflightNumber) return;
    latestChecks = data.checks;
    renderChecks();
  } catch (error) {
    if (number !== preflightNumber) return;
    summary.className = "summary bad";
    summary.textContent = "The check could not run.";
    latestChecks = [];
    checks.replaceChildren();
    showError(error);
  }
}

form.elements.dest_dir.addEventListener("input", () => {
  clearTimeout(typingTimer);
  typingTimer = setTimeout(preflight, TYPING_PAUSE_MS);
});
dockerBox.addEventListener("change", () => latestChecks.length && renderChecks());
document.getElementById("preflight-again").addEventListener("click", preflight);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.getElementById("start");
  setWorking(button, true, "Starting…");
  try {
    const data = await postJson("/api/runs", {
      repo_url: form.elements.repo_url.value,
      dest_dir: form.elements.dest_dir.value,
      use_docker: dockerBox.checked,
    });
    localStorage.setItem(FOLDER_KEY, form.elements.dest_dir.value.trim());
    window.location = `/runs/${data.run_id}`;
  } catch (error) {
    setWorking(button, false);
    showError(error);
    document.getElementById("error").focus();
  }
});

const wantedRepo = new URLSearchParams(window.location.search).get("repo");
if (wantedRepo) form.elements.repo_url.value = wantedRepo;
form.elements.dest_dir.value ||= localStorage.getItem(FOLDER_KEY) || "";
preflight();
