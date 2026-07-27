from __future__ import annotations

import inspect
import json

import pytest
from mcp.types import CallToolResult

from wechat_local_mcp.formatting import output
from wechat_local_mcp.server import (
    wechat_chat_summary,
    wechat_find_todos,
    wechat_find_todos_ui,
    wechat_list_chats,
    wechat_list_recent_chats,
    wechat_read_chat,
    wechat_read_chat_ui,
    wechat_recent_messages,
    wechat_search_messages,
)


def test_default_output_is_json_with_structured_content() -> None:
    data = {"count": 1, "items": [{"content": "请确认排期"}]}

    result = output(data)

    assert isinstance(result, CallToolResult)
    assert result.structuredContent == data
    assert json.loads(result.content[0].text) == data


def test_markdown_remains_available_without_losing_structure() -> None:
    data = {
        "chat": {"display_name": "示例聊天"},
        "messages": [{"time": "2026-01-01 09:00", "sender": "示例用户", "content": "请确认排期"}],
    }

    result = output(data, "markdown")

    assert result.content[0].text.startswith("# 示例聊天")
    assert result.structuredContent == data


def test_invalid_response_format_is_rejected() -> None:
    with pytest.raises(ValueError, match="response_format"):
        output({}, "xml")


def test_all_query_tools_default_to_json() -> None:
    tools = (
        wechat_list_recent_chats,
        wechat_read_chat_ui,
        wechat_find_todos_ui,
        wechat_list_chats,
        wechat_read_chat,
        wechat_search_messages,
        wechat_recent_messages,
        wechat_find_todos,
        wechat_chat_summary,
    )

    for tool in tools:
        assert inspect.signature(tool).parameters["response_format"].default == "json"
