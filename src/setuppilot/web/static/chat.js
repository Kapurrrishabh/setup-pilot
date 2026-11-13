const messages = document.getElementById("messages");
const askForm = document.getElementById("ask-form");
const askFields = document.getElementById("ask-fields");
const indexForm = document.getElementById("index-form");
const repoSelect = document.getElementById("repo");
const examples = document.getElementById("examples");
// Without a Groq key the page starts with asking turned off, and it stays off.
const canAsk = !askFields.disabled;
let tokens = 0;
let answers = 0;

function addMessage(kind, text) {
  const box = element("div", undefined, `message ${kind}`);
  box.append(element("p", text));
  messages.append(box);
  messages.scrollTop = messages.scrollHeight;
  return box;
}

// Citation links come from the server, but only github.com links are followed.
function citationChip(citation) {
  const lines = citation.start_line === citation.end_line ? `${citation.start_line}` : `${citation.start_line}-${citation.end_line}`;
  // The repo card (a summary of the manifests) has no line numbers.
  const label = citation.start_line ? `${citation.path}:${lines}` : citation.path;
  const url = githubUrl(citation.url);
  if (!url) return element("span", label, "chip");
  const link = element("a", label, "chip");
  link.href = url;
  link.target = "_blank";
  link.rel = "noreferrer";
  return link;
}

// "Run it [1]." becomes text with a small link to source 1 below the answer.
function withCitations(text, citations, prefix) {
  const known = new Set(citations.map((citation) => citation.n));
  return text.split(/(\[\d+\])/).flatMap((part) => {
    const n = Number(part.slice(1, -1));
    if (!/^\[\d+\]$/.test(part) || !known.has(n)) return withCode(part);
    const link = element("a", part, "cite-ref");
    link.href = `#${prefix}-s${n}`;
    link.setAttribute("aria-label", `source ${n}`);
    return link;
  });
}

function citationList(citations, prefix) {
  const list = element("ol", undefined, "citations");
  list.setAttribute("aria-label", "Sources");
  citations.forEach((citation) => {
    const item = element("li");
    item.id = `${prefix}-s${citation.n}`;
    item.tabIndex = -1;
    item.append(element("span", `[${citation.n}]`, "cite-number"), citationChip(citation));
    if (citation.quote) {
      const lines = element("details");
      lines.append(element("summary", "Show the lines"), element("pre", citation.quote, "output"));
      item.append(lines);
    }
    list.append(item);
  });
  return list;
}

function addAnswer(answer) {
  const prefix = `a${++answers}`;
  if (answer.status === "answered") {
    const box = element("div", undefined, "message bot");
    const text = element("p");
    text.append(...withCitations(answer.text, answer.citations, prefix));
    box.append(text, citationList(answer.citations, prefix));
    messages.append(box);
  } else {
    // Not found, small talk and out of scope are plain text; "not found" also says where it looked.
    const box = addMessage(answer.status === "not_found" ? "bot not_found" : "bot", answer.text);
    if (answer.looked_at.length) {
      const chips = element("div", undefined, "chips");
      chips.append(element("span", "I looked in:", "chips-label"), ...answer.looked_at.map((path) => element("span", path, "chip")));
      box.append(chips);
    }
  }
  messages.scrollTop = messages.scrollHeight;
}

function addProblem(prefix, error) {
  const box = addMessage("error", `${prefix} ${error.message}`);
  if (error.details) box.append(element("p", error.details, "message-details"));
}

function setAsking(isAsking) {
  askFields.disabled = isAsking || !canAsk;
  examples.querySelectorAll("button").forEach((button) => (button.disabled = isAsking || !canAsk));
}

function addRepoOption(key, isPrivate) {
  [...repoSelect.options].filter((option) => option.value === "").forEach((option) => option.remove());
  const label = isPrivate ? `${key} (private)` : key;
  const found = [...repoSelect.options].find((option) => option.value === key);
  if (found) found.textContent = label;
  else repoSelect.prepend(new Option(label, key));
}

// Private repos come only to the browser that signed in, so the page itself never lists them.
async function addPrivateRepos() {
  if (!localStorage.getItem(BINDING_STORAGE_KEY)) return;
  try {
    const listed = await getJson("/api/chat/repos");
    listed.private.forEach((key) => addRepoOption(key, true));
  } catch (error) {
    if (error.status !== 401) showError(error);
  }
}

askForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = askForm.elements.question;
  const question = input.value.trim();
  const repo = repoSelect.value;
  if (!repo) {
    addMessage("note", "Add a repo first, using the box above. Then ask your question here.");
    document.getElementById("repo-url").focus();
    return;
  }
  if (!question) return;
  addMessage("you", question);
  input.value = "";
  const pending = addMessage("pending", "Looking in the repo's files…");
  setAsking(true);
  try {
    const answer = await postJson("/api/chat/ask", { repo, question });
    pending.remove();
    addAnswer(answer);
    tokens += answer.tokens;
    document.getElementById("tokens").textContent = tokens.toLocaleString();
    examples.querySelector(".examples-label").textContent = "Ask next:";
  } catch (error) {
    pending.remove();
    addProblem("Could not answer:", error);
    input.value = question;
  } finally {
    setAsking(false);
    input.focus();
  }
});

examples.querySelectorAll("button").forEach((button) => {
  button.addEventListener("click", () => {
    askForm.elements.question.value = button.textContent;
    askForm.requestSubmit();
  });
});

repoSelect.addEventListener("change", () => {
  messages.replaceChildren();
  addMessage("note", `Now asking about ${repoSelect.value}. Earlier questions do not carry over.`);
  examples.querySelector(".examples-label").textContent = "Try one:";
});

indexForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.getElementById("index-button");
  const problem = document.getElementById("index-error");
  problem.hidden = true;
  const repo = pickedRepo();
  if (!repo) {
    problem.textContent = repoSource() === "github" ? "Pick one of your repos from the list first." : "Paste the repo's GitHub URL first.";
    problem.hidden = false;
    return;
  }
  setWorking(button, true, "Indexing…");
  try {
    const data = await postJson("/api/chat/index", repo);
    addRepoOption(data.repo, false);
    // Only the server knows whether a repo read through the sign-in is private.
    if (repo.access === "github") await addPrivateRepos();
    repoSelect.value = data.repo;
    const cut = data.dropped ? ` ${data.dropped} files were left out because the repo is large.` : "";
    addMessage("note", `Indexed ${data.repo}: ${data.chunks} parts of its files are ready.${cut} Ask a question below.`);
    document.getElementById("repo-url").value = "";
  } catch (error) {
    problem.textContent = error.details ? `${error.message} ${error.details}` : error.message;
    problem.hidden = false;
  } finally {
    setWorking(button, false);
    // The button was turned off while it had focus, so focus fell to the page.
    if (document.activeElement === document.body) button.focus();
  }
});

const wantedRepo = new URLSearchParams(window.location.search).get("index");
if (wantedRepo) {
  github.isSourceChosen = true;
  setSource("url");
  document.getElementById("repo-url").value = wantedRepo;
}
addPrivateRepos();
