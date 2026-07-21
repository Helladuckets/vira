<#
Vira on Windows: provision, run, keep running. The run.sh analog.

  powershell -ExecutionPolicy Bypass -File scripts\run.ps1
      Every run, idempotently: find Python 3.10+, create .venv if missing
      (python -m venv --copies), install requirements.txt, then serve
      http://localhost:8377 in the foreground. Re-running reuses whatever
      already exists. On an interactive first run it offers to register
      the start-at-login task (below) instead of serving in the window.

  powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Register
      Create or update the "Vira" Task Scheduler task: starts at sign-in,
      action is this script's -Serve relaunch loop, never stopped by the
      72-hour default execution limit or battery power. Records the task
      name as windows_task_name in data\config.json so the in-app updater
      is allowed to restart the server (server/update.py refuses without
      a supervisor). Add -StartNow to also start it immediately.

  powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Serve
      The supervised relaunch loop the scheduled task runs: serve, and
      when the server exits (crash or a deliberate updater restart),
      relaunch it after 3s. launchd-KeepAlive semantics for Windows.
      Output appends to data\server.log.

  powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Unregister
      Remove the scheduled task and clear windows_task_name.

  powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -DryRun
      Provision (python + venv + dependencies) and report, without
      starting the server. CI exercises this on windows-latest.

Windows PowerShell 5.1 compatible - stock Windows 10/11, no pwsh needed.
#>
[CmdletBinding()]
param(
  [switch]$Serve,
  [switch]$Register,
  [switch]$Unregister,
  [switch]$StartNow,
  [switch]$DryRun,
  [string]$TaskName = "Vira",
  [int]$Port = 8377
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$LogPath = Join-Path $Root "data\server.log"

function Say($msg) { Write-Host "[vira] $msg" }

function Fail($msg) {
  Write-Host ""
  Write-Host "[vira] $msg" -ForegroundColor Red
  exit 1
}

# ---- python discovery ----------------------------------------------------

function Test-Python($exe, $extra) {
  # The Microsoft Store puts a python.exe stub on PATH that opens the Store
  # instead of running Python, so "it exists" proves nothing - only a real
  # version banner counts.
  try {
    $out = & $exe ($extra + @("--version")) 2>&1
    if ($LASTEXITCODE -eq 0 -and "$out" -match "Python 3\.(\d+)") {
      return ([int]$Matches[1] -ge 10)
    }
  } catch { }
  return $false
}

function Find-Python {
  # The leading comma keeps PowerShell from unrolling the array on return
  # (a bare single-element return would hand the caller a string).
  if (Test-Python "py" @("-3")) { return ,@("py", "-3") }
  if (Test-Python "python" @()) { return ,@("python") }
  Fail ("Python 3.10+ not found. Install it from https://www.python.org/downloads/windows/ " +
        "and tick 'Add python.exe to PATH' in the installer, then re-run this script. " +
        "(The Microsoft Store 'python' shortcut does not count.)")
}

# ---- provisioning (idempotent) ------------------------------------------

function Ensure-Venv {
  if (Test-Path $VenvPy) {
    Say ".venv present"
    return
  }
  $py = Find-Python
  $exe = $py[0]
  $extra = @($py | Select-Object -Skip 1)
  Say "creating .venv (python -m venv --copies)"
  & $exe ($extra + @("-m", "venv", "--copies", $Venv))
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPy)) {
    Fail "could not create the virtual environment (.venv)"
  }
}

function Ensure-Deps {
  try { & git --version *> $null } catch { $global:LASTEXITCODE = 1 }
  if ($LASTEXITCODE -ne 0) {
    Fail ("Git is required (a dependency installs straight from GitHub). " +
          "Install Git for Windows: https://git-scm.com/download/win - then re-run.")
  }
  Say "installing dependencies (first run downloads packages; later runs are quick)"
  & $VenvPy -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements.txt")
  if ($LASTEXITCODE -ne 0) { Fail "pip install -r requirements.txt failed (see output above)" }
  Say "dependencies ready"
}

# ---- the scheduled task (the supervisor) --------------------------------

