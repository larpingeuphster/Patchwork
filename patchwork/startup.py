"""Start Patchwork when the user signs in to Windows, as administrator, without a UAC prompt.

Programs that always run as administrator can't start from the usual "Run"
registry key (Windows silently blocks them), so Patchwork registers a Task
Scheduler task that runs at sign-in with highest privileges, the same way
other tools such as Fleasion do. The task is re-registered on each launch so
it always points at the .exe currently in use (each release has its own
file name).
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "Patchwork for RIVALS"
BACKGROUND_FLAG = "--background"


def supported() -> bool:
    """Only the packaged Windows app can be started at sign-in."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def _user() -> str:
    domain = os.environ.get("USERDOMAIN")
    name = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{name}" if domain else name


def task_xml(exe: str, user: str) -> str:
    """Task Scheduler definition: at sign-in, highest privileges, no battery or time limits."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts Patchwork for RIVALS when you sign in. Turn this off in Patchwork's Settings.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
      <Delay>PT10S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <Arguments>{BACKGROUND_FLAG}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def enable(exe: str | None = None) -> str | None:
    """Register (or refresh) the sign-in task. Returns an error message, or None on success."""
    if not supported():
        return "Starting with Windows only works in the installed Patchwork.exe."
    exe = exe or sys.executable
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(path).write_text(task_xml(exe, _user()), encoding="utf-16")
        result = _schtasks("/Create", "/TN", TASK_NAME, "/XML", path, "/F")
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    finally:
        Path(path).unlink(missing_ok=True)
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip() or f"schtasks exited with {result.returncode}"
    return None


def disable() -> str | None:
    if not supported():
        return None
    try:
        result = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if result.returncode != 0 and is_enabled():
        return (result.stderr or result.stdout).strip()
    return None


def is_enabled() -> bool:
    if not supported():
        return False
    try:
        return _schtasks("/Query", "/TN", TASK_NAME).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
