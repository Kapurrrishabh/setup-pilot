// The repo picker: paste a URL, or sign in with GitHub and pick one of your repos. The token stays
// on the server; this page sees only the code to type, the login name and this browser's key.

// Provisional: a call to this app only; the server asks GitHub no more often than GitHub allows.
const POLL_MS = 2000;
const REPO_PAGE = 50;
// Provisional: the list, and its count that is read aloud, change only once typing pauses this long.
const SEARCH_PAUSE_MS = 300;
const GITHUB_PANELS = ["gh-loading", "gh-off", "gh-signed-out", "gh-elsewhere", "gh-waiting", "gh-signed-in"];
const github = { status: null, pendingId: null, expiresAt: 0, repos: null, isComplete: true, shown: REPO_PAGE, picked: null, isSourceChosen: false };

function showPanel(id) {
  GITHUB_PANELS.forEach((panel) => ($(panel).hidden = panel !== id));
}

function showGitHubProblem(problem) {
  $("gh-problem").textContent = problem ? `${problem.message} ${problem.details || ""}`.trim() : "";
  $("gh-problem").hidden = !problem;
}

function setLink(link, url) {
  const safe = githubUrl(url);
  if (safe) link.href = safe;
  else link.removeAttribute("href");
}

function repoSource() {
  return $("repo-picker").querySelector('input[name="repo_source"]:checked').value;
}

function setSource(source) {
  $("repo-picker").querySelector(`input[name="repo_source"][value="${source}"]`).checked = true;
  $("source-url").hidden = source !== "url";
  $("source-github").hidden = source !== "github";
}

// What the form sends: {repo_url, access}, or null when nothing is picked yet.
function pickedRepo() {
  if (repoSource() === "github") {
    return github.picked && github.status?.is_this_browser ? { repo_url: github.picked, access: "github" } : null;
  }
  const typed = $("repo-url").value.trim();
  return typed ? { repo_url: typed, access: "public" } : null;
}

async function loadGitHubStatus() {
  let status;
  try {
    status = await getJson("/api/github/status");
  } catch (error) {
    showPanel("gh-signed-out");
    showGitHubProblem(error);
    return;
  }
  github.status = status;
  // A key the server no longer knows (signed out, or someone signed in after us) is useless now.
  if (!status.is_this_browser) forgetBinding();
  $("gh-notice").textContent = status.notice ?? "";
  $("gh-notice").hidden = !status.notice;
  if (!status.is_enabled) {
    $("gh-turn-on").textContent = status.turn_on;
    showPanel("gh-off");
  } else if (status.is_this_browser) {
    $("gh-login").textContent = `@${status.login}`;
    setLink($("gh-manage"), status.install_url);
    showPanel("gh-signed-in");
    if (!github.isSourceChosen) setSource("github");
    if (github.repos === null) await loadRepos();
  } else {
    github.repos = null;
    showPanel(status.is_signed_in ? "gh-elsewhere" : "gh-signed-out");
  }
}

async function loadRepos() {
  $("gh-count").textContent = "Loading your repos…";
  try {
    const data = await getJson("/api/github/repos");
    github.repos = data.repos;
    github.isComplete = data.is_complete;
    setLink($("gh-manage"), data.install_url);
  } catch (error) {
    github.repos = [];
    $("gh-count").textContent = "";
    showGitHubProblem(error);
    if (error.status === 401) {
      github.repos = null;
      await loadGitHubStatus();
    }
    return;
  }
  github.shown = REPO_PAGE;
  renderRepos();
}

function matchingRepos() {
  const query = $("gh-search").value.trim().toLowerCase();
  return (github.repos ?? []).filter((repo) => `${repo.full_name}\n${repo.description ?? ""}`.toLowerCase().includes(query));
}

const shortDate = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });

function repoRow(repo) {
  const radio = element("input");
  radio.type = "radio";
  radio.name = "gh_repo";
  radio.value = repo.url;
  radio.checked = repo.url === github.picked;
  const title = element("span", undefined, "repo-title");
  const badge = element("span", labels.visibility[repo.visibility] ?? repo.visibility, "badge");
  badge.dataset.tone = repo.visibility === "public" ? "neutral" : "info";
  title.append(element("strong", repo.full_name), badge);
  const body = element("span", undefined, "repo-body");
  body.append(title);
  if (repo.description) body.append(element("span", repo.description, "repo-desc"));
  const updated = repo.pushed_at && `Updated ${shortDate.format(new Date(repo.pushed_at))}`;
  const meta = [repo.language, updated, repo.is_archived && "Archived", repo.is_fork && "Fork"].filter(Boolean);
  if (meta.length) body.append(element("span", meta.join(" · "), "repo-meta"));
  const option = element("label", undefined, "repo-option");
  option.append(radio, body);
  const item = element("li");
  item.append(option);
  return item;
}

