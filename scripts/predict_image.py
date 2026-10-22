"""Predict the OSD code in a saved 1080p frame.

This is the same logic used by evo_osd_monitor.py, without a live stream:
  * narrow ROI (43,971)-(219,1040) and wide ROI (0,920)-(520,1080)
  * each ROI is read with two preprocessing variants (raw + stretch/binarize)
  * alphabetic codes are supported; the raw string is printed

Usage:
    python /work/scripts/predict_image.py --image /work/snapshots/x.jpg --gpu
    python /work/scripts/predict_image.py --image x.jpg --roi 80,975,230,1040

Use --roi when the OSD is not at the default position (e.g. panorama frames
with a pipe or scaffolding on the left edge).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import easyocr

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evo_osd_monitor import FIXED_ROI, WIDE_ROI  # noqa: E402
from osd_digits import default_profile, read_digits  # noqa: E402
from osd_ocr import (  # noqa: E402
    ALLOW_ALNUM,
    ALLOW_DIGITS,
    read_number,
    scale_box,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-len", type=int, default=4)
    parser.add_argument("--roi", default="",
                        help="x1,y1,x2,y2; skips the default ROI candidates")
    parser.add_argument("--allowlist", choices=["digits", "alnum"],
                        default="alnum")
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--cell", action="store_true",
                        help="use the row-projection/per-cell pipeline instead "
                             "of the legacy whole-line reader")
    parser.add_argument("--save-crop", default="",
                        help="write the best ROI crop to this path")
    args = parser.parse_args()

    image = cv2.imread(args.image)
    if image is None:
        raise SystemExit(f"cannot read {args.image}")
    print(f"image: {image.shape[1]}x{image.shape[0]}")

    if args.cell:
        reader = easyocr.Reader(["en"], gpu=args.gpu, verbose=False)
        # The expected string only supplies type/length here; the digits are
        # not used as a prior for recognition.
        expected = "0" * max(1, args.expected_len)
        profile = default_profile("", expected)
        if args.roi:
            profile["roi"] = [int(v) for v in args.roi.split(",")]
        result = read_digits(reader, image, profile, decoder="beamsearch")
        print(f"cells  {result['text']!r:<12} "
              f"conf={result['confidence']:.3f} "
              f"reason={result['reason']} cells={result['cell_texts']}")
        if args.save_crop and result.get("line") is not None:
            cv2.imwrite(args.save_crop, result["line"])
            print(f"crop: {args.save_crop}")
        return

    if args.roi:
        x1, y1, x2, y2 = (int(v) for v in args.roi.split(","))
        rois = [("custom", (x1, y1, x2, y2))]  # absolute pixels
    else:
        height, width = image.shape[:2]
        rois = [("narrow", scale_box(FIXED_ROI, width, height)),
                ("wide", scale_box(WIDE_ROI, width, height))]

    allow = ALLOW_ALNUM if args.allowlist == "alnum" else ALLOW_DIGITS
    reader = easyocr.Reader(["en"], gpu=args.gpu, verbose=False)

    results = []
    for name, (x1, y1, x2, y2) in rois:
        roi = image[y1:y2, x1:x2]
        text, conf, source = read_number(
            reader, roi, args.expected_len, allow, numeric_expected=True)
        print(f"{name:<6} {text!r:<12} conf={conf:.3f} ({source})")
        results.append((name, text, conf, roi))

    pool = [r for r in results if r[1] and len(r[1]) == args.expected_len]
    best = max(pool or results, key=lambda r: r[2])
    print(f"final: {best[1]!r}  roi={best[0]}  conf={best[2]:.3f}")

    if args.save_crop:
        cv2.imwrite(args.save_crop, best[3])
        print(f"crop: {args.save_crop}")


if __name__ == "__main__":
    main()
