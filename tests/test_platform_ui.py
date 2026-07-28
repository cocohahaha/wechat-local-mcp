from __future__ import annotations

import sys

from wechat_local_mcp import platform_ui


def test_backend_name_matches_current_platform() -> None:
    expected = {
        "darwin": "macos_vision_ocr",
        "win32": "windows_media_ocr",
    }.get(sys.platform, "unsupported")

    assert platform_ui.backend_name() == expected


def test_install_hint_is_actionable() -> None:
    hint = platform_ui.install_hint()

    assert "uv sync" in hint or "仅支持" in hint
