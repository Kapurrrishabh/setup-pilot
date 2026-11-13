# Evaluation report

Two rounds so far, both on one MacBook (Apple silicon) with a local Postgres standing in for
Supabase:

- **0.1** (2026-11-02), with **no Groq key**: the setup flow without the LLM, and chatbot *search*
  (retrieval) only.
- **0.2** (2026-11-12): the chatbot's search was rebuilt and measured again on a bigger question
  set, still without the LLM.
- **0.2 with real Groq** (2026-11-13): the 18-repo setup evaluation with the AI on and off, and
  the chatbot's full answers. Results are in `eval/results/v2-ai/`, `eval/results/v2-no-ai/` and
  `eval/results/v2-chat/`.

## Setup agent (0.2): 18 repos, AI on and off, real Groq

`uv run setuppilot eval-setup`, run twice on the same 18 repos: once as shipped, and once with
`agent.use_ai_planning: false`. The Mac had every tool except the .NET SDK and Maven (java-classmate
brings its own `mvnw`). Go ran with `GOPROXY=direct`, because this network blocks
proxy.golang.org.

| | Stack right | Install right | Verified | Avg time | Avg tokens | Avg approvals |
|---|---|---|---|---|---|---|
| AI on | **18/18** | 17/18 | 17/18 | 55 s | 3,663 | 1.7 |
| AI off | **18/18** | 17/18 | 17/18 | 5 s | 526 | 1.7 |

- **The one miss is the same in both**: ardalis/GuardClauses (.NET) stops at "missing tool",
  because the .NET SDK is not installed here. On Windows SetupPilot offers to install it with
  winget; the evaluation always answers Stop to install questions, so it never installs anything.
- **On these repos the AI does not change the outcome.** All 18 use stacks SetupPilot already has
  hand-written plans and checks for, so the AI and the files agree. What the AI adds shows up
  elsewhere: the analysis card, sub-projects in monorepos (fastapi/full-stack-fastapi-template has
  a Python backend, a Bun frontend and Compose), the start command, and diagnosis and fixes when a
  command fails. This set does not measure those well; a set of unusual repos is future work.
- **Cost.** A run with the AI makes 2 LLM calls (analysis, report) and about 3,700 tokens, inside
  the 30,000-token budget and below the 3 to 6 calls the spec allows. The extra time is mostly waiting for Groq's free-tier
  limit of 8,000 tokens a minute, not the LLM itself.
- **Bug found and fixed by this run.** In the first AI-on pass, 2 repos lost the AI analysis: the
  model used a tool or service name outside the schema's list (Maven, a frontend service), and
  Groq's strict mode refused the whole answer. Those names are now plain text to Groq and mapped
  in code; both repos then got the analysis (4,440 and 5,657 tokens) and still verified.

## Chatbot (0.2): full answers with real Groq

`uv run setuppilot eval-chat --mode full`: the 74 questions, each answered by the LLM from the
retrieved files (follow-ups are asked after their earlier question, in the same chat).

| Kind | Questions | Correct | Grounded | Correct "I don't know" |
|---|---|---|---|---|
| General (setup, license, layout) | 25 | **23/25 (92%)** | **22/25 (88%)** | - |
| Code | 28 | 19/28 (68%) | 19/28 (68%) | - |
| Follow-up | 11 | **9/11 (82%)** | **9/11 (82%)** | - |
| Not in the repo | 5 | - | - | **5/5** |
| Off-topic | 5 | - | - | **5/5** |
| **All answerable** | 64 | **51/64 (80%)** | 50/64 (78%) | **10/10 (100%)** |

About 1,260 tokens per question. "Grounded" means the answer cites the file the fact is in.

- **General questions and follow-ups meet the 80% target; code questions do not (68%).** Two
  kinds of misses: (1) right answers in other words, such as "returns the number plus one" for
  `add_one` (the scorer wants the exact words); (2) real misses where the fact is a default value
  inside a class or function definition (itsdangerous `Signer`'s default salt and key derivation,
  cors's default methods, slugify's error for a non-string). There the bot said it could not find
  the answer instead of guessing, which is the safe failure. Better chunking of class bodies and
  defaults is the next step.
- **It never made up an answer** for the 10 questions the repo cannot answer or that are off-topic.

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

- **The setup evaluation on Windows and Linux**: run `.\test.ps1 setup` (Windows) or
  `./test.sh setup` and compare with the macOS table above.
- **Chatbot replay**: `uv run setuppilot eval-chat --mode replay` needs the answer cache to
  survive a restart, which needs Supabase. Run `uv run setuppilot eval-chat` with a Groq key to
  measure the answers again after a change. Every answer is cached, so `--mode replay`
  repeats it for free, and a stopped run carries on when re-run. Scores are saved per question in
  `eval/results/chat-<mode>.jsonl`. After you change chat settings, delete that file first, or the
  old scores are reused (the run prints how many). After a change to chunk or embedding settings,
  also index each repo again with `uv run setuppilot index <url>`, because the evaluation indexes
  a repo again only when its embedding model changed.
