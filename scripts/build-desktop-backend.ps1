$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonCandidates = @(
    $env:TRADEWIND_PYTHON,
    (Join-Path $projectRoot ".venv\Scripts\python.exe"),
    (Join-Path $projectRoot "venv\Scripts\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }

if (-not $pythonCandidates) {
    throw "Python 3.13 not found. Set TRADEWIND_PYTHON to python.exe."
}

$python = @($pythonCandidates)[0]
Push-Location $projectRoot
try {
    & $python -m PyInstaller --noconfirm "TradewindBackend.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    $source = Join-Path $projectRoot "dist\tradewind-backend.exe"
    $binaryDir = Join-Path $projectRoot "frontend\src-tauri\binaries"
    $target = Join-Path $binaryDir "tradewind-backend-x86_64-pc-windows-msvc.exe"
    New-Item -ItemType Directory -Path $binaryDir -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $target -Force
    Write-Host "Tradewind backend sidecar: $target"
}
finally {
    Pop-Location
}
