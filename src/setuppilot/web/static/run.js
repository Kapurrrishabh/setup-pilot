// The live run page. Everything comes from the server's saved view, so a refresh loses nothing.
const runId = document.getElementById("run").dataset.runId;
const $ = (id) => document.getElementById(id);
let questionId = null;

function render(view) {
  $("repo").textContent = view.run.repo_url;
  $("status").textContent = view.run.status;
  $("busy").hidden = !view.is_busy;
  $("steps").replaceChildren(
    ...view.steps.map((step) => {
      const item = element("li", "", step.is_ok ? "ok" : "bad");
      item.append(element("strong", step.name), element("span", ` ${step.result_summary}`));
      item.append(element("span", `  ${step.duration_ms} ms${step.tokens ? `, ${step.tokens} tokens` : ""}`, "muted"));
      return item;
    })
  );
  if (view.problem) showError(view.problem);
  questionId = view.question_id;
  renderQuestion(view.question);
  $("stopped").hidden = !view.can_carry_on;
  $("report-card").hidden = view.report === null;
  if (view.report !== null) {
    $("report").textContent = view.report;
    $("report-title").textContent = view.is_verified ? "Report: setup verified" : "Report: NOT verified";
  }
  const isOver = view.report !== null || view.run.status === "cancelled";
  $("cancel").hidden = isOver;
  if (view.error) showError(view.error);
  if (isOver) events.close();
}

function renderQuestion(question) {
  $("question").hidden = question === null;
  if (question === null) return;
  const isChoice = question.kind === "choose_stack";
  $("question-actions").hidden = isChoice;
  $("edit-box").hidden = true;
  $("question-options").replaceChildren();
  if (isChoice) {
    $("question-title").textContent = "Which way should this repo be set up?";
    $("question-reason").textContent = question.conflicts.join(" ");
    $("question-command").textContent = "";
    $("question-warning").textContent = "";
    question.options.forEach((option, index) => {
      const missing = option.prereqs.filter((c) => !c.is_installed).map((c) => c.tool);
      const label = `${option.label} (from ${option.evidence.join(", ")})${missing.length ? ` — missing: ${missing.join(", ")}` : ""}`;
      const button = element("button", label, "option");
      button.addEventListener("click", () => send({ choice: index }));
      $("question-options").append(button);
    });
    return;
  }
  $("question-title").textContent = `Step ${question.step}: approve this command?`;
  $("question-reason").textContent = question.reason + (question.in_docker ? " (runs in Docker)" : "");
  $("question-command").textContent = question.command;
  $("question-warning").textContent = question.warning;
  $("question-edit-error").hidden = !question.edit_error;
  $("question-edit-error").textContent = question.edit_error ? `Your edit was blocked: ${question.edit_error}` : "";
  $("edit-command").value = question.command;
}

async function send(reply) {
  $("question").hidden = true;
  try {
    await postJson(`/api/runs/${runId}/answer`, { ...reply, question_id: questionId });
  } catch {
    $("question").hidden = false;
  }
}

document.querySelectorAll("#question-actions button").forEach((button) => {
  button.addEventListener("click", () => {
    if (button.dataset.action === "edit") {
      $("edit-box").hidden = false;
      $("edit-command").focus();
    } else {
      send({ action: button.dataset.action });
    }
  });
});
$("edit-send").addEventListener("click", () => send({ action: "edit", command: $("edit-command").value }));
$("carry-on").addEventListener("click", () => {
  if (confirm("Carry on? The last approved command may run again.")) {
    postJson(`/api/runs/${runId}/continue`, {}).catch(() => {});
  }
});
$("cancel").addEventListener("click", () => {
  if (confirm("Stop this run? A running command is stopped too.")) {
    postJson(`/api/runs/${runId}/cancel`, {}).catch(() => {});
  }
});

const events = new EventSource(`/api/runs/${runId}/events`);
events.onmessage = (message) => render(JSON.parse(message.data));
events.onerror = () => {
  if (events.readyState === EventSource.CLOSED) {
    showError({ message: "Lost contact with SetupPilot. Is it still running? Reload the page.", details: "" });
  }
};
