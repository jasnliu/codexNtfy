# Unified multi-computer bridge: planned, not implemented

The current repository only sends completion notifications. The following is a
design outline for a future companion service, not documentation of working
commands.

## Roles

- **Agent on each computer:** connect to that computer's existing Codex server,
  discover available conversations, maintain stable aliases tied to thread IDs,
  report status, and deliver authenticated messages to explicitly named chats.
- **One coordinator:** track enrolled devices and heartbeats, collect fresh
  reports for `/list`, and send one combined response. Mark missing reports as
  stale/offline rather than silently treating them as current.
- **ntfy transport:** carry authenticated commands and reports on separate
  channels from user-facing replies and the existing completion alerts.

For example, `/send desktop/moosic Run the tests` would address the `moosic`
alias on the explicitly named `desktop` device. A saved registry would keep
aliases stable, rather than renumbering chats every time `/list` is requested.
Names should be collision-safe and should not reveal full prompts by default.

The coordinator can run beside an agent on an always-on computer or on a server.
If it is offline, unified commands are unavailable. Automatic failover is not
part of the initial design.

## Codex integration

Use supported local interfaces, not direct writes to Codex's databases. The
documented app-server exposes `thread/loaded/list`, `thread/read`, thread names,
and runtime status. The local CLI inspected during planning also exposed
`codex queue --thread ... --message ...`.

Verify API/CLI capabilities and connection to the **existing running server**
on each supported Codex version/client before implementing discovery or delivery.
Starting a separate server is not proof of access to chats in another client.
Loaded threads can remain in memory after a window closes, so label the results
as available sessions, not an exact list of visible windows. Filter out internal
recap and child-agent threads by default. Keep existing sandbox and approval
policies; remotely queued prompts are not approval bypasses.

## Local state, not Git synchronization

Future state would live outside the clone:

```text
~/.config/codex-ntfy-bridge/config.toml       # device ID, role, channel settings
~/.local/share/codex-ntfy-bridge/state.sqlite3 # aliases, processed IDs, device state
```

Keep credentials in the OS keychain or a restricted secrets file. Store code,
configuration examples, tests, and launch-service templates in GitHub. Never
commit live state or Codex credentials. Report agent/protocol versions in
heartbeats so incompatible devices can be identified after updates.

## Required security and reliability

- Protected ntfy topics plus authenticated device enrollment and authorized
  command senders; an obscure topic name alone is not command authentication.
- Signed/validated message envelopes, unique message IDs, expiry timestamps,
  request IDs, and replay/duplicate protection.
- Durable acknowledgement tracking. Do not blindly resend ambiguous deliveries
  after a crash, since that could run a prompt twice.
- Reject stale commands after sleep or reconnection; never replay the entire
  topic cache as executable work.
- Explicit device/chat addressing and an acknowledgement identifying the target.
- One coordinator owns a list request and emits one bounded-size reply (or
  explicitly numbered parts if it exceeds ntfy's message limit).
- Separate command, report, reply, and completion channels to prevent loops.
- Privacy controls for chat names and working-directory metadata sent through
  ntfy; transport authentication does not make the service provider unable to
  read message bodies.

References: [Codex app-server](https://learn.chatgpt.com/docs/app-server),
[ntfy subscriptions](https://docs.ntfy.sh/subscribe/api/).
