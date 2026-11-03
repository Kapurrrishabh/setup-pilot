const messages = document.getElementById("messages");
let tokens = 0;

function addMessage(who, text, sources) {
  const box = element("div", "", `message ${who}`);
  box.append(element("p", text));
  if (sources && sources.length) {
    const chips = element("div", "", "chips");
    sources.forEach((source) => chips.append(element("span", source, "chip")));
    box.append(chips);
  }
  messages.append(box);
  messages.scrollTop = messages.scrollHeight;
}

document.getElementById("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const question = form.elements.question.value;
  const repo = document.getElementById("repo").value;
  if (!repo) return;
  addMessage("you", question);
  form.elements.question.value = "";
  try {
    const answer = await postJson("/api/chat/ask", { repo, question });
    addMessage("bot", answer.text, answer.sources);
    tokens += answer.tokens;
    document.getElementById("tokens").textContent = tokens;
  } catch {}
});

document.getElementById("index-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector("button");
  button.disabled = true;
  button.textContent = "Indexing… (can take a minute)";
  try {
    const data = await postJson("/api/chat/index", { repo_url: form.elements.repo_url.value });
    const select = document.getElementById("repo");
    if (![...select.options].some((o) => o.value === data.repo)) select.prepend(new Option(data.repo, data.repo));
    select.value = data.repo;
    const cut = data.dropped ? ` ${data.dropped} more were left out to keep the database small.` : "";
    addMessage("bot", `Indexed ${data.repo} (${data.chunks} chunks).${cut} Ask away.`);
  } catch {}
  button.disabled = false;
  button.textContent = "Index this repo";
});
