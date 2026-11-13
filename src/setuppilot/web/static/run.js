// The live run page. It is drawn from the server's saved view, so a refresh loses only the busy timer.
const runId = $("run").dataset.runId;
// A signed-in run's page comes without its repo; the first view, read with this browser's key, has it.
let repoName = "";
// A held or stray key must not answer a question the user has not had time to read.
// Provisional: 700 ms is a guess; watching people use the page would confirm it.
const KEY_DELAY_MS = 700;
// Updates come about once a second, so an older view can still arrive just after an answer.
// Provisional: three updates of margin, not measured.
const ANSWER_LAG_MS = 3000;
// These errors carry the end of a command's output in their details.
const OUTPUT_ERRORS = ["InstallFailed", "CommandTimeout"];
const CARRY_ON_TEXT = "Carrying on from the last saved point";
const TONE_WORDS = { ok: "Done", warn: "Needs a look", bad: "Failed" };
const ANSWER_WORDS = { approve: "approve", reject: "reject", edit: "edit", install: "install", recheck: "check again", done: "done", stop: "stop" };
const ORIGIN_WORDS = { ai: "From the AI", fix: "A fix from the AI", you: "Your edit" };
const VERIFICATION_TONES = { setuppilot: "ok", ai_only: "warn", none: "bad" };
const VERIFICATION_TEXTS = {
  setuppilot: "SetupPilot ran its own checks for this kind of project, and they passed.",
  ai_only: "This is weaker than a check by SetupPilot.",
  none: "Nothing showed that the setup works.",
};

let view = null;
let questionId = null;
let questionShownAt = 0;
let isNoteShown = false;
let sentEdit = null;
// The answer just sent: until the server has moved on, older views must not show its question again.
let answered = null;
let busySince = null;
let carryOnAtStep = null;
let lastNews = "";
let lostContact = false;
// Fix attempts whose details are open; each update redraws the timeline, which would close them.
const openAttempts = new Set();

function isOver(v) {
  return v.report !== null || v.run.status === "cancelled";
}

function isAnsweredView(v) {
  if (answered === null || Date.now() - answered.at > ANSWER_LAG_MS || v.problem) return false;
  return v.question_id === answered.questionId && !v.is_busy && !isOver(v);
}

// The saved status lags a moment behind what the page knows, so the page works it out.
function liveStatus(v) {
  if (isOver(v)) return v.run.status;
  if (isAnsweredView(v)) return "running";
  if (v.question) return "waiting_approval";
  if (v.can_carry_on) return "paused";
  if (v.is_busy) return "running";
  return v.run.status;
}

function showStatus(status) {
  $("status").dataset.status = status;
  $("status").textContent = labels.statuses[status];
  document.title = `${labels.statuses[status]} · ${repoName || "Private repo"} · SetupPilot`;
}

function showRepo(v) {
  if (repoName || !v.run.repo_url) return;
  repoName = repoNameOf(v.run.repo_url);
  $("repo").textContent = repoName;
  const link = githubUrl(v.run.repo_url);
  if (link) {
    $("repo").href = link;
    $("repo").title = link;
  }
  $("dest-dir").textContent = v.run.dest_dir;
}

function render(next) {
  const previous = view;
  view = next;
  showRepo(next);
  const status = liveStatus(next);
  showStatus(status);
  renderProgress(next);
  renderAnalysis(next);
  renderSteps(next);
  if (!isAnsweredView(next)) {
    renderNow(next);
    renderQuestion(next);
  }
  $("stopped").hidden = !next.can_carry_on;
  $("cancel").hidden = isOver(next);
  $("background-note").hidden = isOver(next) || next.can_carry_on;
  renderOutcome(next, previous);
  if (next.problem) showError(next.problem);
  tellNews(next, status, previous);
}

function stepLabel(name) {
  return labels.steps[name]?.label ?? name;
}

