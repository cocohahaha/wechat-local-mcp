from __future__ import annotations

import sqlite3
from hashlib import md5
from pathlib import Path

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
