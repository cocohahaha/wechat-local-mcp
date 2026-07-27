"""Read-only interaction with the local macOS WeChat window."""

from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Iterator

import AppKit
import Quartz

from .models import ChatMessage, ChatSummary, OcrBlock, WindowInfo
from .ocr import recognize_text
from .parser import (
    deduplicate_messages,
    names_match,
    parse_recent_chats,
    parse_visible_messages,
)


LOGGER = logging.getLogger(__name__)
WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"
OWNER_NAMES = {"微信", "WeChat"}


def _window_area(window: dict[object, object]) -> float:
    bounds = window.get(Quartz.kCGWindowBounds) or {}
    return float(bounds.get("Width", 0)) * float(bounds.get("Height", 0))


def find_main_window() -> WindowInfo:
    """Find the largest normal WeChat chat window."""

    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll,
        Quartz.kCGNullWindowID,
    )
    candidates: list[dict[object, object]] = []
    for window in windows:
        owner = str(window.get(Quartz.kCGWindowOwnerName) or "")
        bounds = window.get(Quartz.kCGWindowBounds) or {}
        if owner not in OWNER_NAMES:
            continue
        if int(window.get(Quartz.kCGWindowLayer) or 0) != 0:
            continue
        if float(bounds.get("Width", 0)) < 800 or float(bounds.get("Height", 0)) < 500:
            continue
        candidates.append(window)

    if not candidates:
        raise RuntimeError(
            "找不到微信主窗口。请确认微信已登录，并把主窗口从最小化状态恢复。"
        )

    titled = [
        window
        for window in candidates
        if str(window.get(Quartz.kCGWindowName) or "") in OWNER_NAMES
    ]
    selected = max(titled or candidates, key=_window_area)
    bounds = selected.get(Quartz.kCGWindowBounds) or {}
    return WindowInfo(
        window_id=int(selected[Quartz.kCGWindowNumber]),
        pid=int(selected[Quartz.kCGWindowOwnerPID]),
        title=str(selected.get(Quartz.kCGWindowName) or ""),
        x=float(bounds.get("X", 0)),
        y=float(bounds.get("Y", 0)),
        width=float(bounds.get("Width", 0)),
        height=float(bounds.get("Height", 0)),
    )


def activate_wechat(pid: int) -> None:
    """Bring WeChat to the foreground without editing chat data."""

    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        raise RuntimeError("微信进程已退出，请重新打开微信。")
    app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    time.sleep(0.25)


