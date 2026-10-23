# Unified multi-computer bridge

## Implemented: `/list` only

`list_bridge.py` can run as a local **agent**, a **coordinator**, or both
(`combined`). Exactly one computer should run the coordinator for a group.

1. A user publishes `/list` to the configured command/reply topic.
2. The coordinator broadcasts an HMAC-signed `list_request` on the internal bus.
3. Each online agent queries its existing local Codex daemon, without loading,
   resuming, starting, or interrupting any thread.
4. Agents return signed reports containing the computer label and chat names.
5. As soon as all expected computers have returned complete reports, the
   coordinator sends one unified reply, without a fixed collection delay.
   Only exceptionally large lists are split into numbered messages to respect
   ntfy's message-size limit. Multi-part agent reports are reassembled first.

The combined role incorporates its own local result directly. Remote agents
never post user-facing replies; the coordinator owns each request and reply.

Persistent WebSocket subscriptions avoid frequent HTTP polling. A short cached
replay window, processed-event IDs stored in SQLite, and message-age validation
handle brief disconnections. Internal reports use a separate random bus topic
and a shared HMAC key. All computers must have reasonably synchronized clocks.

No live chat database or history is synchronized through GitHub. Device IDs,
topic names, signing keys, processed IDs, and live local configuration stay
outside the repository. See [setup](listing.md).

## Availability semantics

An available chat is a loaded, user-facing thread on an enrolled computer's
existing local Codex server. Internal/child-agent and ephemeral threads are
filtered. A thread may stay loaded after its window closes. Availability is
not an exact list of visible windows. Untitled chats are labelled `Untitled chat`;
prompt previews are never substituted for names.

Remote agents announce their presence when connecting; the coordinator persists
a registry of known responding agents. Each request snapshots its expected
online computers. A new registration can add a computer to an in-flight request.
Every expected multipart report must be complete before early completion.

There are no periodic heartbeat messages. A silently disconnected computer can
therefore remain expected until one request hits its timeout. That reply marks
the missing computer clearly, and the registry stops waiting for it until a new
registration/report arrives. Late reports update membership but do not produce
duplicate replies. A coordinator with an entirely unknown roster uses one
discovery window; a combined coordinator knows its local agent from the start.

## Security boundary for this test

Only `/list` is accepted from the command topic. Every other message is ignored.
The local Codex adapter allows only `initialize`, `thread/loaded/list`, and
`thread/read`, plus the `initialized` handshake notification. It never invokes
an LLM or sends prompts. The existing completion notifier is independent.

The user-facing topic is not sender-authenticated by this code. Its long random
name reduces accidental discovery, but **is not account access control**.
Anyone with topic access can request/read names. Prefer protected ntfy topics
for real private use. Signing protects internal reports against parties without
the cluster key, but is not encryption; ntfy can read message bodies. Account
authentication, when configured, uses an environment-provided bearer token.

The coordinator needs to remain online. There is no automatic failover,
exactly-once reply guarantee across a crash, or durable pending-request queue.
Requests in progress during a restart may need to be sent again. This is a
testing companion, not a production remote-control service.

## Future work (not commands you can use today)

- Stable chat aliases, explicit device/chat routing, `/send`, `/rename`.
- Authenticated user-command enrollment and authorization before any remote
  prompt execution is considered; preserve sandbox and approval policies.
- Heartbeats/offline inventory, version negotiation, and coordinator failover.
- Durable request/reply delivery and stronger replay protection for commands
  with side effects. Current deduplication is adequate only for read-only lists.

References: [Codex app-server](https://learn.chatgpt.com/docs/app-server),
[ntfy subscriptions](https://docs.ntfy.sh/subscribe/api/).
