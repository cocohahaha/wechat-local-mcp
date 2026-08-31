from __future__ import annotations

from datetime import datetime
from hashlib import md5
from pathlib import Path
import sqlite3

from wechat_local_mcp.media import MediaTextExtractor, _transcribe_faster_whisper
from wechat_local_mcp.server import (
    wechat_read_chat,
    wechat_search_messages,
)


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
    image.write_bytes(b"\xff\xd8\xff" + b"fake-image-for-mocked-ocr" * 4)
    return snapshot, account, cache


def mock_media_engines(monkeypatch) -> None:
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


def test_extracts_and_caches_image_and_voice_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    snapshot, account, cache = make_media_fixture(tmp_path)
    mock_media_engines(monkeypatch)
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


def test_normal_chat_read_automatically_converts_media(
    tmp_path: Path,
    monkeypatch,
) -> None:
    snapshot, account, cache = make_media_fixture(tmp_path)
    mock_media_engines(monkeypatch)
    extractor = MediaTextExtractor(
        snapshot=snapshot,
        account=account,
        cache=cache,
    )
    monkeypatch.setattr(
        "wechat_local_mcp.server._store",
        lambda: extractor.store,
    )
    monkeypatch.setattr(
        "wechat_local_mcp.server._media",
        lambda: extractor,
    )

    read_result = wechat_read_chat("媒体测试", limit=10)
    read_data = read_result.structuredContent
    assert read_data["media_text"]["automatic"] is True
    assert read_data["media_text"]["converted"] == 2
    assert {
        item["content"]
        for item in read_data["messages"]
    } == {"图片里的项目排期", "明天确认报价"}
    assert {
        item["content_source"]
        for item in read_data["messages"]
    } == {"image_ocr", "voice_transcript"}
    assert all("original_content" in item for item in read_data["messages"])

    search_result = wechat_search_messages(
        "确认报价",
        chats=["媒体测试"],
        limit=10,
    )
    search_data = search_result.structuredContent
    assert search_data["count"] == 1
    assert search_data["items"][0]["content"] == "明天确认报价"
    assert search_data["items"][0]["content_source"] == "voice_transcript"


def test_recognizes_sticker_from_local_caption_database(
    tmp_path: Path,
) -> None:
    snapshot, account, cache = make_media_fixture(tmp_path)
    username = "wxid_media"
    table = "Msg_" + md5(username.encode()).hexdigest()
    sticker_md5 = "aabbccddeeff00112233445566778899"
    with sqlite3.connect(snapshot / "message/message_0.db") as connection:
        connection.execute(
            f"INSERT INTO [{table}] VALUES (3, 47, 2, 1700000002, ?, '')",
            (f'<msg><emoji md5="{sticker_md5}" type="2" /></msg>',),
        )
    (snapshot / "emoticon").mkdir()
    with sqlite3.connect(snapshot / "emoticon/emoticon.db") as connection:
        connection.execute(
            "CREATE TABLE kStoreEmoticonCaptionsTable "
            "(package_id_ TEXT, md5_ TEXT, language_ TEXT, caption_ TEXT)"
        )
        connection.execute(
            "INSERT INTO kStoreEmoticonCaptionsTable VALUES "
            "('', ?, 'zh_cn', '收到，马上处理')",
            (sticker_md5,),
        )
    extractor = MediaTextExtractor(
        snapshot=snapshot, account=account, cache=cache
    )

    result = extractor.extract(
        chat="媒体测试",
        media_types={"sticker"},
        start_ts=None,
        end_ts=None,
        limit=10,
        offset=0,
        language="zh",
        model="test-model",
        refresh=False,
    )

    assert result["total"] == 1
    assert result["items"][0]["media_kind"] == "sticker"
    assert result["items"][0]["derived_text"]["text"] == "收到，马上处理"
    assert result["items"][0]["derived_text"]["engine"] == "local_sticker_database"


def test_marks_text_heavy_image_as_screenshot_in_normal_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    snapshot, account, cache = make_media_fixture(tmp_path)
    monkeypatch.setattr(
        "wechat_local_mcp.media._ocr_image",
        lambda path: {
            "text": "第一行\n第二行是很长的聊天截图文字\n第三行还有待办信息",
            "engine": "test_ocr",
            "confidence": 0.9,
            "blocks": [],
            "visual_kind": "screenshot_candidate",
            "classification": {"heuristic": True, "signals": ["dense_text"]},
        },
    )
    monkeypatch.setattr(
        "wechat_local_mcp.media._transcribe_voice",
        lambda data, language, model: {
            "text": "语音", "engine": "test", "model": model,
            "language": language, "confidence": 0.8,
            "review_required": True, "segments": [],
        },
    )
    extractor = MediaTextExtractor(
        snapshot=snapshot, account=account, cache=cache
    )
    messages = extractor.store.read_chat("媒体测试")["messages"]
    enriched = extractor.enrich_messages(
        messages,
        default_chat=extractor.store.resolve("媒体测试"),
        language="zh",
        model="test-model",
    )
    image = next(item for item in enriched if item["local_type"] == 3)
    assert image["message_kind"] == "screenshot_candidate"
    assert image["content_source"] == "screenshot_ocr"


def test_faster_whisper_backend_maps_mlx_default_for_windows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Segment:
        start = 0.0
        end = 1.0
        text = "确认收到"
        avg_logprob = -0.1

    class Info:
        language = "zh"

    class Engine:
        def transcribe(self, path, **options):
            assert options["language"] == "zh"
            return iter([Segment()]), Info()

    selected = []
    monkeypatch.delenv("WECHAT_FASTER_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(
        "wechat_local_mcp.media._faster_whisper_engine",
        lambda name: selected.append(name) or Engine(),
    )

    result = _transcribe_faster_whisper(
        tmp_path / "message.wav",
        "zh",
        "mlx-community/whisper-small-mlx",
    )

    assert selected == ["small"]
    assert result["engine"] == "faster_whisper"
    assert result["text"] == "确认收到"