@contextmanager
def screenshot(window: WindowInfo) -> Iterator[Path]:
    """Capture one WeChat window to a temporary image and delete it afterwards."""

    with tempfile.TemporaryDirectory(prefix="wechat-local-mcp-") as directory:
        image_path = Path(directory) / "window.png"
        result = subprocess.run(
            [
                "/usr/sbin/screencapture",
                "-x",
                "-l",
                str(window.window_id),
                str(image_path),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 or not image_path.is_file():
            detail = result.stderr.strip() or "no image was produced"
            raise RuntimeError(
                "无法截取微信窗口。请在“系统设置 → 隐私与安全性 → 屏幕与系统音频录制”"
                f"中允许 Codex，然后重试。详情：{detail}"
            )
        yield image_path


def capture_blocks() -> tuple[WindowInfo, list[OcrBlock]]:
    """Capture the active WeChat window and run fully local OCR."""

    window = find_main_window()
    activate_wechat(window.pid)
    window = find_main_window()
    with screenshot(window) as image_path:
        blocks = recognize_text(image_path)
    return window, blocks


def _post_mouse_click(x: float, y: float) -> None:
    point = Quartz.CGPoint(x, y)
    down = Quartz.CGEventCreateMouseEvent(
        None,
        Quartz.kCGEventLeftMouseDown,
        point,
        Quartz.kCGMouseButtonLeft,
    )
    up = Quartz.CGEventCreateMouseEvent(
        None,
        Quartz.kCGEventLeftMouseUp,
        point,
        Quartz.kCGMouseButtonLeft,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def _click_normalized(window: WindowInfo, x: float, y: float) -> None:
    screen_x = window.x + x * window.width
    screen_y = window.y + (1.0 - y) * window.height
    _post_mouse_click(screen_x, screen_y)


def _press_key(keycode: int, flags: int = 0) -> None:
    down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
    up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
    Quartz.CGEventSetFlags(down, flags)
    Quartz.CGEventSetFlags(up, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def _type_unicode(text: str) -> None:
    event = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
    Quartz.CGEventKeyboardSetUnicodeString(event, len(text), text)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
    Quartz.CGEventKeyboardSetUnicodeString(up, len(text), text)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def _scroll(window: WindowInfo, delta: int, *, sidebar: bool = False) -> None:
    normalized_x = 0.20 if sidebar else 0.68
    normalized_y = 0.48
    point = Quartz.CGPoint(
        window.x + normalized_x * window.width,
        window.y + (1.0 - normalized_y) * window.height,
    )
    move = Quartz.CGEventCreateMouseEvent(
        None,
        Quartz.kCGEventMouseMoved,
        point,
        Quartz.kCGMouseButtonLeft,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    event = Quartz.CGEventCreateScrollWheelEvent(
        None,
        Quartz.kCGScrollEventUnitLine,
        1,
        delta,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    time.sleep(0.35)


def list_recent_chats(limit: int) -> list[ChatSummary]:
    """Return recent chats visible in the current sidebar."""

    _, blocks = capture_blocks()
    return parse_recent_chats(blocks)[:limit]


def _open_from_visible_sidebar(chat_name: str) -> bool:
    window, blocks = capture_blocks()
    for chat in parse_recent_chats(blocks):
        if names_match(chat.name, chat_name):
            _click_normalized(window, chat.click_x, chat.click_y)
            time.sleep(0.65)
            return True
    return False


def _open_via_search(chat_name: str) -> bool:
    window, _ = capture_blocks()
    _click_normalized(window, 0.18, 0.93)
    time.sleep(0.15)
    _press_key(0, Quartz.kCGEventFlagMaskCommand)
    _type_unicode(chat_name)
    time.sleep(0.9)

    result_window, blocks = capture_blocks()
    matches = [
        block
        for block in blocks
        if block.center_x < 0.34
        and 0.12 < block.center_y < 0.90
        and names_match(block.text, chat_name)
        and block.height >= 0.014
    ]
    if not matches:
        _press_key(53)
        return False

    match = max(matches, key=lambda block: block.center_y)
    _click_normalized(result_window, match.center_x, match.center_y)
    time.sleep(0.7)
    _press_key(53)
    return True


def open_chat(chat_name: str) -> None:
    """Open an exact/fuzzy matching chat without sending any message."""

    if _open_from_visible_sidebar(chat_name):
        return
    if _open_via_search(chat_name):
        return
    raise RuntimeError(
        f"未找到聊天“{chat_name}”。请先在微信里确认名称，或调用 "
        "wechat_list_recent_chats 查看当前可识别的会话名。"
    )


def read_chat(
    chat_name: str,
    *,
    limit: int,
    scroll_pages: int,
) -> list[ChatMessage]:
    """Open a chat and OCR recent visible messages, scrolling to older pages."""

    open_chat(chat_name)
    collected: list[ChatMessage] = []
    window, blocks = capture_blocks()
    collected.extend(parse_visible_messages(blocks, page=0))

    for page in range(1, scroll_pages + 1):
        if len(deduplicate_messages(collected)) >= limit:
            break
        _scroll(window, 32)
        window, blocks = capture_blocks()
        collected = parse_visible_messages(blocks, page=page) + collected

    messages = deduplicate_messages(collected)
    if len(messages) > limit:
        messages = messages[-limit:]
    return messages


def diagnose() -> dict[str, object]:
    """Check app, screen-capture, and OCR readiness without returning chat text."""

    window, blocks = capture_blocks()
    chats = parse_recent_chats(blocks)
    messages = parse_visible_messages(blocks, page=0)
    return {
        "ready": bool(blocks),
        "wechat_pid": window.pid,
        "window_found": True,
        "ocr_line_count": len(blocks),
        "recent_chats_detected": len(chats),
        "visible_messages_detected": len(messages),
        "privacy": {
            "network_used": False,
            "screenshots_persisted": False,
            "messages_modified": False,
        },
    }
