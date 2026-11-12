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
- Never log or print secrets; pass text through `redact`.
- Tests must not need the internet or API keys; the LLM is always faked.
- Text that users see is plain, simple English.
- Keep each pull request to one change, with a short message that says why.

Report security problems privately: see [docs/SECURITY.md](docs/SECURITY.md).
