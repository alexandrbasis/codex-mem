import importlib.util
import io
import itertools
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

_SPEC = importlib.util.spec_from_file_location("codex_mem_installer", Path(__file__).resolve().parents[1] / "scripts/install.py")
installer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "home"
        self.source = Path(self.temp.name).resolve() / "source"
        for name, body in {
            ".codex-plugin/plugin.json": json.dumps({"name": "codex-mem", "version": "1.0.0"}),
            ".mcp.json": "{}", "hooks/hooks.json": "{}",
            "scripts/codex-mem.py": (
                "#!/usr/bin/env python3\nimport sys\n"
                "if sys.argv[-2:] == ['service', 'status']:\n"
                "    print('{\"status\":\"stopped\",\"running\":false,\"lock_held\":false}')\n"
                "else:\n    print('managed-current', *sys.argv[1:])\n"
            ),
        }.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        self.registry = self.home / ".agents/plugins/marketplace.json"

    def run_install(self, apply=True):
        return installer.install(self.source, self.home, apply=apply, register_only=True)

    def set_source_version(self, version):
        manifest_path = self.source / ".codex-plugin/plugin.json"
        value = json.loads(manifest_path.read_text())
        value["version"] = version
        manifest_path.write_text(json.dumps(value))

    def cache_root(self, marketplace="personal"):
        return self.home / ".codex/plugins/cache" / marketplace / "codex-mem"

    def cache_from(self, source, version):
        package = self.cache_root() / version
        package.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, package)
        manifest_path = package / ".codex-plugin/plugin.json"
        manifest_value = json.loads(manifest_path.read_text())
        manifest_value["version"] = version
        manifest_path.write_text(json.dumps(manifest_value))
        return package

    @staticmethod
    def package_bytes(package):
        return {
            str(item.relative_to(package)): item.read_bytes()
            for item in sorted(package.rglob("*")) if item.is_file()
        }

    @staticmethod
    def launcher_path(package):
        return package / "scripts/codex-mem.py"

    @staticmethod
    def launcher_backup_path(package):
        return package / "scripts/codex-mem.py.codex-mem-original"

    def assert_forwarded_cache(self, package, original_launcher):
        launcher = self.launcher_path(package)
        backup = self.launcher_backup_path(package)
        self.assertIn(installer._FORWARDER_MARKER, launcher.read_text())
        self.assertEqual(backup.read_bytes(), original_launcher)

    def assert_cache_restored_with_forwarder(self, package, expected_bytes):
        actual = self.package_bytes(package)
        for name, body in expected_bytes.items():
            if name == "scripts/codex-mem.py":
                continue
            self.assertEqual(body, actual[name], name)
        self.assertEqual(
            expected_bytes["scripts/codex-mem.py"],
            self.launcher_backup_path(package).read_bytes(),
        )
        self.assertIn(installer._FORWARDER_MARKER, self.launcher_path(package).read_text())

    def fake_codex(self, *, remove_cache=False, recreate_cache=False, exit_code=0,
                   record_invocation=False):
        path = Path(self.temp.name) / "fake-codex"
        command = "#!/bin/sh\n"
        if remove_cache:
            command += 'rm -rf "$CODEX_MEM_TEST_OLD_CACHE"\n'
        if recreate_cache:
            command += 'mkdir -p "$CODEX_MEM_TEST_RECREATE_PARENT"\n'
            command += 'cp -R "$CODEX_MEM_TEST_RECREATE_SOURCE" "$CODEX_MEM_TEST_RECREATE_CACHE"\n'
        if record_invocation:
            command += ': > "$CODEX_MEM_TEST_INVOKED"\n'
        if exit_code:
            command += f"exit {exit_code}\n"
        else:
            command += "printf '%s\\n' '{\"installed\":true}'\n"
        path.write_text(command)
        path.chmod(0o700)
        return path

    def apply_with_fake_codex(self, codex):
        with mock.patch.object(installer.Path, "home", return_value=self.home):
            return installer.install(self.source, self.home, apply=True, codex=str(codex))

    def test_preview_writes_nothing(self):
        result = self.run_install(False)
        self.assertFalse(result["applied"])
        self.assertFalse(self.home.exists())

    def test_registration_preserves_other_entries_and_metadata(self):
        existing = {"name": "mine", "interface": {"displayName": "My tools"},
                    "custom": {"preserve": True}, "plugins": [{"name": "another", "source": {"source": "local", "path": "./another"}}]}
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text(json.dumps(existing))
        result = self.run_install()
        installed = json.loads(self.registry.read_text())
        self.assertEqual(installed["plugins"][:-1], existing["plugins"])
        self.assertEqual(installed["interface"], existing["interface"])
        self.assertEqual(installed["custom"], existing["custom"])
        self.assertEqual(result["selector"], "codex-mem@mine")
        self.assertEqual(json.loads(Path(result["registry_backup"]).read_text()), existing)
        self.assertTrue((self.home / "plugins/codex-mem/.codex-mem-managed").is_file())
        self.run_install()
        self.assertEqual(len(json.loads(self.registry.read_text())["plugins"]), 2)

    def test_conflicting_plugin_is_untouched(self):
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text(json.dumps({"name": "personal", "plugins": [{"name": "codex-mem", "source": {"source": "github", "repo": "someone/else"}}]}))
        before = self.registry.read_bytes()
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertEqual(self.registry.read_bytes(), before)

    def test_unmanaged_destination_and_symlink_rejected(self):
        target = self.home / "plugins/codex-mem"
        target.mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.run_install()
        target.rmdir()
        target.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_install()

    def test_copy_failure_keeps_existing_installation(self):
        self.run_install()
        target = self.home / "plugins/codex-mem/.codex-plugin/plugin.json"
        before = target.read_bytes()
        registry_before = self.registry.read_bytes()
        with mock.patch.object(installer.shutil, "copytree", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                self.run_install()
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.registry.read_bytes(), registry_before)

    def test_custom_home_cannot_redirect_real_codex(self):
        with self.assertRaises(ValueError):
            installer.install(self.source, self.home, apply=True)
        self.assertFalse(self.home.exists())

    def test_activation_timeout_reports_partial_state(self):
        result = self.run_install()
        with mock.patch.object(installer.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 120)):
            receipt = installer._activate(result, codex="codex", register_only=False)
        self.assertTrue(receipt["applied"])
        self.assertFalse(receipt["installed"])
        self.assertEqual(receipt["activation_status"], "unknown")
        self.assertEqual(receipt["selector"], "codex-mem@personal")
        self.assertTrue(Path(receipt["destination"]).exists())

    def test_upgrade_restores_old_cache_removed_by_codex(self):
        self.run_install()
        old_target = self.home / "plugins/codex-mem"
        old_cache = self.home / ".codex/plugins/cache/personal/codex-mem/1.0.0"
        old_cache.parent.mkdir(parents=True)
        shutil.copytree(old_target, old_cache)
        old_launcher = (old_cache / "scripts/codex-mem.py").read_bytes()
        self.set_source_version("1.1.0")
        fake = self.fake_codex(remove_cache=True)

        with mock.patch.dict(os.environ, {"CODEX_MEM_TEST_OLD_CACHE": str(old_cache)}):
            result = self.apply_with_fake_codex(fake)

        self.assertTrue(result["installed"])
        self.assertEqual(result["previous_cache"]["status"], "restored")
        self.assertEqual(result["previous_cache"]["version"], "1.0.0")
        self.assertEqual(
            json.loads((old_cache / ".codex-plugin/plugin.json").read_text())["version"], "1.0.0"
        )
        self.assert_forwarded_cache(old_cache, old_launcher)
        self.assertEqual(
            json.loads((self.home / "plugins/codex-mem/.codex-plugin/plugin.json").read_text())["version"],
            "1.1.0",
        )

    def test_upgrade_does_not_overwrite_existing_old_cache(self):
        self.run_install()
        old_target = self.home / "plugins/codex-mem"
        old_cache = self.home / ".codex/plugins/cache/personal/codex-mem/1.0.0"
        old_cache.parent.mkdir(parents=True)
        shutil.copytree(old_target, old_cache)
        sentinel = old_cache / "preserve-me"
        sentinel.write_text("existing cache content")
        self.set_source_version("1.1.0")

        result = self.apply_with_fake_codex(self.fake_codex())

        self.assertTrue(result["installed"])
        self.assertEqual(result["previous_cache"]["status"], "already_present")
        self.assertEqual(sentinel.read_text(), "existing cache content")

    def test_same_version_reinstall_preserves_all_live_cached_versions(self):
        self.set_source_version("1.1.0")
        self.run_install()
        target = self.home / "plugins/codex-mem"
        cache_v1_0 = self.cache_from(target, "1.0.0")
        cache_v1_1 = self.cache_from(target, "1.1.0")
        expected_v1_0 = self.package_bytes(cache_v1_0)
        expected_v1_1 = self.package_bytes(cache_v1_1)

        self.set_source_version("1.2.0")
        self.run_install()
        expected_v1_2 = self.package_bytes(target)
        cache_root = self.cache_root()
        fake = self.fake_codex(remove_cache=True)

        with mock.patch.dict(os.environ, {"CODEX_MEM_TEST_OLD_CACHE": str(cache_root)}):
            result = self.apply_with_fake_codex(fake)

        self.assertTrue(result["installed"])
        self.assertEqual(result["cache_preservation"], "complete")
        self.assert_cache_restored_with_forwarder(cache_root / "1.0.0", expected_v1_0)
        self.assert_cache_restored_with_forwarder(cache_root / "1.1.0", expected_v1_1)
        self.assertEqual(self.package_bytes(cache_root / "1.2.0"), expected_v1_2)
        self.assertFalse((self.home / "plugins/cache").exists())
        retained = {receipt["version"]: receipt for receipt in result["retained_caches"]}
        self.assertEqual(set(retained), {"1.0.0", "1.1.0", "1.2.0"})
        self.assertTrue(all(receipt["status"] == "restored" for receipt in retained.values()))
        refresh = result["legacy_launcher_refresh"]
        self.assertEqual("complete", refresh["status"])
        self.assertEqual(
            {"refreshed", "skipped"},
            {receipt["status"] for receipt in refresh["packages"]},
        )
        self.assertEqual(
            json.loads((target / ".codex-plugin/plugin.json").read_text())["version"], "1.2.0"
        )

    def test_activation_failure_keeps_its_error_after_cache_recovery(self):
        self.run_install()
        old_target = self.home / "plugins/codex-mem"
        old_cache = self.cache_from(old_target, "1.0.0")
        expected_cache = self.package_bytes(old_cache)
        self.set_source_version("1.1.0")
        fake = self.fake_codex(remove_cache=True, exit_code=1)

        with mock.patch.dict(os.environ, {"CODEX_MEM_TEST_OLD_CACHE": str(old_cache)}):
            result = self.apply_with_fake_codex(fake)

        self.assertFalse(result["installed"])
        self.assertIn("Codex activation failed", result["error"])
        self.assertEqual(result["cache_preservation"], "complete")
        self.assertEqual(result["previous_cache"]["status"], "restored")
        self.assert_cache_restored_with_forwarder(old_cache, expected_cache)

    def test_activation_does_not_overwrite_a_cache_recreated_by_codex(self):
        self.run_install()
        old_target = self.home / "plugins/codex-mem"
        old_cache = self.cache_from(old_target, "1.0.0")
        replacement = Path(self.temp.name) / "recreated-cache"
        shutil.copytree(old_cache, replacement)
        sentinel = replacement / "created-by-codex"
        sentinel.write_text("leave this package alone")
        self.set_source_version("1.1.0")
        fake = self.fake_codex(remove_cache=True, recreate_cache=True)

        with mock.patch.dict(os.environ, {
            "CODEX_MEM_TEST_OLD_CACHE": str(old_cache),
            "CODEX_MEM_TEST_RECREATE_PARENT": str(old_cache.parent),
            "CODEX_MEM_TEST_RECREATE_SOURCE": str(replacement),
            "CODEX_MEM_TEST_RECREATE_CACHE": str(old_cache),
        }):
            result = self.apply_with_fake_codex(fake)

        self.assertTrue(result["installed"])
        self.assertEqual(result["previous_cache"]["status"], "already_present")
        self.assertEqual((old_cache / "created-by-codex").read_text(), "leave this package alone")

    def test_legacy_cache_launcher_delegates_to_current_managed_target(self):
        self.run_install()
        old_target = self.home / "plugins/codex-mem"
        old_cache = self.cache_from(old_target, "1.0.0")
        original_launcher = self.launcher_path(old_cache).read_bytes()
        self.set_source_version("1.1.0")

        result = self.apply_with_fake_codex(self.fake_codex())

        refresh = result["legacy_launcher_refresh"]
        self.assertEqual("complete", refresh["status"])
        self.assertEqual("refreshed", refresh["packages"][0]["status"])
        self.assert_forwarded_cache(old_cache, original_launcher)
        completed = subprocess.run(
            [sys.executable, str(self.launcher_path(old_cache)), "probe"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("managed-current probe\n", completed.stdout)

    def test_legacy_cache_refresh_is_idempotent_and_backup_stable(self):
        self.run_install()
        old_target = self.home / "plugins/codex-mem"
        old_cache = self.cache_from(old_target, "1.0.0")
        original_launcher = self.launcher_path(old_cache).read_bytes()
        self.set_source_version("1.1.0")
        self.apply_with_fake_codex(self.fake_codex())
        backup_before = self.launcher_backup_path(old_cache).read_bytes()
        launcher_before = self.launcher_path(old_cache).read_bytes()

        result = self.apply_with_fake_codex(self.fake_codex())

        refresh = result["legacy_launcher_refresh"]
        self.assertEqual("complete", refresh["status"])
        self.assertEqual("already_current", refresh["packages"][0]["status"])
        self.assertEqual(original_launcher, backup_before)
        self.assertEqual(backup_before, self.launcher_backup_path(old_cache).read_bytes())
        self.assertEqual(launcher_before, self.launcher_path(old_cache).read_bytes())

    def test_missing_current_target_fails_closed_without_touching_cache_launcher(self):
        self.run_install()
        old_target = self.home / "plugins/codex-mem"
        old_cache = self.cache_from(old_target, "0.9.0")
        original_launcher = self.launcher_path(old_cache).read_bytes()
        current_launcher = old_target / "scripts/codex-mem.py"
        current_launcher.unlink()

        result = installer._refresh_legacy_launchers(
            self.cache_root(), old_target, "1.0.0"
        )

        self.assertEqual("failed", result["status"])
        self.assertEqual("target_unavailable", result["reason"])
        self.assertEqual(original_launcher, self.launcher_path(old_cache).read_bytes())
        self.assertFalse(self.launcher_backup_path(old_cache).exists())

    def test_unrelated_cache_namespace_is_untouched(self):
        self.run_install()
        old_target = self.home / "plugins/codex-mem"
        old_cache = self.cache_from(old_target, "1.0.0")
        unrelated = self.home / ".codex/plugins/cache/other-plugin/9.9.9/scripts/codex-mem.py"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("unrelated\n")
        self.set_source_version("1.1.0")

        self.apply_with_fake_codex(self.fake_codex())

        self.assertEqual("unrelated\n", unrelated.read_text())
        self.assertTrue(self.launcher_backup_path(old_cache).is_file())

    def test_unsafe_cached_version_aborts_before_activation(self):
        self.run_install()
        target = self.home / "plugins/codex-mem"
        target_before = self.package_bytes(target)
        registry_before = self.registry.read_bytes()
        cache_root = self.cache_root()
        cache_root.mkdir(parents=True)
        (cache_root / "1.0.0").symlink_to(self.source, target_is_directory=True)
        self.set_source_version("1.1.0")
        invoked = Path(self.temp.name) / "codex-was-called"
        fake = self.fake_codex(record_invocation=True)

        with mock.patch.dict(os.environ, {"CODEX_MEM_TEST_INVOKED": str(invoked)}):
            with self.assertRaises(ValueError):
                self.apply_with_fake_codex(fake)

        self.assertFalse(invoked.exists())
        self.assertEqual(self.package_bytes(target), target_before)
        self.assertEqual(self.registry.read_bytes(), registry_before)

    def test_restore_failure_keeps_recovery_backup_and_new_source(self):
        self.run_install()
        old_target = self.home / "plugins/codex-mem"
        old_cache = self.cache_from(old_target, "1.0.0")
        expected_cache = self.package_bytes(old_cache)
        self.set_source_version("1.1.0")
        fake = self.fake_codex(remove_cache=True)

        with mock.patch.dict(os.environ, {"CODEX_MEM_TEST_OLD_CACHE": str(old_cache)}):
            with mock.patch.object(installer, "_restore_cache_package", side_effect=shutil.Error("test")):
                result = self.apply_with_fake_codex(fake)

        self.assertTrue(result["installed"])
        self.assertEqual(result["cache_preservation"], "partial")
        self.assertEqual(result["previous_cache"]["status"], "restore_failed")
        recovery = Path(result["cache_recovery_backup"])
        self.assertEqual(self.package_bytes(recovery / "1.0.0"), expected_cache)
        self.assertEqual(
            json.loads((self.home / "plugins/codex-mem/.codex-plugin/plugin.json").read_text())["version"],
            "1.1.0",
        )

    @staticmethod
    def running_service(owner="100:old", version=None):
        return {"status": "running", "running": True, "lock_held": True,
                "pid": int(owner.split(":")[0]), "owner_id": owner, "runtime_version": version}

    @staticmethod
    def stopped_service():
        return {"status": "stopped", "running": False, "lock_held": False}

    def upgrade_with_service(self, responses, *, activation_exit=0):
        self.run_install()
        self.set_source_version("1.1.0")
        with mock.patch.object(installer, "_service_command", side_effect=responses) as command:
            with mock.patch.object(installer.time, "sleep"):
                result = self.apply_with_fake_codex(self.fake_codex(exit_code=activation_exit))
        return result, command

    def test_first_install_and_register_only_do_not_control_service(self):
        with mock.patch.object(installer, "_service_command") as command:
            first = self.apply_with_fake_codex(self.fake_codex())
            registered = self.run_install()
        command.assert_not_called()
        self.assertEqual(first["runtime_refresh"]["code"], "first_install")
        self.assertEqual(registered["runtime_refresh"]["code"], "register_only")

    def test_upgrade_preserves_stopped_service(self):
        result, command = self.upgrade_with_service([self.stopped_service()])
        self.assertTrue(result["installed"])
        self.assertNotIn("error", result)
        self.assertEqual(result["runtime_refresh"]["code"], "previously_stopped")
        self.assertEqual([call.args[2] for call in command.call_args_list], ["status"])

    def test_upgrade_preserves_existing_shutdown_request(self):
        snapshot = {**self.running_service(), "stop_requested": True}
        result, command = self.upgrade_with_service([snapshot])
        self.assertEqual(result["runtime_refresh"]["status"], "unchanged")
        self.assertEqual(result["runtime_refresh"]["code"], "shutdown_already_requested")
        self.assertEqual(command.call_count, 1)

    def test_upgrade_refreshes_verified_running_service_through_managed_launcher(self):
        old = self.running_service()
        new = self.running_service("200:new", "1.1.0")
        result, command = self.upgrade_with_service([
            old, old, {"status": "stopping"}, old, self.stopped_service(),
            {"status": "started"}, new,
        ])
        self.assertTrue(result["installed"])
        self.assertNotIn("error", result)
        refresh = result["runtime_refresh"]
        self.assertEqual(refresh["status"], "restarted")
        self.assertIsNone(refresh["previous_runtime_version"])
        self.assertEqual(refresh["runtime_version"], "1.1.0")
        self.assertEqual(refresh["pid"], 200)
        calls = command.call_args_list
        self.assertEqual([call.args[2] for call in calls],
                         ["status", "status", "stop", "status", "status", "start", "status"])
        self.assertTrue(calls[0].args[0].parents[1].name.startswith(".codex-mem-stage-"))
        self.assertTrue(all(call.args[0] == self.home / "plugins/codex-mem/scripts/codex-mem.py"
                            for call in calls[1:]))
        self.assertTrue(all(call.args[1] == self.home / ".local/share/codex-mem" for call in calls))
        self.assertEqual(calls[2].kwargs["expected_owner"], old["owner_id"])

    def test_upgrade_keeps_explicit_memory_home_for_all_service_commands(self):
        memory_home = Path(self.temp.name).resolve() / "other-memory"
        old = self.running_service()
        with mock.patch.dict(os.environ, {"CODEX_MEM_HOME": f"  {memory_home}  "}):
            _, command = self.upgrade_with_service([
                old, old, {"status": "stopping"}, self.stopped_service(),
                {"status": "started"}, self.running_service("200:new", "1.1.0"),
            ])
        self.assertTrue(all(call.args[1] == memory_home for call in command.call_args_list))

    def test_unknown_preexisting_owner_is_reported_without_stop_or_start(self):
        for snapshot in ({"status": "unknown", "lock_held": None},
                         {"status": "running", "running": True, "pid": 100},
                         {"status": "starting", "lock_held": True}):
            with self.subTest(snapshot=snapshot):
                result, command = self.upgrade_with_service([snapshot])
                self.assertTrue(result["installed"])
                self.assertEqual(result["runtime_refresh"]["status"], "restart_pending")
                self.assertEqual(result["runtime_refresh"]["code"], "previous_owner_unverified")
                self.assertIn("background service update is not verified", result["error"])
                self.assertEqual(command.call_count, 1)

    def test_owner_change_after_activation_is_not_stopped(self):
        result, command = self.upgrade_with_service([
            self.running_service(), self.running_service("200:other", "1.1.0"),
        ])
        self.assertEqual(result["runtime_refresh"]["code"], "previous_owner_changed")
        self.assertEqual([call.args[2] for call in command.call_args_list], ["status", "status"])

    def test_failed_activation_does_not_stop_preexisting_worker(self):
        result, command = self.upgrade_with_service([self.running_service()], activation_exit=7)
        self.assertFalse(result["installed"])
        self.assertEqual(result["runtime_refresh"]["code"], "activation_not_verified")
        self.assertIn("Codex activation failed", result["error"])
        self.assertEqual(command.call_count, 1)

    def test_atomic_owner_guard_rejection_never_restarts_replacement_worker(self):
        old = self.running_service()
        result, command = self.upgrade_with_service([
            old, old, {"status": "blocked", "code": "owner_changed"},
        ])
        self.assertEqual(result["runtime_refresh"]["code"], "previous_owner_changed")
        self.assertEqual([call.args[2] for call in command.call_args_list], ["status", "status", "stop"])
        self.assertEqual(command.call_args.kwargs["expected_owner"], old["owner_id"])

    def test_shutdown_timeout_does_not_start_competing_worker(self):
        self.run_install()
        target = self.home / "plugins/codex-mem"
        old = self.running_service()
        responses = [old, {"status": "stopping"}, old]
        with mock.patch.object(installer, "_service_command", side_effect=responses) as command:
            with mock.patch.object(installer, "SERVICE_REFRESH_TIMEOUT", 1.0):
                with mock.patch.object(installer.time, "monotonic", side_effect=itertools.count(0, 0.2)):
                    with mock.patch.object(installer.time, "sleep"):
                        refresh = installer._refresh_runtime(target, "1.0.0", self.home / "memory", old)
        self.assertEqual(refresh["status"], "restart_pending")
        self.assertEqual(refresh["code"], "shutdown_pending")
        self.assertEqual([call.args[2] for call in command.call_args_list], ["status", "stop", "status"])

    def test_owner_change_during_stop_never_starts_another_worker(self):
        old = self.running_service()
        result, command = self.upgrade_with_service([
            old, old, {"status": "stopping"}, self.running_service("200:other", "1.1.0"),
        ])
        self.assertEqual(result["runtime_refresh"]["code"], "owner_changed_during_stop")
        self.assertNotIn("start", [call.args[2] for call in command.call_args_list])

    def test_start_failure_is_separate_from_successful_plugin_activation(self):
        old = self.running_service()
        result, _ = self.upgrade_with_service([
            old, old, {"status": "stopping"}, self.stopped_service(),
            {"status": "error", "code": "startup_failed"}, self.stopped_service(),
        ])
        self.assertTrue(result["installed"])
        self.assertEqual(result["runtime_refresh"]["status"], "restart_failed")
        self.assertEqual(result["runtime_refresh"]["code"], "startup_failed")

    def test_new_pid_with_wrong_runtime_version_is_not_reported_updated(self):
        old = self.running_service()
        result, _ = self.upgrade_with_service([
            old, old, {"status": "stopping"}, self.stopped_service(),
            {"status": "started"}, self.running_service("200:new", "1.0.0"),
        ])
        self.assertEqual(result["runtime_refresh"]["status"], "restart_failed")
        self.assertEqual(result["runtime_refresh"]["code"], "runtime_version_unverified")

    def test_service_command_rejects_ambiguous_results_and_bounds_subprocess(self):
        launcher = self.source / "scripts/codex-mem.py"
        data_dir = self.home / "memory"
        for response in (subprocess.CompletedProcess([], 0, "not json"),
                         subprocess.CompletedProcess([], 0, "[]"),
                         subprocess.CompletedProcess([], 2, '{"status":"running"}')):
            with self.subTest(response=response):
                with mock.patch.object(installer.subprocess, "run", return_value=response) as run:
                    result = installer._service_command(launcher, data_dir, "status", timeout=1.5)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(run.call_args.args[0],
                                 [sys.executable, str(launcher), "--data-dir", str(data_dir), "service", "status"])
                self.assertEqual(run.call_args.kwargs["timeout"], 1.5)

    def test_service_command_passes_owner_guard_to_packaged_stop(self):
        response = subprocess.CompletedProcess([], 2, '{"status":"blocked","code":"owner_changed"}')
        with mock.patch.object(installer.subprocess, "run", return_value=response) as run:
            result = installer._service_command(self.source / "scripts/codex-mem.py", self.home / "memory",
                                                "stop", expected_owner="100:old")
        self.assertEqual(result["code"], "owner_changed")
        self.assertEqual(run.call_args.args[0][-3:], ["stop", "--expected-owner", "100:old"])

    def test_cli_stop_dispatches_expected_owner(self):
        from codex_mem import cli

        with mock.patch("codex_mem.service.stop_service", return_value={"status": "stopping"}) as stop:
            with mock.patch("sys.stdout", new=io.StringIO()):
                code = cli.main(["--data-dir", str(self.home / "memory"), "service", "stop",
                                 "--expected-owner", "100:old"])
        self.assertEqual(code, 0)
        stop.assert_called_once_with(str(self.home / "memory"), expected_owner="100:old")


if __name__ == "__main__":
    unittest.main()
