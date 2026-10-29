<#
.SYNOPSIS
  Create (or update) the Windows scheduled tasks that keep the archive growing.

    "omi daily"  Mon-Fri at -DailyAt (default 19:30): every source, the option-chain snapshot,
                 features, and the PowerPoint report (exports\omi_report_YYYYMMDD.pptx).
    "omi retry"  Tue-Sat at -RetryAt (default 07:30): `omi daily` again, to pick up anything
                 OCC / Cboe published late. The chain snapshot skips itself in the morning
                 (it is only taken on the session's own evening), so the retry never mixes
                 overnight open interest into the previous day.

  Times are the machine's local time zone, which should be US Eastern. Tasks run as the
  current user while logged on (no password stored) and catch up after sleep
  (StartWhenAvailable). If group policy blocks Task Scheduler, run
  scripts\run_daily.ps1 -Report by hand each evening.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -DailyAt 20:00 -NoReport
  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Remove
#>
param(
    [string]$DailyAt = "19:30",
    [string]$RetryAt = "07:30",
    [switch]$NoReport,
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\run_daily.ps1"
$names = @("omi daily", "omi retry")

if ($Remove) {
    foreach ($n in $names) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Host "Removed '$n'"
        }
    }
    exit 0
}

if (-not (Test-Path (Join-Path $root ".venv\Scripts\omi.exe"))) {
    throw "Run scripts\setup.ps1 first (.venv not found)."
}
$tz = (Get-TimeZone).Id
if ($tz -notmatch "Eastern") {
    Write-Warning "This machine's time zone is '$tz', not US Eastern. Pass -DailyAt / -RetryAt in local time so they land after 16:15 ET (the chain snapshot window)."
}

$reportArg = if ($NoReport) { "" } else { " -Report" }
$psArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

$jobs = @(
    @{ Name = "omi daily"; At = $DailyAt; Days = "Monday","Tuesday","Wednesday","Thursday","Friday"; Args = "$psArgs$reportArg";
       Desc = "omi: nightly options-data archive (OCC, Cboe chains), features and report." },
    @{ Name = "omi retry"; At = $RetryAt; Days = "Tuesday","Wednesday","Thursday","Friday","Saturday"; Args = $psArgs;
       Desc = "omi: morning retry for anything published late (skips the chain snapshot)." }
)
foreach ($j in $jobs) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $j.Args -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $j.Days -At $j.At
    Register-ScheduledTask -TaskName $j.Name -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description $j.Desc -Force | Out-Null
    Write-Host ("Registered '{0}' at {1} on {2}" -f $j.Name, $j.At, ($j.Days -join ", "))
}
Write-Host ""
Write-Host "Check with:  Get-ScheduledTask -TaskName 'omi*' | Get-ScheduledTaskInfo"
Write-Host "Run now:     Start-ScheduledTask -TaskName 'omi daily'"
Write-Host "Logs:        logs\omi_YYYYMMDD.log   Report: exports\omi_report_YYYYMMDD.pptx"
