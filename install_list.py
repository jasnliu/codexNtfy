#!/usr/bin/env python3
"""Install the optional /list companion, without changing Codex's settings."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import venv

from list_bridge import Config, DEFAULT_CONFIG


ROOT = Path(__file__).resolve().parent
LABEL = "com.local.codex-ntfy-list"


def make_config(existing=None, join=None, topic=None, device_name=None, role=None):
    if existing:
        values = asdict(existing)
    else:
        values = {
            "server": "https://ntfy.sh",
            "topic": "codex-list-" + secrets.token_hex(16),
            "bus_topic": "codex-list-bus-" + secrets.token_hex(16),
            "bus_secret": secrets.token_hex(32),
            "role": "combined",
            "device_name": socket.gethostname().split(".")[0],
            "device_id": secrets.token_hex(16),
        }
    if join:
        for key in ("server", "topic", "bus_topic", "bus_secret", "token_env"):
            values[key] = getattr(join, key)
        values["role"] = "agent"
    if topic:
        values["topic"] = topic
    if device_name:
        values["device_name"] = device_name
    if role:
        values["role"] = role
    return Config(**values)


def config_text(config):
    return "# Local bridge settings/secrets. Do not commit or share publicly.\n" + "".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}\n"
        for key, value in asdict(config).items()
    )


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".ntfy-list-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def launch_agent(python, script, config_path, log_dir):
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(script), "--config", str(config_path)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(log_dir / "list.stdout.log"),
        "StandardErrorPath": str(log_dir / "list.stderr.log"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--topic", help="Explicit command/reply topic; defaults to a random topic")
    parser.add_argument("--device-name")
    parser.add_argument("--role", choices=("combined", "agent", "coordinator"))
    parser.add_argument("--join-config", type=Path,
                        help="Read shared cluster settings from a securely transferred config; defaults to agent role")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-service", action="store_true", help="Install without starting a macOS LaunchAgent")
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    try:
        existing = Config.load(config_path) if config_path.exists() else None
        join = Config.load(args.join_config) if args.join_config else None
        config = make_config(existing, join, args.topic, args.device_name, args.role)
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, f"Invalid configuration: {error}\n")
    home = Path.home()
    app_dir = home / ".local/share/codex-ntfy-bridge/app"
    python = app_dir / "venv/bin/python"
    script = app_dir / "list_bridge.py"
    log_dir = home / "Library/Logs/codex-ntfy-bridge" if sys.platform == "darwin" else home / ".local/state/codex-ntfy-bridge"
    plist = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    if args.dry_run:
        print(f"Would install listing companion: {app_dir}")
        print(f"Would write local config: {config_path}")
        print(f"Role: {config.role}; device: {config.device_name}")
        print("Would install websockets and certifi in an isolated virtual environment.")
        print("No files changed, no service started, no ntfy messages sent.")
        return
    # Stop this companion only; never stop/restart Codex or its daemon.
    if sys.platform == "darwin" and not args.no_service:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    app_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    venv.EnvBuilder(with_pip=True).create(app_dir / "venv")
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                    "-r", str(ROOT / "requirements-list.txt")], check=True)
    atomic_write(script, (ROOT / "list_bridge.py").read_bytes(), 0o700)
    if config_path.exists() and config_path.read_text() != config_text(config):
        backup = config_path.with_name(config_path.name + ".backup-" + secrets.token_hex(4))
        shutil.copy2(config_path, backup)
        backup.chmod(0o600)
    atomic_write(config_path, config_text(config).encode())
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform == "darwin" and not args.no_service:
        atomic_write(plist, plistlib.dumps(launch_agent(python, script, config_path, log_dir)))
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True)
        print("Started the /list companion. It will also start at login.")
    else:
        print("Installed. Run the companion with:")
        import shlex
        print(shlex.join([str(python), str(script), "--config", str(config_path)]))
    print(f"Role: {config.role}; device: {config.device_name}")
    print(f"Topic: {config.server}/{config.topic}")
    print("Send exactly /list to that topic (leave ntfy message caching enabled).")
    print("Only /list is recognized; no Codex prompts can be sent.")
    print("Codex configuration and existing completion notifications were not changed.")


if __name__ == "__main__":
    main()
