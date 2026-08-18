param(
    [string]$Version = (Get-Date -Format "yyyy.MM.dd"),
    [switch]$PackageOnly
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem

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
$frontendDir = Join-Path $projectRoot "frontend"
$outputDir = Join-Path $projectRoot "dist\Tradewind-Portable"
$archiveName = "Tradewind-Portable-$Version.zip"
$archivePath = Join-Path $projectRoot "dist\$archiveName"
$checksumPath = "$archivePath.sha256.txt"

if (-not $PackageOnly) {
    Push-Location $frontendDir
    try {
        & npm run build:check
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend build failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

Push-Location $projectRoot
try {
    if (-not $PackageOnly) {
        & $python -m PyInstaller --noconfirm --clean "Tradewind.spec"
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller failed with exit code $LASTEXITCODE"
        }
    }
    elseif (-not (Test-Path -LiteralPath (Join-Path $outputDir "Tradewind.exe") -PathType Leaf)) {
        throw "PackageOnly requested but Tradewind.exe was not found in $outputDir"
    }

    Copy-Item -LiteralPath "packaging\README.txt" -Destination $outputDir -Force
    Copy-Item -LiteralPath "packaging\portable.flag.example" -Destination $outputDir -Force
    $commit = (& git rev-parse --short HEAD 2>$null)
    if (-not $commit) {
        $commit = "unavailable"
    }
    @(
        "Tradewind Browser Portable Edition"
        "Version: $Version"
        "Source: $commit"
        "Built: $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'))"
    ) | Set-Content -LiteralPath (Join-Path $outputDir "VERSION.txt") -Encoding UTF8

    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    if (Test-Path -LiteralPath $checksumPath) {
        Remove-Item -LiteralPath $checksumPath -Force
    }
    # Use the Windows/.NET ZIP writer so Explorer sees a conventional root
    # directory without "./" entry prefixes. Antivirus may briefly hold a new
    # PyInstaller file, so retry instead of leaving a partial archive.
    $archiveCreated = $false
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        try {
            [IO.Compression.ZipFile]::CreateFromDirectory(
                $outputDir,
                $archivePath,
                [IO.Compression.CompressionLevel]::Optimal,
                $false
            )
            $archiveCreated = $true
            break
        }
        catch [IO.IOException] {
            if (Test-Path -LiteralPath $archivePath) {
                Remove-Item -LiteralPath $archivePath -Force
            }
            if ($attempt -eq 10) {
                throw
            }
            Start-Sleep -Milliseconds (750 * $attempt)
        }
    }
    if (-not $archiveCreated) {
        throw "Release archive was not created"
    }
    $archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
    "$archiveHash  $archiveName" | Set-Content -LiteralPath $checksumPath -Encoding ASCII

    Write-Host ""
    Write-Host "Tradewind portable build: $outputDir"
    Write-Host "Release archive: $archivePath"
    Write-Host "SHA256: $archiveHash"
    Write-Host "Run scripts/test-portable.ps1 before distribution."
}
finally {
    Pop-Location
}
