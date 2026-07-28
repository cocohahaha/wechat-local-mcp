"""Select the local WeChat UI backend for the current operating system."""

from __future__ import annotations

from importlib import import_module
import sys
from types import ModuleType

from .models import ChatMessage, ChatSummary


def backend_name() -> str:
    """Return the stable backend identifier used in MCP responses."""

    if sys.platform == "darwin":
        return "macos_vision_ocr"
    if sys.platform == "win32":
        return "windows_media_ocr"
    return "unsupported"


def install_hint() -> str:
    """Return a platform-specific dependency/setup hint."""

    if sys.platform == "darwin":
        return "运行 uv sync --extra ui，并在系统设置中允许屏幕与系统音频录制。"
    if sys.platform == "win32":
        return "在 Windows 10/11 上运行 uv sync --extra windows，并确认微信主窗口没有最小化。"
    return "UI 读取目前仅支持 macOS 和 Windows 10/11。"


def _backend() -> ModuleType:
    if sys.platform == "darwin":
        return import_module(".macos", __package__)
    if sys.platform == "win32":
        return import_module(".windows", __package__)
    raise RuntimeError(install_hint())


def diagnose() -> dict[str, object]:
    return _backend().diagnose()


def list_recent_chats(limit: int) -> list[ChatSummary]:
    return _backend().list_recent_chats(limit)


def read_chat(chat_name: str, *, limit: int, scroll_pages: int) -> list[ChatMessage]:
    return _backend().read_chat(chat_name, limit=limit, scroll_pages=scroll_pages)
