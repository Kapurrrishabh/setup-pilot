#!/usr/bin/env bash
# The checks for other machines (see "Testing still to do" in README.md). They live in
# scripts/acceptance.py; this only runs it. test.ps1 does the same on Windows.
#
#   ./test.sh                  run every part this machine can run
#   ./test.sh groq supabase    run only these parts
cd "$(dirname "$0")" || exit 2
command -v uv > /dev/null 2>&1 || { echo "uv is missing. Install it: https://docs.astral.sh/uv/"; exit 2; }
exec uv run python scripts/acceptance.py "$@"
