"""Daily Windows backup of the explicit isolated pilot; never an implicit DB."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell(script: str) -> dict:
    prefix = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $ErrorActionPreference='Stop'; "
    encoded = base64.b64encode((prefix + script).encode("utf-16-le")).decode("ascii")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                             "-EncodedCommand", encoded], capture_output=True, timeout=45)
    if result.returncode:
        # Native error output can contain environment/configuration details.
        raise RuntimeError("Windows scheduled-task operation failed; task configuration was not accepted")
    return json.loads(result.stdout.decode("utf-8-sig"))


def task_identity(config: Path) -> str:
    key = hashlib.sha256(str(config.resolve()).casefold().encode()).hexdigest()[:12]
    return "DataCenter-Pilot-Backup-" + key


def _locations(config: Path, backups: Path | None = None) -> tuple[Path, Path, Path]:
    from .backup import _plain_path
    from .pilot import _read_config

    _plain_path(config.absolute())
    _, config = _read_config(config)
    backups = _plain_path((backups or config.parent / "backups").absolute(), missing=True)
    receipts = _plain_path(config.parent / "backup-receipts", missing=True)
    # Keep the scheduled write boundary inspectable and local to this trial.
    if (not backups.is_relative_to(config.parent) or backups == config.parent
            or not receipts.is_relative_to(config.parent)):
        raise ValueError("Scheduled backup output must be beneath the explicit pilot root")
    return config, backups, receipts


def run_backup(config: Path, *, backups: Path | None = None) -> dict:
    from .backup import create_backup
    from .pilot import load_settings

    config, backups, receipts = _locations(config, backups)
    receipts.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    receipt = {"started_at": started, "pilot_config": str(config), "status": "FAILED",
               "retention": "Keep at least seven; no automatic deletion or purge"}
    try:
        settings = load_settings(config)
        # The URL is constructed in memory; it never appears in task arguments.
        receipt["backup"] = create_backup(settings.database_url, settings.storage_dir, backups, timeout=3600)
        receipt["status"] = "PASS"
    except Exception as error:
        receipt["error_type"] = type(error).__name__
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    path = receipts / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8] + ".json")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    receipt["receipt"] = str(path)
    return receipt


def _task_contract(config: Path, backups: Path, at: str) -> str:
    if os.name != "nt" or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", at):
        raise ValueError("A Windows daily time in HH:MM is required")
    name = task_identity(config)
    arguments = subprocess.list2cmdline(["-m", "src.server.backup_schedule", "run", "--pilot-config", str(config),
                                        "--backup-dir", str(backups)])
    return f"""
