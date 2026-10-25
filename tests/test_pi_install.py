"""Public project installer and packaged Python boundaries; real disposable state."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

class PiInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.project = self.home / "project with spaces"
        self.project.mkdir()
        self.memory = self.home / "memory"
        self.env = {**os.environ, "HOME": str(self.home), "CODEX_MEM_HOME": str(self.memory), "PATH": "/usr/bin:/bin"}

    def install(self, action="install", ok=True, script=None, data_dir=None, interpreter=None):
        result = subprocess.run([str(interpreter or sys.executable), str(script or ROOT / "scripts/install_pi.py"), action,
                                 "--project", str(self.project), *(["--data-dir", str(data_dir)] if data_dir else [])], env=self.env, capture_output=True, text=True)
        self.assertEqual(0 if ok else 2, result.returncode, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(self.project / ".pi/codex-mem/scripts/codex-mem.py"),
                                 *args], env=self.env, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_clean_install_has_stable_loadable_package_and_python_without_codex(self):
        installed = self.install()
        package = Path(installed["package"])
        metadata = json.loads((package / "package.json").read_text())
        self.assertEqual(["./pi/extension.ts"], metadata["pi"]["extensions"])
        self.assertTrue((package / "pi/extension.ts").is_file())
        self.assertEqual((ROOT / "codex_mem/pi_schema_helper.js").read_bytes(),
                         (package / "codex_mem/pi_schema_helper.js").read_bytes())
        self.assertTrue((package / "codex_mem/pi_schema.py").is_file())
        self.assertEqual("1.4.0", metadata["codexMem"]["supportedPi"])
        self.assertTrue(self.cli("doctor")["ready"])
        server = json.loads((self.project / ".pi/mcp.json").read_text())["mcpServers"]["codex-mem"]
        request = '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
        response = subprocess.run([server["command"], *server["args"]], input=request,
                                  env={**self.env, **server["env"]}, capture_output=True, text=True)
        self.assertEqual(0, response.returncode, response.stderr)
        tools = json.loads(response.stdout.splitlines()[-1])["result"]["tools"]
        self.assertEqual(11, len(tools))
        self.assertEqual(11, len({tool["name"] for tool in tools}))

    def test_upgrade_and_repeated_install_keep_configuration_database_and_single_tools(self):
        first = self.install()
        self.cli("config", "--no-service-enabled", "--context-chars", "4321")
        saved = self.cli("remember", "--project", str(self.project), "--title", "Upgrade evidence",
                         "--body", "The project keeps its memory.")
        config_before = self.cli("config")
        settings_path = self.project / ".pi/settings.json"
        settings = json.loads(settings_path.read_text())
        settings["defaultProvider"] = "untouched-provider"
        settings["packages"].append("/unrelated/package")
        settings_path.write_text(json.dumps(settings))
        mcp_path = self.project / ".pi/mcp.json"
        mcp = json.loads(mcp_path.read_text())
        mcp["mcpServers"]["other"] = {"command": "unrelated"}
        mcp["mcpServers"]["codex-mem"]["toolExposure"]["memory_remember"] = "direct"
        mcp_path.write_text(json.dumps(mcp))
        release = self.home / "release"
        shutil.copytree(first["package"], release)
        metadata = json.loads((release / "package.json").read_text())
        metadata["version"] = "1.12.1"
        (release / "package.json").write_text(json.dumps(metadata))
        self.install(script=release / "scripts/install_pi.py")
        self.install(script=release / "scripts/install_pi.py")
        self.assertEqual(config_before, self.cli("config"))
        self.assertIn("Upgrade evidence", json.dumps(self.cli("search", "--project", str(self.project), "--query", "Upgrade")))
        current = json.loads(settings_path.read_text())
        self.assertEqual("untouched-provider", current["defaultProvider"])
        self.assertEqual(1, current["packages"].count(first["package"]))
        current_mcp = json.loads(mcp_path.read_text())["mcpServers"]
        self.assertEqual({"codex-mem", "other"}, set(current_mcp))
        self.assertEqual("direct", current_mcp["codex-mem"]["toolExposure"]["memory_remember"])
        self.assertEqual("1.12.1", json.loads((Path(first["package"]) / "package.json").read_text())["version"])

    def test_disable_and_uninstall_preserve_shared_memory_and_other_host(self):
        self.install()
        codex = self.home / ".codex/config.toml"
        codex.parent.mkdir()
        codex.write_text('model = "existing-model"\n')
        self.cli("config", "--no-service-enabled")
        self.cli("remember", "--project", str(self.project), "--title", "Shared evidence",
                 "--body", "Codex and Pi read the same project.")
        before = {str(p): p.read_bytes() for p in self.memory.iterdir() if p.is_file()}
        for action in ("disable", "uninstall", "disable"):
            self.assertEqual("disabled", self.install(action)["status"])
            self.assertEqual([], json.loads((self.project / ".pi/settings.json").read_text())["packages"])
            self.assertEqual({}, json.loads((self.project / ".pi/mcp.json").read_text())["mcpServers"])
            self.assertEqual(before, {str(p): p.read_bytes() for p in self.memory.iterdir() if p.is_file()})
            response = subprocess.run([sys.executable, str(ROOT / "scripts/codex-mem.py"), "search",
                                       "--project", str(self.project), "--query", "Shared"], env=self.env,
                                      capture_output=True, text=True)
            self.assertEqual(0, response.returncode, response.stderr)
            self.assertIn("Shared evidence", response.stdout)
            self.assertEqual('model = "existing-model"\n', codex.read_text())

    def test_install_rejects_existing_mcp_entry_without_changes(self):
        directory = self.project / ".pi"
        directory.mkdir()
        path = directory / "mcp.json"
        original = '{"mcpServers":{"codex-mem":{"command":"existing"}}}'
        path.write_text(original)
        self.assertIn("not managed", self.install(ok=False)["detail"])
        self.assertEqual(original, path.read_text())
        self.assertFalse((directory / "codex-mem").exists())

    def test_install_preserves_native_credentials_and_personal_profile(self):
        native = self.home / ".pi/agent"
        native.mkdir(parents=True)
        for name in ("settings.json", "auth.json", "models.json"):
            (native / name).write_text('{"fixture":"do not change"}')
        before = {p.name: p.read_bytes() for p in native.iterdir()}
        self.install()
        self.install("uninstall")
        self.assertEqual(before, {p.name: p.read_bytes() for p in native.iterdir()})

    def test_check_separates_missing_pi_and_incompatible_version(self):
        for executable, expected in ((str(self.home / "missing-pi"), "Pi is unavailable"),
                                     (sys.executable, "Unsupported Pi version")):
            response = subprocess.run([sys.executable, str(ROOT / "scripts/install_pi.py"), "check",
                                       "--project", str(self.project), "--pi", executable],
                                      env=self.env, capture_output=True, text=True)
            self.assertEqual(2, response.returncode)
            self.assertIn(expected, json.loads(response.stdout)["detail"])

    @unittest.skipUnless(os.environ.get("CODEX_MEM_PI"), "set CODEX_MEM_PI for installed package compatibility")
    def test_installed_pi_checks_package_and_missing_codex_separately(self):
        self.install()
        response = subprocess.run([sys.executable, str(ROOT / "scripts/install_pi.py"), "check",
                                   "--project", str(self.project), "--pi", os.environ["CODEX_MEM_PI"]],
                                  env={**self.env, "PATH": os.environ["PATH"],
                                       "PI_CODING_AGENT_DIR": str(self.home / "agent")},
                                  capture_output=True, text=True)
        self.assertEqual(0, response.returncode, response.stdout + response.stderr)
        check = json.loads(response.stdout)
        self.assertTrue(check["python_ready"])
        self.assertEqual("codex", check["background"]["observation"]["runtime"])

    @unittest.skipUnless(os.environ.get("CODEX_MEM_PI"), "set CODEX_MEM_PI for installed package loading")
    def test_installed_package_load_and_service_start_without_codex(self):
        from test_pi_host import InstalledPiHostTests
        host = InstalledPiHostTests(methodName="test_installed_private_turn_never_creates_sources")
        host.setUp()
        self.addCleanup(host.doCleanups)
        binary = host.root / "bin"
        binary.mkdir()
        (binary / "node").symlink_to(shutil.which("node"))
        (binary / "pi").symlink_to(os.environ["CODEX_MEM_PI"])
        path = str(binary) + ":/usr/bin:/bin"
        self.assertIsNone(shutil.which("codex", path=path))
        env = {"HOME": str(host.root), "PI_CODING_AGENT_DIR": str(host.agent),
               "PATH": path, "CODEX_MEM_HOME": str(host.data)}
        installed = subprocess.run([sys.executable, str(ROOT / "scripts/install_pi.py"), "install",
                                   "--project", str(host.project)], env=env, capture_output=True, text=True)
        self.assertEqual(0, installed.returncode, installed.stdout + installed.stderr)
        package = Path(json.loads(installed.stdout)["package"])
        # Explicitly loading the installed package tests native discovery without persistent trust changes.
        host.start(installed_project=True, execution_path=path)
        host.prompt("Keep local project evidence.")
        self.assertEqual(2, len(host.captured_sources()))
        host.stop()
        declarations = host.requests[0]["tools"]
        tools = [tool["function"]["name"] for tool in declarations if tool["function"]["name"].startswith("mcp__codex_mem__")]
        self.assertEqual(7, len(tools))
        self.assertEqual(7, len(set(tools)))
        launcher = package / "scripts/codex-mem.py"
        def cli(*arguments):
            result = subprocess.run([sys.executable, str(launcher), *arguments], env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            return json.loads(result.stdout)
        # Processing stays disabled in this startup check. #13 owns complete Pi processing.
        cli("config", "--service-enabled", "--no-processor-enabled")
        started = cli("service", "start")
        self.assertIn(started["status"], {"started", "running"})
        self.addCleanup(lambda: cli("service", "stop"))
        self.assertTrue(cli("service", "status")["running"])
        cli("service", "stop")
        deadline = time.monotonic() + 8
        while cli("service", "status")["running"] and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertFalse(cli("service", "status")["running"])
        checked = subprocess.run([sys.executable, str(ROOT / "scripts/install_pi.py"), "check",
                                  "--project", str(host.project), "--pi", os.environ["CODEX_MEM_PI"]],
                                 env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        background = json.loads(checked.stdout)["background"]
        self.assertEqual("codex_runtime_unavailable", background["observation"]["code"])
        cli("config", "--set", 'processing_profile={"runtime":"pi","provider":"fixture","model":"fixture","reasoning_effort":"none","timeout":30}')
        checked = subprocess.run([sys.executable, str(ROOT / "scripts/install_pi.py"), "check",
                                  "--project", str(host.project), "--pi", os.environ["CODEX_MEM_PI"]],
                                 env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        self.assertEqual({"pi"}, {p["runtime"] for p in json.loads(checked.stdout)["background"].values()})
        self.assertTrue(all(p["available"] for p in json.loads(checked.stdout)["background"].values()))

    def test_invalid_upgrade_payload_keeps_previous_package_and_configuration(self):
        first = self.install()
        package = Path(first["package"])
        release = self.home / "broken-release"
        shutil.copytree(package, release)
        (release / "docs/pi-mcp.example.json").write_text("not json")
        before = {str(p.relative_to(package)): p.read_bytes() for p in package.rglob("*") if p.is_file()}
        configuration = {p.name: p.read_bytes() for p in (self.project / ".pi").iterdir() if p.is_file()}
        # Exercise a fresh connection reconstruction during an update.
        mcp = self.project / ".pi/mcp.json"
        mcp.write_text('{"mcpServers":{}}')
        configuration["mcp.json"] = mcp.read_bytes()
        self.install(ok=False, script=release / "scripts/install_pi.py")
        self.assertEqual(before, {str(p.relative_to(package)): p.read_bytes() for p in package.rglob("*") if p.is_file()})
        self.assertEqual(configuration, {p.name: p.read_bytes() for p in (self.project / ".pi").iterdir() if p.is_file()})

    def test_fresh_install_rejects_memory_inside_payload_and_resolved_aliases(self):
        target = self.project / ".pi/codex-mem"
        alias = self.home / "memory-alias"
        alias.symlink_to(target / "memory", target_is_directory=True)
        for memory in (target, target / "memory", target / "sub/../memory", alias):
            with self.subTest(memory=memory):
                self.env["CODEX_MEM_HOME"] = str(memory)
                rejected = self.install(ok=False, data_dir=memory)
                self.assertIn("outside the managed Pi package", rejected["detail"])
                self.assertFalse((self.project / ".pi").exists())

    def legacy_nested_memory(self, *, alias=False):
        package = Path(self.install()["package"])
        memory = package / "memory"
        selected = memory
        if alias:
            selected = self.home / "selected-memory"
            selected.symlink_to(memory, target_is_directory=True)
        self.env["CODEX_MEM_HOME"] = str(selected)
        # Fixture for a previously accepted layout, expressed in its public JSON files.
        runtime_path = package / "pi/runtime.json"
        runtime = json.loads(runtime_path.read_text())
        runtime["data_dir"] = str(selected)
        runtime_path.write_text(json.dumps(runtime))
        receipt_path = self.project / ".pi/codex-mem-install.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["data_dir"] = str(selected)
        receipt["server"]["env"]["CODEX_MEM_HOME"] = str(selected)
        receipt_path.write_text(json.dumps(receipt))
        mcp_path = self.project / ".pi/mcp.json"
        mcp = json.loads(mcp_path.read_text())
        mcp["mcpServers"]["codex-mem"] = receipt["server"]
        mcp_path.write_text(json.dumps(mcp))
        self.cli("config", "--no-service-enabled")
        self.cli("remember", "--project", str(self.project), "--title", "Legacy nested memory",
                 "--body", "Retain this source when rejecting an unsafe upgrade.")
        return package, selected

    def test_update_rejects_legacy_selected_memory_even_when_override_is_omitted(self):
        package, selected = self.legacy_nested_memory(alias=True)
        before = {str(p.relative_to(self.project)): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        # An omitted override must use the saved selection, not the current environment default.
        self.env["CODEX_MEM_HOME"] = str(self.home / "another-memory")
        rejected = self.install(ok=False)
        self.assertIn("outside the managed Pi package", rejected["detail"])
        self.assertEqual(before, {str(p.relative_to(self.project)): p.read_bytes() for p in self.project.rglob("*") if p.is_file()})
        self.env["CODEX_MEM_HOME"] = str(selected)
        self.assertIn("Legacy nested memory", json.dumps(self.cli("search", "--project", str(self.project), "--query", "Legacy")))

    def test_detach_preserves_legacy_nested_store_and_its_alias(self):
        package, selected = self.legacy_nested_memory(alias=True)
        before = {p.name: p.read_bytes() for p in selected.iterdir() if p.is_file()}
        self.env["CODEX_MEM_HOME"] = str(self.home / "another-memory")
        for action in ("disable", "uninstall"):
            self.assertEqual("disabled", self.install(action)["status"])
            self.assertEqual(before, {p.name: p.read_bytes() for p in selected.iterdir() if p.is_file()})
        self.env["CODEX_MEM_HOME"] = str(selected)
        self.assertIn("Legacy nested memory", json.dumps(self.cli("search", "--project", str(self.project), "--query", "Legacy")))

    @unittest.skipUnless(os.environ.get("CODEX_MEM_PI"), "set CODEX_MEM_PI for recorded interpreter check")
    def test_check_reports_missing_recorded_python_and_reinstall_repairs_it(self):
        recorded = self.home / "installed-python"
        recorded.symlink_to(sys.executable)
        package = Path(self.install(interpreter=recorded)["package"])
        self.assertEqual(str(recorded), json.loads((package / "pi/runtime.json").read_text())["python"])
        self.cli("config", "--no-service-enabled")
        self.cli("remember", "--project", str(self.project), "--title", "Interpreter repair",
                 "--body", "Keep memory across replacement of the Python interpreter.")
        recorded.unlink()
        def check():
            return subprocess.run([sys.executable, str(ROOT / "scripts/install_pi.py"), "check",
                "--project", str(self.project), "--pi", os.environ["CODEX_MEM_PI"]],
                env={**self.env, "PATH": os.environ["PATH"], "PI_CODING_AGENT_DIR": str(self.home / "agent")},
                capture_output=True, text=True, timeout=20)
        rejected = check()
        self.assertEqual(2, rejected.returncode, rejected.stdout + rejected.stderr)
        self.assertIn("Recorded Python executable is unavailable", json.loads(rejected.stdout)["detail"])
        self.install()
        repaired = check()
        self.assertEqual(0, repaired.returncode, repaired.stdout + repaired.stderr)
        self.assertTrue(json.loads(repaired.stdout)["python_ready"])
        self.assertIn("Interpreter repair", json.dumps(self.cli("search", "--project", str(self.project), "--query", "Interpreter")))

    def test_update_retains_saved_memory_when_environment_default_changes(self):
        self.install()
        self.cli("config", "--no-service-enabled")
        self.cli("remember", "--project", str(self.project), "--title", "Saved memory directory",
                 "--body", "Keep the installed selection across updates.")
        self.env["CODEX_MEM_HOME"] = str(self.home / "different-default")
        self.install()
        receipt = json.loads((self.project / ".pi/codex-mem-install.json").read_text())
        self.assertEqual(str(self.memory), receipt["data_dir"])
        self.assertFalse((self.home / "different-default").exists())
        self.env["CODEX_MEM_HOME"] = str(self.memory)
        self.assertIn("Saved memory directory", json.dumps(self.cli("search", "--project", str(self.project), "--query", "Saved")))

if __name__ == "__main__":
    unittest.main()
