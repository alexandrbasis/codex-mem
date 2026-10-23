# Install Pi project memory

The release archive contains one Pi package, its Python service, and the existing MCP server. Python 3.10 or later and Pi 1.4.0 are supported. Codex is optional. Install Pi separately through its official distribution. The installer never installs or upgrades either agent.

The installer selects one existing project. Its stable package directory is `<project>/.pi/codex-mem`. It adds that absolute directory to `.pi/settings.json` and one `codex-mem` connection to `.pi/mcp.json`. Pi owns extension loading and native MCP discovery. The extension registers no tools.

## Install and inspect

Extract the release archive. Run these commands from its `codex-mem` directory. Replace `/absolute/project` with the canonical project path. Replace `/absolute/memory` with the existing shared memory directory, when applicable. For a new installation, the default is `CODEX_MEM_HOME` or `~/.local/share/codex-mem`. Updates retain the recorded selection when `--data-dir` is omitted. The memory directory must remain outside `.pi/codex-mem`, including through symbolic links.

```sh
python3 scripts/install_pi.py install --project /absolute/project --data-dir /absolute/memory
python3 scripts/install_pi.py check --project /absolute/project --pi /absolute/path/to/pi
```

`install` works before Pi is installed. `check` requires the exact supported Pi version. It validates the recorded extension/MCP launch configuration and runs `doctor` through that recorded Python executable. Its `background` result reports executable availability separately for each effective processing profile. An unavailable runtime does not prevent package installation, context reads, capture, or service startup. This local check does not test authentication, model availability, or a live provider.

Review the installed files before granting Pi project trust. Start Pi in that project and accept its native trust prompt. Use `/mcp` to inspect the connection and tools. Run `pi mcp list` from an already trusted project to inspect native MCP outside a session. Use `/reload` after changing the installation. Keep built-in MCP enabled. See [memory tool permissions](PI_MCP.md) for explicit write controls.

The installer records the absolute Python executable used for installation and the selected memory directory. The extension and MCP use those paths independently of the working directory. If that Python installation moves, `check` reports the unavailable recorded executable. Repeat installation with the replacement interpreter. Moving the project also requires installation in its new canonical location.

The root `package.json` declares `./pi/extension.ts` and the supported Pi version. The release ZIP includes the complete Python payload and integrity manifest. The nested `pi/package.json` remains a private source-development entry. Use the project installer for the complete integration, including MCP.

## Select background execution

Installation preserves existing memory configuration. Without an explicit profile, the existing Codex background profile remains selected. For a Pi-only installation, explicitly select Pi before enabling processing. The interactive model does not select the background model.

Use the packaged CLI with the same memory directory:

```sh
python3 /absolute/project/.pi/codex-mem/scripts/codex-mem.py --data-dir /absolute/memory config --set 'processing_profile={"runtime":"pi","provider":"YOUR_PROVIDER","model":"EXACT_MODEL_ID","reasoning_effort":"none","timeout":240}'
```

Replace the provider and model placeholders with exact identifiers from Pi. Use native Pi `/login` for authentication. Keep credentials in Pi's native authorization system. The background adapter reads native `auth.json` and `models.json`; it does not copy credentials into memory configuration. See [Pi runtime and authorization](PI_RUNTIME.md) for exact reasoning support and provider limits. Existing per-processor overrides take precedence over this default. Remove an unwanted override with `config --set 'observation_profile=null'` or `config --set 'project_memory_profile=null'`.

The installer preserves personal Pi settings, credentials, subscriptions, and the global Pi profile. It does not start, stop, or reconfigure the shared service. To start the packaged service explicitly, use:

```sh
python3 /absolute/project/.pi/codex-mem/scripts/codex-mem.py --data-dir /absolute/memory service start
python3 /absolute/project/.pi/codex-mem/scripts/codex-mem.py --data-dir /absolute/memory service status
```

A missing Codex executable is reported as `codex_runtime_unavailable` only when that processor selects Codex. A selected Pi runtime reports its own availability. Actual processing still validates the exact model, provider, reasoning level, and completion evidence.

## Update or detach

Extract the new release and repeat `install` with the same project and memory directory. The installer stages a complete payload before replacing its managed directory. Existing memory configuration and database files stay outside that payload. Existing Pi settings, unrelated packages, unrelated MCP connections, and memory-tool exposure choices survive the update. Repeated installation retains one package declaration and one native MCP entry. Reload Pi after the update. A running shared worker is not restarted automatically.

An unmanaged existing `codex-mem` MCP entry produces a conflict error before replacement. Preserve that entry and resolve the ownership conflict before using the installer. Changed managed launch fields also produce a conflict. The installer does not overwrite them silently. Symlink installation paths and malformed configuration are rejected. Updates also reject an earlier memory directory inside the managed package, including a saved symbolic-link alias. Rejection preserves the existing files. Disablement still preserves that store. Relocate such memory outside the payload before attempting another update.

To disable or uninstall this project's integration, run either command:

```sh
python3 scripts/install_pi.py disable --project /absolute/project
python3 scripts/install_pi.py uninstall --project /absolute/project
```

Both commands remove this project's managed package declaration and MCP connection. Restart Pi to unload an already running extension. The managed payload and receipt remain available for sessions that already loaded them and for later reinstallation. Neither command deletes memory, stops the shared service, changes Codex registration, or detaches other projects. After stopping sessions, you can remove `.pi/codex-mem` and `.pi/codex-mem-install.json` to reclaim package files. Keep the shared memory directory.

Codex can be disabled through its normal plugin controls without affecting this separately installed Pi payload. A global Codex Mem disabled switch still applies to shared memory policy in both hosts. See [host capture policy](PI_HOST.md).

## Evidence and limits

Installed Pi documentation states: “A package is an ordinary directory or npm package.” It also states: “Paths are relative to the package root.” These support the root package manifest and absolute local-package declaration. Primary documentation: [Pi packages](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/packages.md), [project trust](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/security.md), and [MCP commands](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/cli.md#mcp-commands). Installed owning files and hashes appear in [installation evidence](PI_INSTALL_EVIDENCE.json). A Ref search found no matching versioned documentation. The installed package establishes compatibility here; web availability of the versioned URLs remains unverified.

Routine tests use public installer, CLI, MCP and real temporary stores. They cover clean installation, versioned payload updates, repeated installation, existing configuration, shared memory, independent detach and native credential preservation. Optional installed-Pi tests use project package discovery, native MCP and a disposable HTTP model provider. They also start the real packaged service with Codex absent from `PATH`. These checks do not establish live model quality or the full host/runtime processing matrix. Issues #13 and #14 own that evidence.

```sh
python3 -m unittest discover -s tests -p test_pi_install.py -v
CODEX_MEM_PI=/absolute/path/to/pi python3 -m unittest discover -s tests -p test_pi_install.py -v
```
