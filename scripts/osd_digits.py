"""OSD cell pipeline: fixed ROI -> row projection -> cells -> solid mask.

This module deliberately stops before a dedicated digit classifier.  Numeric
channels are read with EasyOCR's recognition network on each solid-binarized
cell; alphanumeric/Chinese channels continue to use the legacy whole-line path
in evo_osd_monitor.py.

The pipeline is intentionally conservative:
  * the profile ROI is used as the search/crop box;
  * row projection finds the OSD line;
  * column projection/connected components split the line into cells;
  * each cell keeps the whole white glyph fill (not only the outline rim);
  * counters inside 0/9/4/6/8 are NOT filled;
  * segmentation failures are reported instead of guessing.
"""
from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import cv2
import numpy as np

from osd_ocr import scale_box

# 1080p reference boxes.  Numeric OSDs are usually in the bottom-left corner;
# 320px is wide enough for 4 digits and much safer than the old 760px box.
DEFAULT_DIGIT_ROI = (0, 950, 320, 1050)
DEFAULT_ALNUM_ROI = (0, 920, 520, 1080)

DIGITS_RE = re.compile(r"\d+")


def expected_digits(text: str) -> str:
    return "".join(DIGITS_RE.findall(text or ""))


def default_profile(name: str, expected: str, defaults: dict | None = None) -> dict:
    """Build a profile when config/osd_profiles.json has no entry for a channel."""
    if expected.isdigit():
        profile = {
            "roi": list(DEFAULT_DIGIT_ROI),
            "type": "digits",
            "length": len(expected),
        }
    elif re.search(r"[A-Za-z]", expected or ""):
        profile = {
            "roi": list(DEFAULT_ALNUM_ROI),
            "type": "alnum",
            "length": None,
        }
    else:
        profile = {
            "roi": list(DEFAULT_ALNUM_ROI),
            "type": "cjk",
            "length": None,
        }
    if defaults:
        # Accept either {"roi": ...} or {"digits": {...}, "alnum": {...}}.
        shared = {k: v for k, v in defaults.items()
                  if k in {"roi", "type", "length", "pattern", "alias"}}
        if shared:
            profile.update(shared)
        typed = defaults.get(profile["type"])
        if isinstance(typed, dict):
            profile.update(typed)
    return profile


def load_profiles(path: str | Path | None) -> dict:
    """Load a V2 profile file.  Missing/empty file is not an error."""
    if not path:
        return {"version": 1, "channels": {}}
    path = Path(path)
    if not path.exists():
        return {"version": 1, "channels": {}}
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"profile file must contain a JSON object: {path}")
    if "channels" not in data:
        # Backwards-friendly: an old {channelCode: [x1,y1,x2,y2]} map.
        if all(isinstance(v, list) and len(v) == 4 for v in data.values()):
            data = {"version": 1, "channels": {
                code: {"roi": roi, "type": "digits"}
                for code, roi in data.items()
            }}
        else:
            data = {"version": 1, "channels": data}
    data.setdefault("version", 1)
    data.setdefault("channels", {})
    data.setdefault("defaults", {})
    return data


def _percentile_stretch(gray: np.ndarray, lo_pct: float = 2,
                        hi_pct: float = 98) -> np.ndarray:
    lo, hi = np.percentile(gray, (lo_pct, hi_pct))
    if hi <= lo:
        hi = lo + 1
    return np.clip((gray.astype(np.float32) - lo) * 255.0 / (hi - lo),
                   0, 255).astype(np.uint8)


