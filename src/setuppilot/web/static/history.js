// Without JavaScript every public run still shows.
const tbody = document.querySelector("table.runs tbody");
let rows = [...tbody.querySelectorAll("tr")];
const filters = [...document.querySelectorAll("#runs-tools .filter")];
const search = document.getElementById("runs-search");
const PAGE_SIZE = 50;
let wantedStatus = "";
let shown = PAGE_SIZE;

function apply() {
  const query = search.value.trim().toLowerCase();
  const matches = rows.filter(
    (row) => (!wantedStatus || row.dataset.status === wantedStatus) && row.dataset.repo.includes(query)
  );
  rows.forEach((row) => (row.hidden = true));
  matches.slice(0, shown).forEach((row) => (row.hidden = false));
  filters.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.status === wantedStatus)));
  const runs = rows.length === 1 ? "run" : "runs";
  const total = rows.length === matches.length ? `${rows.length} ${runs}` : `${matches.length} of ${rows.length} ${runs}`;
  const cut = matches.length > shown ? `, showing the first ${shown}` : "";
  document.getElementById("runs-count").textContent = total + cut;
  document.getElementById("runs-none").hidden = matches.length > 0;
  document.getElementById("runs-more").hidden = matches.length <= shown;
}

function choose(status) {
  wantedStatus = status;
  shown = PAGE_SIZE;
  apply();
}

// The same cells history.html draws for a public run.
function signedInRow(run) {
  const row = element("tr");
  const repo = repoNameOf(run.repo_url);
  Object.assign(row.dataset, { status: run.status, repo: repo.toLowerCase(), created: run.created_at });
  const link = element("a", repo);
  link.href = `/runs/${encodeURIComponent(run.id)}`;
  link.title = run.repo_url;
  const badge = element("span", labels.statuses[run.status], "badge");
  badge.dataset.status = run.status;
  const when = element("time", run.created_at);
  when.dateTime = run.created_at;
  showLocalTime(when);
  const tokens = element("td", undefined, "num tokens");
  tokens.append(element("span", "Tokens ", "cell-label"), run.tokens.toLocaleString());
  const problem = element("td", undefined, "problem");
  if (run.error_type && run.status !== "cancelled") {
    const label = element("span", labels.errors[run.error_type] ?? run.error_type);
    label.title = run.message ?? "";
    problem.append(label);
  }
  const cells = [element("td", undefined, "repo"), element("td", undefined, "result"), element("td", undefined, "when")];
  cells[0].append(link, element("span", "Signed-in run", "badge"));
  cells[1].append(badge);
  cells[2].append(when);
  const took = element("td", undefined, "num took");
  took.append(run.took === null ? element("span", "Not finished", "muted") : run.took);
  row.append(...cells, took, tokens, problem);
  return row;
}

function countStatuses() {
  filters.forEach((button) => {
    const count = button.dataset.status ? rows.filter((row) => row.dataset.status === button.dataset.status).length : rows.length;
    button.querySelector(".count").textContent = count;
    button.hidden = button.dataset.status !== "" && count === 0;
  });
}

async function addSignedInRuns() {
  // Only the browser that signed in has a key; others would only get "sign in first".
  if (!localStorage.getItem(BINDING_STORAGE_KEY)) return;
  let runs;
  try {
    runs = (await getJson("/api/github/runs")).runs;
  } catch (error) {
    if (error.status !== 401) showError(error);
    return;
  }
  if (runs.length === 0) return;
  rows = [...rows, ...runs.map(signedInRow)].sort((a, b) => b.dataset.created.localeCompare(a.dataset.created));
  tbody.replaceChildren(...rows);
  document.getElementById("runs-signed-in").hidden = false;
}

async function start() {
  await addSignedInRuns();
  document.getElementById("runs-empty").hidden = rows.length > 0;
  if (!rows.length) return;
  document.getElementById("runs-table").hidden = false;
  document.getElementById("runs-count").hidden = false;
  document.getElementById("runs-tools").hidden = false;
  countStatuses();
  filters.forEach((button) => button.addEventListener("click", () => choose(button.dataset.status)));
  search.addEventListener("input", () => choose(wantedStatus));
  document.getElementById("runs-more").addEventListener("click", () => {
    shown += PAGE_SIZE;
    apply();
  });
  document.getElementById("runs-clear").addEventListener("click", () => {
    search.value = "";
    choose("");
    search.focus();
  });
  const fromLink = new URLSearchParams(window.location.search).get("status") || "";
  choose(filters.some((button) => button.dataset.status === fromLink && !button.hidden) ? fromLink : "");
}

start();
