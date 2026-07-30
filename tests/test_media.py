from __future__ import annotations

from datetime import datetime
from hashlib import md5
from pathlib import Path
import sqlite3

from wechat_local_mcp.media import MediaTextExtractor


def make_media_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    snapshot = tmp_path / "snapshot"
    account = tmp_path / "account"
    cache = tmp_path / "derived"
    (snapshot / "contact").mkdir(parents=True)
    (snapshot / "message").mkdir(parents=True)

    username = "wxid_media"
    with sqlite3.connect(snapshot / "contact/contact.db") as connection:
        connection.execute(
            "CREATE TABLE contact "
            "(username TEXT, remark TEXT, nick_name TEXT, alias TEXT)"
        )
        connection.execute(
            "INSERT INTO contact VALUES (?, '媒体测试', '', '')",
            (username,),
        )

    table = "Msg_" + md5(username.encode()).hexdigest()
    timestamp = 1_700_000_000
    with sqlite3.connect(snapshot / "message/message_0.db") as connection:
        connection.execute(
            f"CREATE TABLE [{table}] "
            "(local_id INTEGER, local_type INTEGER, real_sender_id INTEGER, "
            "create_time INTEGER, message_content TEXT, source TEXT)"
        )
        connection.execute(
            f"INSERT INTO [{table}] VALUES (1, 3, 2, ?, '', '')",
            (timestamp,),
        )
        connection.execute(
            f"INSERT INTO [{table}] VALUES (2, 34, 2, ?, "
            "'<msg><voicemsg voicelength=\"1000\" /></msg>', '')",
            (timestamp + 1,),
        )

    with sqlite3.connect(snapshot / "message/media_0.db") as connection:
        connection.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE VoiceInfo("
            "chat_name_id INTEGER, create_time INTEGER, local_id INTEGER, "
            "svr_id INTEGER, voice_data BLOB, data_index TEXT)"
        )
        connection.execute("INSERT INTO Name2Id VALUES (?)", (username,))
        connection.execute(
            "INSERT INTO VoiceInfo VALUES (1, ?, 2, 1, ?, '0')",
            (timestamp + 1, b"\x02#!SILK_V3\x00\x00"),
        )

    month = datetime.fromtimestamp(timestamp).strftime("%Y-%m")
    image = (
        account
        / "cache"
        / month
        / "Message"
        / md5(username.encode()).hexdigest()
        / "Thumb"
        / f"1_{timestamp}_thumb.jpg"
    )
    image.parent.mkdir(parents=True)
    image.write_bytes(b"fake-image-for-mocked-ocr" * 4)
    return snapshot, account, cache


def test_extracts_and_caches_image_and_voice_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    snapshot, account, cache = make_media_fixture(tmp_path)
    monkeypatch.setattr(
        "wechat_local_mcp.media._ocr_image",
        lambda path: {
            "text": "图片里的项目排期",
            "engine": "test_ocr",
            "confidence": 0.9,
            "blocks": [],
        },
    )
    monkeypatch.setattr(
        "wechat_local_mcp.media._transcribe_voice",
        lambda data, language, model: {
            "text": "明天确认报价",
            "engine": "test_whisper",
            "model": model,
            "language": language,
            "confidence": 0.8,
            "review_required": True,
            "segments": [],
        },
    )
    extractor = MediaTextExtractor(
        snapshot=snapshot,
        account=account,
        cache=cache,
    )

    result = extractor.extract(
        chat="媒体测试",
        media_types={"image", "voice"},
        start_ts=None,
        end_ts=None,
        limit=10,
        offset=0,
        language="zh",
        model="test-model",
        refresh=False,
    )

    assert result["total"] == 2
    assert {item["media_kind"] for item in result["items"]} == {
        "image",
        "voice",
    }
    assert {
        item["derived_text"]["text"]
        for item in result["items"]
    } == {"图片里的项目排期", "明天确认报价"}
    assert all(item["cached"] is False for item in result["items"])

    cached = extractor.extract(
        chat="媒体测试",
        media_types={"image", "voice"},
        start_ts=None,
        end_ts=None,
        limit=10,
        offset=0,
        language="zh",
        model="test-model",
        refresh=False,
    )
    assert all(item["cached"] is True for item in cached["items"])

    search = extractor.search(
        query="确认报价",
        chat="媒体测试",
        media_types={"voice"},
        limit=10,
        offset=0,
    )
    assert search["count"] == 1
    assert search["items"][0]["media_kind"] == "voice"
