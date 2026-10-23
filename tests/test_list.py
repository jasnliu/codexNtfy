from dataclasses import asdict
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import list_bridge as bridge
import install_list as installer


def config(**overrides):
    values = dict(server="https://ntfy.sh", topic="test-commands",
                  bus_topic="test-bus", bus_secret="test-secret-" * 4,
                  role="combined", device_name="Mac", device_id="mac-1",
                  collect_seconds=8, tick_seconds=1)
    values.update(overrides)
    return bridge.Config(**values)


def event(identifier="command-1", text="/list", topic="test-commands", now=1000):
    return {"event": "message", "id": identifier, "message": text,
            "topic": topic, "time": now}


class MemoryState:
    def __init__(self):
        self.ids = set()

    def seen(self, event_id):
        return event_id in self.ids

    def mark(self, event_id, now):
        self.ids.add(event_id)


class CaptureNtfy:
    def __init__(self):
        self.messages = []

    def publish(self, topic, message, title):
        self.messages.append((topic, message, title))


def make_bridge(settings=None, names=None):
    ntfy = CaptureNtfy()
    chats = Mock()
    chats.names.return_value = names if names is not None else ["Chat A", "Chat B"]
    instance = bridge.Bridge(settings or config(), MemoryState(), ntfy, chats)
    instance.started_at = 999
    return instance


def deliver_messages(sender, receiver, prefix, now=1000):
    for index, (topic, text, _) in enumerate(list(sender.ntfy.messages)):
        if topic == "test-bus":
            receiver.handle(event(f"{prefix}-{index}", text, topic, now), now)


