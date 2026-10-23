import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_ntfy", ROOT / "codex-ntfy.py")
notifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notifier)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        # Patch the network even for negative cases: no test can post to ntfy.
        self.http_patch = patch.object(notifier.urllib.request, "urlopen")
        self.http = self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.sound_patch = patch.object(notifier, "SOUND_SCRIPT", Mock())
        self.sound = self.sound_patch.start()
        self.sound.is_file.return_value = False
        self.addCleanup(self.sound_patch.stop)
        self.run_patch = patch.object(notifier.subprocess, "run")
        self.run = self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

    def send(self, **overrides):
        payload = {
            "type": "agent-turn-complete",
            "input-messages": ["Private user prompt"],
            "last-assistant-message": "Private response\n" + notifier.FINAL_MARKER,
        }
        payload.update(overrides)
        notifier.main(json.dumps(payload))

    def test_unmarked_intermediate_sends_nothing(self):
        self.send(**{"last-assistant-message": "Still working"})
        self.http.assert_not_called()
        self.run.assert_not_called()

    def test_marked_final_sends_exactly_one_fixed_private_safe_message(self):
        self.send()
        self.http.assert_called_once()
        request = self.http.call_args.args[0]
        self.assertEqual(request.full_url, "https://ntfy.sh/codex_prompts")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"Codex finished responding.")
        self.assertEqual(request.get_header("Title"), "Codex")
        self.assertEqual(request.get_header("Tags"), "white_check_mark")
        self.assertEqual(self.http.call_args.kwargs, {"timeout": 10})
        self.run.assert_not_called()

    def test_marked_recap_sends_nothing(self):
        self.send(**{"input-messages": [
            notifier.RECAP_PREFIX + "\nOther instructions"
            + notifier.RECAP_SECTION + "\nPrivate history"
        ]})
        self.http.assert_not_called()
        self.run.assert_not_called()

    def test_non_completion_event_sends_nothing(self):
        self.send(type="agent-turn-started")
        self.http.assert_not_called()
        self.run.assert_not_called()

    def test_marker_with_trailing_whitespace_is_accepted(self):
        self.send(**{"last-assistant-message": notifier.FINAL_MARKER + "\n \t"})
        self.http.assert_called_once()

    def test_marker_in_middle_is_not_accepted(self):
        self.send(**{"last-assistant-message": notifier.FINAL_MARKER + " not done"})
        self.http.assert_not_called()

    def test_missing_or_non_text_assistant_message_sends_nothing(self):
        for message in (None, 42, [], {}):
            with self.subTest(message=message):
                self.send(**{"last-assistant-message": message})
        self.http.assert_not_called()

    def test_invalid_json_is_ignored(self):
        for payload in ("not JSON", "{", "", None):
            notifier.main(payload)
        self.http.assert_not_called()

    def test_recap_requires_both_identifiers(self):
        for text in (notifier.RECAP_PREFIX, "Only" + notifier.RECAP_SECTION):
            with self.subTest(text=text):
                self.send(**{"input-messages": [text]})
        self.assertEqual(self.http.call_count, 2)

    def test_unexpected_input_messages_shape_is_safe(self):
        self.send(**{"input-messages": None})
        self.http.assert_called_once()

    def test_success_runs_existing_sound_script_once(self):
        self.sound.is_file.return_value = True
        self.send()
        self.http.assert_called_once()
        self.run.assert_called_once_with(
            [str(self.sound)], check=False,
            stdout=notifier.subprocess.DEVNULL,
            stderr=notifier.subprocess.DEVNULL,
        )

    def test_unreachable_ntfy_fails_quietly_without_sound(self):
        self.sound.is_file.return_value = True
        self.http.side_effect = urllib.error.URLError("offline")
        self.send()
        self.run.assert_not_called()

    def test_unrunnable_sound_script_fails_quietly(self):
        self.sound.is_file.return_value = True
        self.run.side_effect = OSError("not executable")
        self.send()
        self.http.assert_called_once()
        self.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