$dcName={_quote(name)}; $dcExe={_quote(sys.executable)}; $dcArgs={_quote(arguments)}; $dcCwd={_quote(str(ROOT))};
$dcDescription={_quote('DataCenter explicit isolated pilot backup: ' + str(config))};
$dcAt={_quote(at)}; $dcTaskPath={_quote(chr(92))};
$dcPrincipal=New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited;
$dcSettings=New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden;
function Assert-DcTask($dcCandidate) {{
  $dcActions=@($dcCandidate.Actions); $dcTriggers=@($dcCandidate.Triggers);
  if ($dcCandidate.TaskPath -ne $dcTaskPath -or $dcActions.Count -ne 1 -or $dcActions[0].Execute -ne $dcExe -or $dcActions[0].Arguments -ne $dcArgs -or $dcActions[0].WorkingDirectory -ne $dcCwd -or $dcCandidate.Description -ne $dcDescription) {{ throw 'Task ownership or action differs' }};
  $dcUser=$dcCandidate.Principal.UserId;
  if ($dcUser -match '^S-1-') {{ $dcSid=$dcUser }} else {{ $dcSid=([System.Security.Principal.NTAccount]::new($dcUser)).Translate([System.Security.Principal.SecurityIdentifier]).Value }};
  if ($dcSid -ne [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value -or $dcCandidate.Principal.LogonType -ne $dcPrincipal.LogonType -or $dcCandidate.Principal.RunLevel -ne $dcPrincipal.RunLevel) {{ throw 'Task principal differs' }};
  if ($dcTriggers.Count -ne 1 -or $dcTriggers[0].CimClass.CimClassName -ne 'MSFT_TaskDailyTrigger' -or $dcTriggers[0].DaysInterval -ne 1 -or -not $dcTriggers[0].Enabled -or ([datetime]$dcTriggers[0].StartBoundary).ToString('HH:mm') -ne $dcAt) {{ throw 'Task daily trigger differs' }};
  foreach ($dcField in @('Enabled','StartWhenAvailable','MultipleInstances','ExecutionTimeLimit','DisallowStartIfOnBatteries','StopIfGoingOnBatteries','Hidden')) {{
    if ($dcCandidate.Settings.$dcField -ne $dcSettings.$dcField) {{ throw 'Task safety settings differ' }};
  }};
}};
"""


def install(config: Path, *, at: str = "03:00") -> dict:
    config, backups, _ = _locations(config)
    script = _task_contract(config, backups, at) + """
$dcExisting=Get-ScheduledTask -TaskPath $dcTaskPath -TaskName $dcName -ErrorAction SilentlyContinue;
if ($dcExisting) {
  Assert-DcTask $dcExisting;
} else {
  $dcAction=New-ScheduledTaskAction -Execute $dcExe -Argument $dcArgs -WorkingDirectory $dcCwd;
  $dcTrigger=New-ScheduledTaskTrigger -Daily -At $dcAt;
  Register-ScheduledTask -TaskPath $dcTaskPath -TaskName $dcName -Action $dcAction -Trigger $dcTrigger -Principal $dcPrincipal -Settings $dcSettings -Description $dcDescription | Out-Null;
};
$dcTask=Get-ScheduledTask -TaskPath $dcTaskPath -TaskName $dcName; $dcInfo=Get-ScheduledTaskInfo -TaskPath $dcTaskPath -TaskName $dcName;
Assert-DcTask $dcTask;
@{task_name=$dcName;state=[string]$dcTask.State;execute=$dcTask.Actions[0].Execute;arguments=$dcTask.Actions[0].Arguments;working_directory=$dcTask.Actions[0].WorkingDirectory;logon_type=[string]$dcTask.Principal.LogonType;start_when_available=$dcTask.Settings.StartWhenAvailable;last_result=$dcInfo.LastTaskResult} | ConvertTo-Json -Compress
"""
    return _powershell(script)


def status(config: Path, *, start: bool = False, at: str = "03:00") -> dict:
    config, backups, _ = _locations(config)
    script = _task_contract(config, backups, at) + "$dcTask=Get-ScheduledTask -TaskPath $dcTaskPath -TaskName $dcName; Assert-DcTask $dcTask; "
    if start:
        script += "Start-ScheduledTask -TaskPath $dcTaskPath -TaskName $dcName; "
    return _powershell(script + """
$dcTask=Get-ScheduledTask -TaskPath $dcTaskPath -TaskName $dcName; $dcInfo=Get-ScheduledTaskInfo -TaskPath $dcTaskPath -TaskName $dcName;
@{task_name=$dcName;state=[string]$dcTask.State;last_result=$dcInfo.LastTaskResult;last_run=$dcInfo.LastRunTime.ToString('o');next_run=$dcInfo.NextRunTime.ToString('o');logon_type=[string]$dcTask.Principal.LogonType} | ConvertTo-Json -Compress
""")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "install", "status", "start"))
    parser.add_argument("--pilot-config", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--at", default="03:00")
    args = parser.parse_args()
    try:
        if args.command == "run":
            result = run_backup(args.pilot_config, backups=args.backup_dir)
        elif args.command == "install":
            result = install(args.pilot_config, at=args.at)
        else:
            result = status(args.pilot_config, start=args.command == "start", at=args.at)
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") != "FAILED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
