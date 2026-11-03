const form = document.getElementById("setup-form");
const checks = document.getElementById("preflight");

async function preflight() {
  const folder = form.elements.dest_dir.value;
  const response = await fetch(`/api/preflight?folder=${encodeURIComponent(folder)}`, {
    headers: { "X-CSRF-Token": csrfToken },
  });
  const data = await response.json();
  checks.replaceChildren(
    ...data.checks.map((check) => {
      const item = element("li", `${check.is_ok ? "✔" : "✘"} ${check.name}`, check.is_ok ? "ok" : "bad");
      if (check.fix) item.append(element("div", check.fix, "muted"));
      return item;
    })
  );
}

form.elements.dest_dir.addEventListener("change", preflight);
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const data = await postJson("/api/runs", {
      repo_url: form.elements.repo_url.value,
      dest_dir: form.elements.dest_dir.value,
      use_docker: form.elements.use_docker.checked,
    });
    window.location = `/runs/${data.run_id}`;
  } catch {
    button.disabled = false;
  }
});
preflight();
