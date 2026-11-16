"""Decode WeChat 4.x image attachments (.dat) using only local files.

Image key: WeChat derives it from a numeric account code that also appears
in local file names (``kvcomm`` caches, ``ConfSDK/netbridge/<code>.ini``),
plus the account wxid: ``aes = md5(f"{code}{wxid}").hexdigest()[:16]`` and
``xor = code & 0xFF``. Every candidate is verified by decrypting the first
AES block of real V2 thumbnails, so a wrong guess is never returned. Nothing
reads process memory and nothing goes over the network. Because WeChat
rotates its kvcomm files, a verified key is cached (0600) next to keys.json
and re-verified against the newest thumbnail before every use.
Approach adapted from WeChatDataAnalysis / WeFlow.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
from typing import Any, Iterable

from Crypto.Cipher import AES


V1_MAGIC = b"\x07\x08V1\x08\x07"
V2_MAGIC = b"\x07\x08V2\x08\x07"
V1_AES_KEY = b"cfcd208495d565ef"  # fixed by WeChat for V1 files: md5("0")[:16]
DAT_HEADER_SIZE = 15
AES_BLOCK = 16
FILE_ID_RE = re.compile(rb"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
VARIANT_SUFFIXES = {"hd": "_h.dat", "full": ".dat", "thumb": "_t.dat"}
VARIANT_ORDER = {"full": ("hd", "full", "thumb"), "thumb": ("thumb", "full", "hd")}
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_ACCOUNT_SUFFIX_RE = re.compile(r"^(.+?)_[0-9a-f]{4}$", re.IGNORECASE)
_WXID_SUFFIX_RE = re.compile(r"^(wxid_[^_]+)_.+$", re.IGNORECASE)
_HEVC_PARTITION_START = b"\x00\x00\x00\x01\x40\x01"
_MAX_CODE = 0xFFFFFFFF
FORMAT_MIME = {
    "jpg": "image/jpeg", "png": "image/png", "gif": "image/gif",
    "webp": "image/webp", "wxgf": "application/octet-stream",
}


class ImageKeyUnavailable(RuntimeError):
    """No locally derivable key decrypts this account's V2 images."""


@dataclass(frozen=True)
class ImageKey:
    xor: int
    aes: bytes
    source: str
    verified_templates: int = 0

    def public(self) -> dict[str, Any]:
        """Describe the key without exposing it."""
        return {
            "source": self.source,
            "verified_templates": self.verified_templates,
        }


def detect_format(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"wxgf"):
        return "wxgf"
    return None


def _xor(data: bytes, key: int) -> bytes:
    return bytes(data).translate(bytes(value ^ key for value in range(256)))


