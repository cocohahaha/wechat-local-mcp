"""Turn Vision OCR blocks into chats, messages, and stable match keys."""

from __future__ import annotations

import re
import unicodedata

from .models import ChatMessage, ChatSummary, OcrBlock


TIME_LABEL = re.compile(
    r"^(?:\d{1,2}:\d{2}|"
    r"(?:昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天])(?:\s*\d{1,2}:\d{2})?|"
    r"\d{1,2}月\d{1,2}日|\d{4}[/-]\d{1,2}[/-]\d{1,2})$"
)
EDGE_NOISE = re.compile(r"^[\s>|｜:：·•~～^`'\"“”‘’]+|[\s|｜]+$")
CONTROL_TEXT = {
    "搜索",
    "折叠置顶聊天",
    "消息",
    "通讯录",
    "朋友圈",
}


def normalize_text(text: str) -> str:
    """Normalize OCR spacing and compatibility characters."""

    value = unicodedata.normalize("NFKC", text)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def clean_chat_name(text: str) -> str:
    """Remove common OCR edge noise without destroying interior emoji."""

    return EDGE_NOISE.sub("", normalize_text(text)).strip()


def match_key(text: str) -> str:
    """Create a forgiving name-comparison key."""

    value = clean_chat_name(text).casefold()
    return re.sub(r"[\s\-—_·•🌟⭐✨🌸🌺🌹⚜️]+", "", value)


def names_match(left: str, right: str) -> bool:
    """Match exact or near-exact chat names after OCR normalization."""

    left_key = match_key(left)
    right_key = match_key(right)
    if not left_key or not right_key:
        return False
    return (
        left_key == right_key
        or (len(left_key) >= 4 and left_key in right_key)
        or (len(right_key) >= 4 and right_key in left_key)
    )


def _clean_time_label(text: str) -> str:
    return normalize_text(text).strip("|｜")


def parse_recent_chats(blocks: list[OcrBlock]) -> list[ChatSummary]:
    """Parse the time-aligned session rows in WeChat's left sidebar."""

    sidebar = [
        block
        for block in blocks
        if 0.075 <= block.center_x <= 0.325
        and 0.105 <= block.center_y <= 0.89
        and block.confidence >= 0.25
    ]
    time_blocks = [
        block
        for block in sidebar
        if 0.275 <= block.center_x <= 0.325
        and TIME_LABEL.match(_clean_time_label(block.text))
    ]

    chats: list[ChatSummary] = []
    used_names: set[str] = set()
    for time_block in sorted(time_blocks, key=lambda block: -block.center_y):
        candidates = [
            block
            for block in sidebar
            if 0.09 <= block.x <= 0.285
            and block.height >= 0.014
            and abs(block.center_y - time_block.center_y) <= 0.027
            and block is not time_block
            and _clean_time_label(block.text) not in CONTROL_TEXT
            and not TIME_LABEL.match(_clean_time_label(block.text))
        ]
        if not candidates:
            continue

        name_block = min(
            candidates,
            key=lambda block: (
                abs(block.center_y - time_block.center_y),
                -block.height,
            ),
        )
        name = clean_chat_name(name_block.text)
        if not name or name in used_names:
            continue

        preview_candidates = [
            block
            for block in sidebar
            if 0.09 <= block.x <= 0.285
            and time_block.center_y - 0.065 <= block.center_y
            < name_block.center_y - 0.009
            and not TIME_LABEL.match(_clean_time_label(block.text))
        ]
        preview = ""
        if preview_candidates:
            preview_block = max(
                preview_candidates,
                key=lambda block: block.center_y,
            )
            preview = normalize_text(preview_block.text)

        used_names.add(name)
        chats.append(
            ChatSummary(
                name=name,
                preview=preview,
                time_label=_clean_time_label(time_block.text),
                click_x=name_block.center_x,
                click_y=name_block.center_y,
            )
        )
    return chats


def parse_visible_messages(
    blocks: list[OcrBlock],
    *,
    page: int,
) -> list[ChatMessage]:
    """Parse visible message text while excluding sidebars, headers, and images."""

    candidates = [
        block
        for block in blocks
        if block.center_x >= 0.34
        and 0.13 <= block.center_y <= 0.82
        and block.height >= 0.014
        and block.confidence >= 0.25
        and normalize_text(block.text) not in CONTROL_TEXT
    ]

    messages: list[ChatMessage] = []
    for block in sorted(candidates, key=lambda item: -item.center_y):
        text = normalize_text(block.text)
        if len(text) == 1 and text in {"⌘", "^", "⑦", "○", "◎"}:
            continue
        if re.fullmatch(r"[○◎⑦]?\s*\d+(?:\.\d+)?(?:万)?", text):
            continue

        if block.center_x >= 0.69:
            sender = "ME"
        elif block.x >= 0.34:
            sender = "OTHER"
        else:
            sender = "UNKNOWN"
        messages.append(
            ChatMessage(
                sender=sender,
                text=text,
                confidence=round(block.confidence, 3),
                page=page,
            )
        )
    return messages


def deduplicate_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Remove OCR duplicates while preserving chronological screen order."""

    output: list[ChatMessage] = []
    recent_keys: list[str] = []
    for message in messages:
        key = f"{message.sender}:{match_key(message.text)}"
        if not match_key(message.text):
            continue
        if key in recent_keys[-12:]:
            continue
        output.append(message)
        recent_keys.append(key)
    return output
