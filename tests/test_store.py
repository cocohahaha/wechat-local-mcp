from __future__ import annotations

import sqlite3
from hashlib import md5
from pathlib import Path

import zstandard as zstd

from wechat_local_mcp.store import ChatStore


def make_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "contact").mkdir(parents=True)
    (root / "message").mkdir(parents=True)
    with sqlite3.connect(root / "contact/contact.db") as con:
        con.execute("CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT, alias TEXT)")
        con.execute("INSERT INTO contact VALUES ('wxid_alice', 'Alice', '爱丽丝', '')")
    username = "wxid_alice"
    table = "Msg_" + md5(username.encode()).hexdigest()
    with sqlite3.connect(root / "message/message_0.db") as con:
        con.execute(f"CREATE TABLE [{table}] (local_id INTEGER, local_type INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content TEXT, source TEXT)")
        con.execute(f"INSERT INTO [{table}] VALUES (1, 1, 2, 1700000000, '请明天确认报价 [强]', '')")
    return root


def test_read_and_search(tmp_path: Path) -> None:
    store = ChatStore(make_fixture(tmp_path))
    assert store.ready()
    chat = store.read_chat("Alice")
    assert chat["count"] == 1
    assert chat["messages"][0]["sender"] == "我"
    assert "👍" in chat["messages"][0]["content"]
    found = store.search("报价")
    assert found["count"] == 1
    recent = store.recent(limit=10)
    assert recent["count"] == 1
    assert recent["items"][0]["chat"]["display_name"] == "Alice"


def test_todo_heuristic(tmp_path: Path) -> None:
    result = ChatStore(make_fixture(tmp_path)).todos(days=3650)
    assert result["count"] == 1
    assert result["items"][0]["todo_score"] >= 1


def test_parses_zstd_forwarded_chat_history_as_structured_json(
    tmp_path: Path,
) -> None:
    root = tmp_path / "forwarded"
    (root / "contact").mkdir(parents=True)
    (root / "message").mkdir(parents=True)
    username = "wxid_forwarded"
    with sqlite3.connect(root / "contact/contact.db") as connection:
        connection.execute(
            "CREATE TABLE contact "
            "(username TEXT, remark TEXT, nick_name TEXT, alias TEXT)"
        )
        connection.execute(
            "INSERT INTO contact VALUES (?, '转发测试', '', '')", (username,)
        )
    table = "Msg_" + md5(username.encode()).hexdigest()
    record = (
        "<recordinfo><datalist>"
        "<dataitem datatype='1'><datadesc>明天确认发布计划</datadesc>"
        "<dataitemsource><sourcename>小王</sourcename>"
        "<sourcetime>2026-08-30 10:00</sourcetime></dataitemsource></dataitem>"
        "<dataitem datatype='2'><fullmd5>abc123</fullmd5>"
        "<dataitemsource><sourcename>小李</sourcename></dataitemsource></dataitem>"
        "</datalist></recordinfo>"
    )
    message = (
        "<msg><appmsg><title>项目讨论</title><type>19</type>"
        f"<recorditem><![CDATA[{record}]]></recorditem>"
        "</appmsg></msg>"
    )
    compressed = zstd.ZstdCompressor().compress(message.encode())
    local_type = 49 | (19 << 32)
    with sqlite3.connect(root / "message/message_0.db") as connection:
        connection.execute(
            f"CREATE TABLE [{table}] (local_id INTEGER, local_type INTEGER, "
            "real_sender_id INTEGER, create_time INTEGER, message_content BLOB, "
            "WCDB_CT_message_content INTEGER, source TEXT)"
        )
        connection.execute(
            f"INSERT INTO [{table}] VALUES (1, ?, 2, 1700000000, ?, 4, '')",
            (local_type, compressed),
        )

    result = ChatStore(root).read_chat("转发测试")
    item = result["messages"][0]
    assert item["compressed"] is True
    assert item["message_kind"] == "forwarded_chat_history"
    assert item["forwarded"]["title"] == "项目讨论"
    assert item["forwarded"]["count"] == 2
    assert item["forwarded"]["items"][0] == {
        "index": 1,
        "data_type": 1,
        "kind": "text",
        "sender": "小王",
        "time": "2026-08-30 10:00",
        "content": "明天确认发布计划",
    }
    assert item["forwarded"]["items"][1]["kind"] == "image"
    assert "明天确认发布计划" in item["content"]
    assert ChatStore(root).search("发布计划")["count"] == 1
