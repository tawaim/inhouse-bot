# Run every local regression check (see tools/check.py for options).
#   ./check.ps1            everything
#   ./check.ps1 --fast     skip the production-snapshot tests
& "$PSScriptRoot\venv\Scripts\python.exe" "$PSScriptRoot\tools\check.py" @args
exit $LASTEXITCODE
