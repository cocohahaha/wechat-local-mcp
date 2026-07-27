"""On-device OCR using Apple's Vision framework."""

from __future__ import annotations

from pathlib import Path

from Foundation import NSURL
from Vision import (
    VNImageRequestHandler,
    VNRecognizeTextRequest,
    VNRequestTextRecognitionLevelAccurate,
)

from .models import OcrBlock


def recognize_text(image_path: Path) -> list[OcrBlock]:
    """Recognize Simplified Chinese and English text in a local image."""

    request = VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(["zh-Hans", "en-US"])
    request.setUsesLanguageCorrection_(True)

    image_url = NSURL.fileURLWithPath_(str(image_path))
    handler = VNImageRequestHandler.alloc().initWithURL_options_(image_url, {})
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        detail = str(error) if error is not None else "unknown Vision error"
        raise RuntimeError(f"macOS Vision OCR failed: {detail}")

    blocks: list[OcrBlock] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        text = str(candidate.string()).strip()
        if not text:
            continue
        bounds = observation.boundingBox()
        blocks.append(
            OcrBlock(
                text=text,
                confidence=float(candidate.confidence()),
                x=float(bounds.origin.x),
                y=float(bounds.origin.y),
                width=float(bounds.size.width),
                height=float(bounds.size.height),
            )
        )

    blocks.sort(key=lambda block: (-block.center_y, block.x))
    return blocks
