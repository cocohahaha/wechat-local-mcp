from __future__ import annotations

import json
from pathlib import Path


def test_project_mcp_config_uses_direct_portable_stdio_command() -> None:
    root = Path(__file__).parents[1]
    config = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["wechat-local-mcp"]

    assert server == {
        "type": "stdio",
        "command": "${CLAUDE_PROJECT_DIR}/.venv/bin/wechat-local-mcp",
        "args": ["--transport", "stdio"],
    }
    assert "uv" not in server["command"]
