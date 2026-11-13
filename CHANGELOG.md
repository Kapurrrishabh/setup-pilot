# Changelog

## 0.2.0 - 2026-11-13

Windows first, an AI that reads the repo before SetupPilot plans, GitHub sign-in, and a chatbot
that understands code.

Added:

- Windows as the main system. Each command runs in its own Job Object, so Cancel and the time
  limit stop every process it starts. `.cmd` and `.bat` tools like npm are started safely, and
  Windows paths, venvs and the saved PATH are handled. CI runs the tests on real Windows.
- AI-first setup. The AI reads a summary of the repo and proposes how to set it up. SetupPilot
  checks that against the files and its own plans, which stay as the fallback. Every AI command
  still goes through the allowlist and your approval, and only SetupPilot's own checks say
  "verified". `agent.use_ai_planning: false` turns it off.
- Missing tools: SetupPilot offers to install them (winget on Windows, brew on macOS,
  `uv tool install` for Poetry, PDM and Pipenv), then checks again. On Linux it shows the command.
- Recovery: after a failed command the AI explains why and proposes fixes, each needing your
  approval, at most 2 per step and 3 per run. A failed command is never offered again.
- Monorepos: their projects are offered one by one, or picked with `setuppilot run --subdir`.
- New stacks: .NET, PHP with Composer, Dart and Flutter.
- GitHub sign-in through your own read-only GitHub App: pick from your repos, private ones
  included. The token stays in your system's keyring, and the sign-in is tied to the browser
  that signed in. Repos are read through the GitHub API where no clone is needed, and a
  sub-folder clone downloads only that folder.
- Chatbot v2: whole code files cut at functions, search by meaning and by words together, facts
  from manifests and CI with every question, follow-up questions, and sources as file and line
  links at the commit it read. Search finds the right file for 95% of the answerable test
  questions, up from 69%.
- One test runner for every system: `test.ps1` on Windows and `test.sh` elsewhere, both running
  `scripts/acceptance.py`.
- Evaluation: 3 more setup repos (a monorepo, a .NET repo, and one that needs recovery), and 74
  chat questions (was 30), with code, follow-up and off-topic questions.

Changed:

- The README tips step is gone; the AI's analysis replaces it.
- The run page, the terminal and the evaluation answer two new kinds of question: install a
  missing tool, and do a fix by hand.
- A run started through the sign-in belongs to that GitHub user, and only they see it.

Upgrading from 0.1:

- `config.yaml`: `agent.max_readme_chars`, `agent.plan_max_tokens`, `chat.chunk_chars` and
  `chat.chunk_overlap_chars` are gone. New: `agent.use_ai_planning`, `agent.max_evidence_chars`,
  `agent.analyze_max_tokens`, `agent.diagnose_max_tokens`, `agent.max_install_rounds`,
  `agent.max_fixes_per_step`, `agent.max_fixes_per_run` and the `github` section.
  `agent.max_steps` is now 70 and `chat.answer_max_tokens` 1200. Unknown keys are refused, so an
  old `config.yaml` stops at startup with a clear error: start from the new file and put your own
  values back.
- Database: migration 003 empties the chat index (it had no line numbers), so index each repo
  again. Migration 004 adds `runs.owner_github_id`. Both run by themselves at the first start,
  and each file says how to go back.
- New packages: `httpx` and `keyring`. Run `uv sync`.

## 0.1.0 - 2026-11-03

First public version.

- Setup agent: clone, scan, detect the stack, check tools, plan, ask before every command,
  verify and report, for Python, Node, Go, Rust, Ruby, Java and Docker Compose. Docker mode.
- Repo chatbot that answers from the repo's own files, with sources.
- Web app (setup, live run, chat, runs, metrics, settings) and a command-line interface.
- Run history and metrics in Supabase, with a memory fallback.
- Evaluation sets: 15 setup repos and 30 chatbot questions.
