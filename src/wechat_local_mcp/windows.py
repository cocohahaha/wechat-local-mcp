"""Read-only interaction with the local Windows WeChat window."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import logging
from pathlib import Path
import tempfile
import time
from typing import Iterator

from PIL import Image, ImageGrab, ImageStat
import win32api
import win32con
import win32gui
import win32process
import win32ui

from .models import ChatMessage, ChatSummary, OcrBlock, WindowInfo
from .parser import (
    deduplicate_messages,
    names_match,
    parse_recent_chats,
    parse_visible_messages,
)
from .windows_ocr import recognize_text


LOGGER = logging.getLogger(__name__)
PROCESS_NAMES = {"weixin.exe", "wechat.exe"}
OWNER_NAMES = {"微信", "WeChat", "Weixin"}
PW_RENDERFULLCONTENT = 0x00000002
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1


def _process_name(pid: int) -> str:
    handle = None
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
            False,
            pid,
        )
        return Path(win32process.GetModuleFileNameEx(handle, 0)).name
    except Exception:
        return ""
    finally:
        if handle is not None:
            win32api.CloseHandle(handle)


def _window_candidates() -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []

    def collect(hwnd: int, _: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        if width < 800 or height < 500:
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        title = win32gui.GetWindowText(hwnd).strip()
        process_name = _process_name(pid)
        if process_name.casefold() not in PROCESS_NAMES and title not in OWNER_NAMES:
            return
        candidates.append(
            {
                "hwnd": hwnd,
                "pid": pid,
                "title": title,
                "process_name": process_name,
                "x": left,
                "y": top,
                "width": width,
                "height": height,
            }
        )

    win32gui.EnumWindows(collect, None)
    return candidates


def find_main_window() -> WindowInfo:
    """Find the largest normal WeChat/Weixin chat window."""

    candidates = _window_candidates()
    if not candidates:
        raise RuntimeError(
            "找不到微信主窗口。请确认 Windows 微信已登录，并把主窗口从最小化状态恢复。"
        )
    titled = [item for item in candidates if item["title"] in OWNER_NAMES]
    selected = max(
        titled or candidates,
        key=lambda item: int(item["width"]) * int(item["height"]),
    )
    return WindowInfo(
        window_id=int(selected["hwnd"]),
        pid=int(selected["pid"]),
        title=str(selected["title"]),
        x=float(selected["x"]),
        y=float(selected["y"]),
        width=float(selected["width"]),
        height=float(selected["height"]),
    )


def activate_wechat(hwnd: int) -> None:
    """Restore and foreground WeChat without editing chat data."""

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as exc:
        LOGGER.debug("SetForegroundWindow failed: %s", exc)
    time.sleep(0.3)


def _capture_print_window(hwnd: int, width: int, height: int) -> Image.Image | None:
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    source_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    memory_dc = source_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source_dc, width, height)
        memory_dc.SelectObject(bitmap)
        rendered = ctypes.windll.user32.PrintWindow(
            hwnd,
            memory_dc.GetSafeHdc(),
            PW_RENDERFULLCONTENT,
        )
        if rendered != 1:
            return None
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            bits,
            "raw",
            "BGRX",
            0,
            1,
        ).copy()
        if ImageStat.Stat(image.convert("L")).stddev[0] < 2.0:
            return None
        return image
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        memory_dc.DeleteDC()
        source_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


@contextmanager
def screenshot(window: WindowInfo) -> Iterator[Path]:
    """Capture one WeChat window to a temporary image and delete it afterwards."""

    activate_wechat(window.window_id)
    left, top, right, bottom = win32gui.GetWindowRect(window.window_id)
    width, height = right - left, bottom - top
    image = _capture_print_window(window.window_id, width, height)
    if image is None:
        image = ImageGrab.grab(
            bbox=(left, top, right, bottom),
            all_screens=True,
        )
    with tempfile.TemporaryDirectory(prefix="wechat-local-mcp-") as directory:
        image_path = Path(directory) / "window.png"
        image.save(image_path, format="PNG")
        yield image_path


def capture_blocks() -> tuple[WindowInfo, list[OcrBlock]]:
    """Capture the active WeChat window and run fully local Windows OCR."""

    window = find_main_window()
    with screenshot(window) as image_path:
        blocks = recognize_text(image_path)
    return window, blocks


def _click_normalized(window: WindowInfo, x: float, y: float) -> None:
    screen_x = round(window.x + x * window.width)
    screen_y = round(window.y + (1.0 - y) * window.height)
    win32api.SetCursorPos((screen_x, screen_y))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, screen_x, screen_y, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, screen_x, screen_y, 0, 0)


def _press_key(vk: int, *, control: bool = False) -> None:
    if control:
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    if control:
        win32api.keybd_event(
            win32con.VK_CONTROL,
            0,
            win32con.KEYEVENTF_KEYUP,
            0,
        )


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyboardInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _InputUnion)]


def _type_unicode(text: str) -> None:
    encoded = text.encode("utf-16-le")
    for index in range(0, len(encoded), 2):
        code_unit = int.from_bytes(encoded[index:index + 2], "little")
        events = (_Input * 2)(
            _Input(
                type=INPUT_KEYBOARD,
                union=_InputUnion(
                    ki=_KeyboardInput(
                        wVk=0,
                        wScan=code_unit,
                        dwFlags=KEYEVENTF_UNICODE,
                        time=0,
                        dwExtraInfo=0,
                    )
                ),
            ),
            _Input(
                type=INPUT_KEYBOARD,
                union=_InputUnion(
                    ki=_KeyboardInput(
                        wVk=0,
                        wScan=code_unit,
                        dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                        time=0,
                        dwExtraInfo=0,
                    )
                ),
            ),
        )
        sent = ctypes.windll.user32.SendInput(
            len(events),
            ctypes.byref(events),
            ctypes.sizeof(_Input),
        )
        if sent != len(events):
            raise RuntimeError("Windows 无法向微信搜索框输入聊天名称。")


def _scroll(window: WindowInfo, delta: int) -> None:
    screen_x = round(window.x + 0.68 * window.width)
    screen_y = round(window.y + 0.52 * window.height)
    win32api.SetCursorPos((screen_x, screen_y))
    win32api.mouse_event(
        win32con.MOUSEEVENTF_WHEEL,
        screen_x,
        screen_y,
        delta * win32con.WHEEL_DELTA,
        0,
    )
    time.sleep(0.4)


def list_recent_chats(limit: int) -> list[ChatSummary]:
    """Return recent chats visible in the current sidebar."""

    _, blocks = capture_blocks()
    return parse_recent_chats(blocks)[:limit]


def _open_from_visible_sidebar(chat_name: str) -> bool:
    window, blocks = capture_blocks()
    for chat in parse_recent_chats(blocks):
        if names_match(chat.name, chat_name):
            _click_normalized(window, chat.click_x, chat.click_y)
            time.sleep(0.7)
            return True
    return False


def _open_via_search(chat_name: str) -> bool:
    window, _ = capture_blocks()
    _click_normalized(window, 0.18, 0.93)
    time.sleep(0.2)
    _press_key(ord("A"), control=True)
    _type_unicode(chat_name)
    time.sleep(1.0)

    result_window, blocks = capture_blocks()
    matches = [
        block
        for block in blocks
        if block.center_x < 0.36
        and 0.12 < block.center_y < 0.90
        and names_match(block.text, chat_name)
        and block.height >= 0.012
    ]
    if not matches:
        _press_key(win32con.VK_ESCAPE)
        return False

    match = max(matches, key=lambda block: block.center_y)
    _click_normalized(result_window, match.center_x, match.center_y)
    time.sleep(0.8)
    _press_key(win32con.VK_ESCAPE)
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
        _scroll(window, 8)
        window, blocks = capture_blocks()
        collected = parse_visible_messages(blocks, page=page) + collected

    messages = deduplicate_messages(collected)
    return messages[-limit:] if len(messages) > limit else messages


def _version_for_pid(pid: int) -> tuple[str | None, str | None]:
    handle = None
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
            False,
            pid,
        )
        executable = win32process.GetModuleFileNameEx(handle, 0)
        info = win32api.GetFileVersionInfo(executable, "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        version = ".".join(
            str(value)
            for value in (
                win32api.HIWORD(ms),
                win32api.LOWORD(ms),
                win32api.HIWORD(ls),
                win32api.LOWORD(ls),
            )
        )
        return version, executable
    except Exception:
        return None, None
    finally:
        if handle is not None:
            win32api.CloseHandle(handle)


def diagnose() -> dict[str, object]:
    """Check app, capture, and OCR readiness without returning chat text."""

    window, blocks = capture_blocks()
    chats = parse_recent_chats(blocks)
    messages = parse_visible_messages(blocks, page=0)
    version, executable = _version_for_pid(window.pid)
    return {
        "ready": bool(blocks),
        "platform": "windows",
        "wechat_pid": window.pid,
        "wechat_version": version,
        "wechat_executable": executable,
        "window_found": True,
        "ocr_engine": "Windows.Media.Ocr",
        "ocr_line_count": len(blocks),
        "recent_chats_detected": len(chats),
        "visible_messages_detected": len(messages),
        "privacy": {
            "network_used": False,
            "screenshots_persisted": False,
            "messages_modified": False,
        },
    }
