# The checks for other machines (see "Testing still to do" in README.md). They live in
# scripts/acceptance.py; this only runs it. test.sh does the same on macOS and Linux.
#
#   .\test.ps1                  run every part this machine can run
#   .\test.ps1 groq supabase    run only these parts
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Output "uv is missing. Install it: https://docs.astral.sh/uv/"
    exit 2
}
# Push and Pop, so the PowerShell window is left in the folder it was in.
Push-Location $PSScriptRoot
try {
    uv run python scripts/acceptance.py @args
} finally {
    Pop-Location
}
exit $LASTEXITCODE
