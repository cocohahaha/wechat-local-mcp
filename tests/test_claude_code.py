from __future__ import annotations

from pathlib import Path

from wechat_local_mcp.setup import server_config


def test_project_mcp_config_uses_direct_portable_stdio_command() -> None:
    root = Path(__file__).parents[1]
    # Never ship a POSIX-only project config that shadows user-scope Windows config.
    assert not (root / ".mcp.json").exists()
    server = server_config()
    assert Path(server["command"]).is_absolute()
    assert server["type"] == "stdio"
    assert server["args"] == ["-m", "wechat_local_mcp.server", "--transport", "stdio"]
    assert "uv" not in server["command"]
