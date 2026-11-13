# Contributing

SetupPilot runs commands on people's machines, so the safety rules come first.

## Set up

```
git clone https://github.com/Kapurrrishabh/setup-pilot.git setuppilot
cd setuppilot
uv sync
uv run pre-commit install   # lint and format each commit
```

## Before you open a pull request

```powershell
.\test.ps1 basics   # the same lint, format, type and test checks CI runs
```

On macOS and Linux, run `./test.sh basics`. If PowerShell says running scripts is disabled, run
`uv run python scripts/acceptance.py basics`: both scripts only run that file, so a new check for
other machines goes there, not in either script. It checks types for Windows, macOS and Linux
wherever you run it, because mypy skips the code written for the other systems. The 90% gate on
`src/setuppilot/guardrails/` is measured on macOS and Linux; it leaves out the Windows-only part of
`process_tree.py`, which only the Windows CI job tests.

Database tests need a local Postgres with pgvector. Set `TEST_DATABASE_URL`; the database name
must end in `_test`, because the tests empty every table. Never point it at Supabase.

## Rules

- Every command that changes the machine goes through the allowlist and the user's approval.
  Never add a way around them.
- A new allowed command needs safe and unsafe cases in `tests/test_commands.py`, and
  `src/setuppilot/guardrails/` must stay at 90% test coverage or more.
- Repo text and AI text are untrusted. Send repo text to the LLM only inside `data_block`. An AI
  command goes through `validate_ai_command` and the user's approval like any other, and AI text
  never decides "verified".
- In the web pages, show repo and AI text with `textContent` only, and make a link only from a
  URL that starts with `https://github.com/`.
- Never log or print secrets; pass text through `redact`. The GitHub token and the browser's
  binding key never go into a page, a log, a prompt, the database or a command's environment.
- Tests must not need the internet or API keys; the LLM and GitHub are always faked
  (`tests/fakes.py`, `tests/github_fakes.py`).
- Code for one system only (Windows, macOS or Linux) keeps its decisions in pure functions, so a
  test can run them on any system; the Windows CI job runs the rest.
- Text that users see is plain, simple English.
- Keep each pull request to one change, with a short message that says why.

Report security problems privately: see [docs/SECURITY.md](docs/SECURITY.md).