function Write-TaskConfig($name) {
  # Merge windows_task_name into data\config.json (atomic tmp+replace),
  # through the venv python so quoting and encoding stay sane. An empty
  # name clears the key. This is what lets server/update.py restart.
  $code = @'
import json, sys, pathlib
root, name = sys.argv[1], sys.argv[2]
p = pathlib.Path(root) / "data" / "config.json"
p.parent.mkdir(parents=True, exist_ok=True)
try:
    cfg = json.loads(p.read_text())
except Exception:
    cfg = {}
if name:
    cfg["windows_task_name"] = name
else:
    cfg.pop("windows_task_name", None)
tmp = p.with_name("config.json.tmp")
tmp.write_text(json.dumps(cfg, indent=2))
tmp.replace(p)
print("[vira] config windows_task_name = " + (name if name else "(cleared)"))
'@
  & $VenvPy -c $code $Root $name
}

function Register-ViraTask {
  Say "registering scheduled task '$TaskName' (starts at sign-in, relaunches on exit)"
  $action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Serve -Port $Port" `
    -WorkingDirectory $Root
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  # ExecutionTimeLimit zero = no 72-hour kill; battery settings keep a
  # laptop install alive; RestartCount covers the wrapper itself dying.
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Vira personal assistant server" `
    -Force | Out-Null
  Write-TaskConfig $TaskName
  Say "registered. Manage it in Task Scheduler, or:"
  Say "  start:   Start-ScheduledTask -TaskName '$TaskName'"
  Say "  stop:    schtasks /End /TN `"$TaskName`""
  Say "  remove:  powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Unregister"
}

# ---- modes ---------------------------------------------------------------

if ($Unregister) {
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($t) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Say "scheduled task '$TaskName' removed"
  } else {
    Say "no scheduled task named '$TaskName'"
  }
  if (Test-Path $VenvPy) { Write-TaskConfig "" }
  exit 0
}

Ensure-Venv
if ($Serve) {
  # Fast path for supervised relaunches: skip pip unless the venv is brand
  # new. Dependency changes ride the in-app updater, which pip-installs
  # before asking for this restart.
  if (-not (Test-Path (Join-Path $Venv "deps-ok"))) {
    Ensure-Deps
    New-Item -ItemType File -Path (Join-Path $Venv "deps-ok") -Force | Out-Null
  }
} else {
  Ensure-Deps
  New-Item -ItemType File -Path (Join-Path $Venv "deps-ok") -Force | Out-Null
}

if ($Register) {
  Register-ViraTask
  if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Say "task started - Vira is coming up at http://localhost:$Port"
  } else {
    Say "start it now with: Start-ScheduledTask -TaskName '$TaskName'"
  }
  exit 0
}

if ($DryRun) {
  Say "dry run complete: python ok, venv ok, dependencies ok"
  Say "serve with: powershell -ExecutionPolicy Bypass -File scripts\run.ps1"
  exit 0
}

if ($Serve) {
  # The relaunch loop IS the supervisor. The in-app updater restarts by
  # exiting the server process; this loop brings the new code up. The 3s
  # pause keeps a boot-crash from spinning hot.
  New-Item -ItemType Directory -Path (Split-Path -Parent $LogPath) -Force | Out-Null
  Set-Location $Root
  while ($true) {
    if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -gt 5MB)) {
      Clear-Content $LogPath
    }
    "[vira] $(Get-Date -Format s) starting server on port $Port" | Out-File -Append -FilePath $LogPath -Encoding utf8
    & $VenvPy -X utf8 -m uvicorn server.main:app --host 0.0.0.0 --port $Port *>> $LogPath
    "[vira] $(Get-Date -Format s) server exited ($LASTEXITCODE); relaunching in 3s" | Out-File -Append -FilePath $LogPath -Encoding utf8
    Start-Sleep -Seconds 3
  }
}

# Default: interactive first runs get the one-question offer to install the
# supervisor instead of tying Vira to this window. Saying no (or running
# non-interactively) serves in the foreground, exactly like ./run.sh.
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task -and [Environment]::UserInteractive) {
  $ans = Read-Host "Start Vira automatically at sign-in (register a scheduled task)? [y/N]"
  if ($ans -match "^[Yy]") {
    Register-ViraTask
    Start-ScheduledTask -TaskName $TaskName
    Say "task started - open http://localhost:$Port (log: data\server.log)"
    exit 0
  }
  Say "ok - serving in this window. Register later with: powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Register"
}

Say "serving http://localhost:$Port (Ctrl+C stops)"
Set-Location $Root
& $VenvPy -X utf8 -m uvicorn server.main:app --host 0.0.0.0 --port $Port
exit $LASTEXITCODE