function currentPhase(v) {
  if (v.question) return labels.steps[labels.question_steps[v.question.kind]].phase;
  // Every run ends with the report step, so a stopped run is placed at the step that failed.
  const steps = v.steps.filter((step) => step.name !== "report");
  const last = steps.findLast((step) => !step.is_ok) ?? steps.at(-1);
  return last ? labels.steps[last.name]?.phase ?? 0 : 0;
}

function renderProgress(v) {
  const total = labels.phases.length;
  const isVerified = v.report !== null && v.is_verified;
  const current = isVerified ? total : currentPhase(v);
  const isCancelled = v.run.status === "cancelled";
  const stoppedClass = isCancelled ? "cancelled" : "stopped";
  [...$("progress").children].forEach((item, index) => {
    item.className = index < current ? "done" : index === current ? (isOver(v) ? stoppedClass : "current") : "";
    if (index === current && !isOver(v)) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });
  const where = `step ${current + 1} of ${total}: ${labels.phases[current]}`;
  // A cancel while waiting for an answer leaves no failed step: the run stopped after the last one.
  const at = v.steps.some((step) => !step.is_ok) ? "at" : "after";
  if (isVerified) $("progress-text").textContent = `Done: all ${total} steps finished`;
  else if (isCancelled) $("progress-text").textContent = `Cancelled ${at} ${where}`;
  else if (isOver(v)) $("progress-text").textContent = `Stopped ${at} ${where}`;
  else $("progress-text").textContent = where[0].toUpperCase() + where.slice(1);
}

function showNow(text, command) {
  busySince ??= Date.now();
  const parts = command ? [`${text} `, element("code", command)] : [`${text}…`];
  $("now-text").replaceChildren(...parts);
  $("now-time").textContent = clock(Date.now() - busySince);
  $("now").hidden = false;
}

function renderNow(v) {
  if (!v.is_busy || isOver(v)) {
    $("now").hidden = true;
    busySince = null;
    answered = null;
    return;
  }
  const last = v.steps.at(-1);
  const isJustAnswered = answered !== null && v.steps.length <= answered.atStep;
  // Once the command's own step is saved, the run has moved on to whatever comes next.
  const command = v.running_command ?? answered?.command;
  if (command && (isJustAnswered || ["approve", "offer_install"].includes(last?.name))) showNow("Running", command);
  else if (isJustAnswered) showNow(labels.steps[labels.question_steps[answered.kind]].then);
  else if (v.steps.length === carryOnAtStep) showNow(CARRY_ON_TEXT);
  else showNow(last ? labels.steps[last.name]?.then ?? "Working" : labels.first_activity);
}

function stepArguments(v, name) {
  return v.steps.findLast((step) => step.name === name)?.arguments ?? null;
}

function renderAnalysis(v) {
  const analysis = stepArguments(v, "analyze");
  $("analysis").hidden = analysis === null;
  if (analysis === null) return;
  const runnable = analysis.options.filter((option) => option.is_runnable);
  const isAgreed = runnable.length > 0 && runnable.every((option) => option.source === "both");
  if (analysis.summary === null) setBadge($("analysis-source"), "Found in the files (the AI was not used)", "neutral");
  else if (isAgreed) setBadge($("analysis-source"), "The AI and the files agree", "ok");
  else setBadge($("analysis-source"), "The AI and the files did not fully agree", "warn");
  $("analysis-summary").textContent = analysis.summary ?? "";
  $("analysis-summary-box").hidden = analysis.summary === null;
  const tools = stepArguments(v, "prerequisites")?.tools ?? {};
  $("analysis-options").replaceChildren(...analysis.options.map((option) => optionItem(option, tools[option.id])));
  const notes = analysis.notes ?? [];
  $("analysis-notes").replaceChildren(...notes.map((note) => element("li", note)));
  $("analysis-notes").hidden = notes.length === 0;
  // Until tool checks are saved with the step, its summary says what is missing.
  const toolSummary = v.steps.findLast((step) => step.name === "prerequisites")?.result_summary;
  $("analysis-tools").textContent = toolSummary ? `Tools: ${toolSummary}` : "";
  $("analysis-tools").hidden = !toolSummary || Object.keys(tools).length > 0;
}

