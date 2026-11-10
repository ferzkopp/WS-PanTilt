"""Preview or apply Raspberry Pi 5 UART, camera and user-access settings."""

from __future__ import annotations

import argparse
import getpass
import grp
import os
import platform
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


RASPI_CONFIG = "/usr/bin/raspi-config"
USERMOD = "/usr/sbin/usermod"
MODEL_FILE = Path("/proc/device-tree/model")
BOOT_FILES = (Path("/boot/firmware/config.txt"), Path("/boot/firmware/cmdline.txt"))
BACKUP_ROOT = Path("/var/backups/pantilt")
SETTINGS = (
    ("Disable the serial login console", "get_serial_cons", "do_serial_cons", "1"),
    ("Enable GPIO UART0", "get_serial_hw", "do_serial_hw", "0"),
    ("Enable camera auto-detection", "get_camera", "do_camera", "0"),
)


def validate_host() -> None:
    model = MODEL_FILE.read_text().rstrip("\x00\n")
    release = platform.freedesktop_os_release()
    if not model.startswith("Raspberry Pi 5 ") or platform.machine() != "aarch64":
        raise RuntimeError("This script supports Raspberry Pi 5 with a 64-bit OS only")
    if release.get("ID") not in {"raspbian", "debian"} or release.get("VERSION_CODENAME") != "trixie":
        raise RuntimeError("This script supports Raspberry Pi OS Trixie only")
    if not Path(RASPI_CONFIG).is_file() or not Path(USERMOD).is_file():
        raise RuntimeError("Required OS tools are missing: raspi-config and usermod")


def validate_boot_config(text: str) -> None:
    section = "all"
    seen: set[str] = set()
    for line in text.splitlines():
        setting = line.split("#", 1)[0].strip()
        if re.match(r"include\s+", setting):
            raise RuntimeError("Custom boot includes require manual configuration; no changes made")
        if re.match(r"(?:cmdline|os_prefix)\s*=", setting):
            raise RuntimeError("Custom boot paths require manual configuration")
        if setting.startswith("[") and setting.endswith("]"):
            section = setting[1:-1]
        candidate = line.strip().lstrip("#").strip()
        if re.match(r"(?:camera_auto_detect\s*=|dtparam\s*=\s*uart0(?:=|\s|$))", candidate):
            if section != "all":
                raise RuntimeError("Conditional UART/camera settings require manual configuration")
            name = "camera_auto_detect" if candidate.startswith("camera_auto_detect") else "uart0"
            if name in seen:
                raise RuntimeError("Duplicate UART/camera settings require manual configuration")
            seen.add(name)
        if setting.startswith("dtparam=") and "," in setting and re.search(r"(?:=|,)\s*uart0(?:=|,|$)", setting):
            raise RuntimeError("Combined UART parameters require manual configuration")
    if section != "all":
        raise RuntimeError("Boot config must end in an [all] section before using raspi-config")


def read_setting(name: str) -> str:
    result = subprocess.run(
        [RASPI_CONFIG, "nonint", name], check=True, capture_output=True, text=True,
    )
    value = result.stdout.strip()
    if value not in {"0", "1"}:
        raise RuntimeError(f"Unexpected response from raspi-config {name}: {value!r}")
    return value


def missing_groups(username: str) -> list[str]:
    account = pwd.getpwnam(username)
    memberships = set(os.getgrouplist(username, account.pw_gid))
    return [name for name in ("dialout", "video") if grp.getgrnam(name).gr_gid not in memberships]


def configuration_plan(username: str) -> list[tuple[str, list[str]]]:
    commands: list[tuple[str, list[str]]] = []
    for description, getter, setter, desired in SETTINGS:
        if read_setting(getter) != desired:
            commands.append((description, [RASPI_CONFIG, "nonint", setter, desired]))
    groups = missing_groups(username)
    if groups:
        commands.append((
            f"Add {username} to {', '.join(groups)}",
            [USERMOD, "-aG", ",".join(groups), username],
        ))
    return commands


def backup_boot_files() -> Path:
    BACKUP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="configure-", dir=BACKUP_ROOT))
    for source in BOOT_FILES:
        shutil.copy2(source, backup / source.name)
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="apply changes; default is preview only")
    mode.add_argument("--dry-run", action="store_true", help="explicitly select preview only")
    parser.add_argument("--user", default=os.environ.get("SUDO_USER") or getpass.getuser(), help="login user; defaults to the sudo caller")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise PermissionError("Run with sudo, including for preview: raspi-config requires root")
        account = pwd.getpwnam(args.user)
        if account.pw_uid == 0 or args.user.startswith("-"):
            raise ValueError("Select a non-root login account with --user")
        validate_host()
        for path in BOOT_FILES:
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"Expected a regular boot file, not a symlink: {path}")
        validate_boot_config(BOOT_FILES[0].read_text())
        commands = configuration_plan(args.user)
        print(f"Target login user: {args.user}")
        for description, command in commands:
            print(f"- {description}")
        if not commands:
            print("Already configured; no files, groups or backups changed.")
        elif not args.apply:
            print("PREVIEW ONLY: no changes made. Run again with --apply to configure.")
        else:
            backup = backup_boot_files()
            print(f"Boot-file backups: {backup}", flush=True)
            for description, command in commands:
                subprocess.run(command, check=True)
            if configuration_plan(args.user):
                raise RuntimeError("Configuration verification failed; review the boot files and backups")
            print("Configuration applied and verified.")
        print("Power off only before changing wiring. Run pantilt doctor after setup.")
        print("Boot settings and new group memberships may require a reboot and fresh login.")
        print("No reboot, power-off, package installation or hardware commands were performed.")
        return 0
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
