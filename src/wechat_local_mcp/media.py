"""Convert locally cached WeChat image and voice messages into text."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterator
import wave

from .config import account_dir, container_dir, decrypted_dir, media_text_dir
from .store import ChatStore, split_type, table_for


VOICE_SAMPLE_RATE = 24_000


def _read_only_connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _cache_key(
    *,
    chat_username: str,
    local_id: int,
    timestamp: int,
    media_kind: str,
    engine_key: str,
) -> str:
    value = "|".join(
        (
            chat_username,
            str(local_id),
            str(timestamp),
            media_kind,
            engine_key,
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        temporary = Path(handle.name)
    temporary.replace(path)


def _image_candidates(
    account: Path,
    chat_username: str,
    local_id: int,
    timestamp: int,
) -> list[Path]:
    month = datetime.fromtimestamp(timestamp).strftime("%Y-%m")
    chat_hash = table_for(chat_username).removeprefix("Msg_")
    root = account / "cache" / month / "Message" / chat_hash
    candidates: list[Path] = []
    for directory in ("ImageTemp", "Image", "Thumb"):
        media_dir = root / directory
        if not media_dir.is_dir():
            continue
        candidates.extend(media_dir.glob(f"{local_id}_{timestamp}*"))
    files = [
        path
        for path in candidates
        if path.is_file() and path.stat().st_size > 64
    ]
    return sorted(
        files,
        key=lambda path: (
            "thumb" not in path.name.casefold(),
            "hd" in path.name.casefold(),
            path.stat().st_size,
        ),
        reverse=True,
    )


def _ocr_image(path: Path) -> dict[str, Any]:
    try:
        from .ocr import recognize_text
    except ImportError as exc:
        raise RuntimeError(
            "图片 OCR 依赖未安装。请运行 uv sync --extra ui --extra media。"
        ) from exc
    blocks = recognize_text(path)
    lines = [block.text.strip() for block in blocks if block.text.strip()]
    confidences = [block.confidence for block in blocks if block.text.strip()]
    return {
        "text": "\n".join(lines),
        "engine": "macos_vision",
        "confidence": (
            round(sum(confidences) / len(confidences), 4)
            if confidences
            else 0.0
        ),
        "blocks": [block.model_dump() for block in blocks],
    }


def _voice_blob(
    snapshot: Path,
    chat_username: str,
    local_id: int,
    timestamp: int,
) -> bytes | None:
    path = snapshot / "message/media_0.db"
    if not path.is_file():
        return None
    with _read_only_connect(path) as connection:
        name = connection.execute(
            "SELECT rowid FROM Name2Id WHERE user_name = ?",
            (chat_username,),
        ).fetchone()
        if name is None:
            return None
        rows = connection.execute(
            """
            SELECT voice_data
            FROM VoiceInfo
            WHERE chat_name_id = ? AND local_id = ? AND create_time = ?
              AND length(voice_data) > 0
            ORDER BY length(voice_data) DESC
            """,
            (int(name["rowid"]), local_id, timestamp),
        ).fetchall()
    if not rows:
        return None
    return bytes(rows[0]["voice_data"])


@contextmanager
def _voice_wav(voice_data: bytes) -> Iterator[Path]:
    try:
        import pilk
    except ImportError as exc:
        raise RuntimeError(
            "微信 SILK 解码器未安装。请运行 uv sync --extra media。"
        ) from exc
    if not voice_data.startswith((b"\x02#!SILK_V3", b"#!SILK_V3")):
        raise RuntimeError("语音数据不是受支持的微信 SILK V3 格式。")
    with tempfile.TemporaryDirectory(prefix="wechat-local-voice-") as directory:
        silk_path = Path(directory) / "message.silk"
        pcm_path = Path(directory) / "message.pcm"
        wav_path = Path(directory) / "message.wav"
        silk_path.write_bytes(voice_data)
        pilk.decode(str(silk_path), str(pcm_path), pcm_rate=VOICE_SAMPLE_RATE)
        with pcm_path.open("rb") as pcm, wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(VOICE_SAMPLE_RATE)
            wav.writeframes(pcm.read())
        yield wav_path


def _transcribe_voice(
    voice_data: bytes,
    *,
    language: str,
    model: str,
) -> dict[str, Any]:
    # Xet opens many parallel connections and can stall on managed/corporate
    # networks. Standard Hugging Face HTTPS downloads are slower but reliable.
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    try:
        import mlx_whisper
    except ImportError as exc:
        raise RuntimeError(
            "本地语音识别依赖未安装。请运行 uv sync --extra media。"
        ) from exc
    with _voice_wav(voice_data) as wav_path:
        result = mlx_whisper.transcribe(
            str(wav_path),
            path_or_hf_repo=model,
            language=language,
            task="transcribe",
            verbose=False,
            condition_on_previous_text=False,
        )
    text = str(result.get("text") or "").strip()
    segments = result.get("segments") or []
    log_probabilities = [
        float(segment["avg_logprob"])
        for segment in segments
        if isinstance(segment, dict)
        and isinstance(segment.get("avg_logprob"), (int, float))
    ]
    confidence = (
        round(math.exp(sum(log_probabilities) / len(log_probabilities)), 4)
        if log_probabilities
        else None
    )
    return {
        "text": text,
        "engine": "mlx_whisper",
        "model": model,
        "language": str(result.get("language") or language),
        "confidence": confidence,
        "review_required": True,
        "segments": [
            {
                "start": segment.get("start"),
                "end": segment.get("end"),
                "text": str(segment.get("text") or "").strip(),
            }
            for segment in segments
            if isinstance(segment, dict)
        ],
    }


class MediaTextExtractor:
    """Extract and locally cache text derived from WeChat media messages."""

    def __init__(
        self,
        *,
        snapshot: Path | None = None,
        account: Path | None = None,
        cache: Path | None = None,
    ):
        self.snapshot = (snapshot or decrypted_dir()).expanduser().resolve()
        self.account = account or account_dir(container_dir())
        self.cache = (cache or media_text_dir()).expanduser().resolve()
        self.store = ChatStore(self.snapshot)

    def status(self) -> dict[str, Any]:
        voice_database = self.snapshot / "message/media_0.db"
        voice_count = 0
        if voice_database.is_file():
            with _read_only_connect(voice_database) as connection:
                row = connection.execute(
                    "SELECT count(*) AS count FROM VoiceInfo "
                    "WHERE length(voice_data) > 0"
                ).fetchone()
                voice_count = int(row["count"]) if row else 0
        cached_count = (
            sum(1 for _ in self.cache.glob("*.json"))
            if self.cache.is_dir()
            else 0
        )
        return {
            "ready": self.store.ready(),
            "snapshot": str(self.snapshot),
            "account_dir": str(self.account) if self.account else None,
            "voice_database": str(voice_database),
            "voice_messages_available": voice_count,
            "image_cache_available": bool(
                self.account and (self.account / "cache").is_dir()
            ),
            "pilk_installed": importlib.util.find_spec("pilk") is not None,
            "mlx_whisper_installed": (
                importlib.util.find_spec("mlx_whisper") is not None
            ),
            "vision_ocr_installed": (
                importlib.util.find_spec("Vision") is not None
            ),
            "cached_text_items": cached_count,
            "privacy": {
                "audio_uploaded": False,
                "images_uploaded": False,
                "derived_text_cache": str(self.cache),
            },
        }

    def _derive_message(
        self,
        *,
        chat: dict[str, Any],
        message: dict[str, Any],
        language: str,
        model: str,
        refresh: bool,
    ) -> dict[str, Any]:
        """Return one cached or newly derived media-text result."""

        local_id = int(message["local_id"])
        timestamp = int(message["timestamp"])
        base_type, _ = split_type(message["local_type"])
        if base_type not in (3, 34):
            raise ValueError("message is not an image or voice message")
        kind = "image" if base_type == 3 else "voice"
        username = str(chat["username"])
        engine_key = (
            f"{model}:{language}"
            if kind == "voice"
            else "macos_vision"
        )
        key = _cache_key(
            chat_username=username,
            local_id=local_id,
            timestamp=timestamp,
            media_kind=kind,
            engine_key=engine_key,
        )
        cache_path = self.cache / f"{key}.json"
        cached = None if refresh else _safe_json(cache_path)
        if cached is not None:
            return cached | {"cached": True}

        base = {
            "chat": chat,
            "local_id": local_id,
            "local_type": message["local_type"],
            "media_kind": kind,
            "sender": message["sender"],
            "sender_username": message["sender_username"],
            "timestamp": timestamp,
            "time": message["time"],
        }
        try:
            if kind == "image":
                if self.account is None:
                    raise RuntimeError("未找到本机微信账号缓存目录。")
                candidates = _image_candidates(
                    self.account,
                    username,
                    local_id,
                    timestamp,
                )
                if not candidates:
                    raise RuntimeError(
                        "本地没有该图片缓存。请在微信中打开一次原图后重试。"
                    )
                derived = _ocr_image(candidates[0])
                result = base | {
                    "status": "ok",
                    "media_source": str(candidates[0]),
                    "derived_text": derived,
                }
            else:
                voice_data = _voice_blob(
                    self.snapshot,
                    username,
                    local_id,
                    timestamp,
                )
                if not voice_data:
                    raise RuntimeError("明文快照中没有找到该语音数据。")
                derived = _transcribe_voice(
                    voice_data,
                    language=language,
                    model=model,
                )
                result = base | {
                    "status": "ok",
                    "media_source": "decrypted_snapshot:media_0.db",
                    "derived_text": derived,
                }
            _write_json(cache_path, result)
            return result | {"cached": False}
        except Exception as exc:
            return base | {
                "status": "unavailable",
                "error": str(exc),
                "derived_text": None,
                "cached": False,
            }

    def enrich_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        default_chat: dict[str, Any] | None = None,
        language: str,
        model: str,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Automatically replace image/voice placeholders with derived text."""

        enriched: list[dict[str, Any]] = []
        for message in messages:
            base_type, _ = split_type(message.get("local_type"))
            if base_type not in (3, 34):
                enriched.append(message)
                continue
            chat = message.get("chat") or default_chat
            if not isinstance(chat, dict) or not chat.get("username"):
                enriched.append(
                    message
                    | {
                        "media": {
                            "status": "unavailable",
                            "error": "媒体消息缺少聊天标识，无法定位本机缓存。",
                        }
                    }
                )
                continue
            result = self._derive_message(
                chat=chat,
                message=message,
                language=language,
                model=model,
                refresh=refresh,
            )
            item = message | {
                "original_content": message.get("content", ""),
                "media": result,
            }
            derived = result.get("derived_text") or {}
            text = str(derived.get("text") or "").strip()
            if result.get("status") == "ok" and text:
                item["content"] = text
                item["content_source"] = (
                    "image_ocr" if base_type == 3 else "voice_transcript"
                )
            enriched.append(item)
        return enriched

    def extract(
        self,
        *,
        chat: str,
        media_types: set[str],
        start_ts: int | None,
        end_ts: int | None,
        limit: int,
        offset: int,
        language: str,
        model: str,
        refresh: bool,
    ) -> dict[str, Any]:
        page = self.store.media_messages(
            chat,
            media_types,
            start_ts,
            end_ts,
            limit,
            offset,
        )
        output_items: list[dict[str, Any]] = []
        for message in page["items"]:
            output_items.append(
                self._derive_message(
                    chat=page["chat"],
                    message=message,
                    language=language,
                    model=model,
                    refresh=refresh,
                )
            )
        return {
            **{key: value for key, value in page.items() if key != "items"},
            "items": output_items,
            "model": model,
            "language": language,
            "local_only": True,
        }

    def search(
        self,
        *,
        query: str,
        chat: str | None,
        media_types: set[str],
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        needle = query.casefold().strip()
        matches: list[dict[str, Any]] = []
        if self.cache.is_dir():
            for path in self.cache.glob("*.json"):
                item = _safe_json(path)
                if item is None:
                    continue
                if item.get("media_kind") not in media_types:
                    continue
                chat_data = item.get("chat") or {}
                if chat and chat.casefold() not in " ".join(
                    (
                        str(chat_data.get("username") or ""),
                        str(chat_data.get("display_name") or ""),
                    )
                ).casefold():
                    continue
                text = str((item.get("derived_text") or {}).get("text") or "")
                if needle in text.casefold():
                    matches.append(item)
        matches.sort(
            key=lambda item: (
                int(item.get("timestamp") or 0),
                int(item.get("local_id") or 0),
            ),
            reverse=True,
        )
        total = len(matches)
        page = matches[offset : offset + limit]
        return {
            "query": query,
            "chat": chat,
            "media_types": sorted(media_types),
            "total": total,
            "count": len(page),
            "offset": offset,
            "items": page,
            "has_more": offset + len(page) < total,
            "next_offset": (
                offset + len(page)
                if offset + len(page) < total
                else None
            ),
        }
