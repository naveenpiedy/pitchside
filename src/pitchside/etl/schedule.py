"""Daily sync via Windows Task Scheduler (F24).

    pitchside schedule install --at 09:00
    pitchside schedule status
    pitchside schedule remove

Registers a per-user task (no admin rights) that runs `pitchside sync` daily,
appending to data/logs/sync.log. It runs late if the machine was off or asleep
at the scheduled time, and only when a network is available.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys

from pitchside.config import DATA_DIR, ROOT

TASK_NAME = "pitchside-sync"
LOG_FILE = DATA_DIR / "logs" / "sync.log"
NEVER_RUN, RUNNING = 267011, 267009  # Task Scheduler's SCHED_S_TASK_HAS_NOT_RUN / SCHED_S_TASK_RUNNING


class ScheduleError(RuntimeError):
    pass


def _powershell(script: str) -> str:
    if sys.platform != "win32":
        raise ScheduleError("Task Scheduler is Windows-only; use cron to run `pitchside sync` elsewhere")
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise ScheduleError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout.strip()


def sync_command() -> tuple[str, str]:
    """(executable, arguments) the task runs."""
    uv = shutil.which("uv")
    if not uv:
        raise ScheduleError("`uv` not found on PATH")
    inner = f'"{uv}" run --directory "{ROOT}" --no-sync pitchside sync >> "{LOG_FILE}" 2>&1'
    return "cmd.exe", f'/s /c "{inner}"'


def install(at: str = "09:00") -> str:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        raise ScheduleError(f"Time must be HH:MM (24h), got {at!r}")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    exe, args = sync_command()
    q = lambda s: s.replace("'", "''")  # PowerShell single-quoted literal
    _powershell(f"""
$a = New-ScheduledTaskAction -Execute '{q(exe)}' -Argument '{q(args)}' -WorkingDirectory '{q(str(ROOT))}'
$t = New-ScheduledTaskTrigger -Daily -At '{at}'
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s `
  -Description 'pitchside: daily Cricsheet sync (see {q(str(LOG_FILE))})' -Force | Out-Null
""")
    return status()


def status() -> str:
    out = _powershell(f"""
$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if ($null -eq $t) {{ 'null' }} else {{
  $i = $t | Get-ScheduledTaskInfo
  [pscustomobject]@{{ State = "$($t.State)"; NextRun = "$($i.NextRunTime)"; LastRun = "$($i.LastRunTime)";
                      LastResult = $i.LastTaskResult; Action = "$($t.Actions[0].Execute) $($t.Actions[0].Arguments)" }} |
    ConvertTo-Json -Compress
}}""")
    info = json.loads(out) if out and out != "null" else None
    if info is None:
        return f"{TASK_NAME}: not installed"
    result = info["LastResult"]
    if result == NEVER_RUN:
        last = "never run"
    else:
        last = f"last run {info['LastRun']} ({'ok' if result == 0 else 'running' if result == RUNNING else f'exit {result}'})"
    return f"{TASK_NAME}: {info['State']}; next run {info['NextRun']}; {last}; log {LOG_FILE}"


def remove() -> str:
    _powershell(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction Stop")
    return f"{TASK_NAME}: removed"