function setBadge(badge, text, tone) {
  badge.textContent = text;
  badge.dataset.tone = tone;
}

function sourceBadge(source) {
  const badge = element("span", undefined, "badge");
  setBadge(badge, labels.sources[source] ?? source, source === "both" ? "ok" : source === "ai" ? "warn" : "neutral");
  return badge;
}

function optionItem(option, checks) {
  const item = element("li", undefined, option.is_runnable ? "ok" : "optional");
  const body = element("div", undefined, "check-body");
  const title = element("span", undefined, "check-title");
  title.append(element("span", optionName(option.id), "check-name"), sourceBadge(option.source));
  if (!option.is_runnable) title.append(element("span", "Not offered", "badge"));
  body.append(title);
  if (option.note) body.append(element("p", option.note, "hint"));
  if (checks?.length) body.append(element("p", toolsLine(checks), "hint"));
  item.append(body);
  return item;
}

// "web: node/npm" reads as "Node with npm, in web/".
function optionName(id) {
  const [where, stack] = id.includes(": ") ? id.split(": ") : [null, id];
  const [language, manager] = stack.split("/");
  const name = `${language[0].toUpperCase()}${language.slice(1)} with ${manager}`;
  return where ? `${name}, in ${where}/` : name;
}

function diagnosisDetails(found, attempt) {
  const details = element("details", undefined, "step-detail");
  details.open = openAttempts.has(attempt);
  details.addEventListener("toggle", () => (details.open ? openAttempts.add(attempt) : openAttempts.delete(attempt)));
  details.append(element("summary", `Fix attempt ${attempt}: what went wrong`));
  details.append(element("p", `${found.command} failed with exit code ${found.exit_code ?? "none"}.`));
  if (found.proposed.length) details.append(element("p", `Fixes the AI proposed: ${found.proposed.join("; ")}`));
  found.refused.forEach((refused) => details.append(element("p", `Not allowed: ${refused.command} (${refused.reason})`)));
  if (found.missing_tools.length) details.append(element("p", `Tools still missing: ${found.missing_tools.join(", ")}`));
  return details;
}

function renderSteps(v) {
  $("steps-empty").hidden = v.steps.length > 0;
  let attempts = 0;
  $("steps").replaceChildren(
    ...v.steps.map((step) => {
      const item = element("li", undefined, step.tone);
      const main = element("div", undefined, "step-main");
      main.append(
        element("span", `${TONE_WORDS[step.tone]}: `, "visually-hidden"),
        element("strong", stepLabel(step.name)),
        element("span", step.result_summary, "step-summary")
      );
      if (step.name === "diagnose" && step.arguments?.command) main.append(diagnosisDetails(step.arguments, ++attempts));
      const meta = [step.duration_ms >= 1000 ? step.duration : "", step.tokens ? `${step.tokens.toLocaleString()} tokens` : ""];
      item.append(main, element("span", meta.filter(Boolean).join(" · "), "step-meta"));
      return item;
    })
  );
}