def _legacy_xor(data: bytes) -> bytes:
    """Old (pre-4.0) .dat files: the whole file XORed with one byte."""
    for magic in (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF", b"wxgf"):
        if len(data) < len(magic):
            continue
        key = data[0] ^ magic[0]
        if all(data[i] ^ key == magic[i] for i in range(1, len(magic))):
            return _xor(data, key)
    raise ValueError("not a WeChat image .dat file")


def decrypt_dat(data: bytes, key: ImageKey | None) -> bytes:
    """Decrypt V1/V2 (AES-ECB head + raw middle + XOR tail) or legacy XOR files."""
    if data.startswith((V1_MAGIC, V2_MAGIC)):
        if len(data) < DAT_HEADER_SIZE + AES_BLOCK:
            raise ValueError("truncated .dat file")
        if data.startswith(V2_MAGIC):
            if key is None:
                raise ImageKeyUnavailable("V2 image needs the account image key")
            aes_key = key.aes
        else:
            aes_key = V1_AES_KEY
        _, aes_size, xor_size = struct.unpack("<6sLLx", data[:DAT_HEADER_SIZE])
        body = data[DAT_HEADER_SIZE:]
        aes_len = aes_size + (AES_BLOCK - aes_size % AES_BLOCK)  # PKCS7 always pads
        if aes_len > len(body) or xor_size > len(body) - aes_len:
            raise ValueError("corrupt .dat header")
        head = AES.new(aes_key, AES.MODE_ECB).decrypt(body[:aes_len])
        pad = head[-1]
        if not 1 <= pad <= AES_BLOCK or head[-pad:] != bytes([pad]) * pad:
            raise ValueError("image key does not decrypt this file")
        middle = body[aes_len:len(body) - xor_size]
        if xor_size:
            xor_key = key.xor if key is not None else _tail_xor(data)
            if xor_key is None:
                raise ImageKeyUnavailable("V1 image tail needs the account XOR key")
            tail = _xor(body[len(body) - xor_size:], xor_key)
        else:
            tail = b""
        output = head[:-pad] + middle + tail
    else:
        output = _legacy_xor(data)
    if detect_format(output) is None:
        raise ValueError("decoded bytes are not a known image format")
    return output


def _tail_xor(data: bytes) -> int | None:
    """JPEGs end with FF D9; the XOR tail turns that into a key-specific pair."""
    if len(data) < 2:
        return None
    first, second = data[-2] ^ 0xFF, data[-1] ^ 0xD9
    return first if first == second else None


def clean_wxid(value: str) -> str:
    """``wxid_abc_1a2b`` -> ``wxid_abc``; ``name_1a2b`` (account dir) -> ``name``."""
    value = value.strip()
    match = _WXID_SUFFIX_RE.fullmatch(value)
    if match:
        return match.group(1)
    match = _ACCOUNT_SUFFIX_RE.fullmatch(value)
    return match.group(1) if match else value


def wxid_candidates(account: Path) -> list[str]:
    values = [os.environ.get("WECHAT_WXID", ""), account.name]
    seen: list[str] = []
    for value in values:
        for candidate in (clean_wxid(value), value.strip()):
            if candidate and candidate not in seen:
                seen.append(candidate)
    return seen


def code_source_dirs(account: Path) -> list[Path]:
    """Local directories whose file names carry the numeric account code."""
    configured = os.environ.get("WECHAT_IMAGE_KVCOMM_DIR", "").strip()
    if configured:
        return [Path(configured).expanduser()]
    candidates: list[Path] = []
    for parent in (account, *account.parents):
        if parent.name.casefold() == "xwechat_files":
            documents = parent.parent
            candidates += [
                documents / "app_data" / "net" / "kvcomm",  # macOS 4.x
                documents / "xwechat" / "net" / "kvcomm",
                documents / "ConfSDK" / "netbridge",  # <code>.ini, stable
                documents / "ConfSDK" / "netbridge" / "appmsg",
                documents / "ConfSDK" / "kvcomm",
            ]
            caches = documents.parent / "Library" / "Caches" / "com.tencent.xinWeChat"
            if caches.is_dir():
                candidates += sorted(caches.glob("*/kvcomm"))  # 3.x-era kvcomm
            break
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Tencent" / "xwechat" / "net" / "kvcomm")  # Windows
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique and candidate.is_dir():
            unique.append(candidate)
    return unique


def candidate_codes(directories: Iterable[Path]) -> list[int]:
    """Numbers embedded in file names, most frequent first.

    e.g. ``key_<code>_*.statistic`` (Windows), ``key_reportnow_0_<code>_*``
    and ``netbridge/<code>.ini`` (macOS). Every number is only a candidate
    and is verified against real ciphertext before use.
    """
    counts: Counter[int] = Counter()
    for directory in directories:
        try:
            names = [entry.name for entry in os.scandir(directory)]
        except OSError:
            continue
        for name in names:
            for token in re.findall(r"(?<![0-9])\d{5,10}(?![0-9])", name):
                value = int(token)
                if 0 < value <= _MAX_CODE:
                    counts[value] += 1
    return [code for code, _ in counts.most_common()]


def v2_templates(account: Path, limit: int = 8) -> list[tuple[bytes, bytes]]:
    """(first AES block, last two bytes) of the newest V2 thumbnails."""
    attach = account / "msg" / "attach"
    found: list[tuple[float, Path]] = []
    try:
        chats = sorted(
            (entry for entry in os.scandir(attach) if entry.is_dir()),
            key=lambda entry: entry.stat().st_mtime,
            reverse=True,
        )[:200]
    except OSError:
        return []
    for chat in chats:
        try:
            months = sorted(
                (entry.path for entry in os.scandir(chat.path) if _MONTH_RE.match(entry.name)),
                reverse=True,
            )[:2]
        except OSError:
            continue
        for month in months:
            image_dir = Path(month) / "Img"
            if not image_dir.is_dir():
                continue
            for path in image_dir.glob("*_t.dat"):
                try:
                    found.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        if len(found) >= limit * 8:
            break
    templates: list[tuple[bytes, bytes]] = []
    for _, path in sorted(found, reverse=True):
        try:
            with path.open("rb") as stream:
                head = stream.read(DAT_HEADER_SIZE + AES_BLOCK)
                stream.seek(-2, os.SEEK_END)
                tail = stream.read(2)
        except OSError:
            continue
        if head.startswith(V2_MAGIC) and len(head) == DAT_HEADER_SIZE + AES_BLOCK:
            templates.append((head[DAT_HEADER_SIZE:], tail))
        if len(templates) >= limit:
            break
    return templates


def _block_is_image(aes_key: bytes, block: bytes) -> bool:
    return detect_format(AES.new(aes_key, AES.MODE_ECB).decrypt(block)) is not None


def _majority_tail_xor(tails: Iterable[bytes]) -> int | None:
    votes = Counter(
        value for value in (
            (tail[0] ^ 0xFF) if len(tail) == 2 and tail[0] ^ 0xFF == tail[1] ^ 0xD9 else None
            for tail in tails
        ) if value is not None
    )
    return votes.most_common(1)[0][0] if votes else None


def _env_key() -> ImageKey | None:
    aes = os.environ.get("WECHAT_IMAGE_AES_KEY", "").strip()
    xor = os.environ.get("WECHAT_IMAGE_XOR_KEY", "").strip()
    if not aes or not xor:
        return None
    if len(aes) < 16:
        raise ValueError("WECHAT_IMAGE_AES_KEY must be at least 16 characters")
    return ImageKey(xor=int(xor, 0) & 0xFF, aes=aes[:16].encode("ascii"), source="environment")


def _load_cached_key(cache_file: Path | None, account: Path, newest: bytes) -> ImageKey | None:
    if cache_file is None:
        return None
    try:
        entry = json.loads(cache_file.read_text(encoding="utf-8")).get(account.name) or {}
        aes = str(entry["aes"]).encode("ascii")
        xor = int(entry["xor"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if len(aes) != 16 or not 0 <= xor <= 0xFF or not _block_is_image(aes, newest):
        return None
    return ImageKey(xor=xor, aes=aes, source="cache", verified_templates=1)


def _save_cached_key(cache_file: Path | None, account: Path, key: ImageKey) -> None:
    if cache_file is None:
        return
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[account.name] = {"aes": key.aes.decode("ascii"), "xor": key.xor}
    cache_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(cache_file, json.dumps(data, indent=2).encode("utf-8"))


def resolve_image_key(account: Path, cache_file: Path | None = None) -> ImageKey:
    """Find the first account code + wxid pair that decrypts real V2 thumbnails."""
    configured = _env_key()
    if configured is not None:
        return configured
    templates = v2_templates(account)
    if not templates:
        raise ImageKeyUnavailable("本机没有找到 V2 加密的图片缩略图，无法校验图片密钥。")
    newest = templates[0][0]
    cached = _load_cached_key(cache_file, account, newest)
    if cached is not None:
        return cached
    codes = candidate_codes(code_source_dirs(account))
    if not codes:
        raise ImageKeyUnavailable(
            "没有在微信本地缓存的文件名里找到账号 code，无法派生图片密钥。"
            "可以设置 WECHAT_IMAGE_KVCOMM_DIR，或用 WECHAT_IMAGE_AES_KEY/WECHAT_IMAGE_XOR_KEY 手动提供。"
        )
    tail_xor = _majority_tail_xor(tail for _, tail in templates)
    for wxid in wxid_candidates(account):
        for code in codes:
            aes = hashlib.md5(f"{code}{wxid}".encode("utf-8")).hexdigest()[:16].encode("ascii")
            if not _block_is_image(aes, newest):
                continue
            passed = sum(_block_is_image(aes, block) for block, _ in templates)
            if passed * 2 < len(templates):
                continue
            xor = tail_xor if tail_xor is not None else code & 0xFF
            key = ImageKey(xor=xor, aes=aes, source="derived", verified_templates=passed)
            _save_cached_key(cache_file, account, key)
            return key
    raise ImageKeyUnavailable(
        "本地文件名里的候选 code 都无法解开本机 V2 图片；微信可能换了派生方式。"
    )


def file_id_from_packed(packed: Any) -> str | None:
    """The attachment file name stored in ``packed_info_data`` of image rows."""
    if not packed:
        return None
    raw = packed.encode("utf-8", "ignore") if isinstance(packed, str) else bytes(packed)
    match = FILE_ID_RE.search(raw)
    return match.group(0).decode("ascii") if match else None


def attachment_files(
    account: Path, chat_username: str, timestamp: int, file_id: str
) -> dict[str, Path]:
    root = account / "msg" / "attach" / hashlib.md5(chat_username.encode("utf-8")).hexdigest()
    if not root.is_dir():
        return {}
    first = datetime.fromtimestamp(timestamp).strftime("%Y-%m")
    try:
        others = sorted(
            (entry.name for entry in os.scandir(root) if _MONTH_RE.match(entry.name) and entry.name != first),
            reverse=True,
        )
    except OSError:
        others = []
    for month in (first, *others):
        image_dir = root / month / "Img"
        found = {
            variant: image_dir / f"{file_id}{suffix}"
            for variant, suffix in VARIANT_SUFFIXES.items()
            if (image_dir / f"{file_id}{suffix}").is_file()
        }
        if found:
            return found
    return {}


def find_ffmpeg() -> str | None:
    configured = os.environ.get("WECHAT_FFMPEG", "").strip()
    if configured:
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(candidate).is_file():
            return candidate
    return None


def wxgf_to_jpeg(data: bytes, timeout: float = 20.0) -> bytes | None:
    """wxgf wraps HEVC; hand the colour partition to ffmpeg and keep frame 1."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    starts = []
    position = data.find(_HEVC_PARTITION_START, 4)
    while position >= 0 and len(starts) < 4:
        starts.append(position)
        position = data.find(_HEVC_PARTITION_START, position + len(_HEVC_PARTITION_START))
    if not starts:
        return None
    start = starts[-1]
    size = int.from_bytes(data[start - 4:start], "big")
    stream = data[start:start + size] if 0 < size <= len(data) - start else data[start:]
    try:
        process = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "hevc", "-i", "pipe:0",
             "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "pipe:1"],
            input=stream, capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = process.stdout
    return output if process.returncode == 0 and detect_format(output) == "jpg" else None


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(name, 0o600)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


class ImageDecoder:
    """Decode and cache image attachments for one WeChat account."""

    def __init__(self, account: Path | None, cache: Path, key_file: Path | None = None):
        self.account = account
        self.cache = cache.expanduser()
        self.key_file = key_file
        self._key: ImageKey | None = None
        self._key_error: str | None = None

    def key(self) -> ImageKey | None:
        if self._key is None and self._key_error is None and self.account is not None:
            try:
                self._key = resolve_image_key(self.account, self.key_file)
            except (ImageKeyUnavailable, ValueError) as exc:
                self._key_error = str(exc)
        return self._key

    def status(self) -> dict[str, Any]:
        key = self.key()
        return {
            "image_key_ready": key is not None,
            "image_key": key.public() if key else None,
            "image_key_error": self._key_error,
            "ffmpeg_available": find_ffmpeg() is not None,
            "decoded_image_cache": str(self.cache),
        }

    def decode(
        self,
        chat_username: str,
        timestamp: int,
        file_id: str | None,
        *,
        quality: str = "full",
    ) -> dict[str, Any]:
        """Decode one image message to a cached plain file; best variant first."""
        if self.account is None:
            raise RuntimeError("未找到本机微信账号目录。")
        if not file_id:
            raise RuntimeError("这条图片消息没有附件文件名（packed_info_data 为空）。")
        files = attachment_files(self.account, chat_username, timestamp, file_id)
        if not files:
            raise RuntimeError("本机没有这张图片的附件文件，可能只在手机上，或还没在电脑微信里加载过。")
        errors: list[str] = []
        for variant in VARIANT_ORDER.get(quality, VARIANT_ORDER["full"]):
            source = files.get(variant)
            if source is None:
                continue
            try:
                return self._decode_file(chat_username, file_id, variant, source)
            except ImageKeyUnavailable as exc:
                raise RuntimeError(f"图片密钥不可用：{self._key_error or exc}") from exc
            except (OSError, ValueError) as exc:
                errors.append(f"{variant}: {exc}")
        raise RuntimeError("图片附件无法解码：" + "; ".join(errors))

    def _decode_file(self, chat_username: str, file_id: str, variant: str, source: Path) -> dict[str, Any]:
        chat_hash = hashlib.md5(chat_username.encode("utf-8")).hexdigest()
        stem = f"{file_id}_{variant}"
        directory = self.cache / chat_hash
        source_mtime = source.stat().st_mtime
        for existing in directory.glob(f"{stem}.*") if directory.is_dir() else ():
            if existing.stat().st_mtime >= source_mtime and existing.suffix != ".tmp":
                return {
                    "path": str(existing), "variant": variant, "format": existing.suffix[1:],
                    "source": str(source), "cached": True,
                }
        data = source.read_bytes()
        key = self.key() if data.startswith(V2_MAGIC) or data.startswith(V1_MAGIC) else None
        if data.startswith(V2_MAGIC) and key is None:
            raise ImageKeyUnavailable(self._key_error or "image key unavailable")
        plain = decrypt_dat(data, key)
        image_format = detect_format(plain) or "bin"
        note = None
        if image_format == "wxgf":
            converted = wxgf_to_jpeg(plain)
            if converted is None:
                raise ValueError("wxgf (HEVC) 图片需要 ffmpeg 才能转换；请安装 ffmpeg 或设置 WECHAT_FFMPEG")
            plain, image_format, note = converted, "jpg", "converted_from_wxgf"
        target = directory / f"{stem}.{image_format}"
        _write_private(target, plain)
        os.chmod(directory, 0o700)
        os.chmod(self.cache, 0o700)
        result = {
            "path": str(target), "variant": variant, "format": image_format,
            "source": str(source), "cached": False,
        }
        if note:
            result["note"] = note
        return result
