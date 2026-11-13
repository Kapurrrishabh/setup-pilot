# 5-minute demo

Before: in PowerShell on Windows (or a terminal on macOS and Linux), run `uv sync`, put
`GROQ_API_KEY` and `SUPABASE_DB_URL` in `.env`, and run `uv run setuppilot db-check` once. Make an
empty folder: `mkdir ~/demo`. To show sign-in too, register your own GitHub App first (README,
"Sign in with GitHub").

1. **The problem (30 s).** "Setting up an unknown repo wastes the first hour, and install scripts
   can run any code. In SetupPilot the AI reads the repo and plans the setup, but SetupPilot's own
   code checks every command and asks you before each one."
2. **Start the app (30 s).** `uv run setuppilot web`, open http://127.0.0.1:5005. Show the
   pre-flight checklist (internet, Groq key, Supabase, Docker, folder). Type `~/demo` as the
   folder.
3. **Set up a repo (2 min).** Enter `https://github.com/pypa/sampleproject` (or sign in and pick
   one of your repos), click Start. Steps appear live: clone, scan, detect, the AI's analysis
   ("the files and the AI agree"), prerequisites, plan.
   - At the first approval, point out the command, the reason and the warning. Approve.
   - At the second, click **Edit**, try `pip install requests; rm -rf ~`, and show it is blocked.
     Then approve the real command.
   - **Refresh the page** while it installs: nothing is lost.
   - Approve the checks. The report says "Setup verified" only because SetupPilot's own checks
     passed, not because the AI said so.
4. **Recovery (1 min, if there is time).** Set up `https://github.com/openai/gpt-2`. Its pinned
   `regex==2017.4.5` does not install on Python 3.12 or newer. The AI explains why (shown as its
   guess) and proposes an older Python, such as `uv venv --clear --python 3.11 .venv`. Approve
   it, and the install runs again. Every fix needs approval, and a command that already failed is
   never offered again.
5. **Safety (30 s).** Open `docs/SECURITY.md`: the allowlist, approval as a graph interrupt, what
   the AI may and may not do, Job Objects on Windows, secrets kept from install scripts, your own
   read-only GitHub App, the loopback-only web app.
6. **Chatbot (1 min).** Chat page: index `https://github.com/pypa/sampleproject`, then ask:
   - "What license does it use?" (MIT, with a source such as `pyproject.toml` or `LICENSE.txt`)
   - "What does add_one do?" (it adds 1 to a number, citing the lines in `src/sample/simple.py`)
   - then the follow-up "Where is it tested?" (`tests/test_simple.py`)
   - "Write a function that parses the config" (politely refused)
   - "How many downloads does it have?" ("I could not find this in the repo")
7. **Numbers (30 s).** Runs and Metrics pages, then `eval/REPORT.md`: 15/15 setup repos verified
   (0.1, AI off), and chatbot search finds the right file for 95% of the answerable questions, up
   from 69% with the 0.1 code.

If something fails live, say so: the report tells you what failed and why, which is the point.
