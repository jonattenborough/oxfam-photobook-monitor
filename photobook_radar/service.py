"""macOS LaunchAgent installation for shadow-mode local operation."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import webbrowser
import socket
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config

LABELS = ("com.jonattenborough.photobook-radar.web", "com.jonattenborough.photobook-radar.worker", "com.jonattenborough.photobook-radar.backup")


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *args], text=True, capture_output=True, check=check)


def _paths(label: str) -> tuple[Path, Path]:
    return Path.home() / "Library/LaunchAgents" / f"{label}.plist", Path.home() / "Library/Application Support/Photobook Radar/logs"


def install(repo: Path) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("This installer supports macOS only")
    config = load_config()
    if config.mode != "shadow" or config.allow_marketplace_network or config.allow_real_notifications:
        raise RuntimeError("Initial local service installation requires shadow mode")
    python = repo / ".venv/bin/python"
    if not python.is_file():
        raise RuntimeError("Install the locked Python environment first")
    release_name = "build-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release = config.data_dir / "releases" / release_name
    release.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copytree(repo, release, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc"))
    # The process must start outside macOS-protected Documents; otherwise
    # launchd can stall while Python reads its virtual environment config.
    python = release / ".venv/bin/python"
    try:
        with socket.socket() as probe:
            probe.bind((config.bind_host, config.port))
    except OSError as exc:
        raise RuntimeError(f"Dashboard port {config.port} is unavailable") from exc
    for label, command in ((LABELS[0], "web"), (LABELS[1], "worker"), (LABELS[2], "backup")):
        path, logs = _paths(label)
        path.parent.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, mode=0o700, exist_ok=True)
        logs.chmod(0o700)
        data = {
            "Label": label,
            "ProgramArguments": [str(python), "-m", "photobook_radar.cli", command],
            "WorkingDirectory": str(release),
            "RunAtLoad": command != "backup",
            "ThrottleInterval": 30,
            "StandardOutPath": str(logs / f"{command}.out.log"),
            "StandardErrorPath": str(logs / f"{command}.err.log"),
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PHOTOBOOK_RADAR_CONFIG": str(config.data_dir / "config.toml")},
        }
        if command == "backup":
            data["StartInterval"] = 3600
        else:
            data["KeepAlive"] = {"SuccessfulExit": False}
        path.write_bytes(plistlib.dumps(data))
        path.chmod(0o644)
        _launchctl("bootout", _domain(), str(path), check=False)
        _launchctl("bootstrap", _domain(), str(path))
        print(f"Installed {label}: {path}")
    print(f"Installed release: {release}")
    shortcut = Path.home() / "Desktop/Photobook Radar.webloc"
    shortcut.write_bytes(plistlib.dumps({"URL": f"http://127.0.0.1:{config.port}"}))
    print(f"Dashboard shortcut: {shortcut}")


def control(action: str) -> None:
    if action == "open":
        webbrowser.open(f"http://127.0.0.1:{load_config().port}")
        return
    if action == "status":
        for label in LABELS:
            result = _launchctl("print", f"{_domain()}/{label}", check=False)
            print(f"{label}: {'loaded' if result.returncode == 0 else 'not loaded'}")
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "state =" in line or "pid =" in line or "last exit code =" in line:
                        print("  " + line.strip())
        return
    if action not in {"start", "stop", "restart", "uninstall-service"}:
        raise ValueError("Unknown service control")
    for label in LABELS:
        path, _ = _paths(label)
        if action in {"stop", "restart", "uninstall-service"}:
            _launchctl("bootout", _domain(), str(path), check=False)
        if action in {"start", "restart"}:
            _launchctl("bootstrap", _domain(), str(path))
        if action == "uninstall-service":
            path.unlink(missing_ok=True)
    print(f"Service action complete: {action}")
