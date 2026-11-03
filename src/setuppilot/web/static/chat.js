const messages = document.getElementById("messages");
const askForm = document.getElementById("ask-form");
const askFields = document.getElementById("ask-fields");
const indexForm = document.getElementById("index-form");
const repoSelect = document.getElementById("repo");
const examples = document.getElementById("examples");
// Without a Groq key the page starts with asking turned off, and it stays off.
const canAsk = !askFields.disabled;
let tokens = 0;

function addMessage(kind, text) {
  const box = element("div", undefined, `message ${kind}`);
  box.append(element("p", text));
  messages.append(box);
  messages.scrollTop = messages.scrollHeight;
  return box;
}

function sourceLink(repo, path) {
  const link = element("a", path, "chip");
  link.href = `https://github.com/${repo}/blob/HEAD/${path.split("/").map(encodeURIComponent).join("/")}`;
  link.target = "_blank";
  link.rel = "noreferrer";
  return link;
}

function addAnswer(answer, repo) {
  const box = addMessage("bot", answer.text);
  if (answer.sources.length) {
    const chips = element("div", undefined, "chips");
    chips.append(element("span", "Sources:", "chips-label"), ...answer.sources.map((path) => sourceLink(repo, path)));
    box.append(chips);
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

askForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = askForm.elements.question;
  const question = input.value.trim();
  const repo = repoSelect.value;
  if (!repo) {
    addMessage("note", "Add a repo first, using the box above. Then ask your question here.");
    indexForm.elements.repo_url.focus();
    return;
  }
  if (!question) return;
  addMessage("you", question);
  input.value = "";
  examples.hidden = true;
  const pending = addMessage("pending", "Looking in the repo's files…");
  setAsking(true);
  try {
    const answer = await postJson("/api/chat/ask", { repo, question });
    pending.remove();
    addAnswer(answer, repo);
    tokens += answer.tokens;
    document.getElementById("tokens").textContent = tokens.toLocaleString();
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
  addMessage("note", `Now asking about ${repoSelect.value}.`);
  examples.hidden = false;
});

indexForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = indexForm.querySelector("button");
  const problem = document.getElementById("index-error");
  problem.hidden = true;
  setWorking(button, true, "Indexing…");
  try {
    const data = await postJson("/api/chat/index", { repo_url: indexForm.elements.repo_url.value });
    [...repoSelect.options].filter((option) => option.value === "").forEach((option) => option.remove());
    if (![...repoSelect.options].some((option) => option.value === data.repo)) repoSelect.prepend(new Option(data.repo, data.repo));
    repoSelect.value = data.repo;
    const cut = data.dropped ? ` ${data.dropped} more were left out because the repo is large.` : "";
    addMessage("note", `Indexed ${data.repo}: ${data.chunks} parts of its files are ready.${cut} Ask a question below.`);
    indexForm.reset();
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
if (wantedRepo) indexForm.elements.repo_url.value = wantedRepo;
