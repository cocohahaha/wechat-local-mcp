from __future__ import annotations

import hashlib
import html
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

try:
    import zstandard as zstd
except Exception:  # optional for unusual compressed rows
    zstd = None


TYPE_LABELS = {
    1: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频", 47: "表情",
    48: "位置", 49: "链接/文件", 50: "通话", 10000: "系统",
}
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
EMOJI = {"强": "👍", "弱": "👎", "玫瑰": "🌹", "呲牙": "😁", "捂脸": "🤦", "偷笑": "🤭", "旺柴": "🐶", "破涕为笑": "😂", "哇": "😮", "爱心": "❤️", "抱拳": "🙏", "庆祝": "🎉", "拥抱": "🤗", "OK": "👌", "拳头": "✊", "吃瓜": "🍉", "流泪": "😭", "鼓掌": "👏", "好的": "👌", "微笑": "😊", "加油": "💪", "奋斗": "💪", "再见": "👋", "蛋糕": "🎂", "火": "🔥", "疑问": "❓", "大哭": "😭", "笑哭R": "😂", "赞R": "👍"}
EMOJI_RE = re.compile(r"\[([^\[\]]{1,10})\]")


def decode(value: Any, flag: Any = None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raw = bytes(value)
    if (raw.startswith(ZSTD_MAGIC) or flag == 4) and zstd:
        try:
            raw = zstd.ZstdDecompressor().decompress(raw, max_output_size=2_000_000)
        except Exception:
            return "[压缩消息解码失败]"
    return raw.decode("utf-8", "replace")


def normalize(text: str) -> str:
    return EMOJI_RE.sub(lambda m: EMOJI.get(m.group(1), m.group(0)), text or "")


def split_type(local_type: Any) -> tuple[int, int]:
    try:
        value = int(local_type or 0)
    except (TypeError, ValueError):
        return 0, 0
    return (value & 0xFFFFFFFF, value >> 32) if value > 0xFFFFFFFF else (value, 0)


def type_label(local_type: Any) -> str:
    base, _ = split_type(local_type)
    return TYPE_LABELS.get(base, f"type={local_type}")


def table_for(username: str) -> str:
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info([{table}])")}
    except sqlite3.Error:
        return set()


def _tables(root: Path) -> list[Path]:
    return sorted((root / "message").glob("message_[0-9]*.db")) if (root / "message").is_dir() else []


def load_contacts(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    path = root / "contact/contact.db"
    if not path.exists():
        return {}, {}
    contacts: dict[str, str] = {}
    aliases: dict[str, str] = {}
    try:
        with _connect(path) as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='contact'").fetchone():
                return contacts, aliases
            cols = _columns(con, "contact")
            selected = [c for c in ("username", "remark", "nick_name", "alias") if c in cols]
            if "username" not in selected:
                return contacts, aliases
            for row in con.execute(f"SELECT {','.join(selected)} FROM contact"):
                username = str(row["username"] or "")
                if not username:
                    continue
                display = str(row["remark"] or "") if "remark" in selected else ""
                display = display or (str(row["nick_name"] or "") if "nick_name" in selected else "") or username
                contacts[username] = display
                alias = str(row["alias"] or "") if "alias" in selected else ""
                if alias:
                    aliases[alias] = display
    except sqlite3.Error:
        return {}, {}
    return contacts, aliases


def _name_map(con: sqlite3.Connection) -> dict[int, str]:
    out: dict[int, str] = {}
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='Name2Id'").fetchone():
        return out
    try:
        for row in con.execute("SELECT rowid, user_name FROM Name2Id"):
            out[int(row[0])] = decode(row[1])
    except sqlite3.Error:
        return {}
    return out


def _format_content(local_type: Any, content: str) -> str:
    base, sub = split_type(local_type)
    if base == 1:
        return normalize(content.strip())
    if base == 3:
        return "[图片]"
    if base == 34:
        match = re.search(r'voicelength="(\d+)"', content)
        return f"[语音，约 {int(match.group(1)) / 1000:.1f} 秒]" if match else "[语音]"
    if base == 43:
        return "[视频]"
    if base == 47:
        return "[表情]"
    if base == 48:
        return "[位置]"
    if base == 42:
        return "[名片]"
    if base == 50:
        return "[通话]"
    if base == 10000:
        return "[系统消息]" if re.search(r"<[a-zA-Z/!]", content) else normalize(content.strip())
    if base == 49:
        xml_content = content[content.find("<msg>"):] if "<msg>" in content else content
        try:
            root = ET.fromstring(xml_content)
            app_type = int(root.findtext(".//type") or 0)
            title = (root.findtext(".//title") or root.findtext(".//des") or "").strip()
            if app_type == 57:
                return normalize(title) or "[引用回复]"
            if app_type == 5:
                return f"[链接] {normalize(title)}".strip()
            if app_type in (33, 36, 44):
                return f"[小程序] {normalize(title)}".strip()
            if sub == 6 or app_type == 6:
                return f"[文件] {normalize(title)}".strip()
            return f"[链接/文件] {normalize(title)}".strip()
        except Exception:
            title_match = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", content, re.S)
            title = html.unescape(title_match.group(1)).strip() if title_match else ""
            return f"[链接/文件] {normalize(title)}".strip()
    return f"[{type_label(local_type)}] {normalize(content)}".strip()


def _reply(content: str, local_type: Any, contacts: dict[str, str]) -> dict[str, str] | None:
    if split_type(local_type)[0] != 49 or "<refermsg>" not in content:
        return None
    try:
        node = ET.fromstring(content).find(".//refermsg")
        if node is None:
            return None
        display = (node.findtext("displayname") or "").strip()
        quoted = (node.findtext("content") or "").strip()
        from_user = (node.findtext("fromusr") or "").strip()
    except Exception:
        display = (re.search(r"<displayname>(.*?)</displayname>", content, re.S) or ["", ""])[1].strip()
        quoted = (re.search(r"<content>(.*?)</content>", content, re.S) or ["", ""])[1].strip()
        from_user = (re.search(r"<fromusr>(.*?)</fromusr>", content, re.S) or ["", ""])[1].strip()
    display = display or contacts.get(from_user, from_user)
    if not display and not quoted:
        return None
    if "<appmsg" in quoted or "&lt;appmsg" in quoted:
        quoted = html.unescape(quoted)
    return {"to_name": display, "quoted": normalize(quoted)[:500]}


def _row_message(row: sqlite3.Row, chat: dict[str, Any], contacts: dict[str, str], names: dict[int, str]) -> dict[str, Any]:
    content = decode(row["message_content"], row["flag"]) or decode(row["compress_content"], row["flag"])
    sender_id = str(row["real_sender_id"] or "")
    sender_username = names.get(int(sender_id), "") if sender_id.isdigit() else ""
    prefix_match = re.match(r"^([^:\r\n]{1,200}):[ \t]*\r?\n", content)
    if prefix_match:
        prefix = prefix_match.group(1)
        if prefix.startswith("wxid_") or prefix == sender_username or prefix in contacts:
            sender_username = sender_username or prefix
            content = content[prefix_match.end():]
    if split_type(row["local_type"])[0] == 10000:
        sender = "系统"
    elif chat.get("is_group", "@chatroom" in chat["username"]):
        sender = contacts.get(sender_username, sender_username) if sender_username else ""
        if sender.startswith("wxid_"):
            sender = f"群友-{sender[5:11]}"
    elif sender_username and sender_username != chat["username"]:
        sender = contacts.get(sender_username, sender_username)
    elif sender_id == "2":
        sender = "我"
    else:
        sender = chat["display_name"]
    timestamp = int(row["create_time"] or 0)
    when = datetime.fromtimestamp(timestamp, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S") if timestamp else ""
    source = decode(row["source"])
    mentions = []
    match = re.search(r"<atuserlist>(.*?)</atuserlist>", source, re.S)
    if match:
        raw = html.unescape(match.group(1))
        for token in re.split(r"[,、\s]+", re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", raw)):
            if token == "notify@all":
                mentions.append("所有人")
            elif token and token in contacts:
                mentions.append(contacts[token])
    return {
        "local_id": row["local_id"], "local_type": row["local_type"], "type": type_label(row["local_type"]),
        "sender": sender, "sender_username": sender_username, "timestamp": timestamp, "time": when,
        "content": _format_content(row["local_type"], content), "reply_to": _reply(content, row["local_type"], contacts),
        "mentions": mentions,
    }


class ChatStore:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.contacts, self.aliases = load_contacts(self.root)

    def ready(self) -> bool:
        return (self.root / "contact/contact.db").exists() and bool(_tables(self.root))

    def list_chats(self, query: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        q = query.casefold().strip()
        rows = []
        for username, display in self.contacts.items():
            if q and q not in " ".join((username, display, self.aliases.get(username, ""))).casefold():
                continue
            rows.append({"username": username, "display_name": display, "kind": "group" if "@chatroom" in username else "contact"})
        rows.sort(key=lambda x: (x["kind"] != "group", x["display_name"].casefold()))
        total = len(rows)
        page = rows[offset:offset + limit]
        return {"total": total, "count": len(page), "offset": offset, "items": page, "has_more": offset + len(page) < total, "next_offset": offset + len(page) if offset + len(page) < total else None}

    def resolve(self, chat: str) -> dict[str, Any]:
        target = chat.casefold().strip()
        exact = [
            {"username": u, "display_name": d, "kind": "group" if "@chatroom" in u else "contact"}
            for u, d in self.contacts.items() if u.casefold() == target or d.casefold() == target
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ValueError(f"chat name is ambiguous; choose one of: {', '.join(x['username'] for x in exact[:10])}")
        partial = self.list_chats(chat, limit=10)["items"]
        if not partial:
            raise ValueError(f"chat not found: {chat}")
        if len(partial) == 1:
            return partial[0]
        raise ValueError("chat name is ambiguous; candidates: " + ", ".join(x["display_name"] for x in partial))

    def _iter_rows(
        self,
        username: str,
        start_ts: int | None,
        end_ts: int | None,
        *,
        newest_first: bool = False,
    ) -> Iterable[tuple[sqlite3.Row, dict[str, Any], dict[int, str]]]:
        target = table_for(username)
        chat = {"username": username, "display_name": self.contacts.get(username, username), "is_group": "@chatroom" in username}
        for path in _tables(self.root):
            try:
                with _connect(path) as con:
                    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (target,)).fetchone():
                        continue
                    cols = _columns(con, target)
                    required = {"local_id", "local_type", "real_sender_id", "create_time", "message_content"}
                    if not required.issubset(cols):
                        continue
                    selected = ["local_id", "local_type", "real_sender_id", "create_time", "message_content", "compress_content" if "compress_content" in cols else "NULL AS compress_content", "WCDB_CT_message_content AS flag" if "WCDB_CT_message_content" in cols else "NULL AS flag", "source" if "source" in cols else "NULL AS source"]
                    where, params = [], []
                    if start_ts is not None:
                        where.append("create_time >= ?"); params.append(start_ts)
                    if end_ts is not None:
                        where.append("create_time <= ?"); params.append(end_ts)
                    sql = f"SELECT {', '.join(selected)} FROM [{target}]" + ((" WHERE " + " AND ".join(where)) if where else "")
                    if newest_first:
                        sql += " ORDER BY create_time DESC, local_id DESC"
                    names = _name_map(con)
                    for row in con.execute(sql, params):
                        yield row, chat, names
            except sqlite3.Error:
                continue

    def _iter_many_rows(
        self,
        usernames: Iterable[str],
        start_ts: int | None,
        end_ts: int | None,
        *,
        per_chat_limit: int | None = None,
        newest_first: bool = False,
    ) -> Iterable[tuple[sqlite3.Row, dict[str, Any], dict[int, str]]]:
        table_users = {table_for(username): username for username in usernames}
        if not table_users:
            return
        for path in _tables(self.root):
            try:
                with _connect(path) as con:
                    available = {
                        str(row[0])
                        for row in con.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                        )
                    }
                    names = _name_map(con)
                    for target in sorted(available.intersection(table_users)):
                        username = table_users[target]
                        cols = _columns(con, target)
                        required = {"local_id", "local_type", "real_sender_id", "create_time", "message_content"}
                        if not required.issubset(cols):
                            continue
                        selected = [
                            "local_id",
                            "local_type",
                            "real_sender_id",
                            "create_time",
                            "message_content",
                            "compress_content" if "compress_content" in cols else "NULL AS compress_content",
                            "WCDB_CT_message_content AS flag" if "WCDB_CT_message_content" in cols else "NULL AS flag",
                            "source" if "source" in cols else "NULL AS source",
                        ]
                        where, params = [], []
                        if start_ts is not None:
                            where.append("create_time >= ?")
                            params.append(start_ts)
                        if end_ts is not None:
                            where.append("create_time <= ?")
                            params.append(end_ts)
                        sql = f"SELECT {', '.join(selected)} FROM [{target}]"
                        if where:
                            sql += " WHERE " + " AND ".join(where)
                        if newest_first:
                            sql += " ORDER BY create_time DESC, local_id DESC"
                        if per_chat_limit is not None:
                            sql += " LIMIT ?"
                            params.append(per_chat_limit)
                        chat = {
                            "username": username,
                            "display_name": self.contacts.get(username, username),
                            "kind": "group" if "@chatroom" in username else "contact",
                            "is_group": "@chatroom" in username,
                        }
                        for row in con.execute(sql, params):
                            yield row, chat, names
            except sqlite3.Error:
                continue

    def read_chat(self, chat: str, start_ts: int | None = None, end_ts: int | None = None, limit: int = 200) -> dict[str, Any]:
        resolved = self.resolve(chat)
        messages = [_row_message(row, resolved, self.contacts, names) for row, _, names in self._iter_rows(resolved["username"], start_ts, end_ts)]
        messages.sort(key=lambda x: (x["timestamp"], str(x["local_id"])))
        return {"chat": resolved, "count": len(messages[-limit:]), "messages": messages[-limit:]}

    def media_messages(
        self,
        chat: str,
        media_types: set[str],
        start_ts: int | None = None,
        end_ts: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List image and voice messages without loading unrelated message bodies."""

        type_numbers = {
            "image": 3,
            "voice": 34,
        }
        allowed = {type_numbers[name] for name in media_types}
        resolved = self.resolve(chat)
        items: list[dict[str, Any]] = []
        total = 0
        for row, _, names in self._iter_rows(
            resolved["username"],
            start_ts,
            end_ts,
            newest_first=True,
        ):
            base_type, _ = split_type(row["local_type"])
            if base_type not in allowed:
                continue
            if total >= offset and len(items) < limit:
                item = _row_message(row, resolved, self.contacts, names)
                item["media_kind"] = "image" if base_type == 3 else "voice"
                items.append(item)
            total += 1
        return {
            "chat": resolved,
            "media_types": sorted(media_types),
            "total": total,
            "count": len(items),
            "offset": offset,
            "items": items,
            "has_more": offset + len(items) < total,
            "next_offset": (
                offset + len(items)
                if offset + len(items) < total
                else None
            ),
        }

    def search(self, query: str, chats: list[str] | None = None, start_ts: int | None = None, end_ts: int | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if not query.strip():
            raise ValueError("query must not be empty")
        usernames = [self.resolve(x)["username"] for x in chats] if chats else list(self.contacts)
        q = query.casefold()
        matches = []
        for row, resolved, names in self._iter_many_rows(usernames, start_ts, end_ts):
            item = _row_message(row, resolved, self.contacts, names)
            if q in item["content"].casefold():
                matches.append(item | {"chat": {k: resolved[k] for k in ("username", "display_name", "kind")}})
        matches.sort(key=lambda x: (x["timestamp"], str(x["local_id"])), reverse=True)
        total = len(matches)
        page = matches[offset:offset + limit]
        return {"query": query, "total": total, "count": len(page), "offset": offset, "items": page, "has_more": offset + len(page) < total, "next_offset": offset + len(page) if offset + len(page) < total else None}

    def todos(self, chats: list[str] | None = None, days: int = 30, limit: int = 100) -> dict[str, Any]:
        import time
        now = int(time.time())
        since = now - days * 86400
        terms = re.compile(r"待办|todo|行动项|请|需要|记得|跟进|截止|明天|下周|确认|发送|回复|安排|完成|提交|deadline|follow up|please|need to|action", re.I)
        candidates: list[dict[str, Any]] = []
        usernames = [self.resolve(x)["username"] for x in chats] if chats else list(self.contacts)
        for row, resolved, names in self._iter_many_rows(usernames, since, None):
            item = _row_message(row, resolved, self.contacts, names)
            if item["content"] and terms.search(item["content"]) and not item["content"].startswith("["):
                score = len(terms.findall(item["content"])) + (2 if re.search(r"待办|todo|截止|deadline|行动项", item["content"], re.I) else 0)
                candidates.append(item | {"chat": {k: resolved[k] for k in ("username", "display_name", "kind")}, "todo_score": score})
        candidates.sort(key=lambda x: (x["todo_score"], x["timestamp"]), reverse=True)
        return {"days": days, "count": min(len(candidates), limit), "items": candidates[:limit], "heuristic": True}

    def recent(self, chats: list[str] | None = None, limit: int = 50) -> dict[str, Any]:
        usernames = [self.resolve(x)["username"] for x in chats] if chats else list(self.contacts)
        items = []
        for row, resolved, names in self._iter_many_rows(
            usernames,
            None,
            None,
            per_chat_limit=limit,
            newest_first=True,
        ):
            item = _row_message(row, resolved, self.contacts, names)
            items.append(item | {"chat": {k: resolved[k] for k in ("username", "display_name", "kind")}})
        items.sort(key=lambda item: (item["timestamp"], str(item["local_id"])), reverse=True)
        return {"count": min(limit, len(items)), "items": items[:limit]}
