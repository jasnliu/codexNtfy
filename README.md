# codexNtfy

Codex completion notifications through [ntfy](https://ntfy.sh).

This repository contains the existing **outgoing completion notifier**. It does
**not yet implement** incoming prompts, `/list`, chat aliases, multi-computer
discovery, or the unified coordinator. See [the roadmap](docs/unified-bridge.md).

## What it does

`codex-ntfy.py` receives Codex's JSON notification payload as its first argument.
It sends a notification only when:

- The event type is `agent-turn-complete`.
- The request is not Codex's internal conversation-recap request.
- The final assistant message ends with `<!-- CODEX_NOTIFY_COMPLETE -->`
  after trailing whitespace is removed.

It posts the fixed message **“Codex finished responding.”** to
`https://ntfy.sh/codex_prompts`, with title `Codex` and tag `white_check_mark`.
No prompt, response, project path, or conversation history is included.
Network failures are ignored. If `~/.codex/notify-sound.sh` exists, it is run
after a successful HTTP notification; no sound script is included or created.

`notification-final-marker.md` instructs Codex to append that completion marker
only to a finished final response. Some clients display the HTML comment as text.

## Install on a Mac or Linux computer

Requirements: Git, Python **3.11+** for the installer/tests, and a Codex client
that honors the user-level `notify` and `model_instructions_file` settings.
No third-party Python packages are required. Different Codex clients/versions
should be verified individually; this is not a native Codex plugin.

```sh
git clone https://github.com/jasnliu/codexNtfy.git
cd codexNtfy
python3 -m unittest discover -s tests -v
python3 install.py --dry-run
python3 install.py
```

The installer copies only these two assets:

```text
~/.local/bin/codex-ntfy.py
~/.codex/notification-final-marker.md
```

It adds/updates these **top-level** keys in `~/.codex/config.toml`, using the
actual absolute paths for that computer and the Python interpreter running the
installer:

```toml
notify = ["/absolute/path/to/python3", "/absolute/home/.local/bin/codex-ntfy.py"]
model_instructions_file = "/absolute/home/.codex/notification-final-marker.md"
```

It preserves unrelated settings and makes numbered `.ntfy.bak` backups before
changing existing files. It refuses conflicting notification/instruction settings
unless you explicitly pass `--replace-existing`. **Do not use that flag blindly:**
replacing `model_instructions_file` disables the old instructions file. Instead,
merge the marker instructions into your existing instructions file and configure
manually if those instructions should remain active. Only one `notify` command
is configured; this installer does not chain another notifier.

For a custom `CODEX_HOME`, install/configure manually at that location; the
installer intentionally targets the standard `HOME/.codex` layout.

Restart Codex after installation. Subscribe to `codex_prompts` on `ntfy.sh` in
your ntfy client. A normal completed Codex response should then trigger an alert.
**The installer and test suite do not send real test notifications.**

## Update every computer from GitHub

In each computer's clone:

```sh
git pull --ff-only
python3 -m unittest discover -s tests -v
python3 install.py
```

Restart Codex if settings or instruction files changed. The script itself is
executed afresh on each notification. Keep the configured Python interpreter
installed; rerun the installer if its location changes.

GitHub distributes code; it does **not** synchronize active chats or update
installed files automatically. There is no auto-updater. Installation copies
the scripts rather than linking to the clone, so pulling alone does not deploy
an update. `--home /some/home` can target a different home directory for testing
or installation.

## Privacy and security

- `codex_prompts` is the existing fixed topic, shared by every installation of
  this code. It is **not a private per-user topic**. Anyone with access to the
  topic can potentially read or publish alerts. Completion timing is exposed
  even though conversation content is not sent.
- There is no authentication in this existing notifier. For private alerts,
  adapt the URL and authentication for a protected topic before deploying it.
  Keep tokens outside the repository.
- This code never subscribes to ntfy or executes incoming messages. Never use
  this unauthenticated completion topic as a remote command channel.
- Do not commit `~/.codex/config.toml`, `auth.json`, databases, session logs,
  local credentials, or sound scripts to this repository.

## Uninstall

Remove the two notifier settings from `~/.codex/config.toml` (or restore their
previous values from the backups), then remove the two copied assets if no
longer needed. Do not restore an entire old config over newer unrelated changes.
Restart Codex. Uninstalling does not require changing Codex's application files.

## Repository contents

| File | Purpose |
| --- | --- |
| `codex-ntfy.py` | Existing notification script, copied unchanged |
| `notification-final-marker.md` | Existing completion-marker instructions |
| `install.py` | Portable installation with backups and conflict checks |
| `tests/` | Mocked HTTP/sound tests and temporary-directory installer tests |
| `docs/unified-bridge.md` | Planned incoming-command and multi-device architecture |

References: [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[Codex app-server](https://learn.chatgpt.com/docs/app-server),
[ntfy publishing](https://docs.ntfy.sh/publish/).
