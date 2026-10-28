from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from Crypto.Cipher import AES

from wechat_local_mcp import crypto


KEY = bytes(range(32))


def encrypted_db(path: Path, key: bytes = KEY) -> bytes:
    """An empty, valid one-page SQLite DB with SQLCipher's reserved bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA page_size=4096")
        db.execute("VACUUM")
    db.close()
    plain = bytearray(path.read_bytes())
    plain[20] = 80
    plain[105:107] = (4016).to_bytes(2, "big")
    iv = bytes(range(16))
    plain[-80:-64] = iv
    cipher = AES.new(key, AES.MODE_CBC, iv).encrypt(bytes(plain[16:-80]))
    path.write_bytes(b"0123456789abcdef" + cipher + bytes(plain[-80:]))
    return bytes(plain)


def test_roundtrip_and_wrong_key_rejected(tmp_path):
    source = tmp_path / "contact.db"
    plain = encrypted_db(source)
    assert crypto.decrypt_bytes(source.read_bytes(), KEY) == plain
    with pytest.raises(ValueError, match="key|header"):
        crypto.decrypt_bytes(source.read_bytes(), b"x" * 32)
    with pytest.raises(ValueError):
        crypto.decrypt_bytes(b"", KEY)


def test_incremental_skips_without_decrypting(tmp_path, monkeypatch):
    root, out = tmp_path / "source", tmp_path / "out"
    source = root / "contact/contact.db"
    expected = encrypted_db(source)
    keys = {"contact.db": KEY.hex()}
    first = crypto.sync_databases(root, keys, out)
    assert first["count"] == 1
    assert (out / "contact/contact.db").read_bytes() == expected
    with sqlite3.connect(out / "contact/contact.db") as db:
        assert db.execute("SELECT count(*) FROM sqlite_master").fetchone() == (0,)
    monkeypatch.setattr(crypto, "_decrypt_file", lambda *_: pytest.fail("unchanged DB decrypted"))
    second = crypto.sync_databases(root, keys, out)
    assert second["unchanged"] == ["contact/contact.db"]
    assert second["bytes_processed"] == 0
    assert second["count"] == 0


def test_force_and_changed_source_rebuild(tmp_path):
    root, out = tmp_path / "source", tmp_path / "out"
    source = root / "contact.db"
    encrypted_db(source)
    keys = {"contact.db": KEY.hex()}
    crypto.sync_databases(root, keys, out)
    assert crypto.sync_databases(root, keys, out, force=True)["count"] == 1
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
    assert crypto.sync_databases(root, keys, out)["count"] == 1
    (out / "contact.db").unlink()
    assert crypto.sync_databases(root, keys, out)["count"] == 1


def test_wrong_key_preserves_previous_snapshot(tmp_path):
    root, out = tmp_path / "source", tmp_path / "out"
    encrypted_db(root / "contact.db")
    crypto.sync_databases(root, {"contact.db": KEY.hex()}, out)
    previous = (out / "contact.db").read_bytes()
    result = crypto.sync_databases(root, {"contact.db": (b"x" * 32).hex()}, out)
    assert len(result["failed"]) == 1
    assert (out / "contact.db").read_bytes() == previous
    assert not list(out.rglob("*.tmp"))


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_pending_writes_never_report_unchanged(tmp_path, suffix):
    root, out = tmp_path / "source", tmp_path / "out"
    source = root / "contact.db"
    encrypted_db(source)
    keys = {"contact.db": KEY.hex()}
    crypto.sync_databases(root, keys, out)
    Path(str(source) + suffix).write_bytes(b"pending")
    result = crypto.sync_databases(root, keys, out)
    assert result["failed"][0]["code"] == "source_busy"
    assert not result["unchanged"]
    assert result["complete"] is False


def test_source_change_during_read_preserves_snapshot(tmp_path, monkeypatch):
    root, out = tmp_path / "source", tmp_path / "out"
    source = root / "contact.db"
    encrypted_db(source)
    keys = {"contact.db": KEY.hex()}
    crypto.sync_databases(root, keys, out)
    previous = (out / "contact.db").read_bytes()
    decrypt = crypto._decrypt_file

    def mutate(src, target, key):
        decrypt(src, target, key)
        Path(str(src) + "-wal").write_bytes(b"new writes")

    monkeypatch.setattr(crypto, "_decrypt_file", mutate)
    result = crypto.sync_databases(root, keys, out, force=True)
    assert result["failed"][0]["code"] == "source_busy"
    assert (out / "contact.db").read_bytes() == previous


def test_profiles_plan_and_plaintext_without_keys(tmp_path):
    root, out = tmp_path / "source", tmp_path / "out"
    for name in ["contact/contact.db", "message/message_0.db", "message/media_0.db", "message/message_fts.db", "emoticon/emoticon.db"]:
        encrypted_db(root / name)
    keys = {p.name: KEY.hex() for p in root.rglob("*.db")}
    planned = crypto.sync_databases(root, keys, out, profile="chat", dry_run=True)
    assert len(planned["planned"]) == 2
    assert len(planned["deferred"]) == 3
    assert not out.exists()
    assert crypto.sync_databases(root, keys, out, profile="chat")["count"] == 2
    assert crypto.sync_databases(root, keys, out, profile="media")["count"] == 2
    assert crypto.sync_databases(root, keys, out, profile="all")["count"] == 1
    plain = root / "plaintext.db"
    with sqlite3.connect(plain) as db:
        db.execute("CREATE TABLE hello (value)")
    assert "plaintext.db" in crypto.sync_databases(root, {}, out)["decrypted"]


def test_overlapping_roots_rejected(tmp_path):
    encrypted_db(tmp_path / "source/contact.db")
    with pytest.raises(ValueError, match="overlap"):
        crypto.sync_databases(tmp_path / "source", {}, tmp_path / "source/out")


def test_corrupt_manifest_and_changed_destination_rebuild(tmp_path):
    root, out = tmp_path / "source", tmp_path / "out"
    encrypted_db(root / "contact.db")
    keys = {"contact.db": KEY.hex()}
    crypto.sync_databases(root, keys, out)
    manifest = out / ".sync-manifest.json"
    assert KEY.hex() not in manifest.read_text()
    manifest.write_text("{broken")
    assert crypto.sync_databases(root, keys, out)["count"] == 1
    (out / "contact.db").write_bytes(b"broken")
    assert crypto.sync_databases(root, keys, out)["count"] == 1


def test_output_symlink_cannot_escape(tmp_path):
    root, out, elsewhere = tmp_path / "source", tmp_path / "out", tmp_path / "elsewhere"
    encrypted_db(root / "contact/contact.db")
    out.mkdir()
    elsewhere.mkdir()
    try:
        (out / "contact").symlink_to(elsewhere, target_is_directory=True)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
    result = crypto.sync_databases(root, {"contact.db": KEY.hex()}, out)
    assert result["failed"]
    assert not list(elsewhere.iterdir())


def test_second_sync_fails_fast_when_locked(tmp_path):
    root, out = tmp_path / "source", tmp_path / "out"
    encrypted_db(root / "contact.db")
    out.mkdir()
    with crypto._sync_lock(out):
        with pytest.raises(RuntimeError, match="sync"):
            crypto.sync_databases(root, {"contact.db": KEY.hex()}, out)


def test_clean_persistent_journal_is_not_live_writes(tmp_path):
    root, out = tmp_path / "source", tmp_path / "out"
    root.mkdir()
    source = root / "plain.db"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=PERSIST")
        db.execute("CREATE TABLE example (value)")
        db.execute("INSERT INTO example VALUES (1)")
    db.close()
    assert Path(str(source) + "-journal").stat().st_size > 28
    assert crypto.sync_databases(root, {}, out)["complete"] is True


def test_wrong_db_root_never_reports_empty_success(tmp_path):
    root, out = tmp_path / "source", tmp_path / "out"
    encrypted_db(root / "db_storage/message/message_0.db")
    with pytest.raises(ValueError, match="no databases match"):
        crypto.sync_databases(root, {"message_0.db": KEY.hex()}, out, profile="chat")
    assert not out.exists()


@pytest.mark.parametrize("force", [False, True])
def test_output_wal_is_not_overlaid_on_new_snapshot(tmp_path, force):
    root, out = tmp_path / "source", tmp_path / "out"
    root.mkdir()
    source = root / "plain.db"
    db = sqlite3.connect(source)
    db.execute("CREATE TABLE example (value)")
    db.execute("INSERT INTO example VALUES ('source')")
    db.commit()
    db.close()
    assert crypto.sync_databases(root, {}, out)["complete"]
    snapshot = out / "plain.db"
    writer = sqlite3.connect(snapshot)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE example SET value='stale output WAL'")
        writer.commit()
        previous = snapshot.read_bytes()
        result = crypto.sync_databases(root, {}, out, force=force)
        assert result["complete"] is False
        assert result["failed"][0]["code"] == "output_busy"
        assert snapshot.read_bytes() == previous
        assert Path(str(snapshot) + "-wal").exists()
    finally:
        writer.close()


def test_output_write_during_decrypt_preserves_previous_snapshot(tmp_path, monkeypatch):
    root, out = tmp_path / "source", tmp_path / "out"
    encrypted_db(root / "contact.db")
    keys = {"contact.db": KEY.hex()}
    crypto.sync_databases(root, keys, out)
    previous = (out / "contact.db").read_bytes()
    decrypt = crypto._decrypt_file

    def mutate(src, target, key):
        decrypt(src, target, key)
        (out / "contact.db-wal").write_bytes(b"pending output writes")

    monkeypatch.setattr(crypto, "_decrypt_file", mutate)
    result = crypto.sync_databases(root, keys, out, force=True)
    assert result["failed"][0]["code"] == "output_busy"
    assert (out / "contact.db").read_bytes() == previous
