#!/usr/bin/env python3
"""Read-only, unified /list over ntfy. No incoming prompt execution."""

import argparse
from dataclasses import dataclass, field
import fcntl
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import ssl
import time
import tomllib
import unicodedata
import urllib.parse
import urllib.request


LOG = logging.getLogger("codex-ntfy-list")
VERSION = "0.4.0"
MAX_AGE = 60
MAX_BODY = 3500  # Below ntfy's default 4096-byte limit, including UTF-8.
DEFAULT_CONFIG = Path.home() / ".config/codex-ntfy-bridge/config.toml"
DEFAULT_STATE = Path.home() / ".local/share/codex-ntfy-bridge/list.sqlite3"


def tls_context():
    # Python.org macOS builds may have no default CA bundle installed. Use a
    # verified bundle rather than disabling TLS certificate verification.
    import certifi
    return ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or certifi.where())


def clean_name(value, fallback="Untitled chat"):
    if not isinstance(value, str):
        return fallback
    value = " ".join("".join(
        char if not unicodedata.category(char).startswith("C") else " "
        for char in value
    ).split())
    return value[:200] or fallback


@dataclass
class Config:
    server: str
    topic: str
    bus_topic: str
    bus_secret: str
    role: str
    device_name: str
    device_id: str
    socket_path: str = ""
    collect_seconds: float = 8
    tick_seconds: float = 1
    token_env: str = "CODEX_NTFY_TOKEN"

    def __post_init__(self):
        self.server = self.server.rstrip("/")
        parsed = urllib.parse.urlsplit(self.server)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("server must be an HTTPS URL without query or fragment")
        if parsed.username or parsed.password:
            raise ValueError("Do not put credentials in the server URL")
        for topic in (self.topic, self.bus_topic):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", topic):
                raise ValueError("Invalid ntfy topic")
        if self.topic == self.bus_topic:
            raise ValueError("Command/reply and internal bus topics must differ")
        if len(self.bus_secret) < 32:
            raise ValueError("bus_secret must have at least 32 characters")
        if self.role not in ("combined", "agent", "coordinator"):
            raise ValueError("role must be combined, agent, or coordinator")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.device_id):
            raise ValueError("Invalid device_id")
        self.device_name = clean_name(self.device_name, "Computer")[:64]
        for name, value, low, high in (
            ("collect_seconds", self.collect_seconds, 2, 30),
            ("tick_seconds", self.tick_seconds, 0.2, 10),
        ):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
        if not self.socket_path:
            home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            self.socket_path = str(home / "app-server-control/app-server-control.sock")

    @classmethod
    def load(cls, path):
        with Path(path).open("rb") as stream:
            return cls(**tomllib.load(stream))

    @property
    def coordinator(self):
        return self.role in ("combined", "coordinator")

    @property
    def agent(self):
        return self.role in ("combined", "agent")


class CodexUnavailable(RuntimeError):
    pass


class CodexChats:
    """Query the existing daemon over its local WebSocket; never start/resume chats."""

    def __init__(self, socket_path):
        self.socket_path = socket_path

    def names(self):
        return [entry["name"] for entry in self.entries()]

    def entries(self):
        # Lazy import keeps the original completion notifier dependency-free.
        from websockets.sync.client import unix_connect
        from websockets.exceptions import WebSocketException

        try:
            with unix_connect(self.socket_path, open_timeout=3, close_timeout=1,
                              max_size=8 * 1024 * 1024) as connection:
                return self._entries(connection)
        except (OSError, TimeoutError, WebSocketException, ValueError, KeyError,
                TypeError, AttributeError) as error:
            raise CodexUnavailable("Cannot query the running Codex server") from error

    def _names(self, connection):
        return [entry["name"] for entry in self._entries(connection)]

    def _entries(self, connection):
        next_id = 0
        deadline = time.monotonic() + 5

        def call(method, params=None):
            nonlocal next_id
            # A deliberate allowlist: adding any other command requires code changes.
            if method not in ("initialize", "thread/loaded/list", "thread/read"):
                raise ValueError("Non-read-only RPC is forbidden")
            next_id += 1
            connection.send(json.dumps({"id": next_id, "method": method,
                                        "params": params or {}}))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Codex listing deadline exceeded")
                value = json.loads(connection.recv(timeout=remaining))
                if not isinstance(value, dict) or value.get("id") != next_id:
                    continue  # Notifications are not request responses.
                if "error" in value:
                    raise CodexUnavailable("Codex rejected a listing request")
                return value["result"]

        call("initialize", {"clientInfo": {"name": "codex_ntfy_list",
             "title": "Codex ntfy list", "version": VERSION}})
        connection.send(json.dumps({"method": "initialized", "params": {}}))
        ids = []
        cursor = None
        cursors = set()
        while True:
            result = call("thread/loaded/list", {"cursor": cursor} if cursor else {})
            ids.extend(result["data"])
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if cursor in cursors:
                raise CodexUnavailable("Codex returned a repeated listing cursor")
            cursors.add(cursor)
        entries = []
        for thread_id in dict.fromkeys(ids):
            try:
                thread = call("thread/read", {"threadId": thread_id,
                                              "includeTurns": False})["thread"]
            except CodexUnavailable:
                # A chat may close between loaded/list and read. Omit only that chat.
                continue
            source = thread.get("source", "")
            if (thread.get("ephemeral") or thread.get("parentThreadId")
                    or thread.get("threadSource", "user") != "user"
                    or thread.get("canAcceptDirectInput") is False
                    or thread.get("status", {}).get("type") == "notLoaded"
                    or isinstance(source, dict)
                    or str(source).lower().startswith("subagent")):
                continue
            # Never use a prompt preview as the name of an untitled conversation.
            entries.append({"id": str(thread.get("id") or thread_id),
                            "name": clean_name(thread.get("name"))})
        return sorted(entries, key=lambda entry: (entry["name"].casefold(), entry["id"]))


