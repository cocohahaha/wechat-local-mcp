from __future__ import annotations

import json
import os
import plistlib
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


if sys.platform == "win32":
    CONTAINER_DEFAULT = Path.home() / "Documents"
    DECRYPTED_DEFAULT = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        / "wechat-local-vault/decrypted/current"
    )
    KEYS_DEFAULT = (
        Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
        / "wechat-local-mcp/keys.json"
    )
else:
    CONTAINER_DEFAULT = Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents"
    DECRYPTED_DEFAULT = Path.home() / "Library/Application Support/wechat-local-vault/decrypted/current"
    KEYS_DEFAULT = Path.home() / ".config/wechat-local-mcp/keys.json"
LEGACY_KEYS_DEFAULT = Path.home() / ".config/wechat-keys.json"


@dataclass(frozen=True)
class Paths:
    container: Path
    account_dir: Path | None
    decrypted_dir: Path
    keys_file: Path


def container_dir() -> Path:
    return Path(os.environ.get("WECHAT_CONTAINER_DIR", str(CONTAINER_DEFAULT))).expanduser()


def decrypted_dir() -> Path:
    return Path(os.environ.get("WECHAT_DECRYPTED_DIR", str(DECRYPTED_DEFAULT))).expanduser()


def keys_file() -> Path:
    configured = os.environ.get("WECHAT_KEYS_FILE")
    if configured:
        return Path(configured).expanduser()
    if KEYS_DEFAULT.exists() or not LEGACY_KEYS_DEFAULT.exists():
        return KEYS_DEFAULT
    return LEGACY_KEYS_DEFAULT


def account_dir(container: Path | None = None) -> Path | None:
    root = container or container_dir()
    xwechat = root if root.name.casefold() == "xwechat_files" else root / "xwechat_files"
    if not xwechat.is_dir():
        return None
    candidates = [
        p for p in xwechat.iterdir()
        if p.is_dir() and (p / "db_storage").is_dir()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def paths() -> Paths:
    root = container_dir()
    return Paths(root, account_dir(root), decrypted_dir(), keys_file())


def wechat_version() -> str | None:
    if sys.platform != "darwin":
        return None
    info = Path("/Applications/WeChat.app/Contents/Info.plist")
    if not info.exists():
        return None
    try:
        data = plistlib.loads(info.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return None
    value = data.get("CFBundleShortVersionString")
    return str(value) if value else None


def _db_inventory(root: Path | None) -> dict[str, Any]:
    if root is None:
        return {"count": 0, "bytes": 0, "files": []}
    db_root = root / "db_storage"
    files = sorted(db_root.rglob("*.db")) if db_root.is_dir() else []
    return {
        "count": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        "files": [str(p.relative_to(db_root)) for p in files[:200]],
    }


def status() -> dict[str, Any]:
    p = paths()
    encrypted = _db_inventory(p.account_dir)
    decrypted_files = sorted(p.decrypted_dir.rglob("*.db")) if p.decrypted_dir.is_dir() else []
    return {
        "platform": sys.platform,
        "wechat_version": wechat_version(),
        "container_dir": str(p.container),
        "account_dir": str(p.account_dir) if p.account_dir else None,
        "encrypted_db": encrypted,
        "decrypted_dir": str(p.decrypted_dir),
        "decrypted_db": {
            "count": len(decrypted_files),
            "bytes": sum(x.stat().st_size for x in decrypted_files),
            "has_contacts": (p.decrypted_dir / "contact/contact.db").exists(),
            "has_messages": bool(list((p.decrypted_dir / "message").glob("message_*.db"))) if (p.decrypted_dir / "message").is_dir() else False,
        },
        "keys_file": str(p.keys_file),
        "keys_available": p.keys_file.exists(),
        "ready": (p.decrypted_dir / "contact/contact.db").exists() and bool(list((p.decrypted_dir / "message").glob("message_*.db"))) if (p.decrypted_dir / "message").is_dir() else False,
        "privacy": {
            "network": "none required",
            "write_access": "only the configured decrypted snapshot and local state file",
            "message_writes": False,
        },
    }


def load_keys(path: Path | None = None) -> dict[str, str]:
    target = path or keys_file()
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("keys file must be a JSON object mapping database paths to 64-character hex keys")
    result: dict[str, str] = {}
    for name, value in data.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        normalized = value.strip().lower()
        if len(normalized) == 64 and all(c in "0123456789abcdef" for c in normalized):
            result[name] = normalized
    if not result:
        raise ValueError(f"No valid 64-character database keys found in {target}")
    return result
