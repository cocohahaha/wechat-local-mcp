from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable

from Crypto.Cipher import AES


PAGE_SIZE = 4096
RESERVE_SIZE = 80
SQLITE_HEADER = b"SQLite format 3\x00"
CHUNK_SIZE = 1024 * 1024
MANIFEST_VERSION = 1
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
WAL_MAGICS = {0x377F0682: "<", 0x377F0683: ">"}
WAL_VERSION = 3007000
WAL_COPY_ATTEMPTS = 4


class SourceBusyError(ValueError):
    """A live source cannot be safely treated as a complete snapshot."""


class OutputBusyError(ValueError):
    """A snapshot with pending writes must not be replaced or reused."""


def _decrypt_page(page: bytes, key: bytes, first: bool) -> bytes:
    prefix = 16 if first else 0
    iv = page[PAGE_SIZE - RESERVE_SIZE:PAGE_SIZE - RESERVE_SIZE + 16]
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(page[prefix:PAGE_SIZE - RESERVE_SIZE])
    result = (SQLITE_HEADER if first else b"") + plain + page[-RESERVE_SIZE:]
    if first:
        # Validate decrypted bytes, not the header we inserted ourselves.
        if (result[16:18] != b"\x10\x00" or result[18] not in (1, 2)
                or result[19] not in (1, 2) or result[20:24] != b"\x50\x40\x20\x20"):
            raise ValueError("invalid decrypted SQLite header: wrong key or unsupported database format")
    return result


def decrypt_bytes(data: bytes, key: bytes) -> bytes:
    """Decrypt a SQLCipher 4 database page-by-page.

    This intentionally does not attempt to modify the source database. The
    caller should run it against a closed-application snapshot when possible.
    """
    if data[:16] == SQLITE_HEADER:
        return data
    if not data or len(data) % PAGE_SIZE:
        raise ValueError("database size is not a multiple of 4096; copy a stable database snapshot first")
    if len(key) != 32:
        raise ValueError("database key must be 32 bytes")
    return b"".join(_decrypt_page(data[i:i + PAGE_SIZE], key, i == 0)
                    for i in range(0, len(data), PAGE_SIZE))


def _decrypt_file(source: Path, destination: Path, key: bytes) -> None:
    """Bound memory independently of database size; never modify source files."""
    with source.open("rb") as src, destination.open("wb") as dst:
        first = src.read(PAGE_SIZE)
        if first.startswith(SQLITE_HEADER):
            dst.write(first)
            shutil.copyfileobj(src, dst, CHUNK_SIZE)
            return
        if len(first) != PAGE_SIZE or len(key) != 32:
            raise ValueError("invalid database size or key length")
        dst.write(_decrypt_page(first, key, True))
        while chunk := src.read(CHUNK_SIZE):
            if len(chunk) % PAGE_SIZE:
                raise ValueError("database size changed or is not a multiple of 4096")
            decoded = bytearray(len(chunk))
            for offset in range(0, len(chunk), PAGE_SIZE):
                decoded[offset:offset + PAGE_SIZE] = _decrypt_page(chunk[offset:offset + PAGE_SIZE], key, False)
            dst.write(decoded)


def _verify_sqlite(path: Path) -> None:
    # A bounded schema read, not an expensive full integrity scan. immutable
    # avoids creating -wal/-shm files next to a read-only snapshot.
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    finally:
        connection.close()


def _signature(path: Path) -> dict[str, Any]:
    stat = path.stat()
    with path.open("rb") as stream:
        prefix = stream.read(16)
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "ctime_ns": stat.st_ctime_ns,
            "inode": stat.st_ino, "device": stat.st_dev, "prefix": prefix.hex()}


def _live_wal(source: Path) -> Path | None:
    wal = Path(str(source) + "-wal")
    try:
        return wal if wal.stat().st_size else None
    except FileNotFoundError:
        return None


