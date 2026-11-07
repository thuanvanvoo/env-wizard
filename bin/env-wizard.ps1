# env-wizard launcher for Windows (PowerShell 5.1+). Mirrors bin/env-wizard.
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

if ($args.Count -gt 0 -and $args[0] -eq 'healthcheck') {
  $P = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
  if (-not (Test-Path "$P\.env-wizard\state.json") -and -not (Test-Path "$P\env-wizard.yaml")) { exit 0 }
}

function Test-Python($exe) {
  if (-not $exe) { return $false }
  if (-not (Test-Path $exe)) { return $false }
  # The Microsoft Store "python" alias opens the Store instead of running Python.
  if ($exe -like '*\WindowsApps\*') { return $false }
  & $exe -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>$null | Out-Null
  return ($LASTEXITCODE -eq 0)
}

$Py = $env:ENV_WIZARD_PYTHON
if (-not (Test-Python $Py)) {
  $Py = $null
  foreach ($c in @('python', 'python3')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd -and (Test-Python $cmd.Source)) { $Py = $cmd.Source; break }
  }
}
if (-not $Py) {
  $uv = (Get-Command uv -ErrorAction SilentlyContinue | Select-Object -First 1).Source
  if (-not $uv -and (Test-Path "$env:USERPROFILE\.local\bin\uv.exe")) { $uv = "$env:USERPROFILE\.local\bin\uv.exe" }
  if ($uv) {
    Push-Location $env:USERPROFILE
    $cand = (& $uv python find '>=3.10' 2>$null | Select-Object -Last 1)
    Pop-Location
    if (Test-Python $cand) { $Py = $cand }
  }
}
if (-not $Py) {
  if ($args.Count -gt 0 -and $args[0] -eq 'healthcheck') { exit 0 }
  $boot = (Join-Path $Root 'bin\bootstrap.ps1') -replace '\\', '\\'
  Write-Output "{""ok"": false, ""needs_engine"": true, ""bootstrap"": ""$boot"", ""download_mb"": 60, ""needs_password"": false, ""message"": ""env-wizard needs its small helper (uv and Python, about 60 MB, no password) before it can look at this computer.""}"
  exit 3
}

$env:ENV_WIZARD_ROOT = $Root
$env:PYTHONIOENCODING = 'utf-8'
& $Py -I -c "import sys; sys.path.insert(0, sys.argv.pop(1)); from env_wizard.cli import main; sys.exit(main(sys.argv[1:]))" (Join-Path $Root 'src') @args
exit $LASTEXITCODE
