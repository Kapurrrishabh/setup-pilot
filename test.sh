#!/usr/bin/env bash
# The checks that could not be run on the laptop SetupPilot was built on: real Groq and Supabase,
# other machines, Docker Desktop and other browsers. See "Testing still to do" in README.md.
#
#   ./test.sh                  run every part this machine can run
#   ./test.sh groq supabase    run only these parts
#
# Parts: basics groq supabase setup docker chat browser
# Results go to test-results/<time>-<machine>/ . Send that folder to the team; it has no secrets.
set -uo pipefail
cd "$(dirname "$0")" || exit 2

ALL_PARTS="basics groq supabase setup docker chat browser"
PARTS="${*:-$ALL_PARTS}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="test-results/$STAMP-$(hostname -s)"
# Not under Desktop, Documents or Downloads: on macOS, Docker cannot read those folders.
WORK="${SETUPPILOT_TEST_WORK:-$HOME/setuppilot-test/$STAMP}"
SUMMARY=""
mkdir -p "$OUT" "$WORK"
exec > >(tee "$OUT/test.log") 2>&1

note() { SUMMARY+="$1"$'\n'; echo "$1"; }
pass() { note "PASS  $1"; }
fail() { note "FAIL  $1"; }
skip() { note "SKIP  $1 ($2)"; }
info() { note "INFO  $1"; }

# Runs a command, shows it, and records PASS or FAIL by its exit code.
check() {
  local name="$1"; shift
  echo; echo "== $name"; echo "\$ $*"
  if "$@"; then pass "$name"; else fail "$name"; fi
}

has_setting() { [ -n "${!1:-}" ] || grep -qE "^$1=.+" .env 2>/dev/null; }
has_tool() { command -v "$1" > /dev/null 2>&1; }
wants() { [[ " $PARTS " == *" $1 "* ]]; }
# A fixed sleep is too short on a slow first start and wastes time on a fast one.
wait_for_web() {
  for _ in $(seq 60); do curl -fsS -o /dev/null http://127.0.0.1:5005/ && return 0; sleep 1; done
  return 1
}

for part in $PARTS; do
  [[ " $ALL_PARTS " == *" $part "* ]] || { echo "Unknown part: $part. Pick from: $ALL_PARTS"; exit 2; }
done
has_tool uv || { echo "uv is missing. Install it: https://docs.astral.sh/uv/"; exit 2; }
has_tool git || { echo "git is missing. Install git first."; exit 2; }

info "machine: $(uname -sm), $(hostname -s); parts: $PARTS"
info "results: $OUT ; clones: $WORK"

if wants basics; then
  check "install from the lock file" uv sync --locked
  check "lint" uv run ruff check .
  check "format" uv run ruff format --check .
  check "types" uv run mypy
  [ -n "${TEST_DATABASE_URL:-}" ] || info "TEST_DATABASE_URL is not set, so database tests are skipped (CI runs them)"
  check "unit tests" uv run pytest --cov
  check "guardrail coverage at least 90%" uv run coverage report --include="src/setuppilot/guardrails/*" --fail-under=90
fi

if wants groq; then
  if has_setting GROQ_API_KEY; then
    check "one real Groq call" uv run setuppilot ask "Reply with one word: ready"
  else
    skip "groq" "add GROQ_API_KEY to .env"
  fi
fi

if wants supabase; then
  if has_setting SUPABASE_DB_URL; then
    check "Supabase write and read back" uv run setuppilot db-check
    check "row level security on every table, nothing granted to anon" uv run python - <<'EOF'
import os, sys
import psycopg
from dotenv import load_dotenv
load_dotenv(".env")
with psycopg.connect(os.environ["SUPABASE_DB_URL"], prepare_threshold=None) as conn:
    open_tables = conn.execute(
        "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity"
    ).fetchall()
    grants = conn.execute(
        "select distinct table_name, grantee from information_schema.role_table_grants "
        "where table_schema = 'public' and grantee in ('anon', 'authenticated')"
    ).fetchall()
print("tables without row level security:", [r[0] for r in open_tables] or "none")
print("grants to anon/authenticated:", grants or "none")
sys.exit(1 if open_tables or grants else 0)
EOF
    # Stop at the first question, then carry on in a new process: the run must come back from Supabase.
    echo; echo "== pause a run, then resume it from a new process"
    mkdir -p "$WORK/resume"
    paused="$(printf 'q\n' | uv run setuppilot run https://github.com/pypa/sampleproject "$WORK/resume" 2>&1)"
    echo "$paused"
    run_id="$(echo "$paused" | sed -n 's/.*setuppilot resume \([0-9a-f-]*\).*/\1/p' | tail -1)"
    if [ -z "$run_id" ]; then
      fail "pause a run (no 'setuppilot resume <id>' line above)"
    else
      check "resume the paused run and verify it" sh -c "yes a | head -20 | uv run setuppilot resume $run_id"
    fi
    check "list runs from Supabase" uv run setuppilot runs
  else
    skip "supabase" "add SUPABASE_DB_URL to .env (Session pooler string, see README)"
  fi
fi

# The Compose repo leaves its containers running; stop them so a test run leaves nothing behind.
stop_compose_apps() {
  has_tool docker || return 0
  find "$WORK" -maxdepth 4 \( -name compose.yaml -o -name compose.yml -o -name docker-compose.yml \
    -o -name docker-compose.yaml \) | while read -r file; do
    docker compose -f "$file" down --volumes --remove-orphans > /dev/null 2>&1 \
      && echo "Stopped the containers of $file"
  done
}

