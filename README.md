# codexNtfy

Codex completion notifications and a read-only `/list` companion through
[ntfy](https://ntfy.sh).

There are two independent features:

- **Completion notifier:** the existing script, unchanged.
- **Listing companion:** send `/list` to receive the names of available chats.
  One coordinator combines results from computers with enrolled agents.

`/list` is the **only** incoming command. Incoming prompts, `/send`, `/rename`,
and arbitrary command execution are not implemented. Unknown messages are ignored.

## Try `/list`

On the first computer (Python 3.11+, macOS or Linux):

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-list.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python list_bridge.py --local-list
.venv/bin/python install_list.py --device-name macbook
```

The installer prints a new random ntfy topic. Subscribe to **that topic**, then
publish the message `/list` to it. Expect one reply after roughly 8–12 seconds,
containing chat names grouped by computer. You do not send `/list` to the old
`codex_prompts` completion topic unless you explicitly choose it with `--topic`.
Avoid using a predictable public topic for chat names.

On macOS, installation starts a persistent login LaunchAgent. On Linux it prints
the foreground command to run instead. It does not change Codex settings or
restart Codex. No real test message is sent by installation or the tests.

See [listing setup and testing](docs/listing.md) for enrolling another computer,
operational limits, privacy, and uninstall instructions.

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
No third-party Python packages are required for completion notifications.
The optional listing companion uses the pinned dependencies in
`requirements-list.txt`. Different Codex clients/versions
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

These update instructions apply to the completion notifier. To update the
listing companion, also rerun `python3 install_list.py`; it preserves its local
device identity and cluster configuration and restarts only its own service.

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
- The completion script never subscribes to ntfy or executes incoming messages.
  The optional listing companion subscribes separately and recognizes only
  `/list`. It cannot send prompts to Codex. Never use the unauthenticated
  completion topic as a future remote prompt/command channel.
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
| `list_bridge.py` | Read-only agents and unified `/list` coordinator |
| `install_list.py` | Isolated listing installation and macOS login service |
| `list-config.example.toml` | Example only; real local config contains secrets |
| `requirements-list.txt` | Optional listing dependencies |
| `docs/listing.md` | Listing setup, tests, limits, and uninstall |
| `docs/unified-bridge.md` | Current architecture and future work |

References: [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[Codex app-server](https://learn.chatgpt.com/docs/app-server),
[ntfy publishing](https://docs.ntfy.sh/publish/).
