"""User-level schedule rendering and installation for ADE integrations."""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_TIMES = ("08:00", "12:00", "17:00")
SERVICE_NAME = "ade-plane-linear-sync"
LAUNCHD_LABEL = "com.ade.plane-linear-sync"


class ScheduleError(ValueError):
    """The requested schedule cannot be rendered or installed safely."""


def _validate_times(times: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    value = tuple(times)
    if not value or len(set(value)) != len(value):
        raise ScheduleError("sync schedule must contain unique times")
    for item in value:
        try:
            hour, minute = (int(part) for part in item.split(":", 1))
        except (ValueError, AttributeError):
            raise ScheduleError("sync schedule times must use HH:MM") from None
        if (
            not (0 <= hour <= 23 and 0 <= minute <= 59)
            or len(item) != 5
            or item[2] != ":"
        ):
            raise ScheduleError("sync schedule times must use HH:MM")
    return value


def _command(
    root: Path, provider: str, env_file: Path | None, executable: str | None
) -> list[str]:
    command = [
        executable or sys.executable,
        "-m",
        "ade.cli",
        "--root",
        str(root),
        "sync",
        "--provider",
        provider,
    ]
    if env_file:
        command.extend(["--env-file", str(env_file)])
    return command


def render_systemd(
    root: str | Path,
    provider: str = "plane-linear-sync",
    times: tuple[str, ...] = DEFAULT_TIMES,
    env_file: str | Path | None = None,
    executable: str | None = None,
) -> dict[str, str]:
    times = _validate_times(times)
    root = Path(root).expanduser().resolve()
    env_path = Path(env_file).expanduser().resolve() if env_file else None
    command = shlex.join(_command(root, provider, env_path, executable))
    service = "\n".join(
        [
            "[Unit]",
            "Description=ADE Plane to Linear issue synchronization",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={command}",
            "",
        ]
    )
    timer = "\n".join(
        [
            "[Unit]",
            "Description=Run ADE Plane to Linear synchronization on schedule",
            "",
            "[Timer]",
            *[f"OnCalendar=*-*-* {time}:00" for time in times],
            "Persistent=true",
            f"Unit={SERVICE_NAME}.service",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    return {f"{SERVICE_NAME}.service": service, f"{SERVICE_NAME}.timer": timer}


def render_launchd(
    root: str | Path,
    provider: str = "plane-linear-sync",
    times: tuple[str, ...] = DEFAULT_TIMES,
    env_file: str | Path | None = None,
    executable: str | None = None,
) -> bytes:
    times = _validate_times(times)
    root = Path(root).expanduser().resolve()
    env_path = Path(env_file).expanduser().resolve() if env_file else None
    intervals = []
    for item in times:
        hour, minute = (int(part) for part in item.split(":", 1))
        intervals.append({"Hour": hour, "Minute": minute})
    value: dict[str, Any] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": _command(root, provider, env_path, executable),
        "StartCalendarInterval": intervals,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(root / "state" / provider / "scheduler.stdout.log"),
        "StandardErrorPath": str(root / "state" / provider / "scheduler.stderr.log"),
    }
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=False)


def render(
    platform_name: str,
    root: str | Path,
    provider: str = "plane-linear-sync",
    times: tuple[str, ...] = DEFAULT_TIMES,
    env_file: str | Path | None = None,
    executable: str | None = None,
) -> dict[str, str | bytes]:
    if platform_name == "linux":
        return render_systemd(root, provider, times, env_file, executable)
    if platform_name == "darwin":
        return {
            f"{LAUNCHD_LABEL}.plist": render_launchd(
                root, provider, times, env_file, executable
            )
        }
    raise ScheduleError("ADE scheduling supports Linux systemd and macOS launchd")


def _atomic_write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        "wb", dir=path.parent, prefix=".schedule-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(value.encode() if isinstance(value, str) else value)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def install(
    root: str | Path,
    platform_name: str,
    provider: str = "plane-linear-sync",
    times: tuple[str, ...] = DEFAULT_TIMES,
    env_file: str | Path | None = None,
    executable: str | None = None,
    destination: str | Path | None = None,
    enable: bool = False,
) -> dict[str, Any]:
    """Install only ADE-owned user scheduler files.

    ``destination`` is a test/operator override.  Without it the files live in
    the current user's systemd or launchd directory.  Enabling the timer is
    explicit because it changes the user's process manager state.
    """

    root = Path(root).expanduser().resolve()
    rendered = render(platform_name, root, provider, times, env_file, executable)
    state_directory = root / "state" / provider
    state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_directory, 0o700)
    if destination is not None:
        directory = Path(destination).expanduser().resolve()
    elif platform_name == "linux":
        directory = Path.home() / ".config/systemd/user"
    else:
        directory = Path.home() / "Library/LaunchAgents"
    paths = {}
    for name, content in rendered.items():
        path = directory / name
        _atomic_write(path, content)
        paths[name] = str(path)
    if enable and destination is None and platform_name == "linux":
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.timer"],
            check=True,
        )
    elif enable and destination is None and platform_name == "darwin":
        subprocess.run(
            [
                "launchctl",
                "bootstrap",
                f"gui/{os.getuid()}",
                paths[f"{LAUNCHD_LABEL}.plist"],
            ],
            check=True,
        )
    return {
        "platform": platform_name,
        "provider": provider,
        "times": list(times),
        "files": paths,
        "enabled": enable,
    }


def manifest_schedule(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    schedule = manifest.get("schedule", {})
    if not isinstance(schedule, Mapping):
        raise ScheduleError("provider schedule must be an object")
    return _validate_times(schedule.get("times", DEFAULT_TIMES))