class Ntfy:
    def __init__(self, config):
        self.config = config

    def _request(self, url, **kwargs):
        request = urllib.request.Request(url, **kwargs)
        token = os.environ.get(self.config.token_env, "")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        return urllib.request.urlopen(request, timeout=8, context=tls_context())

    def subscribe(self, since):
        from websockets.sync.client import connect

        topics = [self.config.bus_topic]
        if self.config.coordinator:
            topics.append(self.config.topic)
        query = urllib.parse.urlencode({"since": int(since)})
        url = f"wss://{self.config.server.removeprefix('https://')}/{','.join(topics)}/ws?{query}"
        token = os.environ.get(self.config.token_env, "")
        headers = {"Authorization": f"Bearer {token}"} if token else None
        return connect(url, ssl=tls_context(), additional_headers=headers, open_timeout=8,
                       close_timeout=1, max_size=2 * 1024 * 1024)

    def publish(self, topic, message, title):
        data = message.encode("utf-8")
        if len(data) > MAX_BODY:
            raise ValueError("ntfy message exceeds the bridge size limit")
        # Cache reports for brief reconnects and replies for normal mobile/offline
        # delivery. Topics should be protected when names are sensitive.
        with self._request(f"{self.config.server}/{topic}", data=data,
                           headers={"Title": title, "Cache": "yes"}, method="POST") as response:
            response.read()


