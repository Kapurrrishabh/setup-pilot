# SetupPilot

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
git clone <this repo> setuppilot && cd setuppilot
uv sync                    # installs everything, about 1 GB (PyTorch for the chatbot's embeddings)
cp .env.example .env       # then add your free Groq key, and your Supabase URL (see Database)
uv run setuppilot db-check # optional: checks the database and makes the tables
uv run setuppilot web      # open http://127.0.0.1:5005
```

Check that it works: `uv run pytest` (about 600 tests, no internet or keys needed), and
`uv run setuppilot ask "What is uv?"` (one real Groq call; prints the answer and tokens used).

Before real use, copy your Groq limits from https://console.groq.com/settings/limits into
`config.yaml`. The numbers there now are only a starting guess.

More: [security notes](docs/SECURITY.md), [evaluation report](eval/REPORT.md),
[5-minute demo](docs/DEMO.md).

## Web app

```bash
uv run setuppilot web      # then open http://127.0.0.1:5005
```

Pages: **Set up a repo** (URL, folder, Docker mode, pre-flight checklist), the **live run** (steps
as they happen, and the approve / reject / edit box, Cancel, Start a new run), **Chat**, **Runs**,
**Metrics** and **Settings** (tokens used today against the budget, requests left per model).
Runs work in the background: refreshing or closing the page does not stop them, and a run
waiting for approval is still there after the app restarts (with Supabase connected).

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

Without `SUPABASE_DB_URL` the app still works. It keeps data in memory only (logs still go to
the log file), and it tells you so. If the database drops in the middle of a run, the run stops
with a clear message; an LLM answer that was already paid for is still used.

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
- **Not tried for real yet**: the chatbot's full answers (no Groq key yet). See
  [eval/REPORT.md](eval/REPORT.md).
- **Go**: if your network blocks `proxy.golang.org`, run with `GOPROXY=direct` set; SetupPilot
  passes it to Go, also inside Docker mode.
- **Install scripts run code** from the repo and its dependencies. See
  [docs/SECURITY.md](docs/SECURITY.md); use `--docker` for repos you do not trust.
- **The web app** was tested over HTTP (including a run that survived a restart), not yet
  clicked through in every browser. It is for one person on one machine.
- **Budgets** are checked before each call, but two calls at the same moment (a setup run and a
  chat question) can go a little over the daily token budget.
- Windows is not supported (paths like `.venv/bin/python`).

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
- `eval/`: the question set, the 15-repo set, results and the report.
- Logs: JSON lines in `logs/setuppilot.jsonl`, with secrets hidden.

## Status

| Phase | What | State |
|---|---|---|
| 0 | Skeleton: uv, config, logging, ruff, pytest, CI | done, CI not run yet (not pushed) |
| 1 | Groq layer: BIG/SMALL models, rate limiter, retries, budgets, cache, fake LLM | done; real call needs your key |
| 2 | Supabase: tables, row level security, runs/steps/usage/cache stores, memory fallback | done; real check needs your Supabase URL |
| 3 | Repo tools: URL check, shallow size-checked clone, scan, stack detection, prerequisites, compose finder | done |
| 4 | Guardrails: path guard, command allowlist, safe runner (no shell, no secrets, time limit, output tail), Docker mode | done; Docker mode verified on Colima for Python, Node, Go, Rust, Ruby, Java |
| 5 | Agent: fixed LangGraph flow, approve/reject/edit, pause and resume (Postgres checkpointer), max steps | done; 3 fake + 2 real repos verified |
| 6 | Native setup for Python, Node, Go, Rust, Ruby, Java, Compose, with real checks | done; all 7 verified on real repos (Go with `GOPROXY=direct` on this network) |
| 7 | Observability: JSON step logs with run id, runs/steps/tokens/metrics in the DB, `runs`/`show`/`metrics` commands, optional LangSmith | done |
| 8 | Chatbot: index docs/manifests/config, local embeddings, pgvector search, grounded JSON answers with sources, scope rules, short memory, evaluation script | done; full answers need your Groq key |
| 9 | Flask web app: setup, live run (SSE, approve/reject/edit, cancel), chat, runs, metrics, settings, pre-flight | done; tested over HTTP, not yet clicked through in a browser |
| 10 | Evaluation: 15 setup repos (15/15 stacks, 15/15 verified), 30 chatbot questions (search 52% to 80%), guardrail coverage 100% (CI fails below 90%) | done; full chatbot answers need your key |
| 11 | Docs: this README, security notes, evaluation report, demo script | done |
