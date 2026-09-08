import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
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
        self.home = Path(self.temp.name) / "home"
        self.source = Path(self.temp.name) / "source"
        for name, body in {
            ".codex-plugin/plugin.json": json.dumps({"name": "codex-mem", "version": "1.0.0"}),
            ".mcp.json": "{}", "hooks/hooks.json": "{}", "scripts/codex-mem.py": "# test launcher\n",
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
        self.assertEqual((old_cache / "scripts/codex-mem.py").read_bytes(), old_launcher)
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
        self.assertEqual(self.package_bytes(cache_root / "1.0.0"), expected_v1_0)
        self.assertEqual(self.package_bytes(cache_root / "1.1.0"), expected_v1_1)
        self.assertEqual(self.package_bytes(cache_root / "1.2.0"), expected_v1_2)
        self.assertFalse((self.home / "plugins/cache").exists())
        retained = {receipt["version"]: receipt for receipt in result["retained_caches"]}
        self.assertEqual(set(retained), {"1.0.0", "1.1.0", "1.2.0"})
        self.assertTrue(all(receipt["status"] == "restored" for receipt in retained.values()))
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
        self.assertEqual(self.package_bytes(old_cache), expected_cache)

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


if __name__ == "__main__":
    unittest.main()
