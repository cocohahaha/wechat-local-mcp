from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from Crypto.Cipher import AES


PAGE_SIZE = 4096
RESERVE_SIZE = 80
SQLITE_HEADER = b"SQLite format 3\x00"


def decrypt_bytes(data: bytes, key: bytes) -> bytes:
    """Decrypt a SQLCipher 4 database page-by-page.

    This intentionally does not attempt to modify the source database. The
    caller should run it against a closed-application snapshot when possible.
    """
    if data[:16] == SQLITE_HEADER:
        return data
    if len(data) % PAGE_SIZE:
        raise ValueError("database size is not a multiple of 4096; copy a stable database snapshot first")
    out = bytearray()
    for page_number in range(len(data) // PAGE_SIZE):
        page = data[page_number * PAGE_SIZE:(page_number + 1) * PAGE_SIZE]
        prefix = 16 if page_number == 0 else 0
        ciphertext = page[prefix:PAGE_SIZE - RESERVE_SIZE]
        iv = page[PAGE_SIZE - RESERVE_SIZE:PAGE_SIZE - RESERVE_SIZE + 16]
        plaintext = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)
        if page_number == 0:
            out += SQLITE_HEADER
        out += plaintext
        out += page[PAGE_SIZE - RESERVE_SIZE:]
    if not out.startswith(SQLITE_HEADER):
        raise ValueError("key did not produce a SQLite header")
    return bytes(out)


def _key_for(rel: Path, keys: dict[str, str]) -> str | None:
    candidates = [str(rel), str(rel).replace("\\", "/"), rel.name, rel.stem]
    if rel.parts:
        candidates.extend(["/".join(rel.parts[-2:]), "/".join(rel.parts[-3:])])
    for candidate in candidates:
        if candidate in keys:
            return keys[candidate]
    return None


def sync_databases(db_root: Path, keys: dict[str, str], out_root: Path) -> dict[str, Any]:
    """Decrypt all keyed databases into a separate local snapshot directory."""
    db_root = db_root.expanduser().resolve()
    out_root = out_root.expanduser().resolve()
    if not db_root.is_dir():
        raise FileNotFoundError(f"database directory not found: {db_root}")
    files = sorted(db_root.rglob("*.db"))
    if not files:
        raise FileNotFoundError(f"no .db files found under {db_root}")
    out_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(out_root, 0o700)
    ok: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    for source in files:
        rel = source.relative_to(db_root)
        key_hex = _key_for(rel, keys)
        if not key_hex:
            skipped.append(str(rel))
            continue
        destination = out_root / rel
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        try:
            decoded = decrypt_bytes(source.read_bytes(), bytes.fromhex(key_hex))
            temp_destination = destination.with_suffix(destination.suffix + ".tmp")
            temp_destination.write_bytes(decoded)
            os.chmod(temp_destination, 0o600)
            temp_destination.replace(destination)
            ok.append(str(rel))
        except Exception as exc:  # keep other databases usable
            failed.append({"file": str(rel), "error": str(exc)})
    return {
        "source": str(db_root),
        "output": str(out_root),
        "decrypted": ok,
        "skipped_no_key": skipped,
        "failed": failed,
        "count": len(ok),
    }


def load_key_file(path: Path) -> dict[str, str]:
    data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("key file must contain a JSON object")
    return {str(k): str(v).strip().lower() for k, v in data.items() if isinstance(v, str)}
