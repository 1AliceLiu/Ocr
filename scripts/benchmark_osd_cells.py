"""Offline A/B benchmark for the OSD digit pipeline.

This script does NOT touch production code or streams. It reads the manual
annotation CSV produced during the review, loads the saved anomaly frames, and
compares:

  A. current preprocessing: binarize_robust(line) + EasyOCR readtext
  B. proposed preprocessing: row projection -> per-cell solid binarization
     -> EasyOCR recognize() on each cell

The goal is to measure residual errors before deciding whether a dedicated
digit classifier is needed.

Usage:
    docker exec -i easyocr-dev-monitor-1 python /work/scripts/benchmark_osd_cells.py \
        --gpu --out-dir /work/output/benchmark

    # quick smoke test
    docker exec -i easyocr-dev-monitor-1 python /work/scripts/benchmark_osd_cells.py \
        --gpu --limit 5 --out-dir /work/output/benchmark_smoke
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import easyocr
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from osd_ocr import binarize_robust, scale_box, stretch_bgr  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ANNOTATIONS = PROJECT_ROOT / "dataset" / "osd_eval" / "annotations.csv"
DEFAULT_IMAGE_DIR = PROJECT_ROOT / "snapshots" / "osd_error"
DEFAULT_OUT_DIR = PROJECT_ROOT / "output" / "benchmark"


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

def safe_name(index: str | int, code: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z@_-]+", "_", code)
    return f"{int(index):04d}_{cleaned}.jpg"


def clean_manual(text: str) -> str:
    """Drop parenthetical notes such as '556 (背景另有TEL...)'."""
    return re.split(r"[（(]", text or "", maxsplit=1)[0].strip()


def digits_of(text: str) -> str:
    return "".join(re.findall(r"\d+", text or ""))


def edit_distance(a: str, b: str) -> int:
    if len(a) != len(b):
        return max(len(a), len(b))
    return sum(x != y for x, y in zip(a, b))


def percentile_stretch(gray: np.ndarray, lo_pct: float = 2,
                       hi_pct: float = 98) -> np.ndarray:
    lo, hi = np.percentile(gray, (lo_pct, hi_pct))
    if hi <= lo:
        hi = lo + 1
    return np.clip((gray.astype(np.float32) - lo) * 255.0 / (hi - lo),
                   0, 255).astype(np.uint8)


def to_bgr(img: np.ndarray) -> np.ndarray:
    return img if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def upscale(img: np.ndarray, scale: int = 3) -> np.ndarray:
    return cv2.resize(img, None, fx=scale, fy=scale,
                      interpolation=cv2.INTER_CUBIC)


# ---------------------------------------------------------------------------
# Row projection / cell segmentation
# ---------------------------------------------------------------------------

def runs_above(proj: np.ndarray, threshold: float) -> list[tuple[int, int]]:
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
        if merged and a - merged[-1][1] <= 2:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def _filter_components(mask: np.ndarray, gray: np.ndarray, scale: float
                       ) -> tuple[np.ndarray, list[tuple[int, int, int, int]]]:
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    keep = np.zeros_like(mask)
    boxes: list[tuple[int, int, int, int]] = []
    dark = (gray <= 100).astype(np.uint8) * 255
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
        ratio = (b & nd).sum() / max(1, b.sum())
        if ratio < 0.80:
            continue
        keep[labels == i] = 255
        boxes.append((x, y, w, h))
    return keep, boxes


def find_line_and_cells(frame: np.ndarray, target_len: int | None = None
                        ) -> dict:
    """Row projection + column grouping + per-cell crops.

    `target_len` is the number of glyphs to prefer when choosing a group. It is
    used only for group selection; the cells are still obtained from the mask.
    """
    height, width = frame.shape[:2]
    scale = height / 1080.0
    # Bottom-left search box. Panorama keeps x coordinates unscaled.
    x2 = min(width, round(760 * scale) if width / height < 2.5 else 760)
    y1 = max(0, height - round(180 * scale))
    crop = frame[y1:height, 0:x2]

    gray_raw = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = percentile_stretch(gray_raw)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    white = ((gray >= 210) & (hsv[:, :, 1] <= 110)).astype(np.uint8) * 255
    # Use the *raw* grayscale for the dark-outline check. Stretching can make
    # dark outlines look bright and cause all glyphs to be rejected.
    keep, boxes = _filter_components(white, gray_raw, scale)

    proj = (keep > 0).sum(axis=1)
    if proj.max() == 0:
        return {"line": crop, "cells": [], "cell_masks": [], "boxes": [], "band": (0, crop.shape[0]),
                "reason": "NO_MASK", "scale": scale, "crop": crop,
                "keep": keep, "projection": proj}

    threshold = max(3, int(0.20 * proj.max()))
    bands = [r for r in runs_above(proj, threshold)
             if 12 * scale <= r[1] - r[0] <= 70 * scale]
    if not bands:
        return {"line": crop, "cells": [], "cell_masks": [], "boxes": [], "band": (0, crop.shape[0]),
                "reason": "NO_BAND", "scale": scale, "crop": crop,
                "keep": keep, "projection": proj}

    # Score (band, group) pairs together. Choosing the band first can select a
    # background band that happens to have a larger projection sum.
    candidates = []
    for candidate_band in bands:
        in_band = [b for b in boxes
                   if b[1] + b[3] > candidate_band[0] and b[1] < candidate_band[1]]
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
            count = len(group)
            width = max(b[0] + b[2] for b in group) - min(b[0] for b in group)
            height = max(b[3] for b in group)
            target = target_len or count
            score = (
                count == target,                 # exact glyph count first
                -abs(count - target),
                -width / max(1, height),
                candidate_band[1],               # lower band wins ties
                -min(b[0] for b in group),
            )
            candidates.append((score, candidate_band, group))

    if not candidates:
        return {"line": crop, "cells": [], "cell_masks": [], "boxes": [], "band": (0, crop.shape[0]),
                "reason": "NO_GROUP", "scale": scale, "crop": crop,
                "keep": keep, "projection": proj}

    _, band, group = max(candidates, key=lambda item: item[0])
    gx1 = min(b[0] for b in group)
    gx2 = max(b[0] + b[2] for b in group)
    gh = max(b[3] for b in group)

    # Refine the row band using only the selected glyph columns.
    subproj = (keep[:, gx1:gx2] > 0).sum(axis=1)
    sub_threshold = max(3, int(0.25 * subproj.max())) if subproj.max() else 3
    sub_bands = [r for r in runs_above(subproj, sub_threshold)
                 if 12 * scale <= r[1] - r[0] <= 70 * scale]
    if sub_bands:
        band2 = max(sub_bands,
                    key=lambda r: (int(subproj[r[0]:r[1]].sum()), r[1]))
    else:
        band2 = band

    pad_y = round(0.30 * gh)
    pad_x = round(0.35 * gh)
    fy1 = max(0, band2[0] - pad_y)
    fy2 = min(crop.shape[0], band2[1] + pad_y)
    fx1 = max(0, gx1 - pad_x)
    fx2 = min(crop.shape[1], gx2 + pad_x)
    line = crop[fy1:fy2, fx1:fx2]

    # Column projection inside the selected group.
    col_proj = (keep[band2[0]:band2[1], gx1:gx2] > 0).sum(axis=0)
    col_threshold = max(2, int(0.05 * max(1, band2[1] - band2[0])))
    col_runs = runs_above(col_proj, col_threshold)
    col_runs = [(a + gx1, b + gx1) for a, b in col_runs]
    cells = []
    cell_masks = []
    for a, b in col_runs:
        if b - a < 2 * scale:
            continue
        cell = crop[fy1:fy2, a:b]
        if cell.size:
            cells.append(cell)
            cell_masks.append(keep[fy1:fy2, a:b])

    reason = "OK"
    if target_len and len(cells) != target_len:
        # Fallback: use the selected component boxes as cell boundaries.
        fallback = []
        fallback_masks = []
        for x, y, w, h in sorted(group):
            a = max(0, x - round(0.10 * h))
            b = min(crop.shape[1], x + w + round(0.10 * h))
            cell = crop[fy1:fy2, a:b]
            if cell.size:
                fallback.append(cell)
                fallback_masks.append(keep[fy1:fy2, a:b])
        if len(fallback) == target_len:
            cells = fallback
            cell_masks = fallback_masks
            reason = "BOX_FALLBACK"
        else:
            reason = f"CELL_COUNT_{len(cells)}_EXPECTED_{target_len}"

    return {
        "line": line,
        "cells": cells,
        "cell_masks": cell_masks,
        "boxes": sorted(group),
        "band": (fy1, fy2),
        "reason": reason,
        "scale": scale,
        "crop": crop,
        "keep": keep,
        "projection": proj,
    }


# ---------------------------------------------------------------------------
# Solid binarization
# ---------------------------------------------------------------------------

def solid_cell_mask(cell: np.ndarray) -> np.ndarray:
    """Keep the whole white glyph fill, then invert to black on white.

    Important: do NOT fill large counters (the holes inside 0/9/4/6/8).
    """
    if cell.size == 0:
        return np.full(cell.shape[:2], 255, np.uint8)
    gray = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY)
    gray = percentile_stretch(gray)
    hsv = cv2.cvtColor(cell, cv2.COLOR_BGR2HSV)

    white = ((gray >= 210) & (hsv[:, :, 1] <= 110)).astype(np.uint8) * 255
    dark = (gray <= 100).astype(np.uint8) * 255
    near_dark = cv2.dilate(dark, np.ones((5, 5), np.uint8))

    n, labels, stats, _ = cv2.connectedComponentsWithStats(white, 8)
    keep = np.zeros_like(white)
    h, w = white.shape
    for i in range(1, n):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        aspect = bw / bh if bh else 0.0
        if not (0.35 * h <= bh <= 1.05 * h):
            continue
        if not (0.08 * w <= bw <= 1.0 * w):
            continue
        if not (0.10 * h * w <= area <= 0.95 * h * w):
            continue
        if not (0.15 <= aspect <= 1.10):
            continue
        comp = (labels == i).astype(np.uint8) * 255
        boundary = cv2.subtract(comp, cv2.erode(comp, np.ones((3, 3), np.uint8)))
        b = boundary > 0
        nd = near_dark > 0
        if (b & nd).sum() / max(1, b.sum()) < 0.75:
            continue
        keep[labels == i] = 255

    if keep.sum() == 0:
        # Fallback: simple threshold/invert, still better than an empty cell.
        keep = white
    return 255 - cv2.dilate(keep, np.ones((2, 2), np.uint8), iterations=1)


def solid_from_core(core: np.ndarray) -> np.ndarray:
    """Use the already-filtered white fill from the line-level mask.

    This avoids re-thresholding a cell that still contains bright background:
    the line-level mask has already dropped non-glyph components.
    """
    if core is None or core.size == 0 or int(core.sum()) == 0:
        return np.full((1, 1), 255, np.uint8)
    return 255 - cv2.dilate(core, np.ones((2, 2), np.uint8), iterations=1)


def pad_square(img: np.ndarray, size: int = 64) -> np.ndarray:
    h, w = img.shape[:2]
    scale = min(size / max(1, h), size / max(1, w))
    resized = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))),
                         interpolation=cv2.INTER_CUBIC)
    canvas = np.full((size, size), 255, np.uint8)
    y = (size - resized.shape[0]) // 2
    x = (size - resized.shape[1]) // 2
    canvas[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
    return canvas


# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------

def ocr_readtext(reader, image: np.ndarray, allowlist: str,
                 decoder: str) -> tuple[str, float]:
    bgr = to_bgr(image)
    bgr = upscale(bgr, 3)
    result = reader.readtext(bgr, allowlist=allowlist, detail=1,
                             paragraph=False, decoder=decoder)
    text = "".join(item[1] for item in result)
    conf = min((float(item[2]) for item in result), default=0.0)
    return text.strip(), conf


def ocr_cell(reader, cell_mask: np.ndarray, allowlist: str,
             decoder: str) -> tuple[str, float]:
    gray = pad_square(cell_mask, 64)
    # recognize() expects a grayscale image and a horizontal box list.
    result = reader.recognize(
        gray,
        horizontal_list=[[0, gray.shape[1], 0, gray.shape[0]]],
        free_list=[],
        allowlist=allowlist,
        decoder=decoder,
        detail=1,
    )
    text = "".join(item[1] for item in result)
    conf = min((float(item[2]) for item in result), default=0.0)
    return text.strip(), conf


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def codes_match(expected: str, pred: str) -> bool:
    """Compare numeric codes, allowing an optional leading zero.

    '0676' and '676' are the same OSD code. This mirrors the production
    `numbers_match()` behavior for the leading-zero case.
    """
    if not expected or not pred:
        return False
    if expected == pred:
        return True
    if expected.isdigit() and pred.isdigit():
        return (expected.lstrip("0") or expected) == (pred.lstrip("0") or pred)
    return False


def char_accuracy(truth: str, pred: str) -> int:
    """Number of matching positions; length mismatch counts as 0 matches."""
    if len(truth) != len(pred):
        return 0
    return sum(a == b for a, b in zip(truth, pred))


def add_confusion(matrix: Counter, truth: str, pred: str) -> None:
    if len(truth) != len(pred):
        return
    for a, b in zip(truth, pred):
        matrix[(a, b)] += 1


def write_summary(path: Path, rows: list[dict], meta: dict) -> None:
    total = len(rows)
    ocr_rows = [r for r in rows if r["category"] == "OCR"]
    digit_ocr = [r for r in ocr_rows if r["truth_is_digits_only"]]
    non_digit_ocr = [r for r in ocr_rows if not r["truth_is_digits_only"]]
    label_rows = [r for r in rows if r["category"] == "LABEL"]
    no_rows = [r for r in rows if r["category"] == "NO_OCR"]

    def exact(rows, key):
        return sum(1 for r in rows if r["manual_clean"] and r[key] == r["manual_clean"])

    def exact_expected(rows, key):
        return sum(1 for r in rows if r["expected"] and r[key] == r["expected"])

    def match_expected(rows, key):
        return sum(1 for r in rows if r["expected"] and r[key])

    def cell_reason(rows):
        return sum(1 for r in rows if r["segment_reason"] == "OK")

    lines = []
    lines.append("# OSD cell benchmark\n")
    lines.append(f"- annotations: `{meta['annotations']}`")
    lines.append(f"- images: `{meta['image_dir']}`")
    lines.append(f"- rows: {total}")
    lines.append(f"- decoder: {meta['decoder']}, gpu: {meta['gpu']}")
    lines.append("")
    lines.append("## Overall\n")
    lines.append("| metric | A: current | B: solid + per-cell |")
    lines.append("|---|---:|---:|")
    lines.append(f"| exact vs manual OSD (all rows) | {exact(rows, 'a_text')}/{total} | {exact(rows, 'b_text')}/{total} |")
    lines.append(f"| exact vs expected (all rows) | {exact_expected(rows, 'a_text')}/{total} | {exact_expected(rows, 'b_text')}/{total} |")
    lines.append(f"| match expected, leading zero OK (all rows) | {match_expected(rows, 'a_match_expected')}/{total} | {match_expected(rows, 'b_match_expected')}/{total} |")
    lines.append(f"| segment OK | - | {cell_reason(rows)}/{total} |")
    lines.append("")
    lines.append("## OCR-error rows, digit-only OSD (main target)\n")
    lines.append(f"- count: {len(digit_ocr)}")
    lines.append("")
    lines.append("| metric | A: current | B: solid + per-cell |")
    lines.append("|---|---:|---:|")
    lines.append(f"| match expected (leading zero OK) | {match_expected(digit_ocr, 'a_match_expected')}/{len(digit_ocr)} | {match_expected(digit_ocr, 'b_match_expected')}/{len(digit_ocr)} |")
    lines.append(f"| digit accuracy | {sum(r['a_char_ok'] for r in digit_ocr)}/{sum(r['truth_len'] for r in digit_ocr)} | {sum(r['b_char_ok'] for r in digit_ocr)}/{sum(r['truth_len'] for r in digit_ocr)} |")
    lines.append(f"| segment OK | - | {cell_reason(digit_ocr)}/{len(digit_ocr)} |")
    lines.append("")
    lines.append("## OCR-error rows, alnum/CJK OSD (not digit-only)\n")
    lines.append(f"- count: {len(non_digit_ocr)}")
    lines.append("")
    lines.append("| metric | A: current | B: solid + per-cell |")
    lines.append("|---|---:|---:|")
    lines.append(f"| exact vs manual OSD | {exact(non_digit_ocr, 'a_text')}/{len(non_digit_ocr)} | {exact(non_digit_ocr, 'b_text')}/{len(non_digit_ocr)} |")
    lines.append("")
    lines.append("## LABEL-mismatch rows (OSD != expected)\n")
    lines.append(f"- count: {len(label_rows)}")
    lines.append("")
    lines.append("| metric | A: current | B: solid + per-cell |")
    lines.append("|---|---:|---:|")
    lines.append(f"| exact vs manual OSD | {exact(label_rows, 'a_text')}/{len(label_rows)} | {exact(label_rows, 'b_text')}/{len(label_rows)} |")
    lines.append(f"| accidentally == expected | {exact_expected(label_rows, 'a_text')}/{len(label_rows)} | {exact_expected(label_rows, 'b_text')}/{len(label_rows)} |")
    lines.append("")
    lines.append("## NO_OCR rows\n")
    lines.append(f"- count: {len(no_rows)}")
    lines.append("")
    lines.append("## Confusion pairs (digit-only OCR rows)\n")
    lines.append("| pair | A current | B solid |")
    lines.append("|---|---:|---:|")
    for (a, b), n in sorted(meta["a_confusions"].items(), key=lambda kv: -kv[1]):
        if a == b:
            continue
        lines.append(f"| `{a}->{b}` | {n} | {meta['b_confusions'][(a, b)]} |")
    lines.append("")
    lines.append("> `A` uses the current hollow binarization and whole-line `readtext`.")
    lines.append("> `B` uses row/column projection, per-cell solid binarization, and")
    lines.append("> `recognize()` on each cell. Segment failures are listed in the CSV.")
    lines.append("> Digit accuracy and confusion pairs are computed only on digit-only")
    lines.append("> OCR-error rows, so alnum/CJK labels do not distort the numbers.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--image-dir", type=Path, default=DEFAULT_IMAGE_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--decoder", choices=["greedy", "beamsearch"],
                        default="greedy")
    args = parser.parse_args()

    if not args.annotations.exists():
        raise SystemExit(f"annotations not found: {args.annotations}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    cells_dir = args.out_dir / "cells"
    cells_dir.mkdir(exist_ok=True)

    with args.annotations.open(encoding="utf-8-sig", newline="") as fh:
        annotations = list(csv.DictReader(fh))
    if args.limit:
        annotations = annotations[:args.limit]

    print(f"loading EasyOCR (gpu={args.gpu}) ...", flush=True)
    reader = easyocr.Reader(["en"], gpu=args.gpu, verbose=False)
    print(f"device: {reader.device}, rows: {len(annotations)}", flush=True)

    rows: list[dict] = []
    a_confusions: Counter = Counter()
    b_confusions: Counter = Counter()
    for n, ann in enumerate(annotations, 1):
        index = ann.get("index", "").strip()
        code = ann.get("channelCode", "").strip()
        expected = ann.get("expected", "").strip()
        manual = ann.get("人工读到的OSD", "").strip()
        manual_clean = clean_manual(manual)
        category = ann.get("分类", "").strip() or "UNKNOWN"
        image_path = args.image_dir / safe_name(index, code)
        truth = digits_of(manual_clean)
        truth_len = len(truth)
        truth_is_digits_only = bool(truth) and manual_clean == truth
        record = {
            "n": n,
            "index": index,
            "channelCode": code,
            "name": ann.get("name", ""),
            "expected": expected,
            "manual_osd": manual,
            "manual_clean": manual_clean,
            "truth_digits": truth,
            "truth_len": truth_len,
            "truth_is_digits_only": int(truth_is_digits_only),
            "category": category,
            "image": str(image_path),
            "segment_reason": "",
            "cell_count": 0,
            "cells": "",
            "a_text": "",
            "a_conf": 0.0,
            "b_text": "",
            "b_conf": 0.0,
            "a_exact_manual": 0,
            "b_exact_manual": 0,
            "a_exact_expected": 0,
            "b_exact_expected": 0,
            "a_match_expected": 0,
            "b_match_expected": 0,
            "a_char_ok": 0,
            "b_char_ok": 0,
            "note": "",
        }

        frame = cv2.imread(str(image_path)) if image_path.exists() else None
        if frame is None:
            record["note"] = "IMAGE_MISSING"
            rows.append(record)
            print(f"[{n:3d}/{len(annotations)}] {index} {code} IMAGE_MISSING", flush=True)
            continue

        target_len = truth_len if truth_len else (len(expected) if expected.isdigit() else None)
        seg = find_line_and_cells(frame, target_len=target_len)
        record["segment_reason"] = seg["reason"]
        record["cell_count"] = len(seg["cells"])

        line = seg["line"]
        # A: current hollow binarization + whole-line readtext.
        a_img = binarize_robust(line)
        a_text, a_conf = ocr_readtext(reader, a_img, "0123456789", args.decoder)
        record["a_text"] = a_text
        record["a_conf"] = round(a_conf, 4)

        # B: solid per-cell + recognize() per cell.
        b_parts = []
        b_confs = []
        cores = seg.get("cell_masks", [])
        for cell_i, cell in enumerate(seg["cells"]):
            core = cores[cell_i] if cell_i < len(cores) else None
            mask = solid_from_core(core)
            if int(mask.sum()) == 255 * mask.size:  # all-white fallback
                mask = solid_cell_mask(cell)
            # Save a small visual sample for manual inspection.
            if n <= 120:
                name = f"{n:03d}_{index}_{len(b_parts)}.png"
                cv2.imwrite(str(cells_dir / name), pad_square(mask, 64))
            text, conf = ocr_cell(reader, mask, "0123456789", args.decoder)
            b_parts.append(text)
            b_confs.append(conf)
        b_text = "".join(b_parts)
        b_conf = min(b_confs, default=0.0)
        record["b_text"] = b_text
        record["b_conf"] = round(b_conf, 4)
        record["cells"] = "|".join(b_parts)

        record["a_exact_manual"] = int(bool(truth) and a_text == truth)
        record["b_exact_manual"] = int(bool(truth) and b_text == truth)
        record["a_exact_expected"] = int(bool(expected) and a_text == expected)
        record["b_exact_expected"] = int(bool(expected) and b_text == expected)
        record["a_match_expected"] = int(codes_match(expected, a_text))
        record["b_match_expected"] = int(codes_match(expected, b_text))
        record["a_char_ok"] = char_accuracy(truth, a_text)
        record["b_char_ok"] = char_accuracy(truth, b_text)
        if truth and category == "OCR" and truth_is_digits_only:
            add_confusion(a_confusions, truth, a_text)
            add_confusion(b_confusions, truth, b_text)

        rows.append(record)
        print(f"[{n:3d}/{len(annotations)}] {index} {code} "
              f"exp={expected!r} osd={manual!r} "
              f"A={a_text!r} B={b_text!r} seg={seg['reason']}", flush=True)

    # CSV output
    fields = list(rows[0].keys()) if rows else []
    out_csv = args.out_dir / "osd_cell_benchmark.csv"
    with out_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    summary = args.out_dir / "osd_cell_benchmark_summary.md"
    write_summary(summary, rows, {
        "annotations": args.annotations,
        "image_dir": args.image_dir,
        "decoder": args.decoder,
        "gpu": args.gpu,
        "a_confusions": a_confusions,
        "b_confusions": b_confusions,
    })
    print(f"\nwrote: {out_csv}", flush=True)
    print(f"wrote: {summary}", flush=True)


if __name__ == "__main__":
    main()
