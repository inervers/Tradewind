param(
    [string]$Version = ((Get-Date -Format "yyyy.MM.dd") + "-min"),
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem

$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseRoot = Join-Path (Join-Path $projectRoot "outputs\release") $Version
$logsRoot = Join-Path $releaseRoot "logs"
$results = [System.Collections.Generic.List[object]]::new()

New-Item -ItemType Directory -Path $logsRoot -Force | Out-Null

if (-not $Python) {
    $pythonCandidates = @(
        $env:TRADEWIND_PYTHON,
        (Join-Path $projectRoot ".venv\Scripts\python.exe"),
        (Join-Path $projectRoot "venv\Scripts\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }

    if (-not $pythonCandidates) {
        throw "Python 3.13 not found. Set -Python or TRADEWIND_PYTHON."
    }
    $Python = @($pythonCandidates)[0]
}

function Add-StepResult {
    param(
        [string]$Name,
        [string]$Status,
        [string]$Log,
        [double]$Seconds,
        [string]$Detail = ""
    )

    $script:results.Add([pscustomobject]@{
        name = $Name
        status = $Status
        seconds = [math]::Round($Seconds, 2)
        log = $Log
        detail = $Detail
    }) | Out-Null
}

function Invoke-AcceptanceStep {
    param(
        [string]$Name,
        [string]$LogName,
        [scriptblock]$Action
    )

    $logPath = Join-Path $logsRoot $LogName
    $started = Get-Date
    Write-Host "[$Name]"
    try {
        & $Action $logPath
        Add-StepResult -Name $Name -Status "passed" -Log ("logs/" + $LogName) -Seconds (((Get-Date) - $started).TotalSeconds)
    }
    catch {
        $message = $_.Exception.Message
        Add-Content -LiteralPath $logPath -Value ("FAILED: " + $message) -Encoding UTF8
        Add-StepResult -Name $Name -Status "failed" -Log ("logs/" + $LogName) -Seconds (((Get-Date) - $started).TotalSeconds) -Detail $message
        Write-Warning "$Name failed: $message"
    }
}

$sourceCommit = (& git -C $projectRoot rev-parse HEAD).Trim()
$shortCommit = (& git -C $projectRoot rev-parse --short HEAD).Trim()
$trackedChanges = @(& git -C $projectRoot status --porcelain --untracked-files=no)
$allowedTrackedPaths = @("scripts/build-portable.ps1")
$zipPath = Join-Path $projectRoot ("dist\Tradewind-Portable-" + $Version + ".zip")
$checksumPath = "$zipPath.sha256.txt"
$extractRoot = Join-Path ([IO.Path]::GetTempPath()) ("tradewind-release-" + [guid]::NewGuid().ToString("N"))

Invoke-AcceptanceStep -Name "source-tree" -LogName "01-source-tree.txt" -Action {
    param($logPath)
    @(
        "commit=$sourceCommit"
        "short_commit=$shortCommit"
        "tracked_changes=$($trackedChanges.Count)"
    ) | Set-Content -LiteralPath $logPath -Encoding UTF8
    if ($trackedChanges.Count -gt 0) {
        $trackedChanges | Add-Content -LiteralPath $logPath -Encoding UTF8
    }
    $unexpectedTrackedChanges = @($trackedChanges | Where-Object {
        $relativePath = $_.Substring(3).Replace("\", "/")
        $relativePath -notin $allowedTrackedPaths
    })
    if ($unexpectedTrackedChanges.Count -gt 0) {
        throw "Unexpected tracked working-tree changes would make the artifact ambiguous."
    }
}

Invoke-AcceptanceStep -Name "detect-secrets" -LogName "02-detect-secrets.txt" -Action {
    param($logPath)
    & $Python -m pre_commit run detect-secrets --all-files 2>&1 | Tee-Object -FilePath $logPath
    if ($LASTEXITCODE -ne 0) {
        throw "detect-secrets exited with code $LASTEXITCODE"
    }
}

Invoke-AcceptanceStep -Name "backend-tests" -LogName "03-backend-tests.txt" -Action {
    param($logPath)
    & $Python -m pytest -q 2>&1 | Tee-Object -FilePath $logPath
    if ($LASTEXITCODE -ne 0) {
        throw "pytest exited with code $LASTEXITCODE"
    }
}

Invoke-AcceptanceStep -Name "offline-quality-eval" -LogName "04-quality-eval.txt" -Action {
    param($logPath)
    Push-Location $projectRoot
    try {
        & $Python "eval\generate_quality_report.py" 2>&1 | Tee-Object -FilePath $logPath
        if ($LASTEXITCODE -ne 0) {
            throw "quality evaluation exited with code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

Invoke-AcceptanceStep -Name "portable-build" -LogName "05-portable-build.txt" -Action {
    param($logPath)
    $oldPython = $env:TRADEWIND_PYTHON
    try {
        $env:TRADEWIND_PYTHON = $Python
        & (Join-Path $PSScriptRoot "build-portable.ps1") -Version $Version 2>&1 | Tee-Object -FilePath $logPath
    }
    finally {
        if ($null -eq $oldPython) {
            Remove-Item Env:TRADEWIND_PYTHON -ErrorAction SilentlyContinue
        }
        else {
            $env:TRADEWIND_PYTHON = $oldPython
        }
    }
    if (-not (Test-Path -LiteralPath $zipPath -PathType Leaf)) {
        throw "Portable ZIP was not created: $zipPath"
    }
}

Invoke-AcceptanceStep -Name "archive-integrity" -LogName "06-archive-integrity.txt" -Action {
    param($logPath)
    if (-not (Test-Path -LiteralPath $zipPath -PathType Leaf)) {
        throw "Portable ZIP is missing."
    }
    if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
        throw "Checksum file is missing."
    }
    $actualHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
    $expectedHash = ((Get-Content -LiteralPath $checksumPath -Raw).Trim() -split '\s+')[0]
    @(
        "archive=$([IO.Path]::GetFileName($zipPath))"
        "expected_sha256=$expectedHash"
        "actual_sha256=$actualHash"
        "bytes=$((Get-Item -LiteralPath $zipPath).Length)"
    ) | Set-Content -LiteralPath $logPath -Encoding UTF8
    if ($actualHash -ne $expectedHash) {
        throw "ZIP SHA256 does not match its checksum file."
    }
}

Invoke-AcceptanceStep -Name "extracted-portable-smoke" -LogName "07-portable-smoke.txt" -Action {
    param($logPath)
    if (-not (Test-Path -LiteralPath $zipPath -PathType Leaf)) {
        throw "Portable ZIP is missing."
    }
    if (Test-Path -LiteralPath $extractRoot) {
        Remove-Item -LiteralPath $extractRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
    [IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $extractRoot)
    & (Join-Path $PSScriptRoot "test-portable.ps1") -PackageDir $extractRoot 2>&1 | Tee-Object -FilePath $logPath
}

if (Test-Path -LiteralPath $extractRoot) {
    Remove-Item -LiteralPath $extractRoot -Recurse -Force
}

$qualityJson = Join-Path $projectRoot "outputs\eval\quality-report.json"
$qualityMarkdown = Join-Path $projectRoot "outputs\eval\quality-report.md"
if (Test-Path -LiteralPath $qualityJson) {
    Copy-Item -LiteralPath $qualityJson -Destination (Join-Path $releaseRoot "quality-report.json") -Force
}
if (Test-Path -LiteralPath $qualityMarkdown) {
    Copy-Item -LiteralPath $qualityMarkdown -Destination (Join-Path $releaseRoot "quality-report.md") -Force
}
$replayRoot = Join-Path $releaseRoot "replay"
New-Item -ItemType Directory -Path $replayRoot -Force | Out-Null
foreach ($scriptName in @("run-minimum-release.ps1", "build-portable.ps1", "test-portable.ps1")) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $scriptName) -Destination (Join-Path $replayRoot $scriptName) -Force
}

$osInfo = Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
$archiveHash = if (Test-Path -LiteralPath $zipPath) { (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash } else { $null }
$datasetPath = Join-Path $projectRoot "eval\fixtures\quality_cases.json"
$datasetHash = if (Test-Path -LiteralPath $qualityJson) {
    (Get-Content -LiteralPath $qualityJson -Raw -Encoding UTF8 | ConvertFrom-Json).dataset.sha256
}
else {
    (Get-FileHash -LiteralPath $datasetPath -Algorithm SHA256).Hash
}
$overall = if (@($results | Where-Object { $_.status -ne "passed" }).Count -eq 0) { "passed" } else { "failed" }

$summary = [ordered]@{
    schema_version = 1
    project = "Tradewind"
    scope = "Browser Portable minimum release acceptance"
    status = $overall
    version = $Version
    source_commit = $sourceCommit
    source_tree_clean = ($trackedChanges.Count -eq 0)
    tracked_changes = $trackedChanges
    generated_at = (Get-Date).ToString("o")
    environment = [ordered]@{
        windows_product = $osInfo.ProductName
        windows_display_version = $osInfo.DisplayVersion
        windows_build = $osInfo.CurrentBuildNumber
        python = (& $Python --version 2>&1 | Out-String).Trim()
        node = (& node --version 2>&1 | Out-String).Trim()
        npm = (& npm --version 2>&1 | Out-String).Trim()
        clean_os = $false
        clean_os_note = "Host is Windows Home without Windows Sandbox or a configured VM; external clean-Windows replay remains required."
    }
    artifact = [ordered]@{
        zip = if (Test-Path -LiteralPath $zipPath) { [IO.Path]::GetFileName($zipPath) } else { $null }
        zip_sha256 = $archiveHash
        dataset_sha256 = $datasetHash
    }
    steps = $results
}

$summaryPath = Join-Path $releaseRoot "acceptance-summary.json"
$summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryPath -Encoding UTF8

$reportLines = [System.Collections.Generic.List[string]]::new()
$reportLines.Add("# Tradewind Minimum Release Acceptance Report") | Out-Null
$reportLines.Add("") | Out-Null
$reportLines.Add("- Status: $overall") | Out-Null
$reportLines.Add("- Version: $Version") | Out-Null
$reportLines.Add("- Source commit: $sourceCommit") | Out-Null
$reportLines.Add("- ZIP SHA-256: $archiveHash") | Out-Null
$reportLines.Add("- Evaluation dataset SHA-256: $datasetHash") | Out-Null
$reportLines.Add("- Scope: Browser Portable minimum release chain; excludes Tauri, SMTP, and real customer data") | Out-Null
$reportLines.Add("") | Out-Null
$reportLines.Add("## Automated acceptance") | Out-Null
$reportLines.Add("") | Out-Null
$reportLines.Add("| Step | Status | Seconds | Log |") | Out-Null
$reportLines.Add("|---|---:|---:|---|") | Out-Null
foreach ($result in $results) {
    $reportLines.Add("| $($result.name) | $($result.status) | $($result.seconds) | $($result.log) |") | Out-Null
}
$reportLines.Add("") | Out-Null
$reportLines.Add("## Evidence boundary") | Out-Null
$reportLines.Add("") | Out-Null
$reportLines.Add("This report covers the fixed source commit, secret scan, backend tests, offline evaluation, frontend build, ZIP integrity, and an extracted-package smoke test with isolated temporary data, health checking, document import, and the diagnostic privacy contract.") | Out-Null
$reportLines.Add("") | Out-Null
$reportLines.Add("This host is Windows Home without Windows Sandbox or a configured VM, so this run is not evidence of replay on a separate clean Windows machine. Copy the ZIP, checksum, and scripts/test-portable.ps1 to a target without Python or Node and replay the smoke test to add that evidence.") | Out-Null
$reportLines | Set-Content -LiteralPath (Join-Path $releaseRoot "acceptance-report.md") -Encoding UTF8

Write-Host ""
Write-Host "Acceptance status: $overall"
Write-Host "Evidence directory: $releaseRoot"
if ($overall -ne "passed") {
    exit 1
}
