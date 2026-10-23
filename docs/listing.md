# `/list` setup and testing

## Scope

Publish exactly `/list` (surrounding whitespace is allowed) to the configured
ntfy topic. The coordinator replies to the same topic with the names of available
Codex chats, grouped by computer. Other commands/text, including `/send`, are
ignored. No message is forwarded into a Codex conversation.

Example reply:

```text
Available Codex chats

desktop:
- Update music player

macbook:
- Fix skip quizzes
- Untitled chat
```

Only computers running an enrolled agent can contribute. Installing this
companion once on a computer covers that computer's available daemon-backed
user chats; it is not installed separately into each chat.

## First computer: coordinator and local agent

From your clone, using Python 3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-list.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python list_bridge.py --local-list
.venv/bin/python install_list.py --dry-run
.venv/bin/python install_list.py --device-name macbook
```

`--local-list` queries Codex locally and prints names; it does not contact ntfy.
The installer defaults to `combined`, creates new random command and bus topics,
generates a signing key and unique device ID, and prints the **command/reply**
topic URL. Its installer/tests do not publish real test messages. Open that topic
in your ntfy client, subscribe, and publish `/list` yourself. Wait approximately
8–12 seconds for the combined reply. Keep message caching enabled when publishing
so a brief disconnect does not lose your request. Replies are cached using ntfy's
normal retention so mobile clients and briefly disconnected subscribers can
retrieve them. Subscribe to the topic to receive the notification.

The old `codex_prompts` completion topic is not the default listing topic. An
explicit `--topic codex_prompts` is supported for testing but exposes chat names
on a predictable public topic. Do not use it for private information.

### Installed files

```text
~/.config/codex-ntfy-bridge/config.toml
~/.local/share/codex-ntfy-bridge/app/list_bridge.py
~/.local/share/codex-ntfy-bridge/app/venv/
~/.local/share/codex-ntfy-bridge/list.sqlite3
~/.local/share/codex-ntfy-bridge/list.sqlite3.lock
~/Library/LaunchAgents/com.local.codex-ntfy-list.plist   # macOS only
~/Library/Logs/codex-ntfy-bridge/                       # macOS logs
```

The configuration contains the topics/signing key and is mode `0600`. Do not
post or commit it. Installation never changes `.codex/config.toml`, the original
completion scripts, Codex authentication, or conversation files. It does not
start or restart Codex. Installation creates a separate venv and copies the
listing script; the clone's venv is only for development/testing.

macOS uses a user LaunchAgent, starting immediately and at login. Linux
installation prints a foreground command; automatic Linux services and Windows
support are not included in this initial test version.

## Enroll another computer

Securely transfer the coordinator's local `config.toml` to a temporary file on
the other computer. Treat it as a credential; do not transfer through a Git commit.
Clone the same repository there, then run:

```sh
python3 install_list.py --join-config /secure/path/coordinator-config.toml --device-name desktop
```

This copies only shared cluster settings. It generates/preserves that computer's
own device identity and defaults to **agent**, not another coordinator. Delete
the temporary transferred file when no longer needed. Choose distinct device
names. Do not simply duplicate a device's complete live config onto another
machine: that would duplicate its device ID and could merge reports incorrectly.

Both computers subscribe to the shared internal bus. Only the coordinator
listens for user `/list` requests and publishes the final reply. Exactly one
coordinator should be active. An always-on server can use `--role coordinator`
without a local Codex agent, but it needs its own service management on Linux.

## What counts as an available chat?

- A loaded user-facing thread in the computer's **existing** local Codex daemon.
- Working and idle chats are included; old stored/unloaded chats are not.
- Internal, child-agent, ephemeral, and non-input-capable threads are excluded.
- Existing saved names are used. A nameless chat appears as `Untitled chat`.
- Names are normalized to one line and capped at 200 characters. Prompt previews,
  working-directory paths, and conversation contents are not published.

Tested against Codex CLI/daemon **0.163.0** on macOS. The adapter connects to the
daemon's Unix WebSocket control socket and queries `thread/loaded/list` and
`thread/read` with `includeTurns=false`. It does not launch a new app-server to
pretend to discover chats in another process. Other versions/clients, including
standalone `--no-daemon` sessions, are not guaranteed to appear.

`socket_path` can select another existing compatible daemon in local config.
By default the installer resolves `$CODEX_HOME`, or uses `~/.codex`. Loaded is
not identical to visible: closed windows can leave a chat loaded for a while.

## Configuration, transport, and privacy

See `list-config.example.toml`. Re-running `install_list.py` preserves the device
ID and topics unless you explicitly override them. Changed configurations get
a private backup next to the original. The internal bus uses HMAC-signed reports
and requests. Persistent WebSocket subscriptions reconnect with backoff and a
bounded replay window; there is no frequent HTTP polling. Processed message IDs
persist locally to suppress duplicate delivery after reconnect/restart.

This is **read-only testing**. A random topic name is not account authentication.
Anyone who obtains the command topic can request or read chat names. Internal
bus signatures are not encryption, and cached reports are visible to the ntfy
server. For privacy, use protected topics on ntfy and restrict both topics.

For protected topics, provide a bearer token through the configured `token_env`
(default `CODEX_NTFY_TOKEN`). A macOS LaunchAgent does not inherit the terminal's
environment; provision the token in the service environment separately before
starting it. The initial installer does not provision or store account tokens.
Never put credentials in Git or embed them in a topic URL. TLS verification
uses `certifi`; `SSL_CERT_FILE` can select a custom trusted CA bundle.

The coordinator waits `collect_seconds` (default 8) for reports. Offline/sleeping
computers are absent, not falsely labelled current. An available agent whose
Codex server cannot be queried reports that clearly. Large lists are split into
numbered ntfy replies instead of silently dropping names. Keep clocks in sync.
ntfy's own daily/rate limits still apply; each request uses at least a bus request
and a user-facing reply, plus remote agent reports.

## Update and inspect

```sh
git pull --ff-only
python3 install_list.py
launchctl print gui/$(id -u)/com.local.codex-ntfy-list
tail -n 30 ~/Library/Logs/codex-ntfy-bridge/list.stderr.log
```

The installer refreshes dependencies/copied code and restarts only this companion.
`git pull` alone does not update the installed copy. Logs omit topic URLs,
message bodies, chat names, and tokens. A stopped/missing local Codex server does
not stop the bridge; `/list` reports it as unavailable.

## Stop / uninstall (macOS)

```sh
launchctl bootout gui/$(id -u)/com.local.codex-ntfy-list
```

Remove `~/Library/LaunchAgents/com.local.codex-ntfy-list.plist` to prevent a
restart at login. The private config, companion app/venv, state, and logs can
then be removed if desired. Leave Codex and the existing completion notifier
alone. Never uninstall while a request is in flight if you need its reply.

## Verification

The suite uses mocked HTTP, in-memory multi-computer report delivery,
temporary-directory state, and fake read-only RPC responses. No automated test
posts to ntfy. Tests cover command filtering, name-only replies, unified replies,
signatures, stale/duplicate events, output byte limits, state permissions, and
installer identity handling. Local live discovery and a live ntfy subscription
can be checked without publishing a test notification.
