#!/usr/bin/env python3

import json
import subprocess
import sys
import urllib.request
from pathlib import Path


EVENT_TYPE = "agent-turn-complete"
FINAL_MARKER = "<!-- CODEX_NOTIFY_COMPLETE -->"
RECAP_PREFIX = "Write a brief catch-up for a user returning to this Codex task."
RECAP_SECTION = "\nRecent conversation:"
NTFY_URL = "https://ntfy.sh/codex_prompts"
SOUND_SCRIPT = Path.home() / ".codex" / "notify-sound.sh"


def is_internal_recap(input_messages):
    if not isinstance(input_messages, list):
        return False
    return any(
        isinstance(item, str)
        and item.startswith(RECAP_PREFIX)
        and RECAP_SECTION in item
        for item in input_messages
    )


def main(payload_arg):
    try:
        payload = json.loads(payload_arg)
    except (TypeError, json.JSONDecodeError):
        return

    if payload.get("type") != EVENT_TYPE:
        return
    if is_internal_recap(payload.get("input-messages")):
        return

    message = payload.get("last-assistant-message")
    if not isinstance(message, str) or not message.rstrip().endswith(FINAL_MARKER):
        return

    request = urllib.request.Request(
        NTFY_URL,
        data=b"Codex finished responding.",
        headers={"Title": "Codex", "Tags": "white_check_mark"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except Exception:
        return

    if SOUND_SCRIPT.is_file():
        try:
            subprocess.run(
                [str(SOUND_SCRIPT)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass


if __name__ == "__main__" and len(sys.argv) > 1:
    main(sys.argv[1])
