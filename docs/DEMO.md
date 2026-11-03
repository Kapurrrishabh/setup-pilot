# 5-minute demo

Before: run `uv sync`, put `GROQ_API_KEY` and `SUPABASE_DB_URL` in `.env`, and run
`uv run setuppilot db-check` once. Make an empty folder: `mkdir -p ~/demo`.

1. **The problem (30 s).** "Setting up an unknown repo wastes the first hour, and install scripts
   can run any code. SetupPilot does the setup, but asks before every command."
2. **Start the app (30 s).** `uv run setuppilot web`, open http://127.0.0.1:5005. Show the
   pre-flight checklist (internet, Groq key, Supabase, Docker, folder).
3. **Set up a repo (2 min).** Enter `https://github.com/pypa/sampleproject` and `~/demo`, click
   Start. Steps appear live: clone, scan, detect (python/pip), prerequisites, plan.
   - At the first approval, point out the command, the reason and the warning. Approve.
   - At the second, click **Edit**, try `pip install requests; rm -rf ~`, and show it is blocked.
     Then approve the real command.
   - **Refresh the page** while it installs: nothing is lost.
   - Approve the import check. The report says "Setup verified" only because the checks passed.
4. **Safety (30 s).** Open `docs/SECURITY.md`: allowlist, approval as a graph interrupt,
   secrets kept from install scripts, loopback-only web app.
5. **Chatbot (1 min).** Chat page: index `https://github.com/pypa/sampleproject`, ask "What
   license does it use?" (answer with a source chip), then "Write a function that parses the
   config" (politely refused), then "How many downloads does it have?" ("I could not find this
   in the repo").
6. **Numbers (30 s).** Runs and Metrics pages, then `eval/REPORT.md`: 15/15 stacks detected,
   15/15 repos verified, chatbot search finds the right file for 80% of questions.

If something fails live, say so: the report tells you what failed and why, which is the point.
