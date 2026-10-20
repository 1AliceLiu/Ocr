"""Monitor the OSD code of RTSP streams and report mismatches.

For every line of web-spider-label.txt (<expected code> <rtsp url>), the script
grabs one frame at a fixed interval, crops the OSD ROI, runs EasyOCR and writes
a line to output.txt whenever the recognized code != expected code.

Output line format (tab separated):
    <Beijing time>    <expected code>    <detected code>

Usage:
    python scripts/rtsp_osd_monitor.py --interval 60
    python scripts/rtsp_osd_monitor.py --interval 10 --once
    python scripts/rtsp_osd_monitor.py --interval 60 --gpu
"""
import argparse
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ask FFmpeg to use TCP for RTSP: much more stable than UDP on lossy links.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
# Silence FFmpeg's decoder chatter (e.g. HEVC "Could not find ref with POC").
# Override with OPENCV_FFMPEG_LOGLEVEL=info when debugging the stream.
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "quiet")

import cv2
import easyocr

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LABEL_FILE = PROJECT_ROOT / "web-spider-label.txt"
OUTPUT_FILE = PROJECT_ROOT / "output.txt"

# Same ROI as scripts/osd_crop_binarize.py: (x1, y1, x2, y2), origin top-left.
X1, Y1, X2, Y2 = 43, 971, 219, 1040

# Codes may contain letters (e.g. "IPC-322"), digits and a dash.
OCR_ALLOWLIST = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")
BEIJING_TZ = timezone(timedelta(hours=8))


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def read_labels(path: Path) -> list[tuple[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="gbk")

    entries = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise SystemExit(f"{path}:{lineno}: expected '<code> <rtsp url>'")
        expected, url = parts[0].strip(), parts[1].strip()
        if not url.lower().startswith(("rtsp://", "rtsps://")):
            raise SystemExit(f"{path}:{lineno}: not an RTSP url: {url}")
        entries.append((expected, url))

    if not entries:
        raise SystemExit(f"no entries found in: {path}")
    return entries


def crop_roi(frame):
    height, width = frame.shape[:2]
    if width < X2 or height < Y2:
        raise ValueError(
            f"frame is too small for ROI: {width}x{height}, need at least {X2}x{Y2}")
    return frame[Y1:Y2, X1:X2].copy()


def grab_frame(url: str, warmup: int, timeout: float):
    """Open the stream, decode `warmup` frames and return the last good one."""
    params = [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(timeout * 1000),
        cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(timeout * 1000),
    ]
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
    try:
        if not cap.isOpened():
            return None
        frame = None
        for _ in range(max(1, warmup)):
            ok, current = cap.read()
            if ok and current is not None:
                frame = current
        return frame
    finally:
        cap.release()


def recognize(reader, roi) -> tuple[str, float]:
    detections = reader.readtext(
        roi,
        allowlist=OCR_ALLOWLIST,
        detail=1,
        paragraph=False,
    )
    text = " ".join(item[1].strip() for item in detections if item[1].strip())
    text = re.sub(r"\s+", " ", text).strip()
    confidence = min((item[2] for item in detections), default=0.0)
    return text, confidence


def canonical(text: str) -> str:
    """Compare codes ignoring case, spaces, dashes and other separators."""
    return NON_ALNUM.sub("", text).upper()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, default=LABEL_FILE,
                        help=f"label file (default: {LABEL_FILE})")
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE,
                        help=f"mismatch output file (default: {OUTPUT_FILE})")
    parser.add_argument("--interval", type=float, default=60.0,
                        help="seconds between two capture rounds (default: 60)")
    parser.add_argument("--once", action="store_true",
                        help="run a single round and exit")
    parser.add_argument("--gpu", action="store_true",
                        help="run EasyOCR on GPU; default is CPU")
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="RTSP open/read timeout in seconds (default: 15)")
    parser.add_argument("--warmup", type=int, default=5,
                        help="frames to decode before taking the snapshot (default: 5)")
    args = parser.parse_args()

    if args.interval <= 0:
        raise SystemExit("--interval must be > 0")

    entries = read_labels(args.labels)
    reader = easyocr.Reader(["en"], gpu=args.gpu, verbose=False)
    print(f"device: {reader.device}, streams: {len(entries)}, "
          f"interval: {args.interval:g}s")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    round_no = 0
    with args.output.open("a", encoding="utf-8", buffering=1) as out:
        try:
            while True:
                round_no += 1
                started = time.monotonic()
                print(f"--- round {round_no} "
                      f"({now_beijing():%Y-%m-%d %H:%M:%S}) ---")

                for expected, url in entries:
                    try:
                        frame = grab_frame(url, args.warmup, args.timeout)
                        if frame is None:
                            print(f"[capture failed] {url}")
                            continue

                        roi = crop_roi(frame)
                        detected, confidence = recognize(reader, roi)
                        stamp = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
                        ok = canonical(detected) == canonical(expected)

                        print(f"[{stamp}] {url} expected={expected!r} "
                              f"detected={detected!r} conf={confidence:.3f} "
                              f"{'OK' if ok else 'MISMATCH'}")

                        if not ok:
                            out.write(f"{stamp}\t{expected}\t{detected}\n")
                    except Exception as exc:  # one bad stream must not stop others
                        print(f"[error] {url}: {exc}")

                if args.once:
                    break

                elapsed = time.monotonic() - started
                sleep_for = args.interval - elapsed
                if sleep_for > 0:
                    time.sleep(sleep_for)
        except KeyboardInterrupt:
            print("\nstopped by user")

    print(f"mismatches are appended to: {args.output}")


if __name__ == "__main__":
    main()
