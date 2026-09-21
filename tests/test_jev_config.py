"""Opt-in external screening configuration and credential-path boundary."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from codex_mem.cli import main
from codex_mem.config import configure, load_config


class JevConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)

    def test_missing_and_older_config_do_not_enable_external_screening(self) -> None:
        for raw in (None, {"processor_enabled": True}):
            with self.subTest(raw=raw):
                if raw is not None:
                    (self.home / "config.json").write_text(json.dumps(raw))
                config = load_config(self.home)
                self.assertTrue(config.valid)
                self.assertFalse(config["jev_filter_enabled"])
                self.assertEqual("", config["jev_filter_key_file"])
                self.assertEqual([], config["jev_filter_projects"])

    def test_path_persists_without_reading_credentials(self) -> None:
        key_file = self.home / "not-created-key-file"
        configure(self.home, jev_filter_enabled=True, jev_filter_key_file=str(key_file))
        config = load_config(self.home)
        self.assertTrue(config.valid)
        self.assertTrue(config["jev_filter_enabled"])
        self.assertEqual(str(key_file), config["jev_filter_key_file"])
        self.assertFalse(key_file.exists())
        configure(self.home, jev_filter_key_file="")
        self.assertEqual("", load_config(self.home)["jev_filter_key_file"])

    def test_bad_values_are_rejected_and_existing_bad_config_fails_closed(self) -> None:
        for updates in (
            {"jev_filter_enabled": "true"},
            {"jev_filter_enabled": 1},
            {"jev_filter_enabled": None},
            *({"jev_filter_projects": value} for value in (
                None, False, "all", ["relative"], [""], [None], ["/tmp/\x00project"]
            )),
            *({"jev_filter_key_file": value} for value in (
                None, False, [], "relative/key", "~/key", "apikey_example", "/tmp/key\n", "/tmp/\x00key"
            )),
        ):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    configure(self.home, **updates)
                (self.home / "config.json").write_text(json.dumps(updates))
                config = load_config(self.home)
                self.assertFalse(config.valid)
                self.assertFalse(config["processor_enabled"])
                self.assertFalse(config["jev_filter_enabled"])
                (self.home / "config.json").unlink()

    def run_cli(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["config", "--data-dir", str(self.home), *args])
        return status, json.loads(output.getvalue())

    def test_cli_flags_enable_disable_and_persist_key_path(self) -> None:
        key_file = str(self.home / "key")
        status, _ = self.run_cli("--jev-filter-enabled", "--jev-filter-key-file", key_file)
        self.assertEqual(0, status)
        self.assertTrue(load_config(self.home)["jev_filter_enabled"])
        self.assertEqual(key_file, load_config(self.home)["jev_filter_key_file"])
        status, _ = self.run_cli("--no-jev-filter-enabled")
        self.assertEqual(0, status)
        self.assertFalse(load_config(self.home)["jev_filter_enabled"])

    def test_cli_set_uses_boolean_validation_and_hides_invalid_key(self) -> None:
        status, _ = self.run_cli("--set", "jev_filter_enabled=true")
        self.assertEqual(0, status)
        self.assertIs(True, load_config(self.home)["jev_filter_enabled"])
        status, _ = self.run_cli("--set", "jev_filter_enabled=invalid")
        self.assertEqual(2, status)
        self.assertTrue(load_config(self.home)["jev_filter_enabled"])
        secret = "apikey_example_only"
        status, result = self.run_cli("--set", f"jev_filter_key_file={secret}")
        self.assertEqual(2, status)
        self.assertNotIn(secret, json.dumps(result))
        key_file = str(self.home / "key")
        status, _ = self.run_cli("--set", f"jev_filter_key_file={key_file}")
        self.assertEqual(0, status)
        self.assertEqual(key_file, load_config(self.home)["jev_filter_key_file"])

    def test_project_scope_flags_set_and_clear(self) -> None:
        first = str((self.home / "first").resolve())
        second = str((self.home / "second").resolve())
        status, _ = self.run_cli("--jev-filter-project", first, "--jev-filter-project", second)
        self.assertEqual(0, status)
        self.assertEqual([first, second], load_config(self.home)["jev_filter_projects"])
        status, _ = self.run_cli("--set", "jev_filter_projects=" + json.dumps([first, first]))
        self.assertEqual(0, status)
        self.assertEqual([first], load_config(self.home)["jev_filter_projects"])
        status, _ = self.run_cli("--set", "jev_filter_projects=[]")
        self.assertEqual(0, status)
        self.assertEqual([], load_config(self.home)["jev_filter_projects"])


if __name__ == "__main__":
    unittest.main()
