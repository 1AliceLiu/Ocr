"""Recognize the number in every Dahua2 OSD crop with EasyOCR.

Input : dataset/Dahua2/crop/*.png
Output: output/num.txt          one number per line, same order as the sorted
                                crop filenames (blank = no number recognized)
        output/num_report.csv   per-image details for checking

The OSD number is fixed per camera, so after per-image OCR the script applies a
per-camera majority vote (camera key = last '_' part of the filename, e.g.
'dahua1001619'). This removes occasional misreads such as '51513' for '1513'.
Use --raw to disable the correction and keep the raw per-image results.

Usage:
    python scripts/ocr_crop_numbers.py            # CPU (default)
    python scripts/ocr_crop_numbers.py --gpu      # use CUDA if available
    python scripts/ocr_crop_numbers.py --raw      # no per-camera correction
"""
import argparse
import csv
import re
from collections import Counter
from pathlib import Path

import cv2
import easyocr

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CROP_DIR = PROJECT_ROOT / "dataset" / "Dahua2" / "crop"
OUTPUT_FILE = PROJECT_ROOT / "output" / "num.txt"
REPORT_FILE = PROJECT_ROOT / "output" / "num_report.csv"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
DIGITS_ONLY = re.compile(r"\D")


def normalize(text: str) -> str:
    """Keep digits only: OSD numbers may come back as '1 5 0 7'."""
    return DIGITS_ONLY.sub("", text)


def camera_key(path: Path) -> str:
    """'20261005_150604_341800_dahua1001619.png' -> 'dahua1001619'."""
    return path.stem.rsplit("_", 1)[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=CROP_DIR,
                        help=f"crop directory (default: {CROP_DIR})")
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE,
                        help=f"result file (default: {OUTPUT_FILE})")
    parser.add_argument("--report", type=Path, default=REPORT_FILE,
                        help=f"per-image report (default: {REPORT_FILE})")
    parser.add_argument("--gpu", action="store_true",
                        help="run on GPU; default is CPU")
    parser.add_argument("--raw", action="store_true",
                        help="keep raw OCR results, no per-camera correction")
    parser.add_argument("--limit", type=int, default=0,
                        help="process only the first N crops (0 = all)")
    args = parser.parse_args()

    if not args.input.is_dir():
        raise SystemExit(f"input directory not found: {args.input}")

    image_paths = sorted(
        p for p in args.input.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )
    if args.limit > 0:
        image_paths = image_paths[:args.limit]
    if not image_paths:
        raise SystemExit(f"no images found in: {args.input}")

    reader = easyocr.Reader(["en"], gpu=args.gpu, verbose=False)
    print(f"device: {reader.device}, images: {len(image_paths)}")

    rows = []
    for i, path in enumerate(image_paths, 1):
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raw, confidence = "", 0.0
        else:
            detections = reader.readtext(
                image,
                allowlist="0123456789",
                detail=1,
                paragraph=False,
            )
            raw = normalize("".join(item[1] for item in detections))
            confidence = min((item[2] for item in detections), default=0.0)
        rows.append({"path": path, "camera": camera_key(path),
                     "raw": raw, "confidence": confidence})
        print(f"[{i}/{len(image_paths)}] {path.name} -> {raw or '(empty)'}")

    # OSD number is constant per camera: take the most frequent valid raw result.
    consensus = {}
    if not args.raw:
        per_camera = {}
        for row in rows:
            per_camera.setdefault(row["camera"], []).append(row["raw"])
        for camera, values in per_camera.items():
            valid = [v for v in values if v]
            consensus[camera] = Counter(valid).most_common(1)[0][0] if valid else ""

    corrected = 0
    empty_cameras = set()
    for row in rows:
        # Do not fill an empty result: no detection may mean the OSD is missing,
        # which is different from a misread digit string.
        if args.raw or not row["raw"] or not consensus.get(row["camera"]):
            row["final"] = row["raw"]
            row["source"] = "raw"
        else:
            row["final"] = consensus[row["camera"]]
            row["source"] = "consensus"
            if row["final"] != row["raw"]:
                corrected += 1
        if not row["final"]:
            empty_cameras.add(row["camera"])

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(row["final"] for row in rows) + "\n", encoding="utf-8")

    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["image", "camera", "raw", "final", "confidence", "source"])
        for row in rows:
            writer.writerow([row["path"].name, row["camera"], row["raw"],
                             row["final"], f"{row['confidence']:.4f}",
                             row["source"]])

    print(f"\nwrote {len(rows)} lines to {args.output}")
    print(f"report: {args.report}")
    if not args.raw:
        print(f"corrected by per-camera majority: {corrected}")
    if empty_cameras:
        print(f"cameras with empty results: {', '.join(sorted(empty_cameras))}")


if __name__ == "__main__":
    main()