class CommandTests(unittest.TestCase):
    def test_only_exact_list_is_a_command(self):
        instance = make_bridge()
        for index, text in enumerate(("hello", "/send test hi", "/rename", "/list all",
                                      "/LIST", '{"method":"turn/start"}', "Codex finished responding.")):
            instance.handle(event(str(index), text), 1000)
        self.assertEqual(instance.ntfy.messages, [])
        instance.chats.names.assert_not_called()
        instance.handle(event("accepted", " \n/list\t"), 1000)
        self.assertEqual(len(instance.ntfy.messages), 1)

    def test_combined_local_round_trip_replies_with_names_not_prompt_text(self):
        instance = make_bridge(names=["Fix quizzes", "Music player", "Untitled chat"])
        instance.handle(event(), 1000)
        deliver_messages(instance, instance, "bus")
        instance.finish(1007)
        self.assertEqual(len(instance.ntfy.messages), 1)
        instance.finish(1009)
        topic, reply, title = instance.ntfy.messages[-1]
        self.assertEqual(topic, "test-commands")
        self.assertEqual(title, "Codex chats")
        self.assertEqual(reply, "Available Codex chats\n\nMac:\n- Fix quizzes\n- Music player\n- Untitled chat")
        self.assertEqual(instance.pending, {})
        # Reading the reply on the same topic cannot produce a feedback loop.
        count = len(instance.ntfy.messages)
        instance.handle(event("reply", reply), 1009)
        self.assertEqual(len(instance.ntfy.messages), count)

    def test_two_computers_produce_one_unified_reply(self):
        coordinator = make_bridge(config(role="coordinator"))
        mac = make_bridge(config(role="agent"), ["Mac chat"])
        desktop = make_bridge(config(role="agent", device_id="desktop-1", device_name="Desktop"), ["Desktop chat"])
        coordinator.handle(event(), 1000)
        with patch.object(bridge.time, "time", return_value=1000):
            deliver_messages(coordinator, mac, "mac-request")
            deliver_messages(coordinator, desktop, "desktop-request")
        deliver_messages(mac, coordinator, "mac-report")
        deliver_messages(desktop, coordinator, "desktop-report")
        coordinator.finish(1009)
        replies = [value for value in coordinator.ntfy.messages if value[0] == "test-commands"]
        self.assertEqual(len(replies), 1)
        self.assertIn("Desktop:\n- Desktop chat", replies[0][1])
        self.assertIn("Mac:\n- Mac chat", replies[0][1])
        coordinator.chats.names.assert_not_called()

    def test_agent_ignores_user_commands(self):
        instance = make_bridge(config(role="agent"))
        instance.handle(event(), 1000)
        self.assertEqual(instance.ntfy.messages, [])

    def test_duplicates_produce_no_additional_requests(self):
        instance = make_bridge()
        instance.handle(event(), 1000)
        instance.handle(event(), 1001)
        self.assertEqual(len(instance.ntfy.messages), 1)

    def test_stale_and_future_commands_are_ignored(self):
        instance = make_bridge()
        for index, timestamp in enumerate((900, 996, 1006, float("nan"), True, "1000")):
            instance.handle(event(str(index), now=timestamp), 1000)
        self.assertEqual(instance.ntfy.messages, [])

    def test_non_message_events_and_wrong_topics_are_ignored(self):
        instance = make_bridge()
        value = event()
        value["event"] = "keepalive"
        instance.handle(value, 1000)
        instance.handle(event("other", topic="unrelated"), 1000)
        self.assertEqual(instance.ntfy.messages, [])

    def test_forged_bus_request_does_not_query_chats(self):
        instance = make_bridge()
        value = {"version": 1, "kind": "list_request", "sent_at": 1000,
                 "request_id": "bad"}
        forged = bridge.signed(value, "wrong-secret")
        instance.handle(event("forgery", forged, "test-bus"), 1000)
        instance.chats.names.assert_not_called()

    def test_no_reporting_devices_returns_a_clear_reply(self):
        instance = make_bridge(config(role="coordinator"))
        instance.handle(event(), 1000)
        instance.finish(1009)
        self.assertEqual(instance.ntfy.messages[-1][1], "No computers reported available chats.")

    def test_codex_unavailable_is_distinct_from_empty_list(self):
        instance = make_bridge()
        instance.chats.names.side_effect = bridge.CodexUnavailable("private filesystem path")
        instance.handle(event(), 1000)
        deliver_messages(instance, instance, "bus")
        instance.finish(1009)
        text = instance.ntfy.messages[-1][1]
        self.assertIn("Codex is not running or is unavailable.", text)
        self.assertNotIn("private", text)

    def test_empty_chat_list_returns_clear_reply(self):
        instance = make_bridge(names=[])
        instance.handle(event(), 1000)
        deliver_messages(instance, instance, "bus")
        instance.finish(1009)
        self.assertIn("No available chats.", instance.ntfy.messages[-1][1])

    def test_failed_request_publish_can_be_retried(self):
        instance = make_bridge()
        with patch.object(instance.ntfy, "publish", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                instance.handle(event(), 1000)
        self.assertEqual(instance.pending, {})
        self.assertFalse(instance.state.seen("command-1"))
        instance.handle(event(), 1001)
        self.assertEqual(len(instance.ntfy.messages), 1)

    def test_reply_publication_failure_keeps_pending_reply(self):
        instance = make_bridge()
        instance.handle(event(), 1000)
        deliver_messages(instance, instance, "bus")
        with patch.object(instance.ntfy, "publish", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                instance.finish(1009)
        self.assertIn("command-1", instance.pending)
        instance.finish(1010)
        self.assertEqual(instance.pending, {})

    def test_multiple_list_requests_remain_separate(self):
        instance = make_bridge()
        instance.handle(event("first"), 1000)
        instance.handle(event("second"), 1000)
        deliver_messages(instance, instance, "bus")
        instance.finish(1009)
        replies = [value for value in instance.ntfy.messages if value[0] == "test-commands"]
        self.assertEqual(len(replies), 2)

    def test_large_multi_device_reports_preserve_every_chat(self):
        names = [f"Chat {index:03d} " + "漢" * 100 for index in range(40)]
        coordinator = make_bridge(config(role="coordinator"))
        agent = make_bridge(config(role="agent"), names)
        coordinator.handle(event(), 1000)
        with patch.object(bridge.time, "time", return_value=1000):
            deliver_messages(coordinator, agent, "request")
        self.assertGreater(len(agent.ntfy.messages), 1)
        self.assertTrue(all(len(item[1].encode()) <= bridge.MAX_BODY for item in agent.ntfy.messages))
        deliver_messages(agent, coordinator, "report")
        coordinator.finish(1009)
        replies = [value[1] for value in coordinator.ntfy.messages if value[0] == "test-commands"]
        self.assertGreater(len(replies), 1)
        self.assertTrue(all(len(text.encode()) <= bridge.MAX_BODY for text in replies))
        for name in names:
            self.assertIn(name, "\n".join(replies))


class RpcTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("websockets"), "Optional listing dependency not installed")
    def test_real_unix_websocket_handshake_with_read_only_fixture(self):
        import threading
        from websockets.sync.server import unix_serve

        received = []

        def handler(connection):
            for raw in connection:
                request = json.loads(raw)
                received.append(request["method"])
                method = request["method"]
                if method == "initialized":
                    continue
                results = {
                    "initialize": {},
                    "thread/loaded/list": {"data": ["fixture-chat"], "nextCursor": None},
                    "thread/read": {"thread": {"name": "Fixture chat", "source": "vscode",
                                               "threadSource": "user", "status": {"type": "idle"}}},
                }
                connection.send(json.dumps({"id": request["id"], "result": results[method]}))

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = str(Path(directory) / "codex.sock")
            with unix_serve(handler, path) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    self.assertEqual(bridge.CodexChats(path).names(), ["Fixture chat"])
                finally:
                    server.shutdown()
                    thread.join(timeout=3)
        self.assertEqual(received, ["initialize", "initialized", "thread/loaded/list", "thread/read"])

    def test_live_metadata_only_methods_and_title_fallback(self):
        connection = Mock()
        threads = [
            {"name": "Zebra", "source": "vscode", "status": {"type": "active"}},
            {"name": "alpha", "source": "cli", "status": {"type": "idle"}},
            {"name": None, "preview": "SECRET PROMPT", "status": {"type": "idle"}},
            {"name": "child", "parentThreadId": "root"},
            {"name": "temporary", "ephemeral": True},
            {"name": "internal", "threadSource": "internal"},
            {"name": "not available", "canAcceptDirectInput": False},
            {"name": "closed", "status": {"type": "notLoaded"}},
            {"name": "worker", "source": {"subAgent": {}}},
        ]
        responses = [{"id": 1, "result": {}},
                     {"method": "thread/status/changed", "params": {}},
                     {"id": 2, "result": {"data": list(range(len(threads))), "nextCursor": None}}]
        responses += [{"id": index + 3, "result": {"thread": thread}}
                      for index, thread in enumerate(threads)]
        connection.recv.side_effect = [json.dumps(item) for item in responses]
        names = bridge.CodexChats("unused")._names(connection)
        self.assertEqual(names, ["alpha", "Untitled chat", "Zebra"])
        self.assertNotIn("SECRET PROMPT", names)
        calls = [json.loads(call.args[0]) for call in connection.send.call_args_list]
        self.assertTrue(all(item["method"] in ("initialize", "initialized", "thread/loaded/list", "thread/read") for item in calls))
        for item in calls:
            if item["method"] == "thread/read":
                self.assertFalse(item["params"]["includeTurns"])

    def test_loaded_list_pagination(self):
        connection = Mock()
        responses = [
            {"id": 1, "result": {}},
            {"id": 2, "result": {"data": ["a"], "nextCursor": "page2"}},
            {"id": 3, "result": {"data": ["b"], "nextCursor": None}},
            {"id": 4, "result": {"thread": {"name": "A"}}},
            {"id": 5, "result": {"thread": {"name": "B"}}},
        ]
        connection.recv.side_effect = [json.dumps(item) for item in responses]
        self.assertEqual(bridge.CodexChats("unused")._names(connection), ["A", "B"])

    def test_closed_thread_during_discovery_does_not_break_list(self):
        connection = Mock()
        connection.recv.side_effect = [json.dumps(item) for item in (
            {"id": 1, "result": {}}, {"id": 2, "result": {"data": ["a"]}},
            {"id": 3, "error": {"message": "closed"}},
        )]
        self.assertEqual(bridge.CodexChats("unused")._names(connection), [])


class ListenerLoopTests(unittest.TestCase):
    def test_live_loop_replies_on_deadline_without_needing_another_message(self):
        instance = make_bridge()
        clock = [1000]
        # Use a context manager without opening a network connection.
        from unittest.mock import MagicMock
        subscription = MagicMock()
        connection = subscription.__enter__.return_value
        step = [0]

        def receive(**kwargs):
            step[0] += 1
            if step[0] == 1:
                return json.dumps(event())
            if step[0] == 2:
                clock[0] = 1001
                topic, text, _ = instance.ntfy.messages[0]
                return json.dumps(event("bus", text, topic, now=1000))
            if step[0] == 3:
                clock[0] = 1009
                raise TimeoutError
            raise KeyboardInterrupt

        connection.recv.side_effect = receive
        instance.ntfy.subscribe = Mock(return_value=subscription)
        with patch.object(bridge.time, "time", side_effect=lambda: clock[0]), self.assertRaises(KeyboardInterrupt):
            instance.run()
        replies = [item for item in instance.ntfy.messages if item[0] == "test-commands"]
        self.assertEqual(len(replies), 1)
        self.assertIn("Chat A", replies[0][1])

    def test_publish_failure_reconnects_and_replays_unprocessed_command(self):
        from unittest.mock import MagicMock
        instance = make_bridge()
        clock = [1000]
        subscription = MagicMock()
        connection = subscription.__enter__.return_value
        step = [0]
        original_publish = instance.ntfy.publish
        attempts = [0]

        def publish(*args):
            attempts[0] += 1
            if attempts[0] == 1:
                raise OSError("network unavailable")
            original_publish(*args)

        def receive(**kwargs):
            step[0] += 1
            if step[0] in (1, 2):
                clock[0] = 1000 + step[0]
                return json.dumps(event())
            if step[0] == 3:
                topic, text, _ = instance.ntfy.messages[0]
                return json.dumps(event("bus", text, topic, now=1002))
            if step[0] == 4:
                clock[0] = 1011
                raise TimeoutError
            raise KeyboardInterrupt

        connection.recv.side_effect = receive
        instance.ntfy.publish = publish
        instance.ntfy.subscribe = Mock(return_value=subscription)
        with patch.object(bridge.time, "time", side_effect=lambda: clock[0]), patch.object(bridge.time, "sleep") as sleep, self.assertRaises(KeyboardInterrupt):
            instance.run()
        self.assertEqual(instance.ntfy.subscribe.call_count, 2)
        sleep.assert_called_once()
        self.assertTrue(instance.state.seen("command-1"))
        self.assertEqual(len([item for item in instance.ntfy.messages if item[0] == "test-commands"]), 1)


class SecurityAndTransportTests(unittest.TestCase):
    def test_signatures_and_expiry(self):
        value = {"version": 1, "kind": "list_request", "request_id": "one", "sent_at": 1000}
        text = bridge.signed(value, "secret")
        self.assertEqual(bridge.verified(text, "secret", 1000), value)
        for text2, secret, now in ((text, "wrong", 1000), (text, "secret", 1100),
                                    (text, "secret", 990), ("[]", "secret", 1000),
                                    ("bad json", "secret", 1000)):
            self.assertIsNone(bridge.verified(text2, secret, now))

    def test_invalid_topics_roles_and_secret_are_rejected(self):
        for args in ({"topic": "a,b"}, {"topic": "test-bus"}, {"bus_secret": "short"},
                     {"server": "http://ntfy.sh"}, {"server": "https://user:pass@ntfy.sh"},
                     {"role": "execute"}, {"collect_seconds": float("nan")},
                     {"device_id": "../other"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                config(**args)

    def test_names_are_one_line_and_have_no_control_characters(self):
        self.assertEqual(bridge.clean_name("Hello\nworld\x00\x1b"), "Hello world")
        self.assertEqual(bridge.clean_name(None), "Untitled chat")

    @unittest.skipUnless(importlib.util.find_spec("websockets"), "Optional listing dependency not installed")
    def test_subscription_uses_one_persistent_connection_for_both_topics(self):
        transport = bridge.Ntfy(config())
        with patch("websockets.sync.client.connect") as connect, patch.object(bridge, "tls_context"):
            transport.subscribe(1000)
        url = connect.call_args.args[0]
        self.assertEqual(url, "wss://ntfy.sh/test-bus,test-commands/ws?since=1000")

    def test_publish_caches_bus_and_replies_for_mobile_delivery(self):
        transport = bridge.Ntfy(config())
        with patch.object(transport, "_request") as request:
            transport.publish("test-bus", "report", "Codex bus")
            self.assertEqual(request.call_args.kwargs["headers"]["Cache"], "yes")
            transport.publish("test-commands", "reply", "Codex chats")
            self.assertEqual(request.call_args.kwargs["headers"]["Cache"], "yes")

    def test_oversized_publish_is_rejected_before_network(self):
        transport = bridge.Ntfy(config())
        with patch.object(transport, "_request") as request, self.assertRaises(ValueError):
            transport.publish("test-commands", "漢" * 2000, "Codex chats")
        request.assert_not_called()

    def test_optional_token_is_only_put_in_authorization_header(self):
        transport = bridge.Ntfy(config())
        with patch.dict(os.environ, {"CODEX_NTFY_TOKEN": "private-token"}), patch.object(bridge.urllib.request, "urlopen") as request, patch.object(bridge, "tls_context"):
            transport._request("https://ntfy.sh/topic/json")
        http_request = request.call_args.args[0]
        self.assertEqual(http_request.get_header("Authorization"), "Bearer private-token")
        self.assertNotIn("private-token", http_request.full_url)

    def test_seen_ids_persist_and_singleton_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            state = bridge.State(path)
            state.mark("one", 1000)
            with self.assertRaises(RuntimeError):
                bridge.State(path)
            state.close()
            reopened = bridge.State(path)
            self.assertTrue(reopened.seen("one"))
            self.assertFalse(reopened.seen("two"))
            reopened.close()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ListInstallerTests(unittest.TestCase):
    def test_new_configuration_is_random_and_not_public_default(self):
        first, second = installer.make_config(), installer.make_config()
        self.assertNotEqual(first.topic, second.topic)
        self.assertNotEqual(first.bus_secret, second.bus_secret)
        self.assertNotEqual(first.device_id, second.device_id)
        self.assertNotEqual(first.topic, "codex_prompts")
        self.assertEqual(first.role, "combined")

    def test_reinstall_preserves_identity_and_topics(self):
        first = installer.make_config()
        self.assertEqual(asdict(installer.make_config(first)), asdict(first))

    def test_join_shares_cluster_not_device_identity_or_coordinator_role(self):
        coordinator = installer.make_config()
        worker = installer.make_config(join=coordinator, device_name="Desktop")
        for key in ("topic", "bus_topic", "bus_secret", "server"):
            self.assertEqual(getattr(coordinator, key), getattr(worker, key))
        self.assertNotEqual(coordinator.device_id, worker.device_id)
        self.assertEqual(worker.role, "agent")
        self.assertEqual(worker.device_name, "Desktop")

    def test_configuration_serializes_to_valid_toml(self):
        settings = installer.make_config(device_name='A "quoted" computer')
        self.assertEqual(tomllib.loads(installer.config_text(settings)), asdict(settings))

    def test_launch_agent_points_to_companion_not_codex(self):
        value = installer.launch_agent(Path("/app/venv/bin/python"), Path("/app/list_bridge.py"),
                                       Path("/private/config.toml"), Path("/logs"))
        self.assertTrue(value["RunAtLoad"])
        self.assertTrue(value["KeepAlive"])
        self.assertEqual(value["ProgramArguments"], ["/app/venv/bin/python", "/app/list_bridge.py", "--config", "/private/config.toml"])
        self.assertEqual(plistlib.loads(plistlib.dumps(value)), value)


if __name__ == "__main__":
    unittest.main()