function renderQuestion(v) {
  const question = v.question;
  const isNew = question !== null && v.question_id !== questionId;
  questionId = v.question_id;
  $("question").hidden = question === null;
  if (!isNew) return;
  questionShownAt = Date.now();
  closeEdit(false);
  const isChoice = question.kind === "choose_stack";
  const isCommand = question.kind === "approve_command";
  // The answers the server takes for this question; install only when SetupPilot has a command.
  const answers = isChoice ? [] : labels.answers[question.kind].filter((answer) => answer !== "install" || question.command);
  const buttons = [...$("question-actions").querySelectorAll("button")];
  buttons.forEach((button) => (button.hidden = !answers.includes(button.dataset.action)));
  const keys = buttons.filter((button) => !button.hidden).map((button) => `${button.getAttribute("aria-keyshortcuts").toUpperCase()} ${ANSWER_WORDS[button.dataset.action]}`);
  $("question-actions").hidden = isChoice;
  $("keys-hint").textContent = isChoice
    ? "Keys, while this box has focus: press the number of an option. They do nothing while you type."
    : `Keys, while this box has focus: ${keys.join(", ")}. They do nothing while you type.`;
  $("command-box").hidden = !isCommand && !(question.kind === "install_tool" && question.command);
  $("question-note").hidden = !isCommand;
  $("question-docker").hidden = !isCommand || !question.in_docker;
  $("question-options").replaceChildren();
  showFacts([]);
  $("question-edited").hidden = true;
  $("question-edit-error").hidden = true;
  $("question-diagnosis").hidden = true;
  $("question-manual-box").hidden = true;
  $("question-origin").hidden = true;
  $("command-label").textContent = "Command";
  if (isChoice) renderChoice(question);
  else if (isCommand) renderCommand(question);
  else if (question.kind === "install_tool") renderInstall(question);
  else renderFixByHand(question);
  $("question-title").focus();
}

function showFacts(facts) {
  $("question-facts").replaceChildren(...facts.map((fact) => element("li", fact)));
  $("question-facts").hidden = facts.length === 0;
}

function renderInstall(question) {
  $("question-title").textContent = question.command ? `${question.tool} is missing. Install it now?` : `${question.tool} is missing. Install it, then check again`;
  $("question-reason").textContent = question.reason ? `${question.problem} Why it is needed: ${question.reason}.` : question.problem;
  $("command-label").textContent = "SetupPilot can run this for you";
  $("question-command").textContent = question.command ?? "";
  $("manual-label").textContent = question.command ? "Or install it by hand" : `How to install it on ${question.os}`;
  $("question-manual").textContent = question.manual;
  $("question-manual-box").hidden = !question.manual;
  const facts = [];
  // winget's license note already says Windows may ask for admin rights.
  if (question.command && question.needs_admin && !question.note) facts.push("It may ask for admin rights in another window.");
  if (question.note) facts.push(question.note);
  const last = question.last_install;
  if (last) facts.push(`Last try: ${last.command} ended with exit code ${last.exit_code ?? "none"}. ${last.output}`.trim());
  facts.push(`Ask ${question.round}. Stop ends the run and shows the report.`);
  showFacts(facts);
}

function renderFixByHand(question) {
  $("question-title").textContent = "A step failed. Fix it by hand, then try again";
  $("question-reason").textContent = "SetupPilot cannot fix this by itself. Do what is below, then press I fixed it.";
  $("diagnosis-failed").replaceChildren(element("code", question.command), ` failed with exit code ${question.exit_code ?? "none"}.`);
  $("diagnosis-guess").textContent = question.diagnosis.explanation;
  $("diagnosis-todo").textContent = question.user_action ?? "";
  $("diagnosis-todo-box").hidden = !question.user_action;
  $("diagnosis-refused").replaceChildren(
    ...question.refused.map((refused) => {
      const item = element("li");
      item.append(element("code", refused.command), ` (${refused.reason})`);
      return item;
    })
  );
  $("diagnosis-refused-box").hidden = question.refused.length === 0;
  $("question-diagnosis").hidden = false;
}

// Tools an option needs, like "python 3.12.1 installed, uv missing".
function toolsLine(checks) {
  const words = (check) => (check.is_installed ? `${check.version ?? ""} installed`.trim() : check.is_required ? "missing" : "missing, optional");
  return `Tools: ${checks.map((check) => `${check.tool} ${words(check)}`).join(", ")}.`;
}

