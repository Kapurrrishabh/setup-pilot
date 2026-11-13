# Evaluation report

Two rounds so far, both on one MacBook (Apple silicon) with a local Postgres standing in for
Supabase:

- **0.1** (2026-11-02), with **no Groq key**: the setup flow without the LLM, and chatbot *search*
  (retrieval) only.
- **0.2** (2026-11-12): the chatbot's search was rebuilt and measured again on a bigger question
  set, still without the LLM. The setup evaluation has not been run with v2 yet, with the AI on
  or off (see "Not measured yet").

Raw results of the 0.1 runs: `eval/results/*.jsonl` (one line per repo or question) and the
matching `.md` tables.

## Setup agent: 15 small public repos (0.1, AI off)

`uv run setuppilot eval-setup` sets up every repo in `eval/setup_repos.yaml` in a fresh folder.
It approves every step the run needs and rejects the AI's optional steps and checks, so every run
is the same; it stops at a missing tool or a fix to do by hand, so it never installs a tool or
waits for a person. (0.1 rejected README suggestions instead.) "Verified" means the real checks
passed (for example `uv pip check` and an import test, `npm ls`, `cargo build`, `bundle check`,
`mvn compile`), not just exit codes.

`uv run setuppilot eval-setup --docker` runs the same set in Docker mode (Poetry and pnpm repos
are not supported there yet; `.\test.ps1 docker` and `./test.sh docker` leave them out). Its
scores go to `setup-docker.jsonl` and `setup-docker-<time>.md`, so a Docker run never carries on
from native scores. The evaluation does not stop at a Groq limit: the agent carries on without
the LLM (no AI analysis, fixes or summary) and the repo is still saved as done, so after a limit
the Tokens and Approvals columns under-count. To measure tokens again, delete
`eval/results/setup.jsonl` and rerun once the limit has reset.

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

**0.2 adds 3 repos** to the set, 18 in all: fastapi/full-stack-fastapi-template (a monorepo with a
uv backend, a bun frontend and Compose files), ardalis/GuardClauses (.NET) and openai/gpt-2 (its
pinned install fails on Python 3.12 and newer, so it tests recovery). Their expected values come
from SetupPilot's detector and planner on real shallow clones with the AI off.

## Chatbot (0.2): 74 questions over 5 repos (search only)

`uv run setuppilot eval-chat --mode retrieval` asks the questions in `eval/chat_questions.yaml`
and scores what search finds, with no LLM. The set has 25 base questions, 28 about the code, 11
follow-ups (asked right after an earlier question) and 10 that only the LLM can score (5 the repo
cannot answer, 5 off-topic). "Right file" means search returned a file that holds the answer.
"Facts" means every expected fact is in the text the LLM would get (the repo card and the chunks
found).

Before and after, on the same questions (the set as it was before review, searched in memory):

| Questions | 0.1 code: right file | 0.1 code: facts | 0.2 code: right file | 0.2 code: facts |
|---|---|---|---|---|
| Base (25) | 20 | 19 | 24 | 25 |
| Code (28) | 16 | 13 | 27 | 23 |
| Follow-up (11) | 8 | 6 | 11 | 11 |
| All (64) | 44 (69%) | 38 (59%) | **62 (97%)** | **59 (92%)** |

The review then changed 7 questions whose facts could not fail (4 rewritten, 3 now matched inside
longer words) and fixed two chunking problems. Final numbers on the committed set:

| Questions | In memory: right file | In memory: facts | Postgres: right file | Postgres: facts |
|---|---|---|---|---|
| Base (25) | 24 | 25 | 23 | 24 |
| Code (28) | 26 | 24 | 25 | 22 |
| Follow-up (11) | 11 | 11 | 11 | 11 |
| All (64) | **61 (95%)** | **60 (94%)** | **59 (92%)** | **57 (89%)** |

In memory the word search is BM25; on Postgres it is Postgres's own full-text search, which is
what a Supabase install uses.

What made the difference:

- Whole code files are read (0.1 read only their first lines), cut at functions, classes and
  headings, and sized to the embedding model's 256-token window: 0 of 540 chunks are over it,
  where in 0.1, 37 of 233 were cut off.
- Search by words runs next to search by meaning, and the two lists are joined by rank, so an
  exact name like `isOriginAllowed` is found even when its meaning is not close.
- A repo card (facts from the manifests and CI, such as versions and dependencies) goes with
  every question.
- A follow-up is searched together with the question before it.
- The LLM is skipped only when no chunk is close in meaning and no word of the question is in the
  repo. That skips 3 of the 5 off-topic questions and none of the 64 answerable ones.

These were measured during development on 2026-11-12, and their raw files are not in
`eval/results`. Run `uv run setuppilot eval-chat --mode retrieval` to make them again.

## Chatbot (0.1): 30 questions over 5 repos (search only)

`uv run setuppilot eval-chat --mode retrieval` asked the 30 questions of the 0.1 set and checked
whether search returned the file that holds the answer. 25 questions can be answered from the
repo; 5 cannot.

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

- **Setup with the AI on and off** (next): run `uv run setuppilot eval-setup` on all 18 repos
  with the AI on, then again with `agent.use_ai_planning: false`, on Windows and on Linux, and
  compare stack, install and verified, plus tokens and approvals per run. So far there are only
  single real runs: sampleproject was verified with 2 LLM calls and about 3,500 tokens, and gpt-2
  recovered with 2 AI fixes in 5 calls and about 7,200 tokens.
- **Chatbot full answers and "I don't know"** (next): search alone cannot tell an unanswerable
  question from an answerable one, so this depends on the LLM saying `found: false`. Run
  `uv run setuppilot eval-chat` with a Groq key. Every answer is cached, so `--mode replay`
  repeats it for free, and a stopped run carries on when re-run. Scores are saved per question in
  `eval/results/chat-<mode>.jsonl`. After you change chat settings, delete that file first, or the
  old scores are reused (the run prints how many). After a change to chunk or embedding settings,
  also index each repo again with `uv run setuppilot index <url>`, because the evaluation indexes
  a repo again only when its embedding model changed.
