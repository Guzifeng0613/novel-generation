$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$Root = Split-Path -Parent $PSScriptRoot

Push-Location $Root
try {
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 'Python 3.12+ is required')"
    python -m ruff check backend
    python -m pytest
    npm.cmd --prefix frontend run test
    npm.cmd --prefix frontend run build
}
finally {
    Pop-Location
}
