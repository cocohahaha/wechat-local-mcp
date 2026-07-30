from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import (
    account_dir,
    decrypted_dir,
    load_keys,
    paths,
    status,
    whisper_model,
)
from .crypto import sync_databases
from .formatting import JsonToolResult, output
from .media import MediaTextExtractor
from .platform_ui import (
    backend_name,
    diagnose,
    install_hint,
    list_recent_chats,
    read_chat,
)
from .store import ChatStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("wechat-local-mcp")
mcp = FastMCP("wechat-local-mcp")


def _ts(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 time: {value}") from exc


def _store() -> ChatStore:
    store = ChatStore(decrypted_dir())
    if not store.ready():
        raise RuntimeError(
            "微信聊天快照尚未就绪。先调用 wechat_status 查看路径；如果只有加密库，"
            "请用 wechat_sync 导入已捕获的 keys.json，或先准备一个明文 SQLite 快照。"
        )
    return store


def _media() -> MediaTextExtractor:
    extractor = MediaTextExtractor()
    if not extractor.store.ready():
        raise RuntimeError(
            "微信聊天快照尚未就绪。先运行 wechat_status；媒体文字提取需要"
            "已解密的 message_*.db、media_0.db 和本机微信缓存。"
        )
    return extractor


def _with_automatic_media(
    data: dict,
    collection_key: str,
    *,
    extractor: MediaTextExtractor | None = None,
) -> dict:
    """Enrich image and voice messages as part of normal message collection."""

    media = extractor or _media()
    items = media.enrich_messages(
        data.get(collection_key, []),
        default_chat=data.get("chat"),
        language="zh",
        model=whisper_model(),
    )
    processed = [
        item for item in items
        if item.get("content_source") in {"image_ocr", "voice_transcript"}
    ]
    unavailable = [
        item for item in items
        if (item.get("media") or {}).get("status") == "unavailable"
    ]
    return data | {
        collection_key: items,
        "media_text": {
            "automatic": True,
            "converted": len(processed),
            "unavailable": len(unavailable),
            "local_only": True,
        },
    }


def _cached_media_message(item: dict) -> dict:
    """Convert one cached media result to the normal message schema."""

    kind = str(item.get("media_kind") or "")
    chat = item.get("chat") or {}
    text = str((item.get("derived_text") or {}).get("text") or "").strip()
    return {
        "local_id": item.get("local_id"),
        "local_type": item.get("local_type"),
        "type": "图片" if kind == "image" else "语音",
        "sender": item.get("sender") or "",
        "sender_username": item.get("sender_username") or "",
        "timestamp": item.get("timestamp") or 0,
        "time": item.get("time") or "",
        "content": text,
        "original_content": "[图片]" if kind == "image" else "[语音]",
        "content_source": (
            "image_ocr" if kind == "image" else "voice_transcript"
        ),
        "reply_to": None,
        "mentions": [],
        "chat": {
            key: chat.get(key)
            for key in ("username", "display_name", "kind")
        },
        "media": item | {"cached": True},
    }


def _search_with_automatic_media(
    query: str,
    chats: list[str] | None,
    start_ts: int | None,
    end_ts: int | None,
    limit: int,
    offset: int,
) -> dict:
    """Search normal messages plus media text collected automatically earlier."""

    store = _store()
    extractor = _media()
    raw = store.search(
        query,
        chats,
        start_ts,
        end_ts,
        limit=2_147_483_647,
        offset=0,
    )
    raw = _with_automatic_media(raw, "items", extractor=extractor)
    wanted = (
        {store.resolve(chat)["username"] for chat in chats}
        if chats
        else None
    )
    cached = extractor.search(
        query=query,
        chat=None,
        media_types={"image", "voice"},
        limit=2_147_483_647,
        offset=0,
    )
    combined: dict[tuple[str, int, int], dict] = {}
    for message in raw["items"]:
        chat_data = message.get("chat") or {}
        key = (
            str(chat_data.get("username") or ""),
            int(message.get("local_id") or 0),
            int(message.get("timestamp") or 0),
        )
        combined[key] = message
    for result in cached["items"]:
        chat_data = result.get("chat") or {}
        username = str(chat_data.get("username") or "")
        timestamp = int(result.get("timestamp") or 0)
        if wanted is not None and username not in wanted:
            continue
        if start_ts is not None and timestamp < start_ts:
            continue
        if end_ts is not None and timestamp > end_ts:
            continue
        message = _cached_media_message(result)
        key = (
            username,
            int(message.get("local_id") or 0),
            timestamp,
        )
        combined.setdefault(key, message)
    matches = sorted(
        combined.values(),
        key=lambda item: (
            int(item.get("timestamp") or 0),
            int(item.get("local_id") or 0),
        ),
        reverse=True,
    )
    total = len(matches)
    page = matches[offset : offset + limit]
    return {
        "query": query,
        "total": total,
        "count": len(page),
        "offset": offset,
        "items": page,
        "has_more": offset + len(page) < total,
        "next_offset": (
            offset + len(page)
            if offset + len(page) < total
            else None
        ),
        "media_text": {
            "automatic": True,
            "searches_derived_cache": True,
            "local_only": True,
        },
    }


@mcp.tool(
    name="wechat_status",
    title="检查本地微信接入状态",
    description="只读检查微信版本、加密数据库、密钥文件和明文聊天快照是否就绪。不会读取或返回聊天正文。",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def wechat_status() -> JsonToolResult:
    return output(status(), "json")


@mcp.tool(
    name="wechat_ui_diagnose",
    title="诊断微信 UI 只读兜底",
    description="使用本机窗口捕获和系统 OCR 诊断微信主窗口是否可读。支持 macOS Vision 与 Windows.Media.Ocr；不会发送消息或保留截图。",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def wechat_ui_diagnose() -> JsonToolResult:
    try:
        return output(diagnose(), "json")
    except Exception as exc:
        return output(
            {
                "ready": False,
                "error": str(exc),
                "backend": backend_name(),
                "next_step": install_hint(),
            },
            "json",
        )


@mcp.tool(
    name="wechat_media_status",
    title="检查微信图片与语音转文字状态",
    description=(
        "只读检查微信图片缓存、语音数据库、本地 OCR、SILK 解码器、"
        "离线 Whisper 和派生文字缓存是否就绪；不返回聊天正文。"
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def wechat_media_status() -> JsonToolResult:
    try:
        return output(_media().status(), "json")
    except Exception as exc:
        return output(
            {
                "ready": False,
                "error": str(exc),
                "next_step": (
                    "运行 wechat_status；然后执行 "
                    "uv sync --extra ui --extra media。"
                ),
            },
            "json",
        )


@mcp.tool(
    name="wechat_list_recent_chats",
    title="从微信窗口列出最近聊天",
    description="当明文数据库快照还未准备好时，用本机 OCR 读取当前微信窗口左侧可见的最近会话。不会修改界面内容以外的任何数据。",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def wechat_list_recent_chats(limit: int = 20, response_format: str = "json") -> JsonToolResult:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1-100")
    try:
        chats = [item.model_dump() for item in list_recent_chats(limit)]
        return output(
            {"count": len(chats), "items": chats, "backend": backend_name()},
            response_format,
        )
    except Exception as exc:
        return output(
            {"ready": False, "error": str(exc), "backend": backend_name()},
            "json",
        )


@mcp.tool(
    name="wechat_read_chat_ui",
    title="从微信窗口读取可见聊天",
    description="用 OCR 打开指定聊天并读取最近可见/可滚动的消息。它是数据库快照不可用时的只读兜底，消息可能缺少精确时间戳和发送者识别。",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def wechat_read_chat_ui(chat: str, limit: int = 50, scroll_pages: int = 3, response_format: str = "json") -> JsonToolResult:
    if not 1 <= limit <= 300 or not 0 <= scroll_pages <= 20:
        raise ValueError("limit must be 1-300 and scroll_pages must be 0-20")
    try:
        messages = [item.model_dump() for item in read_chat(chat, limit=limit, scroll_pages=scroll_pages)]
        return output(
            {
                "chat": chat,
                "count": len(messages),
                "messages": messages,
                "backend": backend_name(),
            },
            response_format,
        )
    except Exception as exc:
        return output(
            {
                "ready": False,
                "chat": chat,
                "error": str(exc),
                "backend": backend_name(),
            },
            "json",
        )


@mcp.tool(
    name="wechat_find_todos_ui",
    title="从微信窗口提取待办",
    description="对 OCR 读取到的当前聊天消息运行本地启发式待办提取；结果需要人工确认，不会写入任务系统。",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def wechat_find_todos_ui(chat: str, limit: int = 80, scroll_pages: int = 3, min_score: float = 1.5, response_format: str = "json") -> JsonToolResult:
    if not 1 <= limit <= 300 or not 0 <= scroll_pages <= 20 or not 0 <= min_score <= 10:
        raise ValueError("limit must be 1-300, scroll_pages 0-20, min_score 0-10")
    try:
        from .todos import extract_todo_candidates
        messages = read_chat(chat, limit=limit, scroll_pages=scroll_pages)
        candidates = [item.model_dump() for item in extract_todo_candidates(chat, messages, min_score=min_score)]
        return output(
            {
                "chat": chat,
                "count": len(candidates),
                "items": candidates,
                "backend": backend_name(),
                "heuristic": True,
            },
            response_format,
        )
    except Exception as exc:
        return output(
            {
                "ready": False,
                "chat": chat,
                "error": str(exc),
                "backend": backend_name(),
            },
            "json",
        )


@mcp.tool(
    name="wechat_sync",
    title="同步本地微信数据库快照",
    description="将本机微信 db_storage 中有对应 64 位十六进制密钥的数据库，解密到独立的本地只读快照目录。不会修改微信源文件、不会发送消息。建议退出微信后执行。",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def wechat_sync(key_file: str | None = None, output_dir: str | None = None) -> JsonToolResult:
    p = paths()
    account = account_dir(p.container)
    if account is None:
        raise RuntimeError(f"未找到微信账号目录：{p.container / 'xwechat_files'}")
    target_keys = Path(key_file).expanduser() if key_file else p.keys_file
    if not target_keys.exists():
        return output({
            "ready": False,
            "error": "keys_file_missing",
            "keys_file": str(target_keys),
            "next_step": "运行 scripts/capture_keys.py（需先 uv sync --extra keys），或将已有 keys.json 放到该路径。",
        }, "json")
    keys = load_keys(target_keys)
    target_dir = Path(output_dir).expanduser() if output_dir else p.decrypted_dir
    result = sync_databases(account / "db_storage", keys, target_dir)
    result["ready"] = (target_dir / "contact/contact.db").exists() and bool(list((target_dir / "message").glob("message_*.db")))
    return output(result, "json")


@mcp.tool(
    name="wechat_list_chats",
    title="列出微信聊天",
    description="列出明文快照中的联系人和群聊，支持关键词与 offset/limit 分页。",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def wechat_list_chats(query: str = "", limit: int = 50, offset: int = 0, response_format: str = "json") -> JsonToolResult:
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("limit must be 1-200 and offset must be >= 0")
    try:
        return output(_store().list_chats(query, limit, offset), response_format)
    except RuntimeError:
        try:
            items = [item.model_dump() for item in list_recent_chats(min(limit, 100))]
            return output(
                {
                    "total": len(items),
                    "count": len(items),
                    "offset": 0,
                    "items": items,
                    "has_more": False,
                    "backend": backend_name(),
                },
                response_format,
            )
        except Exception as exc:
            return output(
                {
                    "ready": False,
                    "error": str(exc),
                    "backend": backend_name(),
                    "next_step": f"准备明文快照，或{install_hint()}",
                },
                "json",
            )


@mcp.tool(
    name="wechat_read_chat",
    title="读取微信聊天记录",
    description=(
        "按联系人昵称、备注、群名或微信 ID 读取消息；图片会自动 OCR，"
        "语音会自动转写并直接写入消息 content。媒体处理只使用本机数据，"
        "首次语音识别可能下载本地模型。"
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def wechat_read_chat(chat: str, start_time: str | None = None, end_time: str | None = None, limit: int = 200, response_format: str = "json") -> JsonToolResult:
    if not 1 <= limit <= 5000:
        raise ValueError("limit must be 1-5000")
    try:
        data = _store().read_chat(
            chat,
            _ts(start_time),
            _ts(end_time),
            limit,
        )
        return output(
            _with_automatic_media(data, "messages"),
            response_format,
        )
    except RuntimeError:
        try:
            messages = [item.model_dump() for item in read_chat(chat, limit=min(limit, 300), scroll_pages=3)]
            return output(
                {
                    "chat": {"display_name": chat},
                    "count": len(messages),
                    "messages": messages,
                    "backend": backend_name(),
                    "precision_note": "OCR 兜底可能缺少精确时间和 sender；准备明文快照后可获得数据库事实。",
                },
                response_format,
            )
        except Exception as exc:
            return output(
                {
                    "ready": False,
                    "chat": chat,
                    "error": str(exc),
                    "backend": backend_name(),
                    "next_step": f"准备明文快照，或{install_hint()}",
                },
                "json",
            )


@mcp.tool(
    name="wechat_search_messages",
    title="搜索微信消息",
    description=(
        "在全部聊天或指定聊天中搜索消息，同时搜索读取聊天时自动生成的"
        "图片 OCR 和语音转写缓存；支持时间范围和 offset/limit。"
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def wechat_search_messages(query: str, chats: list[str] | None = None, start_time: str | None = None, end_time: str | None = None, limit: int = 50, offset: int = 0, response_format: str = "json") -> JsonToolResult:
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("limit must be 1-500 and offset must be >= 0")
    return output(
        _search_with_automatic_media(
            query,
            chats,
            _ts(start_time),
            _ts(end_time),
            limit,
            offset,
        ),
        response_format,
    )


@mcp.tool(
    name="wechat_recent_messages",
    title="获取最近微信消息",
    description=(
        "从指定聊天或全部聊天中按时间倒序返回最近消息；遇到图片和语音时"
        "自动 OCR/转写，并把识别文字直接放进 content。"
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def wechat_recent_messages(chats: list[str] | None = None, limit: int = 50, response_format: str = "json") -> JsonToolResult:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be 1-500")
    return output(
        _with_automatic_media(_store().recent(chats, limit), "items"),
        response_format,
    )


@mcp.tool(
    name="wechat_find_todos",
    title="从微信聊天中提取待办",
    description="在最近 N 天的聊天文本中用可解释的中英文行动项启发式筛选待办候选；结果需要人工确认，不会自动创建任务或发送消息。",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def wechat_find_todos(chats: list[str] | None = None, days: int = 30, limit: int = 100, response_format: str = "json") -> JsonToolResult:
    if not 1 <= days <= 3650 or not 1 <= limit <= 500:
        raise ValueError("days must be 1-3650 and limit must be 1-500")
    return output(_store().todos(chats, days, limit), response_format)


@mcp.tool(
    name="wechat_chat_summary",
    title="汇总单个微信聊天",
    description=(
        "返回指定聊天的消息量、参与者、行动项候选和最近消息；图片和语音"
        "先自动转换为文字，因此会自然参与摘要和行动项判断。"
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def wechat_chat_summary(chat: str, start_time: str | None = None, end_time: str | None = None, max_messages: int = 1000, response_format: str = "json") -> JsonToolResult:
    if not 1 <= max_messages <= 5000:
        raise ValueError("max_messages must be 1-5000")
    data = _with_automatic_media(
        _store().read_chat(
            chat,
            _ts(start_time),
            _ts(end_time),
            max_messages,
        ),
        "messages",
    )
    people: dict[str, int] = {}
    todos = []
    for item in data["messages"]:
        people[item["sender"]] = people.get(item["sender"], 0) + 1
        if any(term in item["content"].casefold() for term in ("待办", "todo", "截止", "请", "需要", "跟进", "deadline")):
            todos.append(item)
    summary = {
        "chat": data["chat"],
        "message_count": data["count"],
        "participants": sorted(
            people.items(),
            key=lambda x: x[1],
            reverse=True,
        ),
        "todo_candidates": todos[:100],
        "messages": data["messages"],
        "media_text": data["media_text"],
    }
    return output(summary, response_format)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only MCP server for local WeChat chat data")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport="streamable-http")
