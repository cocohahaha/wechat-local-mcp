from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys

from wechat_local_mcp import setup


def test_config_keeps_venv_python_and_explicit_paths(monkeypatch, tmp_path):
    python = tmp_path / "path with spaces/.venv/bin/python"
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setenv("WECHAT_KEYS_FILE", str(tmp_path / "private keys.json"))
    config = setup.server_config()
    assert config["command"] == str(python)
    assert config["env"]["WECHAT_KEYS_FILE"] == str(tmp_path / "private keys.json")
    assert "uv" not in config["args"]


def test_real_stdio_handshake_without_chat_access():
    result = asyncio.run(setup.check_handshake(setup.server_config()))
    assert result["connected"] is True
    assert result["tool_count"] >= 13
    assert result["tested_from_unrelated_directory"] is True


def test_registration_uses_argv_and_does_not_remove_existing(monkeypatch):
    config = setup.server_config()
    monkeypatch.setattr(setup.shutil, "which", lambda _: "/client with spaces/claude")
    monkeypatch.setattr(setup, "_existing_registration", lambda _: {"command": "old", "args": []})
    monkeypatch.setattr(setup.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must preserve existing config")))
    assert setup.register_client("claude", config)["error"] == "registration_exists"
    monkeypatch.setattr(setup, "_existing_registration", lambda _: config)
    assert setup.register_client("claude", config)["already_registered"]
    monkeypatch.setattr(setup, "_existing_registration", lambda _: False)
    assert setup.register_client("claude", config)["error"] == "invalid_existing_registration"


def test_new_claude_registration(monkeypatch, tmp_path):
    config = setup.server_config({"WECHAT_DECRYPTED_DIR": str(tmp_path / "a b")})
    calls = []
    monkeypatch.setattr(setup.shutil, "which", lambda _: "/bin/claude")
    monkeypatch.setattr(setup, "_existing_registration", lambda _: None)

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup.register_client("claude", config)["registered"]
    args, kwargs = calls[0]
    assert args[1:5] == ["mcp", "add-json", "--scope", "user"]
    assert json.loads(args[-1]) == config
    assert not kwargs.get("shell")


def test_new_codex_registration(monkeypatch):
    config = setup.server_config({"WECHAT_KEYS_FILE": "/path with spaces/keys.json"})
    monkeypatch.setattr(setup.shutil, "which", lambda _: "/bin/codex")
    monkeypatch.setattr(setup, "_existing_registration", lambda _: None)

    def run(args, **kwargs):
        assert "WECHAT_KEYS_FILE=/path with spaces/keys.json" in args
        assert args[args.index("--") + 1:] == [config["command"], *config["args"]]
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup.register_client("codex", config)["registered"]


def test_module_bootstrap_json_and_no_implicit_registration(tmp_path):
    result = subprocess.run([sys.executable, "-m", "wechat_local_mcp.setup", "bootstrap", "--client", "claude", "--container-dir", str(tmp_path)], capture_output=True, text=True, check=True, timeout=30)
    data = json.loads(result.stdout)
    assert data["mcp"]["connected"] is True
    assert "registration" not in data
    assert data["history"]["account_found"] is False


def test_salt_scan_reads_only_sixteen_bytes(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("capture_keys", Path(__file__).parents[1] / "scripts/capture_keys.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "test.db").write_bytes(b"0123456789abcdef" + b"x" * 10000)
    monkeypatch.setattr(Path, "read_bytes", lambda _: (_ for _ in ()).throw(AssertionError("full file read")))
    assert module.salt_map(tmp_path) == {b"0123456789abcdef".hex(): "test.db"}