# Prints the Total row of the newest eval table, and fails unless every repo was verified.
all_verified() {
  local table; table="$(ls -t "$1"/setup-*.md 2>/dev/null | head -1)"
  [ -n "$table" ] || { echo "No result table in $1"; return 1; }
  grep "Total" "$table"
  grep "Total" "$table" | awk -F'|' '{ split($6, v, " "); split(v[1], n, "/"); exit (n[1] == n[2] ? 0 : 1) }'
}

# Prints the Total row of the newest chat table for a mode, and fails if grounding is under the
# spec's 80% target.
grounded_80() {
  local table; table="$(ls -t "$1"/chat-"$2"-*.md 2>/dev/null | head -1)"
  [ -n "$table" ] || { echo "No result table in $1"; return 1; }
  grep "Total" "$table"
  grep "Total" "$table" | awk -F'|' '{ split($5, v, " "); split(v[1], n, "/"); exit (n[1] * 100 >= n[2] * 80 ? 0 : 1) }'
}

if wants setup; then
  echo; echo "== setup evaluation: 15 real repos, on this machine (a few minutes)"
  info "repos need their tools: uv, node+npm, pnpm, poetry, go, cargo, ruby+bundler, java+maven, docker. A missing tool shows as PrereqMissing."
  uv run setuppilot eval-setup --workspace "$WORK/eval" --results "$OUT/setup"
  check "every eval repo verified" all_verified "$OUT/setup"
fi

if wants docker; then
  if has_tool docker && docker info > /dev/null 2>&1; then
    echo; echo "== Docker mode: the eval repos whose package manager has a Docker image"
    uv run python - "$OUT/docker-repos.yaml" <<'EOF'
import sys
import yaml
from setuppilot.guardrails.docker_mode import DOCKER_IMAGES
repos = yaml.safe_load(open("eval/setup_repos.yaml"))["repos"]
# Compose repos run docker compose itself, so they are in too.
kept = [r for r in repos if r["expected_manager"] in DOCKER_IMAGES or r["expected_manager"] == "compose"]
yaml.safe_dump({"repos": kept}, open(sys.argv[1], "w"), sort_keys=False)
print(len(kept), "repos:", ", ".join(r["url"].rsplit("/", 1)[1] for r in kept))
EOF
    uv run setuppilot eval-setup --docker --repos "$OUT/docker-repos.yaml" --workspace "$WORK/docker" --results "$OUT/docker"
    check "every Docker-mode repo verified" all_verified "$OUT/docker"
    echo; echo "== can a container reach the web app? (known problem, see docs/SECURITY.md)"
    uv run setuppilot web > "$OUT/web-docker.log" 2>&1 &
    web_pid=$!
    wait_for_web || fail "web app did not start (see $OUT/web-docker.log)"
    if docker run --rm --add-host host.docker.internal:host-gateway python:3.12-slim python -c \
      "import urllib.request as u; u.urlopen(u.Request('http://host.docker.internal:5005/', headers={'Host': '127.0.0.1:5005'}), timeout=5)" \
      > /dev/null 2>&1; then
      info "a container CAN reach the web app on this machine (expected; close the app during --docker runs)"
    else
      info "a container could NOT reach the web app on this machine"
    fi
    kill "$web_pid" 2> /dev/null; wait "$web_pid" 2> /dev/null
  else
    skip "docker" "start Docker (Docker Desktop, or colima start) first"
  fi
fi

if wants chat; then
  echo; echo "== chatbot search only (no LLM)"
  uv run setuppilot eval-chat --mode retrieval --results "$OUT/chat"
  check "chatbot search finds the right file for 80%+" grounded_80 "$OUT/chat" retrieval
  if has_setting GROQ_API_KEY; then
    echo; echo "== chatbot full answers (30 questions, real Groq)"
    uv run setuppilot eval-chat --mode full --results "$OUT/chat"
    check "chatbot answers grounded for 80%+" grounded_80 "$OUT/chat" full
    if has_setting SUPABASE_DB_URL; then
      check "chatbot replay from cached answers (no new LLM calls)" uv run setuppilot eval-chat --mode replay --results "$OUT/chat"
    else
      skip "chat replay" "the answer cache only survives a restart in Supabase"
    fi
  else
    skip "chat full answers" "add GROQ_API_KEY to .env"
  fi
fi

if wants browser; then
  if (echo > /dev/tcp/127.0.0.1/5005) 2> /dev/null; then
    fail "browser (something is already using port 5005; stop it first)"
  else
    uv run setuppilot web > "$OUT/web.log" 2>&1 &
    web_pid=$!
    if wait_for_web; then
      check "click through the web app in a browser" \
        uv run --group e2e python scripts/browser_check.py --work "$WORK/browser"
    else
      fail "web app did not start (see $OUT/web.log)"
    fi
    kill "$web_pid" 2> /dev/null; wait "$web_pid" 2> /dev/null
    info "also click through it by hand in Firefox and Safari (see README)"
  fi
fi

stop_compose_apps
echo; echo "======== summary ========"
printf "%s" "$SUMMARY" | tee "$OUT/summary.txt"
echo "Clones are in $WORK (delete it when done; on macOS Go may need chmod -R u+w first)."
! grep -q "^FAIL" "$OUT/summary.txt"
