from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
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


def _assert_idle(source: Path, *, output: bool = False) -> None:
    for suffix in ("-wal", "-journal"):
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
    """Refresh changed files only. Requires a stable, checkpointed source.

    This is file-level incremental sync, not page/WAL replay. Source metadata,
    key fingerprint and destination metadata must all match to skip a file.
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
        "skipped_no_key": [], "failed": [], "bytes_processed": 0, "bytes_planned": 0,
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
                _assert_idle(source)
                _assert_idle(destination, output=True)
                signature = _signature(source)
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
                        and old.get("key_fingerprint") == fingerprint and destination.is_file()
                        and old.get("output") == _signature(destination)):
                    _assert_idle(source)
                    _assert_idle(destination, output=True)
                    if signature != _signature(source):
                        raise SourceBusyError("source changed during sync; fully quit WeChat and retry")
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
                _decrypt_file(source, temporary, key)
                _verify_sqlite(temporary)
                _assert_idle(source)
                _assert_idle(destination, output=True)
                if signature != _signature(source):
                    raise SourceBusyError("source changed during sync; fully quit WeChat and retry")
                temporary.replace(destination)
                manifest["files"][name] = {"source": signature, "key_fingerprint": fingerprint,
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