function renderRepos() {
  const matches = matchingRepos();
  const total = (github.repos ?? []).length;
  $("gh-repos").replaceChildren(...matches.slice(0, github.shown).map(repoRow));
  $("gh-repos").hidden = matches.length === 0;
  $("gh-more").hidden = matches.length <= github.shown;
  $("gh-empty").hidden = total > 0;
  $("gh-cut").hidden = github.isComplete;
  const word = total === 1 ? "repo" : "repos";
  if (total === 0) $("gh-count").textContent = "";
  else if (matches.length === total) $("gh-count").textContent = `${total} ${word}`;
  else $("gh-count").textContent = `${matches.length} of ${total} ${word} match`;
}

function showCode(started) {
  const link = githubUrl(started.verification_uri);
  if (!link) throw new RequestError({ message: "GitHub sent a sign-in link SetupPilot does not trust.", details: String(started.verification_uri) });
  $("gh-verify").href = link;
  $("gh-warning").textContent = started.warning;
  $("gh-user-code").textContent = started.user_code;
  // Read out one letter at a time, so a screen reader does not try to say it as a word.
  $("gh-user-code-spoken").textContent = [...started.user_code].join(" ");
  github.pendingId = started.pending_id;
  github.expiresAt = Date.now() + started.expires_in * 1000;
  showExpiry();
  showPanel("gh-waiting");
  $("gh-step-1").focus();
}

function showExpiry() {
  $("gh-expires").textContent = `Code works for ${clock(github.expiresAt - Date.now())} more`;
}

function stopWaiting(problem) {
  github.pendingId = null;
  showPanel("gh-signed-out");
  showGitHubProblem(problem);
  $("gh-sign-in").focus();
}

async function waitForGitHub(pendingId) {
  while (github.pendingId === pendingId) {
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    if (github.pendingId !== pendingId) return;
    if (Date.now() >= github.expiresAt) {
      stopWaiting({ message: "The code ran out before you finished on GitHub.", details: "Press Sign in with GitHub to get a new one." });
      return;
    }
    let result;
    try {
      result = await postJson("/api/github/signin/poll", { pending_id: pendingId });
    } catch (error) {
      stopWaiting(error);
      return;
    }
    if (result.state === "done") {
      localStorage.setItem(BINDING_STORAGE_KEY, result.binding_key);
      github.pendingId = null;
      announce(`Signed in to GitHub as ${result.login}.`);
      github.isSourceChosen = false;
      await loadGitHubStatus();
      $("gh-search").focus();
      return;
    }
    if (result.state === "failed") stopWaiting(result);
  }
}

async function signOut(button) {
  setWorking(button, true, "Signing out…");
  try {
    const done = await postJson("/api/github/signout", {});
    forgetBinding();
    github.repos = null;
    github.picked = null;
    github.pendingId = null;
    showGitHubProblem(done.warning);
    announce("Signed out of GitHub.");
    await loadGitHubStatus();
    if (!$("gh-sign-in").closest("[hidden]")) $("gh-sign-in").focus();
  } catch (error) {
    showGitHubProblem(error);
  } finally {
    setWorking(button, false);
  }
}

$("repo-picker").querySelectorAll('input[name="repo_source"]').forEach((radio) => {
  radio.addEventListener("change", () => {
    github.isSourceChosen = true;
    setSource(radio.value);
  });
});
$("gh-sign-in").addEventListener("click", async () => {
  const button = $("gh-sign-in");
  setWorking(button, true, "Getting a code…");
  showGitHubProblem(null);
  try {
    showCode(await postJson("/api/github/signin", {}));
  } catch (error) {
    showGitHubProblem(error);
    return;
  } finally {
    setWorking(button, false);
  }
  announce("Your sign-in code is ready. Copy it, then open github.com/login/device.");
  waitForGitHub(github.pendingId);
});
$("gh-copy").addEventListener("click", () => copyText($("gh-user-code").textContent, $("gh-user-code")));
// Cancelling forgets the waiting code on the server too, so it cannot finish later.
$("gh-cancel").addEventListener("click", () => signOut($("gh-cancel")));
$("gh-sign-out").addEventListener("click", () => signOut($("gh-sign-out")));
$("gh-sign-out-elsewhere").addEventListener("click", () => signOut($("gh-sign-out-elsewhere")));
$("gh-refresh").addEventListener("click", () => loadRepos());
$("gh-more").addEventListener("click", () => {
  github.shown += REPO_PAGE;
  renderRepos();
});
let searchTimer = null;
$("gh-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    github.shown = REPO_PAGE;
    renderRepos();
  }, SEARCH_PAUSE_MS);
});
$("gh-repos").addEventListener("change", (event) => {
  if (event.target.name === "gh_repo") github.picked = event.target.value;
});
setInterval(() => {
  if (github.pendingId !== null) showExpiry();
}, 1000);
// Signing in or out in another tab shows here when you come back. Waiting has its own poll.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && github.pendingId === null && github.status?.is_enabled) loadGitHubStatus();
});
loadGitHubStatus();
