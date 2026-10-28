"""Agent-friendly bootstrap: validate stdio, emit portable config, opt-in registration.

Never reads chat messages, captures keys, decrypts databases, or downloads models.
"""
from __future__ import annotations

import argparse
import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Any

SERVER_NAME = "wechat-local-mcp"
PATH_ENV = ("WECHAT_CONTAINER_DIR", "WECHAT_DECRYPTED_DIR", "WECHAT_KEYS_FILE", "WECHAT_MEDIA_TEXT_DIR", "WECHAT_IMAGE_KEY_FILE")


def server_config(overrides: dict[str, str] | None = None) -> dict[str, Any]:
    # Do NOT resolve the Python symlink: doing so would leave the virtualenv.
    executable = os.path.abspath(sys.executable)
    environment = {
        name: str(Path(value).expanduser().absolute())
        for name in PATH_ENV if (value := os.environ.get(name))
    }
    environment.update(overrides or {})
    result: dict[str, Any] = {
        "type": "stdio", "command": executable,
        "args": ["-m", "wechat_local_mcp.server", "--transport", "stdio"],
    }
    if environment:
        result["env"] = environment
    return result


async def check_handshake(config: dict[str, Any], timeout: float = 20) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run() -> dict[str, Any]:
        parameters = StdioServerParameters(command=config["command"], args=config["args"], env=config.get("env"))
        # Check cwd independence and keep stderr/logs out of JSON stdout.
        with tempfile.TemporaryDirectory(prefix="wechat-mcp-check-") as directory, tempfile.TemporaryFile(mode="w+") as log:
            parameters.cwd = directory
            async with stdio_client(parameters, errlog=log) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialization = await session.initialize()
                    listing = await session.list_tools()
                    names = [item.name for item in listing.tools]
                    required = {"wechat_status", "wechat_sync", "wechat_read_chat"}
                    if not required.issubset(names):
                        raise RuntimeError("server is missing expected tools")
                    return {"connected": True, "protocol_version": initialization.protocolVersion,
                            "tool_count": len(names), "tested_from_unrelated_directory": True}

    try:
        return await asyncio.wait_for(run(), timeout=timeout)
    except TimeoutError:
        return {"connected": False, "error": "handshake_timeout", "next_step": "Run the configured Python with -m wechat_local_mcp.server --help."}
    except Exception as exc:
        return {"connected": False, "error": type(exc).__name__, "next_step": "Reinstall the base environment; check that the configured Python imports wechat_local_mcp.server."}


def _existing_registration(client: str) -> dict[str, Any] | None:
    """Read only the named entry; never echo the client's other config/secrets."""
    if client == "claude":
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        path = Path(config_dir) / ".claude.json" if config_dir else Path.home() / ".claude.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("mcpServers", {}).get(SERVER_NAME)
    path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    if not path.exists():
        return None
    return tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers", {}).get(SERVER_NAME)


def register_client(client: str, config: dict[str, Any]) -> dict[str, Any]:
    executable = shutil.which(client)
    if executable is None:
        return {"registered": False, "error": "client_not_found", "client": client}
    try:
        existing = _existing_registration(client)
    except (OSError, ValueError, TypeError, AttributeError):
        return {"registered": False, "error": "cannot_read_client_config", "next_step": "Inspect the client configuration; no changes were made."}
    if existing is not None:
        if not isinstance(existing, dict):
            return {"registered": False, "error": "invalid_existing_registration", "client": client,
                    "next_step": "The existing entry is not an object; inspect it manually. No changes were made."}
        if (existing.get("command") == config["command"] and existing.get("args", []) == config["args"]
                and existing.get("env", {}) == config.get("env", {})):
            return {"registered": True, "already_registered": True, "client": client}
        return {"registered": False, "error": "registration_exists", "client": client,
                "next_step": "An existing entry was preserved. Review it with the client's mcp get command; remove/update only that entry if you intend to replace it."}
    if client == "claude":
        arguments = [executable, "mcp", "add-json", "--scope", "user", SERVER_NAME, json.dumps(config)]
    else:
        arguments = [executable, "mcp", "add", SERVER_NAME]
        for name, value in config.get("env", {}).items():
            arguments.extend(["--env", f"{name}={value}"])
        arguments.extend(["--", config["command"], *config["args"]])
    try:
        # Never shell=True; repository paths may contain spaces or shell syntax.
        process = subprocess.run(arguments, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"registered": False, "error": type(exc).__name__, "client": client}
    # Do not echo CLI output which may include unrelated config or credentials.
    return {"registered": process.returncode == 0, "client": client, "exit_code": process.returncode,
            "next_step": "Restart the client and inspect its MCP list." if process.returncode == 0 else "Client registration failed; use the generated mcpServers config or inspect client mcp help."}


def bootstrap(client: str, register: bool, overrides: dict[str, str] | None = None) -> dict[str, Any]:
    from .config import status

    config = server_config(overrides)
    handshake = asyncio.run(check_handshake(config))
    previous = {key: os.environ.get(key) for key in config.get("env", {})}
    try:
        os.environ.update(config.get("env", {}))
        inventory = status()
        history = {"snapshot_available": inventory["ready"], "keys_file_exists": inventory["keys_available"],
                   "account_found": bool(inventory["account_dir"])}
    except (OSError, ValueError) as exc:
        history = {"snapshot_available": False, "error": type(exc).__name__}
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    result: dict[str, Any] = {
        "version": version("wechat-local-mcp"), "python": config["command"], "platform": sys.platform,
        "mcp": handshake, "history": history, "mcpServers": {SERVER_NAME: config},
        "next_steps": [
            "MCP connected and history available are separate checks; no chat data was read by this bootstrap.",
            "If history is unavailable: supply your own keys/snapshot, or install the platform UI extra for visible-window OCR. See INSTALL.md.",
            "With keys ready, fully quit WeChat, run wechat-local-sync sync --dry-run, then sync. Default chat profile skips media/index databases.",
        ],
    }
    if register:
        result["registration"] = register_client(client, config) if handshake["connected"] else {"registered": False, "error": "handshake_failed"}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("bootstrap", help="check MCP stdio and generate portable JSON configuration")
    command.add_argument("--client", choices=("claude", "codex", "config", "none"), default="config")
    command.add_argument("--register", action="store_true", help="explicitly register with the chosen local client")
    command.add_argument("--container-dir", type=Path)
    command.add_argument("--decrypted-dir", type=Path)
    command.add_argument("--keys-file", type=Path)
    args = parser.parse_args()
    if args.register and args.client not in ("claude", "codex"):
        parser.error("--register requires --client claude or codex")
    overrides = {name: str(value.expanduser().absolute()) for name, value in (
        ("WECHAT_CONTAINER_DIR", args.container_dir), ("WECHAT_DECRYPTED_DIR", args.decrypted_dir),
        ("WECHAT_KEYS_FILE", args.keys_file),
    ) if value is not None}
    result = bootstrap(args.client, args.register, overrides)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["mcp"]["connected"] or (args.register and not result["registration"]["registered"]):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