function renderChoice(question) {
  const projects = new Set(question.options.map((option) => option.project));
  $("question-title").textContent = projects.size > 1 ? "Which part of this repo should SetupPilot set up?" : "Which way should SetupPilot set up this repo?";
  const conflicts = question.conflicts.join(" ");
  $("question-reason").textContent = `It found more than one way. Pick the one the README uses. ${conflicts}`.trim();
  question.options.forEach((option, index) => {
    const missing = option.prereqs.filter((check) => check.is_blocking).map((check) => check.tool);
    const button = element("button", undefined, "option");
    button.type = "button";
    if (index < 9) {
      button.setAttribute("aria-keyshortcuts", String(index + 1));
      const key = element("kbd", String(index + 1));
      key.setAttribute("aria-hidden", "true");
      button.append(key);
    }
    const body = element("span", undefined, "option-body");
    const title = element("span", undefined, "option-title");
    title.append(element("strong", optionName(option.label)), sourceBadge(option.source));
    body.append(title, element("span", `Found in ${option.evidence.join(", ")}`, "muted"));
    if (option.note) body.append(element("span", option.note, "muted"));
    if (option.prereqs.length) body.append(element("span", toolsLine(option.prereqs), "muted"));
    if (missing.length) {
      body.append(element("span", `Missing: ${missing.join(", ")}. SetupPilot asks you about installing it before it goes on.`, "option-problem"));
    }
    button.append(body);
    button.addEventListener("click", () => send({ choice: index }));
    $("question-options").append(button);
  });
}

function renderCommand(question) {
  const optional = question.is_optional ? " It is optional." : "";
  $("question-title").textContent = `Command ${question.step}: ${question.is_check ? "run this check?" : "run this command?"}`;
  $("question-reason").textContent = `${question.reason}${optional}`;
  $("question-origin").textContent = ORIGIN_WORDS[question.origin] ?? "";
  $("question-origin").hidden = !ORIGIN_WORDS[question.origin];
  if (question.diagnosis) showFacts([`The AI's guess at what went wrong (it may be wrong): ${question.diagnosis.explanation}`]);
  $("question-command").textContent = question.command;
  $("edit-command").value = question.command;
  if (question.edit_error) {
    $("question-edit-error").textContent = `Your edit was blocked, so the original command is back. ${question.edit_error}`;
    $("question-edit-error").hidden = false;
  } else if (sentEdit !== null && question.command === sentEdit) {
    $("question-edited").hidden = false;
  }
  sentEdit = null;
  $("question-warning").textContent = question.in_docker
    ? "Approving runs code from this repo inside a Docker container."
    : "Approving can run code from this repo as you.";
  // The full warning opens once per page; after that it stays one short line.
  $("question-note").open = !isNoteShown;
  isNoteShown = true;
}

