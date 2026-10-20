"""Crop the Dahua2 OSD ROI and binarize it to keep white character cores.

Input : dataset/Dahua2/images/*.jpg
Output: dataset/Dahua2/crop/*.png
        dataset/Dahua2/crop-binary/*.png

Usage:
    python scripts/osd_crop_binarize.py
"""
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = PROJECT_ROOT / "dataset" / "Dahua2"
INPUT_DIR = DATASET_DIR / "images"
CROP_DIR = DATASET_DIR / "crop"
BINARY_DIR = DATASET_DIR / "crop-binary"

# ROI in image pixels, origin at the top-left corner.
# x2 / y2 are exclusive, so the crop is (X2 - X1) x (Y2 - Y1) = 176 x 69.
X1, Y1, X2, Y2 = 43, 971, 219, 1040

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}


def binarize_osd(roi: np.ndarray) -> np.ndarray:
    """Keep white character cores that are surrounded by a dark outline."""
    g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

    white = ((g >= 200) & (hsv[:, :, 1] <= 90)).astype(np.uint8) * 255
    dark = (g <= 110).astype(np.uint8)
    near_dark = cv2.dilate(dark, np.ones((5, 5), np.uint8))
    mask = cv2.bitwise_and(white, near_dark * 255)

    # Keep only character-sized connected components, drop background fragments.
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, 8)
    keep = np.zeros_like(mask)
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if 25 <= h <= 60 and 4 <= w <= 45 and a >= 60:
            keep[lab == i] = 255
    return keep


def main() -> None:
    if not INPUT_DIR.is_dir():
        raise SystemExit(f"input directory not found: {INPUT_DIR}")

    CROP_DIR.mkdir(parents=True, exist_ok=True)
    BINARY_DIR.mkdir(parents=True, exist_ok=True)

    image_paths = sorted(
        p for p in INPUT_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )
    if not image_paths:
        raise SystemExit(f"no images found in: {INPUT_DIR}")

    processed = 0
    skipped = []

    for path in image_paths:
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            skipped.append((path.name, "cannot be read"))
            continue

        height, width = image.shape[:2]
        if width < X2 or height < Y2:
            skipped.append((path.name, f"too small: {width}x{height}"))
            continue

        roi = image[Y1:Y2, X1:X2].copy()
        mask = binarize_osd(roi)

        out_name = path.stem + ".png"
        if not cv2.imwrite(str(CROP_DIR / out_name), roi):
            skipped.append((path.name, "failed to write crop"))
            continue
        if not cv2.imwrite(str(BINARY_DIR / out_name), mask):
            skipped.append((path.name, "failed to write binary mask"))
            continue

        processed += 1

    print(f"processed: {processed}")
    print(f"crop     -> {CROP_DIR}")
    print(f"binary   -> {BINARY_DIR}")
    if skipped:
        print(f"skipped: {len(skipped)}")
        for name, reason in skipped:
            print(f"  - {name}: {reason}")


if __name__ == "__main__":
    main()
