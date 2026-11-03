# Contributing

SetupPilot runs commands on people's machines, so the safety rules come first.

## Set up

```bash
git clone https://github.com/Kapurrrishabh/setup-pilot.git setuppilot && cd setuppilot
uv sync
uv run pre-commit install   # lint and format each commit
```

## Before you open a pull request

```bash
./test.sh basics   # the same lint, format, type and test checks CI runs
```

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
