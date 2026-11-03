// Without JavaScript every row still shows.
const rows = [...document.querySelectorAll("table.runs tbody tr")];
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

if (rows.length) {
  document.getElementById("runs-tools").hidden = false;
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
  choose(filters.some((button) => button.dataset.status === fromLink) ? fromLink : "");
}
