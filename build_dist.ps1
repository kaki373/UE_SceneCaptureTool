<#
.SYNOPSIS
  Builds the distribution zip UE_SceneCaptureTool_v<VERSION>.zip.

.DESCRIPTION
  Stages Install.bat, VERSION, CHANGELOG.md, README.md, INSTALL.txt,
  requirements.txt, the two design docs and ue5_capture\ (*.py + README.md,
  no __pycache__) in a temp folder and zips it. docs\ (internal working notes)
  is not shipped. The zip layout mirrors the repo root so the README links work.
  Windows PowerShell 5.1 compatible. Keep this file ASCII (5.1 reads BOM-less
  scripts as ANSI).

.PARAMETER OutDir
  Where the zip is written. Default: <repo>\dist (git-ignored).

.PARAMETER Wheels
  Also download win_amd64 / cp311 wheels of requirements.txt into wheels\ so
  Install.bat can install offline. Needs internet at build time; adds ~50 MB.

.PARAMETER Python
  Interpreter used for -Wheels. Default: the bundled Python of the newest
  installed UE 5.8 / 5.7.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File build_dist.ps1
  powershell -ExecutionPolicy Bypass -File build_dist.ps1 -Wheels -OutDir D:\share
#>
param(
    [string]$OutDir = "",
    [switch]$Wheels,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $Root "dist" }

$version = (Get-Content -LiteralPath (Join-Path $Root "VERSION") -TotalCount 1).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$') {
    throw "VERSION is not semantic (x.y.z): '$version'"
}
$name = "UE_SceneCaptureTool_v$version"
Write-Host "Building $name"

# --- stage ------------------------------------------------------------------
$stage = Join-Path $env:TEMP ("{0}_build_{1}" -f $name, [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force (Join-Path $stage "ue5_capture") | Out-Null

$rootFiles = @("Install.bat", "VERSION", "CHANGELOG.md", "README.md", "INSTALL.txt",
               "requirements.txt", "ue5_capture_sequence_spec.md", "ue5_capture_tool_prompt.md")
foreach ($f in $rootFiles) {
    $src = Join-Path $Root $f
    if (-not (Test-Path -LiteralPath $src)) { throw "missing: $f" }
    Copy-Item -LiteralPath $src -Destination $stage
}
# Only the tool body: *.py + README.md. __pycache__ and anything else stay behind.
Get-ChildItem -LiteralPath (Join-Path $Root "ue5_capture") -File |
    Where-Object { $_.Extension -eq ".py" -or $_.Name -eq "README.md" } |
    ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $stage "ue5_capture") }

# Install.bat must be CRLF (cmd mis-resolves labels in LF files) and its batch
# part ASCII; a checkout with core.autocrlf=false would otherwise ship LF.
$batPath = Join-Path $stage "Install.bat"
$bytes = [System.IO.File]::ReadAllBytes($batPath)
$text = [System.Text.Encoding]::UTF8.GetString($bytes)
if ($text -notmatch "`r`n") {
    Write-Host "  Install.bat is LF: converting to CRLF"
    $text = $text -replace "`n", "`r`n"
    [System.IO.File]::WriteAllText($batPath, $text, (New-Object System.Text.UTF8Encoding($false)))
}
$batHead = $text.Substring(0, $text.IndexOf("#===PYTHON==="))
if ($batHead -match '[^\x00-\x7F]') { throw "Install.bat batch part contains non-ASCII characters" }

# --- optional offline wheels -------------------------------------------------
if ($Wheels) {
    if (-not $Python) {
        foreach ($v in @("5.8", "5.7")) {
            $key = "HKLM:\SOFTWARE\EpicGames\Unreal Engine\$v"
            if (Test-Path $key) {
                $dir = (Get-ItemProperty $key).InstalledDirectory
                $cand = Join-Path $dir "Engine\Binaries\ThirdParty\Python3\Win64\python.exe"
                if (Test-Path -LiteralPath $cand) { $Python = $cand; break }
            }
        }
    }
    if (-not $Python) { throw "-Wheels: no UE 5.8 / 5.7 Python found; pass -Python" }
    $wheelDir = Join-Path $stage "wheels"
    Write-Host "  downloading wheels with $Python"
    & $Python -m pip download --disable-pip-version-check --only-binary=:all: `
        --platform win_amd64 --python-version 3.11 --implementation cp `
        --dest $wheelDir -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip download failed ($LASTEXITCODE)" }
}

# --- self-checks -------------------------------------------------------------
if (Get-ChildItem -LiteralPath $stage -Recurse -Directory -Filter "__pycache__") { throw "__pycache__ in stage" }
if (-not (Test-Path -LiteralPath (Join-Path $stage "ue5_capture\capture_menu.py"))) { throw "capture_menu.py missing" }
# Site-specific leftovers (warn only: measurement-history comments are allowed).
$pattern = 'D:[/\\]webui|E:[/\\]work|X:[/\\]|C:[/\\]Users[/\\]\w'
$hits = Get-ChildItem -LiteralPath $stage -Recurse -File |
    Where-Object { @(".py", ".md", ".txt", ".bat") -contains $_.Extension } |
    Select-String -Pattern $pattern
foreach ($h in $hits) {
    Write-Warning ("site-specific path: {0}:{1}: {2}" -f $h.Path.Substring($stage.Length + 1), $h.LineNumber, $h.Line.Trim())
}

# --- zip ---------------------------------------------------------------------
New-Item -ItemType Directory -Force $OutDir | Out-Null
$zip = Join-Path (Resolve-Path -LiteralPath $OutDir).Path "$name.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
# Entries are added one by one: in Windows PowerShell 5.1 both Compress-Archive
# and ZipFile.CreateFromDirectory write "ue5_capture\x.py" entry names
# (backslash), which non-Windows unzip tools turn into flat file names.
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::Open($zip, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    Get-ChildItem -LiteralPath $stage -Recurse -File | Sort-Object FullName | ForEach-Object {
        $rel = $_.FullName.Substring($stage.Length + 1).Replace('\', '/')
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $_.FullName, $rel,
            [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
} finally {
    $archive.Dispose()
}
Remove-Item -LiteralPath $stage -Recurse -Force

$archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
try {
    Write-Host ""
    Write-Host "Contents:"
    foreach ($e in $archive.Entries) { Write-Host ("  {0,10}  {1}" -f $e.Length, $e.FullName) }
    Write-Host ("  {0} entries" -f $archive.Entries.Count)
} finally {
    $archive.Dispose()
}
Write-Host ""
Write-Host ("Wrote {0}  ({1:N0} bytes)" -f $zip, (Get-Item -LiteralPath $zip).Length)
