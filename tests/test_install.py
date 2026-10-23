import importlib.util
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ntfy_install", ROOT / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class ConfigTests(unittest.TestCase):
    settings = {
        "notify": ["/usr/bin/python3", "/home/example/codex-ntfy.py"],
        "model_instructions_file": "/home/example/marker.md",
    }

    def test_missing_keys_are_top_level_before_tables(self):
        original = '# Keep this comment\nmodel = "example"\n\n[tui]\nnotifications = true\n'
        updated = installer.updated_config(original, self.settings)
        parsed = tomllib.loads(updated)
        self.assertEqual(parsed["notify"], self.settings["notify"])
        self.assertTrue(parsed["tui"]["notifications"])
        self.assertTrue(updated.endswith(original))

    def test_existing_multiline_assignment_is_replaced(self):
        original = '''# Comment
notify = [
    "python3", # Keep unrelated config, not this replaced assignment
    "/old/script.py",
]
model_instructions_file = "/old/instructions.md"
model = "unchanged"
[profiles.test]
notify = ["leave", "this"]
'''
        updated = installer.updated_config(original, self.settings)
        parsed = tomllib.loads(updated)
        self.assertEqual(parsed["notify"], self.settings["notify"])
        self.assertEqual(parsed["model"], "unchanged")
        self.assertEqual(parsed["profiles"]["test"]["notify"], ["leave", "this"])
        self.assertTrue(updated.startswith("# Comment\n"))

    def test_table_like_text_in_multiline_string_is_not_a_header(self):
        original = 'description = """\n[not_a_table]\ntext\n"""\nnotify = ["old"]\n'
        updated = installer.updated_config(original, self.settings)
        self.assertEqual(tomllib.loads(updated)["description"],
                         tomllib.loads(original)["description"])
        self.assertEqual(tomllib.loads(updated)["notify"], self.settings["notify"])

    def test_quoted_keys_and_paths_with_spaces(self):
        original = '"notify" = ["old"]\n"model_instructions_file" = "old"\n'
        settings = {
            "notify": ["/a path/python3", '/a "quoted" path/script.py'],
            "model_instructions_file": "/a path/marker.md",
        }
        self.assertEqual(tomllib.loads(installer.updated_config(original, settings)), settings)

    def test_empty_configuration(self):
        self.assertEqual(tomllib.loads(installer.updated_config("", self.settings)),
                         self.settings)

    def test_invalid_original_configuration_is_rejected(self):
        with self.assertRaises(tomllib.TOMLDecodeError):
            installer.updated_config("notify = [", self.settings)

    def test_update_is_idempotent(self):
        first = installer.updated_config('[tui]\nnotifications = true', self.settings)
        self.assertEqual(installer.updated_config(first, self.settings), first)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.config = self.home / ".codex/config.toml"
        self.print_patch = patch("builtins.print")
        self.print_patch.start()
        self.addCleanup(self.print_patch.stop)

    def write_config(self, text):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(text)

    def test_fresh_install_copies_assets_and_sets_absolute_paths(self):
        installer.install(self.home)
        script = self.home / ".local/bin/codex-ntfy.py"
        marker = self.home / ".codex/notification-final-marker.md"
        self.assertEqual(script.read_bytes(), (ROOT / "codex-ntfy.py").read_bytes())
        self.assertEqual(marker.read_bytes(), (ROOT / "notification-final-marker.md").read_bytes())
        parsed = tomllib.loads(self.config.read_text())
        self.assertEqual(parsed["notify"], [str(Path(sys.executable).resolve()), str(script)])
        self.assertEqual(parsed["model_instructions_file"], str(marker))
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_second_install_does_not_create_backups(self):
        installer.install(self.home)
        installer.install(self.home)
        self.assertEqual(list(self.home.rglob("*.bak")), [])

    def test_existing_other_notifier_is_not_replaced_without_consent(self):
        original = 'notify = ["another-notifier"]\n'
        self.write_config(original)
        with self.assertRaisesRegex(ValueError, "notify points elsewhere"):
            installer.install(self.home)
        self.assertEqual(self.config.read_text(), original)
        self.assertFalse((self.home / ".local").exists())

    def test_existing_other_instructions_are_not_replaced_without_consent(self):
        original = 'model_instructions_file = "/my/important.md"\n'
        self.write_config(original)
        with self.assertRaisesRegex(ValueError, "model_instructions_file points elsewhere"):
            installer.install(self.home)
        self.assertEqual(self.config.read_text(), original)
        self.assertFalse((self.home / ".local").exists())

    def test_explicit_replacement_backs_up_config_preserving_other_settings(self):
        original = 'notify = ["old"]\nmodel = "keep"\n[tui]\nnotifications = false\n'
        self.write_config(original)
        installer.install(self.home, replace_existing=True)
        self.assertEqual(Path(str(self.config) + ".ntfy.bak").read_text(), original)
        parsed = tomllib.loads(self.config.read_text())
        self.assertEqual(parsed["model"], "keep")
        self.assertFalse(parsed["tui"]["notifications"])

    def test_existing_notifier_interpreter_can_be_updated(self):
        script = self.home / ".local/bin/codex-ntfy.py"
        self.write_config(f'notify = ["python3", "{script}"]\n')
        installer.install(self.home)
        self.assertEqual(tomllib.loads(self.config.read_text())["notify"][0],
                         str(Path(sys.executable).resolve()))

    def test_dry_run_does_not_write_anything(self):
        installer.install(self.home, dry_run=True)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_invalid_config_does_not_install_assets(self):
        self.write_config("notify = [")
        with self.assertRaises(tomllib.TOMLDecodeError):
            installer.install(self.home)
        self.assertFalse((self.home / ".local").exists())

    def test_symlink_targets_are_rejected(self):
        elsewhere = self.home / "other.toml"
        elsewhere.write_text("")
        self.config.parent.mkdir()
        self.config.symlink_to(elsewhere)
        with self.assertRaisesRegex(ValueError, "symlink"):
            installer.install(self.home)
        self.assertEqual(elsewhere.read_text(), "")

    def test_existing_assets_are_backed_up_on_upgrade(self):
        script = self.home / ".local/bin/codex-ntfy.py"
        script.parent.mkdir(parents=True)
        script.write_text("old version")
        installer.install(self.home)
        self.assertEqual(Path(str(script) + ".ntfy.bak").read_text(), "old version")

    def test_backup_names_are_not_reused(self):
        self.write_config("model = 'first'\n")
        installer.backup(self.config)
        self.config.write_text("model = 'second'\n")
        installer.backup(self.config)
        self.assertEqual(Path(str(self.config) + ".ntfy.bak").read_text(), "model = 'first'\n")
        self.assertEqual(Path(str(self.config) + ".ntfy.1.bak").read_text(), "model = 'second'\n")


if __name__ == "__main__":
    unittest.main()
