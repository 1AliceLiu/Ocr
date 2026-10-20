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

ALLOW_DIGITS = "0123456789"
ALLOW_ALNUM = ("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
               "abcdefghijklmnopqrstuvwxyz-_")


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


def detect_roi(frame: np.ndarray, base=(0, 900, 700, 1080),
               pad: int = 8) -> np.ndarray:
    """Find the OSD text line inside `base` and return a tight crop.

    The OSD sits at the bottom-left, but its width depends on the code format
    (4 digits vs 9 alphanumeric characters). Detecting the widest text group
    nearest the bottom keeps both cases inside the crop.
    """
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = base
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(width, x2), min(height, y2)
    region = frame[y1:y2, x1:x2]
    if region.size == 0:
        return region

    mask = binarize_osd(stretch_bgr(region))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            np.ones((3, 15), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for contour in contours:
        bx, by, bw, bh = cv2.boundingRect(contour)
        if 12 <= bh <= 80 and 3 <= bw <= 90 and bw * bh >= 30:
            boxes.append((bx, by, bw, bh))
    if not boxes:
        return region

    groups: list[list[tuple[int, int, int, int]]] = []
    for box in sorted(boxes, key=lambda b: b[0]):
        for group in groups:
            gy1 = min(b[1] for b in group)
            gy2 = max(b[1] + b[3] for b in group)
            if not (box[1] + box[3] < gy1 or box[1] > gy2):
                group.append(box)
                break
        else:
            groups.append([box])

    def group_width(group):
        return (max(b[0] + b[2] for b in group)
                - min(b[0] for b in group))

    groups = [g for g in groups if group_width(g) >= 20]
    if not groups:
        return region

    # Lowest group wins; ties go to the wider one.
    group = max(groups, key=lambda g: (max(b[1] + b[3] for b in g),
                                       group_width(g)))
    bx1 = max(0, min(b[0] for b in group) - pad)
    by1 = max(0, min(b[1] for b in group) - pad)
    bx2 = min(region.shape[1], max(b[0] + b[2] for b in group) + pad)
    by2 = min(region.shape[0], max(b[1] + b[3] for b in group) + pad)
    return region[by1:by2, bx1:bx2]


def _read(reader, image, allowlist: str):
    detections = reader.readtext(
        image, allowlist=allowlist, detail=1, paragraph=False)
    text = "".join(item[1] for item in detections).strip()
    confidence = min((float(item[2]) for item in detections), default=0.0)
    return text, confidence


def read_number(reader, roi: np.ndarray, expected_len: int | None = None,
                allowlist: str = ALLOW_DIGITS,
                numeric_expected: bool = False):
    """Return (text, confidence, source) using two complementary variants.

    `allowlist` controls the character set. Use ALLOW_ALNUM for codes such as
    "P40A2-087"; the raw string (letters and dashes included) is preserved.
    """
    candidates = []
    for source, image in (
        ("raw", upscale(roi)),
        ("stretch", upscale(binarize_robust(roi))),
    ):
        text, confidence = _read(reader, image, allowlist)
        candidates.append((text, confidence, source))

    # A numeric channel label is a strong prior: prefer a digit-only reading
    # of the same length, which rejects background text such as a lone "Y".
    # Alphanumeric OSD codes are often longer than the label (1517 vs
    # P40A2-087), so they are kept as a fallback.
    pool = candidates
    if numeric_expected and expected_len:
        exact = [c for c in candidates
                 if c[0].isdigit() and len(c[0]) == expected_len]
        same_len = [c for c in candidates if len(c[0]) == expected_len]
        if exact:
            pool = exact
        elif same_len:
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