function renderOutcome(v, previous) {
  const wasHidden = $("report-card").hidden;
  $("report-card").hidden = !isOver(v);
  if (!isOver(v)) return;
  const outcome = v.run.status === "cancelled" ? "cancelled" : v.is_verified ? "verified" : "failed";
  $("report-card").dataset.outcome = outcome;
  // A signed-in run's repo is picked again from the list: its URL would stay in the browser's history.
  const isSignedInRun = v.run.owner_github_id !== null;
  const again = isSignedInRun ? "/" : `/?repo=${encodeURIComponent(v.run.repo_url)}`;
  const chat = isSignedInRun ? "/chat" : `/chat?index=${encodeURIComponent(v.run.repo_url)}`;
  const level = v.verification ?? "none";
  setBadge($("verification-badge"), labels.verification[level], VERIFICATION_TONES[level]);
  $("verification-text").textContent = VERIFICATION_TEXTS[level];
  $("outcome-verification").hidden = outcome === "cancelled";
  const plan = stepArguments(v, "plan");
  const start = plan?.start_command;
  $("start-command").textContent = start ?? "";
  // A sub-project's start command runs in its own folder, not at the repo's top.
  const slash = (v.repo_dir ?? "").includes("\\") ? "\\" : "/";
  const inProject = plan?.project && plan.project !== "." ? `${slash}${plan.project.split("/").join(slash)}` : "";
  $("start-where").textContent = v.repo_dir ? v.repo_dir + inProject : "";
  $("start-hint").hidden = !start || outcome === "cancelled" || v.repo_dir === null;
  // A second run into the same folder stops at once, because the repo's folder is already there.
  $("outcome-retry").replaceChildren("The repo's files are still in ", element("code", v.repo_dir), ". To try again, delete only that folder, or pick a different folder on the setup page.");
  $("outcome-retry").hidden = outcome === "verified" || v.repo_dir === null;
  let details = "";
  let isOutput = false;
  if (outcome === "verified") {
    $("report-title").textContent = "Setup verified";
    $("outcome-message").replaceChildren("SetupPilot's own checks passed. Your repo is ready in ", element("code", v.repo_dir), ".");
    setActions([["Ask questions about this repo", chat], ["See all runs", "/runs"]]);
  } else if (outcome === "cancelled") {
    $("report-title").textContent = "You cancelled this run";
    $("outcome-message").textContent = "Nothing more was run.";
    setActions([["Set up this repo again", again], ["See all runs", "/runs"]]);
  } else {
    const unchecked = v.verification === "ai_only" ? "The checks the AI suggested all passed, but SetupPilot has no checks of its own for this kind of project." : "SetupPilot's checks did not pass.";
    const error = v.error || { error_type: "VerifyFailed", message: unchecked, details: "" };
    $("report-title").textContent = error.error_type === "VerifyFailed" ? "Setup not verified" : labels.errors[error.error_type] ?? error.error_type;
    $("outcome-message").replaceChildren(...withCode(error.message));
    details = error.details || "";
    isOutput = OUTPUT_ERRORS.includes(error.error_type);
    setActions([["Try again", again], ["See all runs", "/runs"]]);
  }
  $("outcome-fix").textContent = isOutput ? "" : details;
  $("outcome-fix").hidden = isOutput || !details;
  $("outcome-output-text").textContent = isOutput ? details : "";
  $("outcome-output").hidden = !isOutput || !details;
  $("report").textContent = v.report ?? "";
  $("report-box").hidden = v.report === null;
  const focus = document.activeElement;
  const isFocusFree = focus === document.body || focus === $("now") || $("question").contains(focus);
  if (wasHidden && (previous === null || isFocusFree)) $("report-title").focus();
}

function setActions(links) {
  $("outcome-actions").replaceChildren(
    ...links.map(([text, href], index) => {
      const link = element("a", text, index === 0 ? "button" : "button secondary");
      link.href = href;
      return link;
    })
  );
}

function tellNews(v, status, previous) {
  const last = v.steps.at(-1);
  const step = last ? ` Last step: ${stepLabel(last.name)}, ${TONE_WORDS[last.tone].toLowerCase()}.` : "";
  const news = `${labels.statuses[status]}.${step}`;
  // The first view is read from the page itself; only changes are said aloud.
  if (previous !== null && news !== lastNews) announce(news);
  lastNews = news;
}

async function send(reply) {
  const command = ["approve", "install"].includes(reply.action) ? view.question.command : null;
  answered = { questionId, atStep: view.steps.length, command, at: Date.now(), kind: view.question.kind };
  if (reply.action === "edit") sentEdit = reply.command.trim();
  $("question").hidden = true;
  showStatus("running");
  if (command) showNow("Running", command);
  else showNow(labels.steps[labels.question_steps[view.question.kind]].then);
  $("now").focus();
  try {
    await postJson(`/api/runs/${runId}/answer`, { ...reply, question_id: questionId });
  } catch (error) {
    answered = null;
    sentEdit = null;
    showStatus(liveStatus(view));
    $("now").hidden = true;
    $("question").hidden = false;
    showError(error);
    $("question-title").focus();
  }
}

function editButton() {
  return $("question-actions").querySelector('[data-action="edit"]');
}

function openEdit() {
  $("edit-box").hidden = false;
  editButton().setAttribute("aria-expanded", "true");
  const box = $("edit-command");
  box.focus();
  box.setSelectionRange(box.value.length, box.value.length);
}

function closeEdit(isBackToButton) {
  $("edit-box").hidden = true;
  editButton().setAttribute("aria-expanded", "false");
  if (isBackToButton) editButton().focus();
}

