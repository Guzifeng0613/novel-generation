[CmdletBinding()]
param(
    [switch]$Demo,
    [switch]$AllowRealApi,
    [string]$DataDir,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$Root = Split-Path -Parent $PSScriptRoot

function Repair-MissingAlembicStamp {
    $NeedsStamp = python -c @'
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

from backend.app.config import get_settings

url = get_settings().database_url
parsed = urlparse(url)
if parsed.scheme != "sqlite":
    print("0")
    raise SystemExit

database_path = unquote(parsed.path)
if database_path.startswith("/") and len(database_path) > 3 and database_path[2] == ":":
    database_path = database_path[1:]
path = Path(database_path)
if not path.exists():
    print("0")
    raise SystemExit

with sqlite3.connect(path) as con:
    tables = {
        row[0]
        for row in con.execute("select name from sqlite_master where type='table'")
    }
    has_core_schema = {"projects", "runs", "tasks"}.issubset(tables)
    if "alembic_version" not in tables or not has_core_schema:
        print("0")
        raise SystemExit
    version_rows = con.execute("select count(*) from alembic_version").fetchone()[0]
    print("1" if version_rows == 0 else "0")
'@
    if ($NeedsStamp.Trim() -eq "1") {
        Write-Host "Existing SQLite schema has no Alembic stamp; stamping current head before upgrade."
        python -m alembic stamp head
    }
}

if ([bool]$Demo -eq [bool]$AllowRealApi) {
    throw "Choose exactly one mode: -Demo (fake Provider, no API charge) or -AllowRealApi (real Provider calls permitted)."
}

if ($Demo) {
    $env:NOVEL_TEST_FAKE_PROVIDER = "1"
    if ([string]::IsNullOrWhiteSpace($DataDir)) {
        $DataDir = Join-Path $Root "data\demo"
    }
}
else {
    # An explicit process value wins over a stale value in .env because load_dotenv uses override=False.
    $env:NOVEL_TEST_FAKE_PROVIDER = "0"
}

if (-not [string]::IsNullOrWhiteSpace($DataDir)) {
    $ResolvedDataDir = if ([System.IO.Path]::IsPathRooted($DataDir)) {
        [System.IO.Path]::GetFullPath($DataDir)
    }
    else {
        [System.IO.Path]::GetFullPath((Join-Path $Root $DataDir))
    }
    $env:NOVEL_DATA_DIR = $ResolvedDataDir
    # Keep an optional .env database override from defeating explicit data isolation.
    $env:NOVEL_DATABASE_URL = ""
}

Push-Location $Root
try {
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 'Python 3.12+ is required')"

    if ($AllowRealApi) {
        python -c "from backend.app.config import get_settings; import sys; s = get_settings(); sys.exit(0 if (s.openai_api_key or s.deepseek_api_key) else 'Set OPENAI_API_KEY or DEEPSEEK_API_KEY on the server before using -AllowRealApi')"
    }

    Repair-MissingAlembicStamp
    python -m alembic upgrade head
    if (-not $SkipBuild) {
        npm.cmd --prefix frontend run build
    }
    elseif (-not (Test-Path -LiteralPath "frontend\dist\index.html")) {
        throw "frontend/dist/index.html is missing; rerun without -SkipBuild."
    }

    # This entry point reads NOVEL_HOST/NOVEL_PORT and serves frontend/dist from FastAPI.
    python -m backend.app.main
}
finally {
    Pop-Location
}
