# Runs the headless smoke scripts on each given engine, one engine at a time, in throwaway projects.
# Only the commandlets started here are waited on / stopped (by PID). Never touches other editors.
# Usage: powershell -ExecutionPolicy Bypass -File run_all_smokes.ps1 -Engines "D:\Unreal\UE_5.7","D:\Unreal\UE_5.8" [-Work dir] [-Site dir] [-Scripts a.py,b.py]
param(
  [string[]]$Engines,
  [string]$Work = (Join-Path $env:TEMP "UE_SceneCaptureTool_smoke"),
  [string]$Site = "",
  [string[]]$Scripts = @("smoke_core.py", "smoke_mrq.py", "smoke_ui.py", "smoke_bg.py")
)

# powershell -File passes "a","b" as one string "a,b"
$Engines = @($Engines | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
$Scripts = @($Scripts | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
if ($Engines.Count -eq 0) {
  Write-Host "Usage: run_all_smokes.ps1 -Engines <engine root>[,<engine root>...] [-Work <dir>] [-Site <site-packages dir>] [-Scripts <py>[,<py>...]]"
  Write-Host "  -Engines  UE install roots, e.g. -Engines `"D:\Unreal\UE_5.7`",`"D:\Unreal\UE_5.8`""
  Write-Host "  -Work     throwaway projects and outputs (default: %TEMP%\UE_SceneCaptureTool_smoke)"
  Write-Host "  -Site     extra site-packages used only when the engine Python lacks numpy/Pillow"
  exit 1
}

New-Item -ItemType Directory -Force $Work | Out-Null
$prevOut = $env:UE_CAPTURE_SMOKE_OUT
$prevSite = $env:UE_CAPTURE_SMOKE_SITE
if ($Site) { $env:UE_CAPTURE_SMOKE_SITE = $Site }

$report = @()
foreach ($eng in $Engines) {
  $exe = Join-Path $eng "Engine\Binaries\Win64\UnrealEditor-Cmd.exe"
  $bv = Join-Path $eng "Engine\Build\Build.version"
  if (-not (Test-Path $exe) -or -not (Test-Path $bv)) { $report += "$eng : SKIP (UnrealEditor-Cmd.exe or Build.version not found)"; continue }
  $v = Get-Content $bv -Raw | ConvertFrom-Json
  $mm = "$($v.MajorVersion).$($v.MinorVersion)"
  $name = "Proj$($v.MajorVersion)$($v.MinorVersion)"
  $projDir = Join-Path $Work $name
  $proj = Join-Path $projDir "$name.uproject"
  if (-not (Test-Path $proj)) {
    New-Item -ItemType Directory -Force $projDir | Out-Null
    $json = '{"FileVersion":3,"EngineAssociation":"' + $mm + '","Plugins":[{"Name":"PythonScriptPlugin","Enabled":true},{"Name":"MovieRenderPipeline","Enabled":true}]}'
    [IO.File]::WriteAllText($proj, $json)
  }
  $env:UE_CAPTURE_SMOKE_OUT = Join-Path $Work "out_$mm"
  foreach ($s in $Scripts) {
    $script = $s
    if (-not [IO.Path]::IsPathRooted($script)) { $script = Join-Path $PSScriptRoot $s }
    $tag = "UE_$($mm)_$([IO.Path]::GetFileName($script))"
    $t0 = Get-Date
    $p = Start-Process -FilePath $exe -ArgumentList @("`"$proj`"", "-run=pythonscript", "-script=`"$script`"", "-unattended", "-nosplash", "-nopause", "-AllowCommandletRendering") -PassThru -RedirectStandardOutput (Join-Path $Work "$tag.out.txt") -RedirectStandardError (Join-Path $Work "$tag.err.txt")
    $ok = $p.WaitForExit(900000)
    if (-not $ok) {
      Stop-Process -Id $p.Id -Force
      $report += "$tag : TIMEOUT after 15 min (pid $($p.Id) stopped)"
      continue
    }
    $log = Get-ChildItem (Join-Path $projDir "Saved\Logs\*.log") -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -ge $t0 } | Sort-Object LastWriteTime | Select-Object -Last 1
    if (-not $log) { $report += "$tag : (no log written; exit=$($p.ExitCode))"; continue }
    $sum = Select-String -Path $log.FullName -Pattern "SUMMARY engine=" | Select-Object -Last 1
    $fails = Select-String -Path $log.FullName -Pattern "\] FAIL " | ForEach-Object { $_.Line -replace '^.*LogPython: ', '' }
    if ($sum) { $line = $sum.Line -replace '^.*LogPython: ', '' } else { $line = "(no SUMMARY; exit=$($p.ExitCode))" }
    $report += "$tag : $line"
    foreach ($f in $fails) { $report += "    $f" }
  }
}

$env:UE_CAPTURE_SMOKE_OUT = $prevOut
$env:UE_CAPTURE_SMOKE_SITE = $prevSite
$report | Out-File (Join-Path $Work "smoke_report.txt") -Encoding utf8
$report
