from __future__ import annotations

from hashlib import md5
import io
import os
from pathlib import Path
import sqlite3
import stat
import struct

from Crypto.Cipher import AES
import pytest

from wechat_local_mcp import images
from wechat_local_mcp.media import MediaTextExtractor


WXID = "wxid_tester"
CODE = 305419896  # 0x12345678
AES_KEY = md5(f"{CODE}{WXID}".encode()).hexdigest()[:16].encode()
XOR_KEY = CODE & 0xFF


def jpeg_bytes(size: tuple[int, int] = (24, 16)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


def encode_v2(plain: bytes, aes_key: bytes = AES_KEY, xor_key: int = XOR_KEY, aes_size: int = 1024, xor_size: int = 64) -> bytes:
    aes_size = min(aes_size, len(plain))
    xor_size = min(xor_size, len(plain) - aes_size)
    head = plain[:aes_size]
    pad = 16 - len(head) % 16
    encrypted = AES.new(aes_key, AES.MODE_ECB).encrypt(head + bytes([pad]) * pad)
    middle = plain[aes_size:len(plain) - xor_size]
    tail = bytes(value ^ xor_key for value in plain[len(plain) - xor_size:])
    return images.V2_MAGIC + struct.pack("<LLx", aes_size, xor_size) + encrypted + middle + tail


def make_account(tmp_path: Path, *, with_code_file: bool = True) -> Path:
    documents = tmp_path / "Documents"
    account = documents / "xwechat_files" / f"{WXID}_ab12"
    (account / "db_storage").mkdir(parents=True)
    if with_code_file:
        netbridge = documents / "ConfSDK" / "netbridge"
        netbridge.mkdir(parents=True)
        (netbridge / f"{CODE}.ini").write_text("")
        (netbridge / "1794837098.ini").write_text("")  # decoy number
    return account


def add_image(account: Path, chat: str, file_id: str, month: str = "2026-11", plain: bytes | None = None) -> bytes:
    plain = plain or jpeg_bytes()
    folder = account / "msg" / "attach" / md5(chat.encode()).hexdigest() / month / "Img"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{file_id}.dat").write_bytes(encode_v2(plain))
    (folder / f"{file_id}_t.dat").write_bytes(encode_v2(plain))
    return plain


def test_v2_roundtrip_and_wrong_key_rejected():
    plain = jpeg_bytes((64, 48))
    key = images.ImageKey(xor=XOR_KEY, aes=AES_KEY, source="test")
    assert images.decrypt_dat(encode_v2(plain), key) == plain
    with pytest.raises(ValueError):
        images.decrypt_dat(encode_v2(plain), images.ImageKey(xor=XOR_KEY, aes=b"0" * 16, source="test"))
    with pytest.raises(images.ImageKeyUnavailable):
        images.decrypt_dat(encode_v2(plain), None)


def test_v1_uses_fixed_key_and_legacy_xor():
    plain = jpeg_bytes()
    v1 = bytearray(encode_v2(plain, aes_key=images.V1_AES_KEY))
    v1[:6] = images.V1_MAGIC
    assert images.decrypt_dat(bytes(v1), images.ImageKey(xor=XOR_KEY, aes=b"x" * 16, source="t")) == plain
    assert images.decrypt_dat(bytes(value ^ 0x5A for value in plain), None) == plain


def test_resolves_verifies_and_caches_key(tmp_path):
    account = make_account(tmp_path)
    add_image(account, "wxid_friend", "a" * 32)
    cache = tmp_path / "config" / "image_key.json"
    key = images.resolve_image_key(account, cache)
    assert (key.aes, key.xor, key.source) == (AES_KEY, XOR_KEY, "derived")
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    # WeChat rotates the files that carry the code; the verified cache keeps working.
    (tmp_path / "Documents" / "ConfSDK" / "netbridge" / f"{CODE}.ini").unlink()
    again = images.resolve_image_key(account, cache)
    assert (again.aes, again.source) == (AES_KEY, "cache")


def test_stale_cache_is_ignored_and_missing_code_is_explained(tmp_path):
    account = make_account(tmp_path, with_code_file=False)
    add_image(account, "wxid_friend", "b" * 32)
    cache = tmp_path / "image_key.json"
    cache.write_text('{"%s": {"aes": "0000000000000000", "xor": 1}}' % account.name)
    with pytest.raises(images.ImageKeyUnavailable, match="code"):
        images.resolve_image_key(account, cache)


def test_file_id_from_packed_info():
    packed = b'\x08\xea\t\x10\x02\x1a"" ' + b"d9871f06b6623a8b592e0af9f3ab0876" + b"X\x00"
    assert images.file_id_from_packed(packed) == "d9871f06b6623a8b592e0af9f3ab0876"
    assert images.file_id_from_packed(b"\x08\x02") is None
    assert images.file_id_from_packed(None) is None


def make_snapshot(tmp_path: Path, chat: str, file_id: str, timestamp: int) -> Path:
    snapshot = tmp_path / "snapshot"
    (snapshot / "contact").mkdir(parents=True)
    (snapshot / "message").mkdir(parents=True)
    with sqlite3.connect(snapshot / "contact/contact.db") as connection:
        connection.execute("CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT, alias TEXT)")
        connection.execute("INSERT INTO contact VALUES (?, '图片好友', '', '')", (chat,))
    table = "Msg_" + md5(chat.encode()).hexdigest()
    with sqlite3.connect(snapshot / "message/message_1.db") as connection:
        connection.execute(
            f"CREATE TABLE [{table}] (local_id INTEGER, local_type INTEGER, real_sender_id INTEGER, "
            "create_time INTEGER, message_content TEXT, source TEXT, packed_info_data BLOB)"
        )
        connection.execute(
            f"INSERT INTO [{table}] VALUES (7, 3, 5, ?, '', '', ?)",
            (timestamp, b'\x1a"" ' + file_id.encode() + b"X\x00"),
        )
        connection.execute(f"INSERT INTO [{table}] VALUES (8, 3, 5, ?, '', '', NULL)", (timestamp - 60,))
    with sqlite3.connect(snapshot / "message/message_0.db") as connection:
        connection.execute(
            f"CREATE TABLE [{table}] (local_id INTEGER, local_type INTEGER, real_sender_id INTEGER, "
            "create_time INTEGER, message_content TEXT, source TEXT)"
        )
        connection.execute(f"INSERT INTO [{table}] VALUES (1, 3, 5, ?, '', '')", (timestamp - 86400 * 40,))
    return snapshot


def test_decode_images_newest_first_across_shards(tmp_path):
    chat = "wxid_friend"
    timestamp = 1_794_800_000
    file_id = "c" * 32
    account = make_account(tmp_path)
    plain = add_image(account, chat, file_id)
    snapshot = make_snapshot(tmp_path, chat, file_id, timestamp)
    extractor = MediaTextExtractor(
        snapshot=snapshot, account=account, cache=tmp_path / "text", image_cache=tmp_path / "images"
    )
    result = extractor.decode_images(chat="图片好友", start_ts=None, end_ts=None, limit=10, offset=0)
    assert [item["local_id"] for item in result["items"]] == [7, 8, 1]
    first = result["items"][0]
    assert first["status"] == "ok" and first["variant"] == "full"
    assert Path(first["path"]).read_bytes() == plain
    assert stat.S_IMODE(Path(first["path"]).stat().st_mode) == 0o600
    assert result["items"][1]["status"] == "unavailable"
    again = extractor.decode_images(chat="图片好友", start_ts=None, end_ts=None, limit=1, offset=0)
    assert again["items"][0]["cached"] is True


def test_view_images_tool_returns_image_content(tmp_path, monkeypatch):
    from wechat_local_mcp import server

    chat = "wxid_friend"
    timestamp = 1_794_800_000
    account = make_account(tmp_path)
    add_image(account, chat, "d" * 32)
    snapshot = make_snapshot(tmp_path, chat, "d" * 32, timestamp)
    monkeypatch.setattr(
        server, "_media",
        lambda: MediaTextExtractor(snapshot=snapshot, account=account, cache=tmp_path / "t", image_cache=tmp_path / "i"),
    )
    result = server.wechat_view_images(chat="图片好友", limit=1)
    kinds = [item.type for item in result.content]
    assert kinds == ["text", "text", "image"]
    assert result.content[2].mimeType == "image/jpeg"

    exported = server.wechat_export_images(chat="图片好友", output_dir=str(tmp_path / "out"))
    assert len(exported.structuredContent["exported"]) == 1
    assert os.listdir(tmp_path / "out")[0].endswith(".jpg")


def test_voice_found_in_later_media_shard(tmp_path):
    from wechat_local_mcp.media import _voice_blob

    snapshot = tmp_path / "snapshot"
    (snapshot / "message").mkdir(parents=True)
    for index, has_voice in ((0, False), (1, True)):
        with sqlite3.connect(snapshot / f"message/media_{index}.db") as connection:
            connection.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY)")
            connection.execute(
                "CREATE TABLE VoiceInfo(chat_name_id INTEGER, create_time INTEGER, local_id INTEGER, voice_data BLOB)"
            )
            connection.execute("INSERT INTO Name2Id VALUES ('wxid_friend')")
            if has_voice:
                connection.execute("INSERT INTO VoiceInfo VALUES (1, 100, 3, ?)", (b"\x02#!SILK_V3data",))
    assert _voice_blob(snapshot, "wxid_friend", 3, 100) == b"\x02#!SILK_V3data"
