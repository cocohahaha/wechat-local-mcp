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


def _unit(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)


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
        # Vision returns normalized coordinates with float noise such as -7e-09.
        x = _unit(bounds.origin.x)
        y = _unit(bounds.origin.y)
        blocks.append(
            OcrBlock(
                text=text,
                confidence=_unit(candidate.confidence()),
                x=x,
                y=y,
                width=min(_unit(bounds.size.width), 1.0 - x),
                height=min(_unit(bounds.size.height), 1.0 - y),
            )
        )

    blocks.sort(key=lambda block: (-block.center_y, block.x))
    return blocks
