$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$Root = Split-Path -Parent $PSScriptRoot

Push-Location $Root
try {
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 'Python 3.12+ is required')"
    node -e "const [major, minor] = process.versions.node.split('.').map(Number); if (!((major === 20 && minor >= 19) || (major === 22 && minor >= 12) || major > 22)) { console.error('Node.js 20.19+ or 22.12+ is required'); process.exit(1) }"
    python -m pip install -e .
    python -m pip install pytest pytest-asyncio ruff
    npm.cmd --prefix frontend install
}
finally {
    Pop-Location
}
