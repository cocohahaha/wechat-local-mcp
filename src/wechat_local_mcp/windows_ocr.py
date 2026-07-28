"""On-device OCR using Windows.Media.Ocr.

WinRT imports stay inside the async function so this module remains importable
on macOS/Linux for parser tests and source builds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import OcrBlock


def blocks_from_lines(
    lines: Iterable[tuple[str, Iterable[tuple[float, float, float, float]]]],
    *,
    image_width: float,
    image_height: float,
) -> list[OcrBlock]:
    """Convert top-left Windows OCR rectangles to normalized Vision-style blocks."""

    if image_width <= 0 or image_height <= 0:
        raise ValueError("image dimensions must be positive")

    blocks: list[OcrBlock] = []
    for text, rectangles in lines:
        clean_text = text.strip()
        rects = list(rectangles)
        if not clean_text or not rects:
            continue
        left = min(rect[0] for rect in rects)
        top = min(rect[1] for rect in rects)
        right = max(rect[0] + rect[2] for rect in rects)
        bottom = max(rect[1] + rect[3] for rect in rects)
        blocks.append(
            OcrBlock(
                text=clean_text,
                # Windows.Media.Ocr does not expose word confidence.
                confidence=0.75,
                x=max(0.0, min(1.0, left / image_width)),
                y=max(0.0, min(1.0, 1.0 - bottom / image_height)),
                width=max(0.0, min(1.0, (right - left) / image_width)),
                height=max(0.0, min(1.0, (bottom - top) / image_height)),
            )
        )
    blocks.sort(key=lambda block: (-block.center_y, block.x))
    return blocks


async def _recognize_text_async(image_path: Path) -> list[OcrBlock]:
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import (
            BitmapAlphaMode,
            BitmapDecoder,
            BitmapPixelFormat,
        )
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage import StorageFile
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Windows OCR 依赖。请运行 uv sync --extra windows。"
        ) from exc

    storage_file = await StorageFile.get_file_from_path_async(str(image_path.resolve()))
    stream = await storage_file.open_read_async()
    bitmap: Any | None = None
    try:
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_converted_async(
            BitmapPixelFormat.BGRA8,
            BitmapAlphaMode.PREMULTIPLIED,
        )
        language = Language("zh-Hans")
        engine = OcrEngine.try_create_from_language(language)
        if engine is None:
            engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise RuntimeError(
                "Windows 未安装可用的 OCR 语言包。请在“设置 → 时间和语言 → 语言和区域”"
                "中安装简体中文语言功能。"
            )
        result = await engine.recognize_async(bitmap)
        lines: list[tuple[str, list[tuple[float, float, float, float]]]] = []
        for line in result.lines:
            rectangles = [
                (
                    float(word.bounding_rect.x),
                    float(word.bounding_rect.y),
                    float(word.bounding_rect.width),
                    float(word.bounding_rect.height),
                )
                for word in line.words
            ]
            lines.append((str(line.text), rectangles))
        return blocks_from_lines(
            lines,
            image_width=float(bitmap.pixel_width),
            image_height=float(bitmap.pixel_height),
        )
    finally:
        if bitmap is not None:
            bitmap.close()
        stream.close()


def recognize_text(image_path: Path) -> list[OcrBlock]:
    """Recognize Simplified Chinese/English text in a local screenshot."""

    return asyncio.run(_recognize_text_async(image_path))
