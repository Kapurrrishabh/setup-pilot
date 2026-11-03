# SetupPilot

[![CI](https://github.com/Kapurrrishabh/setup-pilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Kapurrrishabh/setup-pilot/actions/workflows/ci.yml)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An AI agent that sets up a GitHub repo on your machine and asks you before every command, plus
a chatbot that answers questions about the repo from its own files.

| Approve each command | See that it really works |
|---|---|
| ![The run page asking to approve uv venv .venv](docs/screenshots/approve.png) | ![A verified run with its report](docs/screenshots/verified.png) |

## Problem

Setting up an unknown open-source repo wastes the first hour: old READMEs, guessing the
package manager, missing tools, and install errors. Running install commands from an unknown
repo is also risky, because install scripts can run any code.

## Goals

- **Setup agent:** give a public GitHub URL and a folder. SetupPilot clones the repo, finds the
  stack, checks the tools you need, makes an isolated environment, installs dependencies and
  checks that the setup really works. Every command that changes your machine needs your
  approval first.
- **Repo chatbot:** ask general questions about a repo (what it does, layout, how to run it,
  license). It answers only from the repo's own files, with sources.
- Free to run: Groq free tier, Supabase free Postgres, local embeddings.

## Out of scope

- Private repos, and any host other than `https://github.com/<owner>/<repo>`.
- Writing or fixing code. The chatbot answers questions about the repo only.
- Paid APIs or other LLM providers.
- Running any command without your approval.

## Quick start

You need macOS or Linux, git, and [uv](https://docs.astral.sh/uv/getting-started/installation/)
(it brings Python 3.12+). Everything else is free.

```bash
git clone https://github.com/Kapurrrishabh/setup-pilot.git setuppilot && cd setuppilot
uv sync                    # installs everything, about 1 GB (PyTorch for the chatbot's embeddings)
cp .env.example .env       # then add your free Groq key, and your Supabase URL (see Database)
uv run setuppilot db-check # optional: checks the database and makes the tables
uv run setuppilot web      # open http://127.0.0.1:5005
```

Check that it works: `./test.sh basics` (lint, types and about 750 tests; no internet or keys
needed), and `uv run setuppilot ask "What is uv?"` (one real Groq call; prints the answer and
tokens used). Run `uv run setuppilot --version` to see which version you have.

Before real use, copy your Groq limits from https://console.groq.com/settings/limits into
`config.yaml`. The numbers there now are only a starting guess.

More: [security notes](docs/SECURITY.md), [evaluation report](eval/REPORT.md),
[5-minute demo](docs/DEMO.md), [testing still to do](#testing-still-to-do),
[contributing](CONTRIBUTING.md), [changelog](CHANGELOG.md).

## Web app

```bash
uv run setuppilot web      # then open http://127.0.0.1:5005
```

Pages: **Set up a repo** (URL, folder, Docker mode, pre-flight check), the **live run** (a
progress bar, the approve / reject / edit box with keyboard keys A, R and E, the timeline of
steps, Cancel, and a clear verified / not verified result with the next step), **Chat**, **Runs**
(filter by result, search by repo), **Metrics** and **Settings** (tokens used today against the
budget, requests left per model). It follows your system's light or dark theme, works on narrow
windows, works with the keyboard only, and announces new steps and questions to screen readers
(checked with axe; not yet tried with a real screen reader).

Runs work in the background: refreshing or closing the page does not stop them, and a run
waiting for approval is still there after the app restarts (with Supabase connected). A run that
was stopped by a restart shows as **Paused**; open it and press Carry on. Do not drive the same
run from the terminal and the web app at the same time.

![The Runs page in dark mode](docs/screenshots/runs-dark.png)

The app listens on 127.0.0.1 only and refuses any other host in config.yaml, because it runs
commands on your machine. It also refuses requests from other websites open in your browser.

## Set up a repo

```bash
mkdir -p ~/work
uv run setuppilot run https://github.com/pypa/sampleproject ~/work
uv run setuppilot resume <run id>        # carry on after you stopped with q, or after a restart
uv run setuppilot run <url> ~/work --docker   # install inside a container (needs Docker)
```

The flow is fixed: clone, scan, detect the stack, check your tools, ask which stack if there are
several, plan, then **ask you before every command** (approve, reject or edit), run it, check that
the setup really works, and report. It says "verified" only when those checks pass.

- Commands run without a shell, from an allowlist, inside the repo folder, with a time limit.
  They never get your API keys or other secrets.
- Install scripts can still run code from the repo. Use `--docker` for repos you do not trust,
  from the terminal with the web app closed (a container can reach the web app; see
  [docs/SECURITY.md](docs/SECURITY.md)).
- The LLM only reads the README for extra steps and writes the report. It can suggest a
  command, but that command goes through the allowlist and your approval like any other.
- Without a Groq key the run still works; it skips the README tips and the AI report.
- A paused run is saved in Supabase, so it survives a restart. Without a database it is kept
  in memory only.
- Docker mode supports pip, uv, npm, yarn, bun, Go, Cargo, Bundler, Maven and Gradle repos (and
  Compose runs on your machine). Poetry, Pipenv, PDM, conda and pnpm repos are not supported in
  Docker mode yet.
- With `--docker`, `.venv` and `node_modules` are built for Linux inside the image. Use them from
  that image, or run again without `--docker` to use them on your machine.
- SetupPilot never deletes anything. After a clone that timed out or was refused for size, or a
  failed install, remove that half-finished folder yourself before a new run into the same place.

### Docker (for `--docker` and Compose repos)

On macOS, Colima is a free Docker engine:

```bash
brew install colima docker docker-compose docker-buildx
mkdir -p ~/.colima/_lima/_config && cp docs/colima-override.yaml ~/.colima/_lima/_config/override.yaml
colima start
```

- Add `{"cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]}` to
  `~/.docker/config.json` so `docker compose` and `docker buildx` are found.
- The override keeps ports that containers publish on this Mac only. Without it, Colima opens them
  to your whole network.
- macOS does not let Colima read Desktop, Documents or Downloads, so a `--docker` run there fails
  with "operation not permitted". Use a folder like `~/work`.
- Stop it with `colima stop` when you are done.

## Ask about a repo (chatbot)

```bash
uv run setuppilot index https://github.com/pypa/sampleproject   # clone (if needed) and index it
uv run setuppilot chat pypa/sampleproject                        # ask questions; q to stop
uv run setuppilot eval-chat --mode retrieval                     # score search only, no LLM
uv run setuppilot eval-chat                                      # full score (needs GROQ_API_KEY)
uv run setuppilot eval-chat --mode replay                        # rerun from cached answers, free
```

The chatbot answers general questions (what the repo does, its layout, setup, license, how to
contribute) only from the repo's own files, and shows the files it used. It says "I could not
find this in the repo" when nothing fits, and it does not write or fix code. It cannot run
anything. Embeddings are made on your machine with `all-MiniLM-L6-v2` (downloaded once, about
90 MB); each question costs one LLM call. The evaluation saves after every question, so if a
limit stops it, run the same command later to carry on.

The terminal chat needs a database (`SUPABASE_DB_URL`): without one, the index from
`setuppilot index` is gone when that command ends. Without a database, use the web app's Chat
page, which indexes and answers in one process. If an answer is cut off, raise
`chat.answer_max_tokens` in `config.yaml`.

## History and metrics

```bash
uv run setuppilot runs              # recent runs: status, time, tokens
uv run setuppilot show <run id>     # one run's steps: time, tokens, errors
uv run setuppilot metrics           # success rate, average time and tokens, rate-limit hits, common errors
```

A run's time includes any time it waited for your approval. To send traces to LangSmith (free
tier), set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` in `.env`; it is off otherwise.

## Database (Supabase, free)

1. Make a free project at https://supabase.com.
2. Go to Connect (or Project Settings > Database) and copy the **Session pooler** connection
   string. The direct one needs IPv6, which many home networks do not have. Put it in `.env` as
   `SUPABASE_DB_URL`, with your database password in place of `[YOUR-PASSWORD]`.
3. Run `uv run setuppilot db-check`. The first run creates the tables (with row level security
   on), then it writes one run and reads it back.

Without `SUPABASE_DB_URL` the app still works (except the terminal chat; use the web app's Chat
page). It keeps data in memory only (logs still go to the log file), and it tells you so. If the
database connection breaks with an error in the middle of a run, the run stops with a clear
message; an LLM answer that was already paid for is still used.

For a stricter connection, add `?sslmode=verify-full&sslrootcert=<path>` to `SUPABASE_DB_URL`,
using the CA certificate from Supabase's database settings.

Database tests run against a local Postgres when `TEST_DATABASE_URL` is set. The database name
must end in `_test`, because the tests empty every table. CI starts its own Postgres for this.

## Limits and known problems

- **Groq free tier**: daily limits per model. When they run out, the setup still works (it skips
  the README tips and the AI summary); the chatbot says "Daily limit reached, try again later".
- **Supported stacks**: Python (pip, uv, Poetry, Pipenv, PDM, conda), Node (npm, yarn, pnpm, bun),
  Go, Rust, Ruby, Java (Maven, Gradle) and Docker Compose. A repo with only a Dockerfile, or with
  its real project in a sub-folder (monorepos), is not set up automatically; it says so.
- **Not tried for real yet**: see [Testing still to do](#testing-still-to-do).
- **Go**: if your network blocks `proxy.golang.org`, run with `GOPROXY=direct` set; SetupPilot
  passes it to Go, also inside Docker mode.
- **Install scripts run code** from the repo and its dependencies. See
  [docs/SECURITY.md](docs/SECURITY.md); use `--docker` for repos you do not trust.
- **The web app** is clicked through in Chrome by `scripts/browser_check.py` (also in CI), and
  was checked with axe (no accessibility problems found) in light and dark, at desktop and
  phone sizes. Firefox, Safari and real screen readers are not tried yet. It is for one person on
  one machine, and has no login (see [docs/SECURITY.md](docs/SECURITY.md)).
- **Slow Groq**: if Groq takes a question but never answers, the chat can wait up to about 4
  minutes (4 tries of up to 30 seconds on each model, plus short waits) before it says Groq is
  busy or not answering.
- **Silent network drops**: if the network to Supabase goes quiet with no error (Wi-Fi drops),
  a database call can wait several minutes, until your computer's network gives up, before the
  run fails. Until then the run page still shows the run as working.
- **Rate limits are counted per process**: each command, and the web app, starts counting
  requests per model from zero, so after a restart or with two commands at once it relies on
  Groq's own "slow down" answer (retry, then the small model, then "Daily limit reached"). The
  daily token budget is read from the database when Supabase is connected, so it holds across
  restarts.
- **Build flags**: a README step like `go build -ldflags "-s -w"` is refused, because `-ldflags`
  can also pick the program that links. Run it yourself if you need it.
- **Budgets** are checked before each call, but two calls at the same moment (a setup run and a
  chat question) can go a little over the daily token budget.
- Windows is not supported (paths like `.venv/bin/python`).

## Testing still to do

Everything below was built and tested on one MacBook (Apple silicon) with no Groq key and a local
Postgres instead of Supabase. CI on GitHub runs lint, types, all unit tests on Python 3.12 and
3.13 (with a real Postgres and pgvector), a dependency audit, the package build, and the Chrome
click-through. These checks still need a key, a Supabase project or another machine:

| What | Why it is not done yet | How to run it |
|---|---|---|
| Real Groq calls: `ask`, README tips and the AI report in a run, tokens per run (the setup table's Tokens column; `setuppilot show <run id>` lists each call) | no Groq key on the build laptop | `./test.sh groq setup` |
| Chatbot full answers and "I don't know" (target 80%+ grounded) | no Groq key | `./test.sh chat` |
| Real Supabase: tables, row level security, pause and resume across a restart, cached replay | no Supabase project yet | `./test.sh supabase chat` |
| The 15-repo setup evaluation on Linux or Windows (WSL) | only macOS tried | `./test.sh setup` |
| Docker mode and Compose on Docker Desktop, and whether a container can reach the web app | only Colima tried | `./test.sh docker` |
| The web app in Firefox and Safari | only Chrome tried | by hand (steps below) |
| LangSmith tracing (optional) | no LangSmith key | set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` in `.env`, run a setup, look for the trace |

How to run them:

```bash
cp .env.example .env    # add GROQ_API_KEY and SUPABASE_DB_URL (Session pooler string)
./test.sh               # every part this machine can run
./test.sh groq chat     # or only some parts: basics groq supabase setup docker chat browser
```

Each part says PASS, FAIL or SKIP (with what it needs). Results go to
`test-results/<time>-<machine>/` (the log, the summary and the eval tables); send that folder to
the team. It holds no secrets. Clones go to `~/setuppilot-test/` (not Desktop or Documents,
because Docker cannot read those on macOS); delete it when done. The `setup` part needs the
stacks' tools (uv, node and npm, pnpm, poetry, go, cargo, ruby and bundler, java and maven,
docker); a missing tool shows as PrereqMissing in the table. The `browser` part needs Google
Chrome (or run `uv run --group e2e playwright install chromium` and set
`SETUPPILOT_E2E_BROWSER=chromium`); without it, that part is skipped. On Windows, run it inside
WSL.

By hand, in Firefox and Safari: open http://127.0.0.1:5005, start a setup of
`https://github.com/pypa/sampleproject`, press Edit and try `rm -rf /` (it must be refused),
approve the rest and check "Setup verified", reload the page (the run must still be there), start
another run and Cancel it, open Runs, Metrics and Settings, and on Chat index the repo and ask
"What license does it use?" (with a key: MIT, with LICENSE.txt as a source). Try it with the
keyboard only, and with a narrow window.

## Future work

- Run the full chatbot evaluation with a Groq key and tune the prompts from the results.
- Pick Docker-mode images by the repo's version hint.
- Set up sub-projects in monorepos, and Dockerfile-only repos.
- Explain setup failures and suggest fixes (still behind approval).
- Package SetupPilot as a tool you can install with `uv tool install`.
- A login for the web app, so other programs on the same computer cannot use it.

## Layout

- `config.yaml`: models, rate limits, budgets and every other setting. Secrets live in `.env`.
- `src/setuppilot/`: `llm` (Groq, limits, cache), `db` (Supabase/Postgres and memory stores),
  `tools` (URL check, clone, scan, detect, prerequisites), `guardrails` (paths, allowlist,
  runner, Docker mode), `agent` (the LangGraph flow), `setup` (plans and checks per stack),
  `chat` (the chatbot), `web` (Flask), `evaluation`.
- `tests/`: pytest tests. The LLM is always faked. Store tests run on the in-memory fake, and
  also on a real Postgres when `TEST_DATABASE_URL` is set.
- `test.sh` and `scripts/browser_check.py`: the checks for other machines (see above).
- `.github/`: CI, Dependabot, and issue and pull request templates. `docs/`: security notes,
  the demo script, the Colima settings and the screenshots.
- `eval/`: the question set, the 15-repo set, results and the report.
- Logs: JSON lines in `logs/setuppilot.jsonl`, with secrets hidden.

## Status

| Phase | What | State |
|---|---|---|
| 0 | Skeleton: uv, config, logging, ruff, pytest, CI | done; CI green on GitHub |
| 1 | Groq layer: BIG/SMALL models, rate limiter, retries, budgets, cache, fake LLM | done; real call needs your key |
| 2 | Supabase: tables, row level security, runs/steps/usage/cache stores, memory fallback | done; real check needs your Supabase URL |
| 3 | Repo tools: URL check, shallow size-checked clone, scan, stack detection, prerequisites, compose finder | done |
| 4 | Guardrails: path guard, command allowlist, safe runner (no shell, no secrets, time limit, output tail), Docker mode | done; Docker mode verified on Colima for Python, Node, Go, Rust, Ruby, Java |
| 5 | Agent: fixed LangGraph flow, approve/reject/edit, pause and resume (Postgres checkpointer), max steps | done; 3 fake + 2 real repos verified |
| 6 | Native setup for Python, Node, Go, Rust, Ruby, Java, Compose, with real checks | done; all 7 verified on real repos (Go with `GOPROXY=direct` on this network) |
| 7 | Observability: JSON step logs with run id, runs/steps/tokens/metrics in the DB, `runs`/`show`/`metrics` commands, optional LangSmith | done |
| 8 | Chatbot: index docs/manifests/config, local embeddings, pgvector search, grounded JSON answers with sources, scope rules, short memory, evaluation script | done; full answers need your Groq key |
| 9 | Flask web app: setup, live run (SSE, approve/reject/edit, cancel), chat, runs, metrics, settings, pre-flight | done; clicked through in Chrome (also in CI); Firefox and Safari still to do |
| 10 | Evaluation: 15 setup repos (15/15 stacks, 15/15 verified), 30 chatbot questions (search 52% to 80%), guardrail coverage 100% (CI fails below 90%) | done; full chatbot answers need your key |
| 11 | Docs: this README, security notes, evaluation report, demo script | done |
| Final | Whole-project review (78 confirmed findings fixed or documented), web UI redesign, type checking, license, contributing guide, security scanning | done |