class State:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = Path(str(path) + ".lock").open("a")
        os.chmod(self.lock.name, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("Another list bridge is already using this state file")
        self.connection = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.connection.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, time REAL)")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS agents (
            cluster TEXT, id TEXT, name TEXT, online INTEGER, last_seen REAL,
            PRIMARY KEY (cluster, id))""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS list_snapshots (
            cluster TEXT, list_id TEXT, saved_at REAL, entries TEXT,
            PRIMARY KEY (cluster, list_id))""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS latest_list (
            cluster TEXT PRIMARY KEY, list_id TEXT)""")
        self.connection.commit()

    def seen(self, event_id):
        return self.connection.execute("SELECT 1 FROM seen WHERE id=?", (event_id,)).fetchone() is not None

    def mark(self, event_id, now):
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (event_id, now))
            self.connection.execute("DELETE FROM seen WHERE time < ?", (now - 86400,))

    def online_agents(self, cluster):
        return dict(self.connection.execute(
            "SELECT id, name FROM agents WHERE cluster=? AND online=1", (cluster,)))

    def remember_agent(self, cluster, device_id, name, now):
        with self.connection:
            self.connection.execute("""INSERT INTO agents VALUES (?,?,?,1,?)
                ON CONFLICT(cluster,id) DO UPDATE SET name=excluded.name,
                online=1, last_seen=excluded.last_seen""", (cluster, device_id, name, now))

    def offline_agent(self, cluster, device_id):
        with self.connection:
            self.connection.execute("UPDATE agents SET online=0 WHERE cluster=? AND id=?",
                                    (cluster, device_id))

    def save_list(self, cluster, list_id, entries, now):
        """Remember exactly the displayed numbering, not a future re-sorted list."""
        with self.connection:
            self.connection.execute("""INSERT INTO list_snapshots VALUES (?,?,?,?)
                ON CONFLICT(cluster,list_id) DO UPDATE SET saved_at=excluded.saved_at,
                entries=excluded.entries""", (cluster, list_id, now, packed(entries)))
            self.connection.execute("""INSERT INTO latest_list VALUES (?,?)
                ON CONFLICT(cluster) DO UPDATE SET list_id=excluded.list_id""", (cluster, list_id))
            self.connection.execute("""DELETE FROM list_snapshots WHERE cluster=? AND list_id NOT IN (
                SELECT list_id FROM list_snapshots WHERE cluster=?
                ORDER BY saved_at DESC, rowid DESC LIMIT 20)
                AND list_id != ?""", (cluster, cluster, list_id))

    def get_list(self, cluster, list_id=None):
        if list_id is None:
            row = self.connection.execute("SELECT list_id FROM latest_list WHERE cluster=?",
                                          (cluster,)).fetchone()
            if row is None:
                return []
            list_id = row[0]
        row = self.connection.execute("SELECT entries FROM list_snapshots WHERE cluster=? AND list_id=?",
                                      (cluster, list_id)).fetchone()
        return json.loads(row[0]) if row else []

    def close(self):
        self.connection.close()
        self.lock.close()


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def signed(value, secret):
    value = dict(value)
    value["signature"] = hmac.new(secret.encode(), packed(value).encode(), hashlib.sha256).hexdigest()
    return packed(value)


def verified(text, secret, now):
    try:
        value = json.loads(text)
        if not isinstance(value, dict):
            return None
        signature = value.pop("signature", None)
        if not isinstance(signature, str):
            return None
        expected = hmac.new(secret.encode(), packed(value).encode(), hashlib.sha256).hexdigest()
        sent_at = value.get("sent_at")
        if (not hmac.compare_digest(signature, expected) or value.get("version") != 1
                or isinstance(sent_at, bool) or not isinstance(sent_at, (int, float))
                or not math.isfinite(sent_at) or not -5 <= now - sent_at <= MAX_AGE
                or not isinstance(value.get("request_id"), str)
                or not 1 <= len(value["request_id"]) <= 100):
            return None
        return value
    except (TypeError, ValueError, UnicodeError):
        return None


def chat_batches(entries):
    batches, current = [], []
    for entry in entries:
        entry = {"id": entry["id"], "name": clean_name(entry["name"])}
        if current and len(packed(current + [entry]).encode()) > 2000:
            batches.append(current)
            current = []
        current.append(entry)
    batches.append(current)
    return batches


def message_parts(text):
    """Preserve UTF-8 and every name, splitting oversized lists into numbered replies."""
    if len(text.encode()) <= MAX_BODY:
        return [text]
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        if len((current + line).encode()) > MAX_BODY - 100:
            if current:
                parts.append(current.rstrip())
            current = ""
        while len(line.encode()) > MAX_BODY - 100:
            prefix = line.encode()[:MAX_BODY - 100].decode("utf-8", errors="ignore")
            parts.append(prefix)
            line = line[len(prefix):]
        current += line
    if current:
        parts.append(current.rstrip())
    return [f"Part {index}/{len(parts)}\n{part}" for index, part in enumerate(parts, 1)]


@dataclass
class Pending:
    deadline: float
    expected: dict = field(default_factory=dict)
    discovery: bool = False
    reports: dict = field(default_factory=dict)
    reply: list | None = None
    next_part: int = 0
    selections: list = field(default_factory=list)


class Bridge:
    def __init__(self, config, state, ntfy=None, chats=None):
        self.config = config
        self.state = state
        self.ntfy = ntfy or Ntfy(config)
        self.chats = chats or CodexChats(config.socket_path)
        self.pending = {}
        self.started_at = time.time()
        self.cluster = hashlib.sha256(packed([
            config.server, config.topic, config.bus_topic,
        ]).encode()).hexdigest()
        if config.coordinator and config.agent:
            self.state.remember_agent(self.cluster, config.device_id,
                                      config.device_name, self.started_at)

    def announce(self, now):
        # Presence is announced at connection time, not every few seconds:
        # frequent heartbeat publications would exhaust ntfy's free message quota.
        if self.config.agent and not self.config.coordinator:
            self.emit("agent_online", self.config.device_id, now,
                      device_id=self.config.device_id, device_name=self.config.device_name)

    def emit(self, kind, request_id, now, **extra):
        body = {"version": 1, "kind": kind, "request_id": request_id,
                "sent_at": now, **extra}
        self.ntfy.publish(self.config.bus_topic, signed(body, self.config.bus_secret), "Codex list bus")

    def handle(self, event, now):
        if event.get("event") != "message":
            return
        event_id, timestamp = event.get("id"), event.get("time")
        if (not isinstance(event_id, str) or not 1 <= len(event_id) <= 100
                or isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                or not math.isfinite(timestamp) or not -5 <= now - timestamp <= MAX_AGE
                or self.state.seen(event_id)):
            return
        topic, text = event.get("topic"), event.get("message")
        if not isinstance(text, str):
            return
        value = verified(text, self.config.bus_secret, now) if topic == self.config.bus_topic else None
        # Recover a recent registration after restarting the coordinator, but
        # never replay user commands or old list requests from before startup.
        if timestamp < self.started_at - 2 and not (value and value.get("kind") == "agent_online"):
            return
        if topic == self.config.topic and self.config.coordinator and text.strip() == "/list":
            if event_id not in self.pending:
                if len(self.pending) >= 20:
                    return  # Bound resource use; there is no model invocation here.
                expected = self.state.online_agents(self.cluster)
                self.pending[event_id] = Pending(now + self.config.collect_seconds,
                                                expected=expected, discovery=not bool(expected))
                try:
                    self.emit("list_request", event_id, now)
                except Exception:
                    del self.pending[event_id]
                    raise
                LOG.info("Accepted /list request")
        elif topic == self.config.bus_topic:
            if value:
                self._bus(value, now)
        self.state.mark(event_id, now)

    def _bus(self, value, now):
        request_id = value["request_id"]
        if value.get("kind") == "agent_online" and self.config.coordinator:
            device_id = value.get("device_id")
            if not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", device_id):
                return
            name = clean_name(value.get("device_name"), "Computer")[:64]
            self.state.remember_agent(self.cluster, device_id, name, now)
            # A computer can join while a request is already in flight. Probe
            # that computer without making all the others repeat their work.
            for pending_id, pending in self.pending.items():
                if device_id not in pending.expected and pending.reply is None:
                    self.emit("list_request", pending_id, now, target_device_id=device_id)
                    pending.expected[device_id] = name
        elif value.get("kind") == "list_request" and self.config.agent:
            if value.get("target_device_id", self.config.device_id) != self.config.device_id:
                return
            error = False
            try:
                entries = self.chats.entries()
            except CodexUnavailable:
                entries, error = [], True
                LOG.warning("Local Codex server is unavailable")
            batches = chat_batches(entries)
            for index, batch in enumerate(batches):
                fields = dict(device_id=self.config.device_id, device_name=self.config.device_name,
                              names=[entry["name"] for entry in batch],
                              thread_ids=[entry["id"] for entry in batch],
                              unavailable=error, part=index, parts=len(batches))
                if self.config.coordinator and request_id in self.pending:
                    # The combined coordinator already owns its local result.
                    self._bus({"kind": "list_report", "request_id": request_id, **fields}, now)
                else:
                    self.emit("list_report", request_id, time.time(), **fields)
        elif value.get("kind") == "list_report" and self.config.coordinator:
            pending = self.pending.get(request_id)
            device_id, names = value.get("device_id"), value.get("names")
            thread_ids = value.get("thread_ids")
            part, parts = value.get("part"), value.get("parts")
            if (not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", device_id)
                    or not isinstance(names, list) or len(names) > 2000
                    or any(not isinstance(name, str) for name in names)
                    or type(part) is not int or type(parts) is not int
                    or not 1 <= parts <= 1000 or not 0 <= part < parts):
                return
            # Older agents can still list names, but those entries must not be
            # guessed/routed by name when selection is implemented later.
            if thread_ids is None:
                thread_ids = [None] * len(names)
            elif (not isinstance(thread_ids, list) or len(thread_ids) != len(names)
                  or any(not isinstance(item, str) or not 1 <= len(item) <= 200 for item in thread_ids)):
                return
            # Even a late report is evidence that this agent is online for the
            # next request. It must not cause a second reply to an old request.
            self.state.remember_agent(self.cluster, device_id,
                                      clean_name(value.get("device_name"), "Computer")[:64], now)
            if not pending or pending.reply is not None:
                return
            pending.expected.setdefault(device_id, clean_name(value.get("device_name"), "Computer")[:64])
            if device_id not in pending.reports and len(pending.reports) >= 100:
                return
            report = pending.reports.setdefault(device_id, {
                "name": clean_name(value.get("device_name"), "Computer"),
                "parts": parts, "chunks": {}, "unavailable": bool(value.get("unavailable")),
            })
            if report["parts"] == parts:
                report["chunks"][part] = [{"name": clean_name(name), "id": thread_id}
                                         for name, thread_id in zip(names, thread_ids)]

    def finish(self, now):
        for request_id, pending in list(self.pending.items()):
            complete = {device_id for device_id, report in pending.reports.items()
                        if len(report["chunks"]) == report["parts"]}
            all_responded = (bool(pending.expected) and not pending.discovery
                             and pending.expected.keys() <= complete)
            if pending.reply is None and now < pending.deadline and not all_responded:
                continue
            if pending.reply is None:
                lines = []
                for device_id, name in sorted(pending.expected.items(), key=lambda item: item[1].casefold()):
                    if device_id not in complete:
                        self.state.offline_agent(self.cluster, device_id)
                    if device_id not in pending.reports:
                        lines.extend([f"{name}:", "No response (offline or timed out).", ""])
                for device_id, report in sorted(pending.reports.items(),
                        key=lambda item: (item[1]["name"].casefold(), item[0])):
                    lines.append(f"{report['name']}:")
                    if report["unavailable"]:
                        lines.append("Codex is not running or is unavailable.")
                    elif len(report["chunks"]) != report["parts"]:
                        lines.append("Chat list incomplete; try /list again.")
                    else:
                        entries = [entry for part in range(report["parts"])
                                   for entry in report["chunks"][part]]
                        entries.sort(key=lambda entry: (entry["name"].casefold(), entry["id"] or ""))
                        for entry in entries:
                            number = len(pending.selections) + 1
                            lines.append(f"{number}. {entry['name']}")
                            pending.selections.append({"number": number, "device_id": device_id,
                                "device_name": report["name"], "thread_id": entry["id"],
                                "name": entry["name"]})
                        if not entries:
                            lines.append("No available chats.")
                    lines.append("")
                body = "Available Codex chats\n\n" + "\n".join(lines).rstrip() if lines else "No computers reported available chats."
                pending.reply = message_parts(body)
            while pending.next_part < len(pending.reply):
                self.ntfy.publish(self.config.topic, pending.reply[pending.next_part], "Codex chats")
                pending.next_part += 1
            self.state.save_list(self.cluster, request_id, pending.selections, now)
            del self.pending[request_id]
            LOG.info("Replied to /list (%s)", "all computers responded" if all_responded else "timeout/discovery")

    def run(self):
        LOG.info("List bridge started (%s); only /list is supported", self.config.role)
        # Recover recent signed registrations; handle() still rejects old commands.
        watermark = self.started_at - MAX_AGE
        delay = 1
        while True:
            try:
                with self.ntfy.subscribe(max(watermark - 2, time.time() - MAX_AGE)) as connection:
                    LOG.info("Connected to ntfy; listening for list messages")
                    self.announce(time.time())
                    while True:
                        try:
                            raw = connection.recv(timeout=self.config.tick_seconds)
                        except TimeoutError:
                            raw = None
                        if raw is not None:
                            try:
                                event = json.loads(raw)
                            except (ValueError, UnicodeError):
                                event = None
                            if isinstance(event, dict):
                                self.handle(event, time.time())
                                timestamp = event.get("time")
                                if type(timestamp) in (int, float) and math.isfinite(timestamp):
                                    watermark = max(watermark, min(timestamp, time.time()))
                        self.finish(time.time())
                        delay = 1
            except Exception as error:
                # Avoid logging message bodies, topic URLs, chat names, or credentials.
                LOG.warning("List bridge will retry after %s", type(error).__name__)
                delay = min(max(delay * 2, 2), 30)
            time.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--local-list", action="store_true",
                        help="Print local names without contacting ntfy or reading config")
    parser.add_argument("--socket", help="Local Codex socket (for --local-list)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.local_list:
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        path = args.socket or str(home / "app-server-control/app-server-control.sock")
        try:
            print("\n".join(CodexChats(path).names()) or "No available chats.")
        except CodexUnavailable as error:
            parser.exit(1, f"{error}\n")
        return
    try:
        config = Config.load(args.config)
        state = State(args.state)
    except (OSError, ValueError, RuntimeError, TypeError) as error:
        parser.exit(1, f"Cannot start list bridge: {error}\n")
    try:
        Bridge(config, state).run()
    except KeyboardInterrupt:
        pass
    finally:
        state.close()


if __name__ == "__main__":
    main()
