#!/usr/bin/env python3
"""Install the completion notifier without copying private Codex configuration."""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parent
KEYS = ("notify", "model_instructions_file")


def updated_config(original, settings):
    """Replace only complete top-level assignments, preserving unrelated TOML."""
    tomllib.loads(original)
    lines = original.splitlines(keepends=True)
    spans = {}
    start = None
    for index, line in enumerate(lines):
        if start is None:
            stripped = line.lstrip()
            if not stripped.strip() or stripped.startswith("#"):
                continue
            if stripped.startswith("["):
                break
            start = index
        try:
            parsed = tomllib.loads("".join(lines[start:index + 1]))
        except tomllib.TOMLDecodeError:
            continue
        for key in KEYS:
            if key in parsed:
                spans[key] = (start, index + 1)
        start = None

    replacements = {
        key: f"{key} = {json.dumps(settings[key], ensure_ascii=False)}\n"
        for key in KEYS
    }
    for key, (begin, end) in sorted(
        spans.items(), key=lambda item: item[1][0], reverse=True
    ):
        lines[begin:end] = [replacements[key]]
    missing = "".join(replacements[key] for key in KEYS if key not in spans)
    result = missing + "".join(lines)
    parsed = tomllib.loads(result)
    for key in KEYS:
        if parsed[key] != settings[key]:
            raise ValueError(f"Could not safely update top-level {key}")
    return result


def backup(path):
    """Keep existing backups instead of overwriting an earlier recovery point."""
    if not path.exists():
        return
    candidate = Path(str(path) + ".ntfy.bak")
    number = 1
    while candidate.exists():
        candidate = Path(str(path) + f".ntfy.{number}.bak")
        number += 1
    shutil.copy2(path, candidate)
    candidate.chmod(0o600)
    print(f"Backup: {candidate}")


def write_file(path, content, mode):
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {path}")
    if path.exists() and path.read_text() == content:
        print(f"Unchanged: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".ntfy-install-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    print(f"Installed: {path}")


def install(home, replace_existing=False, dry_run=False):
    home = home.expanduser().resolve()
    script = home / ".local/bin/codex-ntfy.py"
    marker = home / ".codex/notification-final-marker.md"
    config = home / ".codex/config.toml"
    settings = {
        "notify": [str(Path(sys.executable).resolve()), str(script)],
        "model_instructions_file": str(marker),
    }
    for path in (script, marker, config):
        if path.is_symlink():
            raise ValueError(f"Refusing to replace a symlink: {path}")
    original = config.read_text() if config.exists() else ""
    current = tomllib.loads(original)
    for key in KEYS:
        value = current.get(key)
        # An interpreter change for this same notifier is an ordinary upgrade.
        same_notifier = (
            key == "notify"
            and isinstance(value, list)
            and len(value) == 2
            and value[1] == str(script)
        )
        if value is not None and value != settings[key] and not same_notifier:
            if not replace_existing:
                raise ValueError(
                    f"Existing {key} points elsewhere. Nothing was installed. "
                    "Review it first; --replace-existing explicitly replaces it."
                )
    new_config = updated_config(original, settings)
    files = (
        (script, (ROOT / "codex-ntfy.py").read_text(), 0o755),
        (marker, (ROOT / "notification-final-marker.md").read_text(), 0o644),
        (config, new_config, 0o600),
    )
    if dry_run:
        print("Dry run: no files changed and no notifications sent.")
        for path, _, _ in files:
            print(f"Would install/update: {path}")
        for key in KEYS:
            print(f"{key} = {json.dumps(settings[key])}")
        return
    for path, content, mode in files:
        write_file(path, content, mode)
    print("Done. Restart Codex to load the settings. No test notification was sent.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home(),
                        help="Target home directory (default: your home)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replace-existing", action="store_true",
                        help="Explicitly replace other notification/instruction settings")
    args = parser.parse_args()
    try:
        install(args.home, args.replace_existing, args.dry_run)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
