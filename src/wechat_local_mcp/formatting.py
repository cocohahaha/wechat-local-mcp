from __future__ import annotations

import json
from typing import Any


def output(data: Any, response_format: str = "markdown") -> Any:
    if response_format not in {"markdown", "json"}:
        raise ValueError("response_format must be 'markdown' or 'json'")
    if response_format == "json":
        # FastMCP turns a returned mapping into structuredContent and a
        # machine-readable text block. Returning a raw mapping avoids nesting
        # an MCP result inside another MCP result.
        return data
    return markdown(data)


def markdown(data: Any) -> str:
    if isinstance(data, dict) and "participants" in data and "todo_candidates" in data:
        lines = [f"# {data.get('chat', {}).get('display_name', '聊天摘要')}", "", f"消息数：{data.get('message_count', 0)}", ""]
        if data.get("participants"):
            lines.append("## 参与者")
            lines.extend(f"- {name}: {count} 条" for name, count in data["participants"])
            lines.append("")
        lines.append(f"## 待办候选（{len(data.get('todo_candidates', []))}）")
        lines.extend(f"- `{item.get('time', '')}` **{item.get('sender', '')}**：{item.get('content', '')}" for item in data.get("todo_candidates", []))
        return "\n".join(lines)
    if isinstance(data, dict) and "messages" in data:
        lines = [f"# {data.get('chat', {}).get('display_name', '聊天记录')}", ""]
        for item in data.get("messages", []):
            lines.append(f"- `{item.get('time', '')}` **{item.get('sender', '')}**：{item.get('content', '')}")
        return "\n".join(lines) if len(lines) > 2 else "没有匹配的消息。"
    if isinstance(data, dict) and "items" in data:
        lines = [f"共 {data.get('total', len(data['items']))} 条，当前显示 {len(data['items'])} 条。", ""]
        for item in data["items"]:
            if "chat" in item:
                lines.append(f"- `{item.get('time', '')}` **{item['chat'].get('display_name', '')} / {item.get('sender', '')}**：{item.get('content', '')}")
            else:
                lines.append(f"- **{item.get('display_name', item.get('username', ''))}** ({item.get('kind', '')})")
        return "\n".join(lines)
    if isinstance(data, dict) and "messages" not in data:
        return "\n".join([f"- **{key}**: {value}" for key, value in data.items()])
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)
