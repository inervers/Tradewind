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

function Start-PortableAppAndWait {
    param([Parameter(Mandatory = $true)][string]$Executable)
    $started = Start-Process -FilePath $Executable -WindowStyle Hidden -PassThru
    for ($i = 0; $i -lt 240; $i++) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8101/api/health" -TimeoutSec 1
            if ($health.status -eq "ok" -and $health.service -eq "tradewind") {
                return $started
            }
        }
        catch {}
        Start-Sleep -Milliseconds 250
    }
    if (-not $started.HasExited) { Stop-Process -Id $started.Id -Force }
    throw "Tradewind health check timed out"
}

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
    $process = Start-PortableAppAndWait -Executable $exePath

    $customer = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8101/api/customers" `
        -ContentType "application/json" `
        -Body (@{ name = "Portable Demo"; country = "香港"; phone = "+852 6123 4567" } | ConvertTo-Json)
    if (-not $customer.ok) { throw "Portable customer fixture was not created" }

    $outreachBody = @{
        customer_id = $customer.item.id
        channel = "whatsapp"
        content = "Hello Portable Demo"
        language = "en"
    } | ConvertTo-Json
    $created = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8101/api/outreach/jobs" `
        -ContentType "application/json" -Body $outreachBody
    if (-not $created.ok -or $created.item.status -ne "pending_review") {
        throw "Outreach draft was not persisted"
    }

    Stop-Process -Id $process.Id -Force
    $process.WaitForExit()
    $process = Start-PortableAppAndWait -Executable $exePath

    $jobs = Invoke-RestMethod -Uri "http://127.0.0.1:8101/api/outreach/jobs"
    if (@($jobs | Where-Object { $_.id -eq $created.item.id }).Count -ne 1) {
        throw "Outreach draft did not survive restart"
    }

    $opened = Invoke-RestMethod -Method Post `
        -Uri ("http://127.0.0.1:8101/api/outreach/jobs/" + $created.item.id + "/open-whatsapp") `
        -ContentType "application/json" -Body "{}"
    if (-not $opened.ok -or $opened.item.status -ne "opened" -or $opened.url -notlike "https://wa.me/*") {
        throw "WhatsApp preparation contract failed"
    }

    $suppression = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8101/api/outreach/suppressions" `
        -ContentType "application/json" `
        -Body (@{ channel = "whatsapp"; recipient = "85261234567"; reason = "portable-test" } | ConvertTo-Json)
    if (-not $suppression.ok) { throw "Suppression creation failed" }

    $duplicate = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8101/api/outreach/jobs" `
        -ContentType "application/json" -Body $outreachBody
    if ($duplicate.ok -ne $false) {
        throw "Suppressed recipient was queued again"
    }
    if ($duplicate.code -ne "suppressed") {
        throw "Suppression rejection did not return the expected machine-readable code"
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
        if ($diagnostic.outreach.jobs_total -ne 1 -or $diagnostic.outreach.by_channel.whatsapp -ne 1) {
            throw "Diagnostic outreach aggregates are incomplete"
        }
        $diagnosticText = [System.Collections.Generic.List[string]]::new()
        foreach ($diagnosticEntry in $diagnosticArchive.Entries) {
            if ($diagnosticEntry.Name -match '\.(json|txt)$') {
                $entryReader = [IO.StreamReader]::new($diagnosticEntry.Open(), [Text.Encoding]::UTF8)
                try { $diagnosticText.Add($entryReader.ReadToEnd()) | Out-Null }
                finally { $entryReader.Dispose() }
            }
        }
        $joinedDiagnostic = $diagnosticText -join "`n"
        foreach ($privateValue in @("Portable Demo", "85261234567", "Hello Portable Demo", "portable-test")) {
            if ($joinedDiagnostic.Contains($privateValue)) {
                throw "Diagnostic archive leaked outreach content"
            }
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
