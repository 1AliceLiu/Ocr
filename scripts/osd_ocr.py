"""Robust OSD digit reading for the Dahua camera overlay.

Why this exists
---------------
`binarize_osd()` uses absolute thresholds (grey >= 200). That is crisp on a
bright frame but erases the whole number on dim / low-contrast frames. Feeding
the raw colour crop to EasyOCR instead works when the frame is dark, but the
text detector is easily confused by the background of a normal frame.

Measured on 16 labelled 1080p frames (normal / dim x0.45 / blur / low contrast):

    variant            normal  dim   blur  low-contrast
    raw colour          15/16  15/16 14/16  14/16
    binarize (current)  16/16   0/16  1/16   0/16
    stretch + binarize  16/16  16/16  3/16  16/16
    fuse raw + stretch  16/16  16/16 14/16  16/16

So: normalise brightness first, then binarize, invert (EasyOCR expects dark
text on a light background), and fuse with the raw crop by confidence.
"""
from __future__ import annotations

from collections import Counter

import cv2
import numpy as np

from osd_crop_binarize import binarize_osd


def stretch_bgr(roi: np.ndarray) -> np.ndarray:
    """Percentile contrast stretch, so dim frames reach the binarizer's range."""
    grey = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    lo, hi = np.percentile(grey, (2, 98))
    if hi <= lo:
        hi = lo + 1
    grey = np.clip((grey.astype(np.float32) - lo) * 255.0 / (hi - lo),
                   0, 255).astype(np.uint8)
    return cv2.cvtColor(grey, cv2.COLOR_GRAY2BGR)


def upscale(img: np.ndarray, scale: int = 3) -> np.ndarray:
    return cv2.resize(img, None, fx=scale, fy=scale,
                      interpolation=cv2.INTER_CUBIC)


def binarize_robust(roi: np.ndarray) -> np.ndarray:
    """Stretch -> white-core mask -> invert to black text on white."""
    return 255 - binarize_osd(stretch_bgr(roi))


def _read(reader, image):
    detections = reader.readtext(
        image, allowlist="0123456789", detail=1, paragraph=False)
    text = "".join(item[1] for item in detections).strip()
    confidence = min((float(item[2]) for item in detections), default=0.0)
    return text, confidence


def read_number(reader, roi: np.ndarray, expected_len: int | None = None):
    """Return (text, confidence, source) using two complementary variants."""
    candidates = []
    for source, image in (
        ("raw", upscale(roi)),
        ("stretch", upscale(binarize_robust(roi))),
    ):
        text, confidence = _read(reader, image)
        candidates.append((text, confidence, source))

    # Prefer candidates whose length matches the expected code; this removes
    # spurious "2544" + background digits strings.
    pool = candidates
    if expected_len:
        same_len = [c for c in candidates if len(c[0]) == expected_len]
        if same_len:
            pool = same_len

    text, confidence, source = max(pool, key=lambda c: c[1])

    # When both variants agree, the answer is much more trustworthy.
    voices = [c[0] for c in candidates if c[0]]
    if len(set(voices)) == 1 and voices:
        confidence = max(confidence, 0.99)
        source = "raw+stretch"

    return text, confidence, source


def majority(texts: list[str]) -> str:
    """Temporal vote across frames; blank results are ignored."""
    valid = [t for t in texts if t]
    return Counter(valid).most_common(1)[0][0] if valid else ""