def _wal_checksum(data: bytes, endian: str, state: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    a, b = state
    for x, y in struct.iter_unpack(endian + "II", data):
        a = (a + x + b) & 0xFFFFFFFF
        b = (b + y + a) & 0xFFFFFFFF
    return a, b


def _apply_wal(database: Path, wal: Path) -> dict[str, Any]:
    """Replay committed WAL frames into a private copy of the database.

    Follows SQLite's own recovery rule: frames are read until the first salt
    mismatch, bad checksum or short frame, and only frames up to the last
    commit record are applied. SQLCipher checksums the encrypted page bytes,
    so this works on the still-encrypted copy. Never touches WeChat's files.
    """
    info = {"wal_bytes": wal.stat().st_size, "committed_frames": 0, "applied_pages": 0}
    with wal.open("rb") as stream:
        header = stream.read(WAL_HEADER_SIZE)
        if len(header) < WAL_HEADER_SIZE:
            raise SourceBusyError("WAL header is incomplete; WeChat may be writing, retry in a moment")
        magic, version, page_size = struct.unpack(">III", header[:12])
        endian = WAL_MAGICS.get(magic)
        if endian is None or version != WAL_VERSION:
            raise SourceBusyError("unrecognized WAL header; fully quit WeChat and retry, do not delete its WAL files")
        with database.open("rb") as db_stream:
            db_head = db_stream.read(18)
        if db_head[:16] == SQLITE_HEADER:
            expected = int.from_bytes(db_head[16:18], "big")
            expected = 65536 if expected == 1 else expected
        else:
            expected = PAGE_SIZE
        if page_size != expected:
            raise ValueError(f"WAL page size {page_size} does not match database page size {expected}")
        state = _wal_checksum(header[:24], endian)
        if state != struct.unpack(">II", header[24:32]):
            raise SourceBusyError("WAL header checksum mismatch; retry in a moment")
        salt = header[16:24]
        pending: dict[int, int] = {}
        committed: dict[int, int] = {}
        db_pages: int | None = None
        frames = 0
        while True:
            offset = stream.tell()
            frame_header = stream.read(WAL_FRAME_HEADER_SIZE)
            page = stream.read(page_size)
            if len(frame_header) < WAL_FRAME_HEADER_SIZE or len(page) < page_size:
                break
            if frame_header[8:16] != salt:
                break  # leftover frames from a previous WAL generation
            state = _wal_checksum(page, endian, _wal_checksum(frame_header[:8], endian, state))
            if state != struct.unpack(">II", frame_header[16:24]):
                break  # torn or half-written frame ends the valid log, as in SQLite
            pgno, commit_size = struct.unpack(">II", frame_header[:8])
            if pgno == 0:
                break
            frames += 1
            pending[pgno] = offset + WAL_FRAME_HEADER_SIZE
            if commit_size:
                committed.update(pending)
                pending.clear()
                db_pages = commit_size
                info["committed_frames"] = frames
        if db_pages is None:
            return info
        committed = {pgno: at for pgno, at in committed.items() if pgno <= db_pages}
        existing = database.stat().st_size // page_size
        missing = [pgno for pgno in range(existing + 1, db_pages + 1) if pgno not in committed]
        if missing:
            raise SourceBusyError("WAL commit is missing database extension pages; retry in a moment")
        with database.open("r+b") as target:
            target.truncate(db_pages * page_size)
            for pgno, at in sorted(committed.items()):
                stream.seek(at)
                target.seek((pgno - 1) * page_size)
                target.write(stream.read(page_size))
        info["applied_pages"] = len(committed)
        info["database_pages"] = db_pages
    return info


def _wal_snapshot(source: Path, work: Path) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Copy database + WAL as one consistent point in time, then replay the WAL.

    On APFS the copies are clones, so the window in which WeChat could write
    between the two copies is tiny; signatures before and after must match.
    """
    database_copy, wal_copy = work / "database.enc", work / "database.enc-wal"
    for attempt in range(WAL_COPY_ATTEMPTS):
        wal = _live_wal(source)  # a checkpoint may have emptied it meanwhile
        try:
            before = (_signature(source), _signature(wal) if wal else None)
            shutil.copyfile(source, database_copy)
            if wal:
                shutil.copyfile(wal, wal_copy)
            after_wal = _live_wal(source)
            after = (_signature(source), _signature(after_wal) if after_wal else None)
        except FileNotFoundError:
            before, after = (None, None), None
        if before == after:
            info = _apply_wal(database_copy, wal_copy) if wal else {"wal_bytes": 0}
            wal_copy.unlink(missing_ok=True)
            return database_copy, before[0], before[1], info
        time.sleep(0.15 * (attempt + 1))
    raise SourceBusyError("database kept changing while copying its WAL snapshot; retry in a moment")


def _assert_idle(source: Path, *, output: bool = False, allow_wal: bool = False) -> None:
    for suffix in ("-wal", "-journal"):
        if suffix == "-wal" and allow_wal:
            continue
        sidecar = Path(str(source) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            if suffix == "-journal":
                with sidecar.open("rb") as stream:
                    header = stream.read(28)
                if header == b"\x00" * 28:
                    # SQLite PERSIST mode zeros the header after commit, but
                    # deliberately leaves the (now cold) journal file in place.
                    continue
            if output:
                raise OutputBusyError("snapshot has pending WAL/journal writes: close the program writing this snapshot, then retry; do not delete its log files")
            raise SourceBusyError("non-empty WAL/journal: fully quit WeChat, then retry; do not delete its WAL files")


@contextmanager
def _sync_lock(root: Path):
    path = root / ".sync.lock"
    if path.is_symlink():
        raise ValueError("sync lock must not be a symlink")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("another sync is already running for this output directory") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == MANIFEST_VERSION and isinstance(data.get("files"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": MANIFEST_VERSION, "files": {}}


def _write_manifest(path: Path, data: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=".sync-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def _selected(rel: Path, profile: str) -> bool:
    normalized = rel.as_posix()
    chat = normalized == "contact/contact.db" or bool(re.fullmatch(r"message/message_\d+\.db", normalized))
    media = bool(re.fullmatch(r"message/media_\d+\.db", normalized)) or normalized.startswith("emoticon/")
    return profile == "all" or chat or (profile == "media" and media)


def _key_for(rel: Path, keys: dict[str, str]) -> str | None:
    candidates = [str(rel), str(rel).replace("\\", "/"), rel.name, rel.stem]
    if rel.parts:
        candidates.extend(["/".join(rel.parts[-2:]), "/".join(rel.parts[-3:])])
    for candidate in candidates:
        if candidate in keys:
            return keys[candidate]
    return None


def sync_databases(
    db_root: Path, keys: dict[str, str], out_root: Path, *, profile: str = "all",
    force: bool = False, dry_run: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Refresh changed files only; WeChat may keep running.

    A database with a live WAL is snapshotted together with its WAL into a
    private temporary copy, and committed WAL frames are replayed there before
    decryption. Source files are only ever read. Source metadata (database and
    WAL), key fingerprint and destination metadata must all match to skip a file.
    """
    started = time.perf_counter()
    if profile not in ("chat", "media", "all"):
        raise ValueError("profile must be chat, media or all")
    db_root = db_root.expanduser().resolve()
    out_root = out_root.expanduser().resolve()
    if out_root.is_relative_to(db_root) or db_root.is_relative_to(out_root):
        raise ValueError("source and output directories must not overlap")
    if not db_root.is_dir():
        raise FileNotFoundError(f"database directory not found: {db_root}")
    files = sorted(db_root.rglob("*.db"))
    if not files:
        raise FileNotFoundError(f"no .db files found under {db_root}")
    if not any(_selected(source.relative_to(db_root), profile) for source in files):
        raise ValueError(f"no databases match profile={profile}; point --db-dir to the account's db_storage directory")
    if not dry_run:
        out_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(out_root, 0o700)
    result: dict[str, Any] = {
        "source": str(db_root), "output": str(out_root), "profile": profile, "dry_run": dry_run,
        "decrypted": [], "unchanged": [], "planned": [], "deferred": [],
        "skipped_no_key": [], "failed": [], "wal_replayed": [], "bytes_processed": 0, "bytes_planned": 0,
    }
    with nullcontext() if dry_run else _sync_lock(out_root):
        manifest_path = out_root / ".sync-manifest.json"
        if manifest_path.is_symlink():
            raise ValueError("sync manifest must not be a symlink")
        manifest = _read_manifest(manifest_path)
        if manifest.get("source") not in (None, str(db_root)):
            raise ValueError("output belongs to another account/source; choose a separate output directory")
        manifest["source"] = str(db_root)
        for source in files:
            rel = source.relative_to(db_root)
            name = rel.as_posix()
            if not _selected(rel, profile):
                result["deferred"].append(name)
                continue
            temporary: Path | None = None
            try:
                destination = out_root / rel
                if not source.resolve().is_relative_to(db_root) or not destination.resolve().is_relative_to(out_root):
                    raise ValueError("database symlink escapes source/output directory")
                _assert_idle(source, allow_wal=True)  # a hot rollback journal still blocks
                _assert_idle(destination, output=True)
                wal = _live_wal(source)
                signature = _signature(source)
                wal_signature = _signature(wal) if wal else None
                plain = signature["prefix"] == SQLITE_HEADER.hex()
                key_hex = _key_for(rel, keys)
                if not key_hex and not plain:
                    result["skipped_no_key"].append(name)
                    continue
                key = b"" if plain else bytes.fromhex(key_hex or "")
                if not plain and len(key) != 32:
                    raise ValueError("database key must be 64 hex characters")
                fingerprint = hashlib.sha256(key).hexdigest()
                old = manifest["files"].get(name, {})
                if (not force and isinstance(old, dict) and old.get("source") == signature
                        and old.get("wal") == wal_signature
                        and old.get("key_fingerprint") == fingerprint and destination.is_file()
                        and old.get("output") == _signature(destination)):
                    _assert_idle(source, allow_wal=wal is not None)
                    _assert_idle(destination, output=True)
                    current_wal = _live_wal(source)
                    if signature != _signature(source) or wal_signature != (_signature(current_wal) if current_wal else None):
                        raise SourceBusyError("source changed during sync; retry in a moment")
                    result["unchanged"].append(name)
                    continue
                result["bytes_planned"] += signature["size"]
                if dry_run:
                    result["planned"].append(name)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(destination.parent, 0o700)
                if progress:
                    progress({"file": name, "bytes": signature["size"], "event": "decrypting"})
                fd, temp_name = tempfile.mkstemp(prefix=".decrypt-", suffix=".tmp", dir=destination.parent)
                os.close(fd)
                temporary = Path(temp_name)
                if wal is None:
                    _decrypt_file(source, temporary, key)
                    _verify_sqlite(temporary)
                    _assert_idle(source)
                    _assert_idle(destination, output=True)
                    if signature != _signature(source):
                        raise SourceBusyError("source changed during sync; retry in a moment")
                else:
                    with tempfile.TemporaryDirectory(prefix=".wal-", dir=destination.parent) as work:
                        snapshot, signature, wal_signature, wal_info = _wal_snapshot(source, Path(work))
                        _decrypt_file(snapshot, temporary, key)
                    _verify_sqlite(temporary)
                    _assert_idle(destination, output=True)
                    result["wal_replayed"].append({"file": name, **wal_info})
                temporary.replace(destination)
                manifest["files"][name] = {"source": signature, "wal": wal_signature,
                                           "key_fingerprint": fingerprint,
                                           "output": _signature(destination)}
                result["decrypted"].append(name)
                result["bytes_processed"] += signature["size"]
                # Persist each completed file so an interrupted first sync resumes.
                _write_manifest(manifest_path, manifest)
            except Exception as exc:  # failed files never replace a usable snapshot
                code = "source_busy" if isinstance(exc, SourceBusyError) else "output_busy" if isinstance(exc, OutputBusyError) else "sync_failed"
                result["failed"].append({"file": name, "error": str(exc), "code": code})
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    result["count"] = len(result["decrypted"])
    result["unchanged_count"] = len(result["unchanged"])
    result["complete"] = not result["failed"] and not result["skipped_no_key"]
    result["elapsed_seconds"] = round(time.perf_counter() - started, 4)
    return result


def load_key_file(path: Path) -> dict[str, str]:
    data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("key file must contain a JSON object")
    return {str(k): str(v).strip().lower() for k, v in data.items() if isinstance(v, str)}
