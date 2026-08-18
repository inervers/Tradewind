param(
    [string]$PackageDir = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $PackageDir) {
    $PackageDir = Join-Path $projectRoot "dist\Tradewind-Portable"
}
$packageDirPath = (Resolve-Path -LiteralPath $PackageDir).Path
$exePath = Join-Path $packageDirPath "Tradewind.exe"
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "Tradewind.exe not found: $exePath"
}

$forbiddenNames = @(
    ".env", "config.json", "customers.json", "vision_cache.json",
    "crawler_seen.json", "ocr_errors.log", "crawler_errors.log"
)
$forbidden = Get-ChildItem -LiteralPath $packageDirPath -Recurse -File | Where-Object {
    $_.Name -in $forbiddenNames -or
    $_.FullName.Contains("crawler_photos") -or
    $_.FullName.Contains("photo_scan")
}
if ($forbidden) {
    throw "Release contains runtime/private files: $($forbidden.FullName -join ', ')"
}

$tempDataRoot = Join-Path ([IO.Path]::GetTempPath()) ("tradewind-portable-test-" + [guid]::NewGuid().ToString("N"))
$oldDataDir = $env:TRADEWIND_DATA_DIR
$oldNoBrowser = $env:TRADEWIND_NO_BROWSER
$process = $null

function Invoke-MultipartFilePost {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$FilePath
    )

    Add-Type -AssemblyName System.Net.Http
    $client = New-Object System.Net.Http.HttpClient
    $multipart = New-Object System.Net.Http.MultipartFormDataContent
    $fileBytes = [IO.File]::ReadAllBytes($FilePath)
    $fileContent = New-Object System.Net.Http.ByteArrayContent(, $fileBytes)
    $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("application/octet-stream")
    $multipart.Add($fileContent, "file", [IO.Path]::GetFileName($FilePath))
    try {
        $response = $client.PostAsync($Uri, $multipart).GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw "Multipart POST failed: HTTP $([int]$response.StatusCode) $body"
        }
        return ($body | ConvertFrom-Json)
    }
    finally {
        if ($response) { $response.Dispose() }
        $multipart.Dispose()
        $client.Dispose()
    }
}

try {
    $env:TRADEWIND_DATA_DIR = $tempDataRoot
    $env:TRADEWIND_NO_BROWSER = "1"
    $process = Start-Process -FilePath $exePath -WindowStyle Hidden -PassThru

    $health = $null
    for ($i = 0; $i -lt 240; $i++) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8101/api/health" -TimeoutSec 1
            if ($health.status -eq "ok" -and $health.service -eq "tradewind") {
                break
            }
        }
        catch {
            $health = $null
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $health) {
        throw "Tradewind health check timed out"
    }

    $start = Invoke-MultipartFilePost -Uri "http://127.0.0.1:8101/api/templates/extract" -FilePath (Join-Path $packageDirPath "README.txt")
    $result = $null
    for ($i = 0; $i -lt 240; $i++) {
        $result = Invoke-RestMethod -Uri ("http://127.0.0.1:8101/api/extract/tasks/" + $start.task_id)
        if ($result.status -ne "running") {
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if ($result.status -ne "done" -or @($result.items).Count -ne 1 -or $result.ocr) {
        throw "Document import smoke test failed: $($result | ConvertTo-Json -Compress -Depth 5)"
    }

    $diagnosticPath = Join-Path $tempDataRoot "diagnostic.zip"
    Invoke-WebRequest -Uri "http://127.0.0.1:8101/api/diagnostics/export" -OutFile $diagnosticPath
    $diagnosticArchive = [IO.Compression.ZipFile]::OpenRead($diagnosticPath)
    try {
        $entry = $diagnosticArchive.GetEntry("diagnostic.json")
        if (-not $entry) {
            throw "Diagnostic archive does not contain diagnostic.json"
        }
        $reader = [IO.StreamReader]::new($entry.Open(), [Text.Encoding]::UTF8)
        try {
            $diagnostic = $reader.ReadToEnd() | ConvertFrom-Json
        }
        finally {
            $reader.Dispose()
        }
        $privacy = $diagnostic.privacy
        if ($privacy.raw_logs_included -or $privacy.keys_included -or $privacy.customer_content_included -or $privacy.photos_included -or $privacy.email_content_included) {
            throw "Diagnostic privacy contract failed"
        }
    }
    finally {
        $diagnosticArchive.Dispose()
    }

    $hash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash
    Write-Host "Portable smoke test passed."
    Write-Host "EXE SHA256: $hash"
}
finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
    }
    if ($null -eq $oldDataDir) { Remove-Item Env:TRADEWIND_DATA_DIR -ErrorAction SilentlyContinue } else { $env:TRADEWIND_DATA_DIR = $oldDataDir }
    if ($null -eq $oldNoBrowser) { Remove-Item Env:TRADEWIND_NO_BROWSER -ErrorAction SilentlyContinue } else { $env:TRADEWIND_NO_BROWSER = $oldNoBrowser }
    if (Test-Path -LiteralPath $tempDataRoot) {
        Remove-Item -LiteralPath $tempDataRoot -Recurse -Force
    }
}
