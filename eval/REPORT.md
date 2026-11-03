# Evaluation report

Run on 2026-11-02 on one MacBook (Apple silicon), with a local Postgres standing in for Supabase.
There was **no Groq key**, so every number below comes from code that does not call the LLM:
the setup flow without README tips or the AI summary, and chatbot *search* (retrieval) only.
Full chatbot answers still need to be measured with a key (see "Not measured yet").

Raw results: `eval/results/*.jsonl` (one line per repo or question) and the matching `.md` tables.

## Setup agent: 15 small public repos

`uv run setuppilot eval-setup` sets up every repo in `eval/setup_repos.yaml` in a fresh folder. It
approves SetupPilot's own install and check commands and rejects README suggestions, so every run
is the same. "Verified" means the real checks passed (for example `uv pip check` and an import
test, `npm ls`, `cargo build`, `bundle check`, `mvn compile`), not just exit codes.

`uv run setuppilot eval-setup --docker` runs the same set in Docker mode (Poetry and pnpm repos
are not supported there yet; `./test.sh docker` leaves them out). Its scores go to
`setup-docker.jsonl` and `setup-docker-<time>.md`, so a Docker run never carries on from native
scores. The evaluation does not stop at a Groq limit: the agent carries on without the LLM (no
README tips, no AI summary) and the repo is still saved as done, so after a limit the Tokens and
Approvals columns under-count. Stack, install and verified do not use the LLM, so they stay right.
To measure tokens again, delete `eval/results/setup.jsonl` and rerun once the limit has reset.

| Run | What changed | Stack right | Install command right | Verified |
|---|---|---|---|---|
| 1 | First run | 11/15 (73%) | 11/15 (73%) | 11/15 (73%) |
| 2 | Scoring fix: the chosen stack is recorded even when a tool is missing | **15/15 (100%)** | 11/15 (73%) | 11/15 (73%) |
| 3 | Installed Poetry, pnpm and Go on the test laptop | **15/15 (100%)** | 14/15 (93%) | 13/15 (87%) |
| 4 | Installed Docker (Colima, with buildx); Go run with `GOPROXY=direct` | **15/15 (100%)** | **15/15 (100%)** | **15/15 (100%)** |

Final run, per repo:

| Repo | Detected | Verified | Time (s) | Approvals | Note |
|---|---|---|---|---|---|
| pypa/sampleproject | python/pip | yes | 4 | 3 | venv, install, import test |
| tartley/colorama | python/pip | yes | 3 | 3 | uses requirements.txt plus the project, not requirements-dev.txt |
| mahmoud/boltons | python/pip | yes | 3 | 3 |  |
| pallets/itsdangerous | python/uv | yes | 3 | 2 | `uv sync` |
| python-poetry/cleo | python/poetry | yes | 7 | 2 | venv kept inside the repo |
| JedWatson/classnames | node/npm | yes | 4 | 1 |  |
| tj/commander.js | node/npm | yes | 23 | 1 |  |
| nodeca/js-yaml | node/npm | yes | 7 | 1 |  |
| unjs/defu | node/pnpm | yes | 4 | 1 |  |
| dtolnay/itoa | rust/cargo | yes | 5 | 2 | `cargo build` as the check |
| rust-lang/log | rust/cargo | yes | 4 | 2 |  |
| fatih/color | go/go | yes | 12 | 2 | this network blocks proxy.golang.org, so `GOPROXY=direct` was set |
| github/scientist | ruby/bundler | yes | 4 | 2 |  |
| FasterXML/java-classmate | java/maven | yes | 40 | 2 | uses the repo's `./mvnw` |
| dockersamples/wordsmith | docker/compose | yes | 42 | 1 | 7 containers up; warned that port 8080 is open to the network |

Average time 11 s, average 1.9 approvals, 0 LLM tokens (no key). In run 3 the two failures were
outside SetupPilot (network and a missing tool), and both were reported honestly as not verified.

**Docker mode** (`--docker`, installs inside a container) was also run on Colima for one repo per
stack: sampleproject (Python), classnames (Node), itoa (Rust), scientist (Ruby) and
java-classmate (Java) and fatih/color (Go, with `GOPROXY=direct`) were all verified.

## Chatbot: 30 questions over 5 repos (search only)

`uv run setuppilot eval-chat --mode retrieval` asks the 30 questions in `eval/chat_questions.yaml`
and checks whether search returns the file that holds the answer. 25 questions can be answered
from the repo; 5 cannot.

| | Run 1 | Run 2 | Run 3 (final) |
|---|---|---|---|
| Right file found (25 answerable) | 13/25 (52%) | 21/25 (84%) | **20/25 (80%)** |
| What changed | top 5 chunks, cutoff 0.3 | at most 2 chunks per file, cutoff 0.2 | review fixes to file selection (below) |

Run 3 fixed real problems found in review: source files whose names start with a doc word (like
`license_utils.py`) were indexed in full, and licenses deep inside vendored folders counted as the
repo's own. Doc names now count only at the top level, in `docs/` and in `.github/`. One question
lost its hit: slugify's README intro now scores 0.200, right at the cutoff.

How the change was chosen (right file found, out of 25):

| Option | cutoff 0.3 | cutoff 0.2 | cutoff 0.15 |
|---|---|---|---|
| No limit per file | 13 | 20 | 21 |
| At most 2 chunks per file | 14 | **21** | 22 |
| Repo name added to the question, 2 per file | 19 | 20 | 20 |

0.2 was kept, not 0.15: an off-topic question ("What is the capital of France?") scores about
0.16, so 0.2 still answers it with "I could not find this in the repo" without an LLM call.

Per repo in the final run: sampleproject 4/5, itoa 5/5, itsdangerous 3/5, cors 4/5, slugify 4/5.
The five misses: a CI question whose answer is in a workflow file that scores low, two
itsdangerous questions (oldest Python version, how to install) and two "what does it do"
questions, short questions that share few words with the README.

## Not measured yet

- **Chatbot answers and "I don't know"**: search alone cannot tell an unanswerable question
  from an answerable one (no cutoff separated the 5 unanswerable questions), so this depends on
  the LLM saying `found: false`. Run `uv run setuppilot eval-chat` with a Groq key. Every answer
  is cached, so `--mode replay` repeats it for free, and a stopped run carries on when re-run.
  Scores are saved per question in `eval/results/chat-<mode>.jsonl`. After you change chat
  settings, delete that file first, or the old scores are reused (the run prints how many). After
  a change to chunk or embedding settings, also re-index each repo with
  `uv run setuppilot index <url>`, because the evaluation does not re-index a repo it already has.
- **LLM calls per setup run** (target 3 to 6): with a key, each run makes 2 calls (README tips and
  the summary), plus retries if Groq is busy.