def _runs_above(proj: np.ndarray, threshold: float,
                max_gap: int = 2) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start = None
    for i, value in enumerate(proj):
        if value >= threshold and start is None:
            start = i
        elif value < threshold and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(proj)))

    merged: list[tuple[int, int]] = []
    for a, b in runs:
        if merged and a - merged[-1][1] <= max_gap:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def _filter_components(mask: np.ndarray, gray_raw: np.ndarray,
                       scale: float) -> tuple[np.ndarray, list[tuple[int, int, int, int]]]:
    """Keep glyph-sized white components whose boundary is near a dark outline."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    keep = np.zeros_like(mask)
    boxes: list[tuple[int, int, int, int]] = []

    dark = (gray_raw <= 100).astype(np.uint8) * 255
    near_dark = cv2.dilate(dark, np.ones((5, 5), np.uint8))

    min_h, max_h = 24 * scale, 70 * scale
    min_w, max_w = 6 * scale, 50 * scale
    min_area, max_area = 120 * scale * scale, 2500 * scale * scale

    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        aspect = w / h if h else 0.0
        if not (min_h <= h <= max_h and min_w <= w <= max_w):
            continue
        if not (min_area <= area <= max_area):
            continue
        if not (0.15 <= aspect <= 0.90):
            continue
        comp = (labels == i).astype(np.uint8) * 255
        boundary = cv2.subtract(comp, cv2.erode(comp, np.ones((3, 3), np.uint8)))
        b = boundary > 0
        nd = near_dark > 0
        if (b & nd).sum() / max(1, b.sum()) < 0.80:
            continue
        keep[labels == i] = 255
        boxes.append((x, y, w, h))
    return keep, boxes


def _choose_subset(group: list[tuple[int, int, int, int]],
                   target: int | None
                   ) -> list[tuple[int, int, int, int]]:
    """Choose the `target` glyph-like components with the most regular layout.

    A group can contain background components that pass the per-component
    filter.  For numeric OSDs the real digits have nearly equal height, width
    and spacing, so the most regular subset is usually the correct one.
    """
    if not target or len(group) <= target:
        return group
    if len(group) > 12:
        # Avoid combinatorial explosion on a very noisy group.  Keep the
        # components with the most common height; the real digits survive.
        heights = sorted(b[3] for b in group)
        median_h = heights[len(heights) // 2]
        group = sorted(group, key=lambda b: abs(b[3] - median_h))[:12]

    def spread(values: list[float]) -> float:
        if not values:
            return 0.0
        arr = np.asarray(values, dtype=np.float32)
        return float(arr.std() / (abs(arr.mean()) + 1e-6))

    best = None
    best_score = float("inf")
    for subset in itertools.combinations(group, target):
        subset = list(subset)
        heights = [b[3] for b in subset]
        widths = [b[2] for b in subset]
        centers = [b[0] + b[2] / 2 for b in subset]
        ycenters = [b[1] + b[3] / 2 for b in subset]
        gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
        score = (spread(heights) + spread(widths) + spread(gaps)
                 + spread(ycenters))
        if score < best_score:
            best_score = score
            best = subset
    return best or group


def _group_is_regular(group: list[tuple[int, int, int, int]]) -> bool:
    """True when the group looks like a complete, evenly spaced digit line.

    Used to avoid dropping a real leading digit just because the channel name
    expects fewer digits (e.g. OSD 1079 while the name says 779).
    """
    if len(group) < 2:
        return True
    heights = [b[3] for b in group]
    widths = [b[2] for b in group]
    centers = [b[0] + b[2] / 2 for b in group]
    gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
    if min(heights) <= 0 or max(heights) / min(heights) > 1.25:
        return False
    if min(widths) <= 0 or max(widths) / min(widths) > 2.0:
        return False
    if gaps and min(gaps) <= 0:
        return False
    if gaps and max(gaps) / min(gaps) > 1.60:
        return False
    return True


def _analyze_crop(crop: np.ndarray, expected_len: int | None,
                  scale: float) -> dict:
    gray_raw = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = _percentile_stretch(gray_raw)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

    keep = np.zeros_like(gray_raw)
    boxes: list[tuple[int, int, int, int]] = []
    white = np.zeros_like(gray_raw)
    threshold_used = None
    for threshold in (210, 190, 170):
        white = ((gray >= threshold) & (hsv[:, :, 1] <= 110)).astype(np.uint8) * 255
        keep, boxes = _filter_components(white, gray_raw, scale)
        if boxes:
            threshold_used = threshold
            break

    proj = (keep > 0).sum(axis=1)
    if proj.max() == 0:
        return {"reason": "NO_MASK", "crop": crop, "keep": keep, "proj": proj,
                "white": white, "threshold": threshold_used, "cell_boxes": []}

    band_threshold = max(3, int(0.20 * int(proj.max())))
    bands = [r for r in _runs_above(proj, band_threshold)
             if 12 * scale <= r[1] - r[0] <= 70 * scale]
    if not bands:
        return {"reason": "NO_BAND", "crop": crop, "keep": keep, "proj": proj,
                "white": white, "threshold": threshold_used, "cell_boxes": []}

    candidates = []
    for band in bands:
        in_band = [b for b in boxes
                   if b[1] + b[3] > band[0] and b[1] < band[1]]
        in_band.sort(key=lambda b: b[0])
        groups: list[list[tuple[int, int, int, int]]] = []
        for box in in_band:
            if groups:
                last = groups[-1]
                gap = box[0] - max(b[0] + b[2] for b in last)
                max_h = max(b[3] for b in last)
                if gap <= 0.6 * max_h:
                    last.append(box)
                    continue
            groups.append([box])
        for group in groups:
            # Background scaffolding/steel bars often add short components at
            # the same x positions.  Drop components much shorter than the
            # median glyph before scoring the group.
            heights = sorted(b[3] for b in group)
            median_h = heights[len(heights) // 2]
            group = [b for b in group if b[3] >= 0.70 * median_h]
            extra_glyphs = False
            if expected_len and len(group) > expected_len:
                if _group_is_regular(group):
                    # Keep the full line: an extra leading digit is a real
                    # label mismatch, not background noise.
                    extra_glyphs = True
                else:
                    group = _choose_subset(group, expected_len)
            if not group:
                continue
            count = len(group)
            width = max(b[0] + b[2] for b in group) - min(b[0] for b in group)
            height = max(b[3] for b in group)
            target = expected_len or count
            score = (
                count == target,
                -abs(count - target),
                -width / max(1, height),
                band[1],
                -min(b[0] for b in group),
            )
            candidates.append((score, band, group, extra_glyphs))

    if not candidates:
        return {"reason": "NO_GROUP", "crop": crop, "keep": keep, "proj": proj,
                "white": white, "threshold": threshold_used, "cell_boxes": []}

    _, band, group, extra_glyphs = max(candidates, key=lambda item: item[0])
    gx1 = min(b[0] for b in group)
    gx2 = max(b[0] + b[2] for b in group)
    gh = max(b[3] for b in group)

    subproj = (keep[:, gx1:gx2] > 0).sum(axis=1)
    sub_threshold = max(3, int(0.25 * int(subproj.max()))) if subproj.max() else 3
    sub_bands = [r for r in _runs_above(subproj, sub_threshold)
                 if 12 * scale <= r[1] - r[0] <= 70 * scale]
    band2 = (max(sub_bands, key=lambda r: (int(subproj[r[0]:r[1]].sum()), r[1]))
             if sub_bands else band)

    pad_y = round(0.30 * gh)
    pad_x = round(0.35 * gh)
    # Do not let the refined band cut off a separate top bar.  A 7 can have a
    # thin horizontal top stroke above the main body; the first-pass band
    # includes it, so take the union of both bands.
    band_start = min(band[0], band2[0])
    band_stop = max(band[1], band2[1])
    fy1 = max(0, band_start - pad_y)
    fy2 = min(crop.shape[0], band_stop + pad_y)
    fx1 = max(0, gx1 - pad_x)
    fx2 = min(crop.shape[1], gx2 + pad_x)
    line = crop[fy1:fy2, fx1:fx2]

    col_proj = (keep[band2[0]:band2[1], gx1:gx2] > 0).sum(axis=0)
    col_threshold = max(2, int(0.05 * max(1, band2[1] - band2[0])))
    col_runs = _runs_above(col_proj, col_threshold)
    col_runs = [(a + gx1, b + gx1) for a, b in col_runs]

    cells = []
    cell_masks = []
    cell_boxes = []
    reason = "OK"

    # When the component count already matches the expected digit count, use
    # the component boxes directly with a small pad.  Column-projection runs can
    # shave a few pixels off a glyph's right edge (e.g. 3 -> 7), which is exactly
    # the failure mode this pipeline is meant to prevent.
    if expected_len and len(group) == expected_len:
        for x, y, w, h in sorted(group):
            pad = max(1, round(0.03 * h))
            a = max(0, x - pad)
            b = min(crop.shape[1], x + w + pad)
            cell = crop[fy1:fy2, a:b]
            if cell.size:
                cells.append(cell)
                cell_masks.append(keep[fy1:fy2, a:b])
                cell_boxes.append((a, b, fy1, fy2))
    else:
        for a, b in col_runs:
            if b - a < 2 * scale:
                continue
            cell = crop[fy1:fy2, a:b]
            if cell.size:
                cells.append(cell)
                cell_masks.append(keep[fy1:fy2, a:b])
                cell_boxes.append((a, b, fy1, fy2))

        if expected_len and len(cells) != expected_len:
            fallback = []
            fallback_masks = []
            fallback_boxes = []
            for x, y, w, h in sorted(group):
                pad = max(1, round(0.03 * h))
                a = max(0, x - pad)
                b = min(crop.shape[1], x + w + pad)
                cell = crop[fy1:fy2, a:b]
                if cell.size:
                    fallback.append(cell)
                    fallback_masks.append(keep[fy1:fy2, a:b])
                    fallback_boxes.append((a, b, fy1, fy2))
            if len(fallback) == expected_len:
                cells = fallback
                cell_masks = fallback_masks
                cell_boxes = fallback_boxes
                reason = "BOX_FALLBACK"
            else:
                reason = f"CELL_COUNT_{len(cells)}_EXPECTED_{expected_len}"

    return {
        "reason": reason,
        "crop": crop,
        "keep": keep,
        "white": white,
        "threshold": threshold_used,
        "proj": proj,
        "line": line,
        "cells": cells,
        "cell_masks": cell_masks,
        "cell_boxes": cell_boxes,
        "band": (fy1, fy2),
        "first_band": band,
        "group": group,
        "gx": (gx1, gx2),
        "line_origin": (fx1, fy1),
        "col_threshold": col_threshold,
        "extra_glyphs": extra_glyphs,
    }


def find_osd_line(frame: np.ndarray, roi: tuple[int, int, int, int],
                  expected_len: int | None = None) -> dict:
    """Crop `roi`, find the OSD line, and split it into glyph cells.

    If the configured ROI is too tight and no glyph group is found, it retries
    once with a small margin.  This protects against a slightly mis-calibrated
    per-channel ROI without falling back to the old 760px search box.
    """
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = scale_box(roi, width, height)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return {"reason": "EMPTY_ROI", "roi": (x1, y1, x2, y2),
                "line": crop, "cells": [], "cell_masks": [], "cell_boxes": []}

    scale = height / 1080.0
    result = _analyze_crop(crop, expected_len, scale)
    result["roi"] = (x1, y1, x2, y2)

    if result["reason"] in {"NO_MASK", "NO_BAND", "NO_GROUP"}:
        margin_x = round(24 * scale)
        margin_y = round(20 * scale)
        ex1 = max(0, x1 - margin_x)
        ey1 = max(0, y1 - margin_y)
        ex2 = min(width, x2 + margin_x)
        ey2 = min(height, y2 + margin_y)
        if (ex1, ey1, ex2, ey2) != (x1, y1, x2, y2):
            expanded = frame[ey1:ey2, ex1:ex2]
            retry = _analyze_crop(expanded, expected_len, scale)
            retry["roi"] = (ex1, ey1, ex2, ey2)
            retry["expanded"] = True
            if retry["reason"] not in {"NO_MASK", "NO_BAND", "NO_GROUP"}:
                return retry

    return result


def solid_binarize(core: np.ndarray) -> np.ndarray:
    """Turn a filtered white-core mask into solid black glyphs on white.

    `core` is the already-filtered white fill.  Keep the whole fill and add a
    1px dilation for the black outline.  Do NOT fill the counters of 0/9/4/6/8.
    """
    if core is None or core.size == 0 or int(core.sum()) == 0:
        return np.full((1, 1), 255, np.uint8)
    core = cv2.morphologyEx(core, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    return 255 - cv2.dilate(core, np.ones((2, 2), np.uint8), iterations=1)


def pad_square(img: np.ndarray, size: int = 64) -> np.ndarray:
    h, w = img.shape[:2]
    scale = min(size / max(1, h), size / max(1, w))
    resized = cv2.resize(
        img, (max(1, round(w * scale)), max(1, round(h * scale))),
        interpolation=cv2.INTER_CUBIC)
    canvas = np.full((size, size), 255, np.uint8)
    y = (size - resized.shape[0]) // 2
    x = (size - resized.shape[1]) // 2
    canvas[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
    return canvas


def ocr_cell(reader, cell_mask: np.ndarray, decoder: str = "beamsearch",
             allowlist: str = "0123456789") -> tuple[str, float]:
    gray = pad_square(solid_binarize(cell_mask), 64)
    result = reader.recognize(
        gray,
        horizontal_list=[[0, gray.shape[1], 0, gray.shape[0]]],
        free_list=[],
        allowlist=allowlist,
        decoder=decoder,
        detail=1,
    )
    text = "".join(item[1] for item in result).strip()
    confidence = min((float(item[2]) for item in result), default=0.0)
    return text, confidence


def read_digits(reader, frame: np.ndarray, profile: dict,
                decoder: str = "beamsearch") -> dict:
    """Full numeric-cell read.  Returns a dict, never raises for OCR failures."""
    roi = profile.get("roi") or DEFAULT_DIGIT_ROI
    expected_len = profile.get("length")
    if not expected_len and profile.get("expected"):
        expected_len = len(expected_digits(profile["expected"]))
    expected_len = int(expected_len) if expected_len else None

    found = find_osd_line(frame, tuple(roi), expected_len)
    if not found.get("cells"):
        return {
            "text": "",
            "confidence": 0.0,
            "cell_texts": [],
            "reason": found.get("reason", "NO_CELLS"),
            "line": found.get("line"),
            "roi": found.get("roi"),
            "extra_glyphs": bool(found.get("extra_glyphs")),
        }

    texts = []
    confs = []
    for core in found["cell_masks"]:
        text, conf = ocr_cell(reader, core, decoder=decoder)
        texts.append(text)
        confs.append(conf)

    text = "".join(texts)
    return {
        "text": text,
        "confidence": min(confs, default=0.0),
        "cell_texts": texts,
        "reason": found.get("reason", "OK"),
        "line": found.get("line"),
        "roi": found.get("roi"),
        "extra_glyphs": bool(found.get("extra_glyphs")),
    }