$("question-actions").querySelectorAll("button").forEach((button) => {
  button.addEventListener("click", () => {
    if (button.dataset.action === "edit") openEdit();
    else send({ action: button.dataset.action });
  });
});
$("edit-box").addEventListener("submit", (event) => {
  event.preventDefault();
  send({ action: "edit", command: $("edit-command").value });
});
$("edit-command").addEventListener("keydown", (event) => {
  // A new line would be refused as a shell part, so Enter sends, like a one-line box.
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("edit-box").requestSubmit();
  } else if (event.key === "Escape") {
    closeEdit(true);
  }
});
$("edit-cancel").addEventListener("click", () => closeEdit(true));
$("copy-start").addEventListener("click", () => copyText($("start-command").textContent, $("start-command")));
// The line that failed is at the end of the output, so open it there.
$("outcome-output").addEventListener("toggle", () => {
  const box = $("outcome-output-text");
  box.scrollTop = box.scrollHeight;
});

document.addEventListener("keydown", (event) => {
  if (event.repeat || event.altKey || event.ctrlKey || event.metaKey) return;
  if ($("question").hidden || !$("edit-box").hidden || Date.now() - questionShownAt < KEY_DELAY_MS) return;
  // Single keys act only inside the question (WCAG 2.1.4), so a stray key elsewhere answers nothing.
  if (!$("question").contains(event.target) || event.target.closest("input, textarea, select, [contenteditable]")) return;
  const key = event.key.toLowerCase();
  const box = $("question-actions").hidden ? $("question-options") : $("question-actions");
  const button = box.querySelector(`[aria-keyshortcuts="${key}"]:not([hidden])`);
  if (!button) return;
  event.preventDefault();
  button.click();
});

$("carry-on").addEventListener("click", async () => {
  if (!confirm("Carry on from the last saved point? The last command you approved may run again.")) return;
  try {
    await postJson(`/api/runs/${runId}/continue`, {});
    carryOnAtStep = view.steps.length;
    showStatus("running");
    $("steps-title").focus();
  } catch (error) {
    showError(error);
  }
});
$("cancel").addEventListener("click", () => {
  if (confirm("Cancel this run? A command that is running now is stopped too.\n\nOK cancels the run. Cancel keeps it going.")) {
    postJson(`/api/runs/${runId}/cancel`, {}).catch(showError);
  }
});

setInterval(() => {
  if (busySince !== null) $("now-time").textContent = clock(Date.now() - busySince);
}, 1000);

// Read with fetch, not EventSource: EventSource cannot send headers, and a run read through a
// GitHub sign-in needs this browser's key. Provisional: 3 s, about what EventSource waited.
const RETRY_MS = 3000;

class LostContact extends Error {}

function lost() {
  throw new LostContact();
}

// The server ends the stream once the run is over (and a cancelled run has saved its last step).
async function readEvents() {
  const headers = bindingHeaders();
  const response = await fetch(`/api/runs/${runId}/events`, { headers }).catch(lost);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 && BINDING_HEADER in headers) forgetBinding();
    throw new RequestError(data.error || { message: `Could not follow this run (${response.status}).` });
  }
  // The app makes a new page token each time it starts, so after a restart the page must reload.
  if (lostContact) window.location.reload();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read().catch(lost);
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const event = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      // Pings start with ":" and carry nothing.
      if (event.startsWith("data: ")) render(JSON.parse(event.slice("data: ".length)));
    }
  }
}

async function followEvents() {
  for (;;) {
    try {
      await readEvents();
      if (view !== null && isOver(view)) return;
    } catch (error) {
      // The server refused (a sign-in is needed, or there is no such run): trying again cannot help.
      if (error instanceof RequestError) {
        showError(error);
        return;
      }
      if (!(error instanceof LostContact)) throw error;
      lostContact = true;
      showError({
        message: "Lost contact with SetupPilot. Trying again…",
        details: "If you stopped the app, start it again with: uv run setuppilot web",
      });
    }
    await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
  }
}

followEvents();
