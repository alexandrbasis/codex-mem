"""Synthetic LaunchAgent tests; never invoke the host's launchctl."""
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest

from codex_mem.dashboard import DashboardServerError
from codex_mem.ui_autostart import LABEL, MARKER, install_autostart, remove_autostart


class FakeLaunchctl:
    def __init__(self):
        self.calls = []
        self.loaded = None
        self.fail_bootstrap = False

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        assert kwargs["timeout"] == 5
        command = args[1]
        if command == "print":
            return subprocess.CompletedProcess(args, 0 if self.loaded else 113, "path = " + str(self.loaded) if self.loaded else "", "")
        if command == "bootstrap":
            if self.fail_bootstrap:
                return subprocess.CompletedProcess(args, 1, "", "secret failure detail")
            self.loaded = args[3]
        elif command == "bootout":
            self.loaded = None
        return subprocess.CompletedProcess(args, 0, "", "")


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.base = Path(self.temp.name) / "data"
        self.path = self.home / "Library" / "LaunchAgents" / (LABEL + ".plist")
        self.launchctl = FakeLaunchctl()
        self.kwargs = {"_home": self.home, "_run": self.launchctl, "_platform": "darwin"}

    def install(self, port=8765):
        return install_autostart(self.base, port, **self.kwargs)

    def test_plist_and_private_append_logs(self):
        self.base.mkdir()
        log = self.base / "dashboard.stdout.log"
        log.write_text("existing\n")
        result = self.install()
        with self.path.open("rb") as handle:
            value = plistlib.load(handle)
        self.assertEqual(value["Label"], LABEL)
        self.assertEqual(value["EnvironmentVariables"], {MARKER: "1"})
        self.assertTrue(value["RunAtLoad"])
        self.assertTrue(value["KeepAlive"])
        self.assertEqual(value["ThrottleInterval"], 10)
        self.assertEqual(value["ProcessType"], "Interactive")
        self.assertEqual(value["ProgramArguments"][2:], ["--data-dir", str(self.base.resolve()), "ui", "--port", "8765", "--persistent-token"])
        self.assertTrue(Path(value["ProgramArguments"][1]).is_absolute())
        self.assertEqual(log.read_text(), "existing\n")
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result, {"status": "installed", "path": str(self.path), "port": 8765})

    def test_duplicate_does_not_restart_changed_does(self):
        self.install()
        self.launchctl.calls.clear()
        self.install()
        self.assertEqual([c[1] for c in self.launchctl.calls], ["print", "print"])
        self.launchctl.calls.clear()
        self.install(8766)
        self.assertEqual([c[1] for c in self.launchctl.calls], ["print", "bootout", "bootstrap", "print"])
        self.assertEqual(self.launchctl.calls[1][2], "gui/" + str(os.getuid()) + "/" + LABEL)

    def test_background_dashboard_is_reloaded_for_interactive_requests(self):
        self.install()
        value = plistlib.loads(self.path.read_bytes())
        value['ProcessType'] = 'Background'
        self.path.write_bytes(plistlib.dumps(value))
        self.launchctl.calls.clear()
        self.install()
        self.assertEqual([call[1] for call in self.launchctl.calls],
                         ['print', 'bootout', 'bootstrap', 'print'])
        upgraded = plistlib.loads(self.path.read_bytes())
        self.assertEqual('Interactive', upgraded['ProcessType'])
        self.assertEqual(value['ProgramArguments'], upgraded['ProgramArguments'])

    def test_failure_explicit_and_no_subprocess_details(self):
        self.launchctl.fail_bootstrap = True
        with self.assertRaisesRegex(DashboardServerError, "Check your macOS") as caught:
            self.install()
        self.assertNotIn("secret", str(caught.exception))

    def test_unmanaged_plist_preserved(self):
        self.path.parent.mkdir(parents=True)
        contents = plistlib.dumps({"Label": LABEL})
        self.path.write_bytes(contents)
        with self.assertRaisesRegex(DashboardServerError, "unmanaged"):
            self.install()
        with self.assertRaises(DashboardServerError):
            remove_autostart(**self.kwargs)
        self.assertEqual(self.path.read_bytes(), contents)
        self.assertEqual(self.launchctl.calls, [])

    def test_symlink_plist_and_log_preserved(self):
        self.path.parent.mkdir(parents=True)
        victim = Path(self.temp.name) / "victim"
        victim.write_text("keep")
        self.path.symlink_to(victim)
        with self.assertRaisesRegex(DashboardServerError, "symlink"):
            self.install()
        self.path.unlink()
        self.base.mkdir()
        (self.base / "dashboard.stderr.log").symlink_to(victim)
        with self.assertRaisesRegex(DashboardServerError, "log path"):
            self.install()
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse(self.path.exists())

    def test_foreign_loaded_job_preserved(self):
        self.launchctl.loaded = "/another/unmanaged.plist"
        with self.assertRaisesRegex(DashboardServerError, "another job"):
            self.install()
        self.assertEqual([c[1] for c in self.launchctl.calls], ["print"])

    def test_hard_link_log_preserves_unrelated_bytes_and_mode(self):
        self.base.mkdir()
        victim = Path(self.temp.name) / "unrelated"
        victim.write_text("keep bytes")
        victim.chmod(0o644)
        os.link(victim, self.base / "dashboard.stdout.log")
        with self.assertRaisesRegex(DashboardServerError, "hard links"):
            self.install()
        self.assertEqual(victim.read_text(), "keep bytes")
        self.assertEqual(victim.stat().st_mode & 0o777, 0o644)
        self.assertFalse(self.path.exists())

    def test_hard_link_plist_preserves_unrelated_bytes_and_mode(self):
        self.install()
        victim = Path(self.temp.name) / "unrelated.plist"
        os.link(self.path, victim)
        victim.chmod(0o644)
        before = victim.read_bytes()
        self.launchctl.calls.clear()
        with self.assertRaisesRegex(DashboardServerError, "hard links"):
            self.install(8766)
        self.assertEqual(victim.read_bytes(), before)
        self.assertEqual(victim.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.launchctl.calls, [])

    def test_remove_only_exact_managed_job(self):
        self.install()
        self.launchctl.calls.clear()
        result = remove_autostart(self.base, **self.kwargs)
        self.assertEqual(result["status"], "removed")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.launchctl.calls[-1], ["/bin/launchctl", "bootout", "gui/" + str(os.getuid()) + "/" + LABEL])
        self.assertTrue((self.base / "dashboard.stdout.log").exists())
        self.launchctl.calls.clear()
        self.assertEqual(remove_autostart(**self.kwargs)["status"], "absent")
        self.assertEqual(self.launchctl.calls, [])

    def test_unsupported_and_subprocess_timeout(self):
        with self.assertRaisesRegex(DashboardServerError, "requires macOS"):
            install_autostart(self.base, _platform="linux")
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("launchctl", 5)
        with self.assertRaisesRegex(DashboardServerError, "Check your macOS"):
            install_autostart(self.base, _home=self.home, _platform="darwin", _run=timeout)


if __name__ == "__main__":
    unittest.main()
