# Installs env-wizard's engine prerequisites in user space (no admin): uv, then a Python.
# Run ONLY after the user agreed.
$ErrorActionPreference = 'Stop'
$UvVersion = if ($env:ENV_WIZARD_UV_VERSION) { $env:ENV_WIZARD_UV_VERSION } else { '0.12.23' }
$PyVersion = if ($env:ENV_WIZARD_PY_VERSION) { $env:ENV_WIZARD_PY_VERSION } else { '3.13' }
$EwHome = if ($env:ENV_WIZARD_HOME) { $env:ENV_WIZARD_HOME } else { "$env:USERPROFILE\.env-wizard" }
New-Item -ItemType Directory -Force -Path "$EwHome\logs" | Out-Null
$Log = "$EwHome\logs\bootstrap.log"
$Bin = "$env:USERPROFILE\.local\bin"
$items = @{}

$uv = (Get-Command uv -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $uv -and (Test-Path "$Bin\uv.exe")) { $uv = "$Bin\uv.exe" }
if (-not $uv) {
  Write-Host "Downloading uv $UvVersion..."
  try {
    $env:UV_NO_MODIFY_PATH = '1'
    Invoke-RestMethod "https://astral.sh/uv/$UvVersion/install.ps1" | Invoke-Expression *>> $Log
  } catch {
    Write-Output '{"ok": false, "error": "Could not download uv. Check the internet connection and try again."}'
    exit 1
  }
  $uv = "$Bin\uv.exe"
  $items['uv'] = @{ version = $UvVersion; method = 'uv-standalone'; needs_path_block = $true;
                    paths_created = @("$Bin\uv.exe", "$Bin\uvx.exe", "$Bin\uvw.exe") }
}

Push-Location $env:USERPROFILE
& $uv python find $PyVersion *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Downloading Python $PyVersion..."
  & $uv python install $PyVersion *>> $Log
  if ($LASTEXITCODE -ne 0) {
    Pop-Location
    Write-Output '{"ok": false, "error": "Could not download Python. Check the internet connection and try again."}'
    exit 1
  }
  $items['python'] = @{ version = $PyVersion; method = 'uv-python'; details = @{ uv_request = $PyVersion } }
}
Pop-Location

@{ items = $items; imported = $false } | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 "$EwHome\bootstrap.json"
Write-Output '{"ok": true, "message": "env-wizard is ready."}'
