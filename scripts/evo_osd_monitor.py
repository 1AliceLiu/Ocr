"""Every minute: grab a 1080p frame from the first N channels and OCR the OSD.

This is the token-aware version of rtsp_osd_monitor.py. The platform's RTSP
token lives ~30 seconds and is single-use, so each round asks MTS/StartVideo
for a fresh URL per channel and uses it immediately.

Default behaviour:
  * input  : out/streams.txt (first 20 channels)
  * stream : main stream (1920x1080 or larger), no resizing
  * frames : 3 consecutive frames per channel, OCR each, majority vote
  * ROI    : adaptive by default: narrow (43,971)-(219,1040) for numeric
             labels, wide (0,920)-(520,1080) for alphanumeric codes;
             override with --roi or --roi-file
  * chars  : alphanumeric by default (P40A2-087); --allowlist digits for 0-9
  * output : output.txt, one tab-separated line per channel per round:
             time  expected  detected  channelCode  channelName  confidence  status
  * extra  : output/osd_rounds.csv (same data with a header)
             snapshots/osd20/<index>_<code>.jpg (latest frame, overwritten)

Status values: OK, MISMATCH, NO_OCR, NO_EXPECTED, CAPTURE_FAILED.

Run it inside the easyocr container (it has cv2 + easyocr):

    docker compose exec -e EVO_BASE=... -e EVO_USER=... -e EVO_PASSWORD=... \
        easyocr python /work/scripts/evo_osd_monitor.py --once

    # continuous, every 60 seconds
    docker compose exec -e ... easyocr python /work/scripts/evo_osd_monitor.py
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import easyocr
import imageio_ffmpeg

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evo_pull import START_VIDEO, STOP_VIDEO, Platform, is_ok, pick_rtsp  # noqa: E402
from evo_snapshot import read_channels, safe_name  # noqa: E402
from osd_ocr import (  # noqa: E402
    ALLOW_ALNUM,
    ALLOW_DIGITS,
    detect_roi,
    majority,
    read_number,
    scale_box,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Bottom-left OSD ROI for a 1920x1080 frame: (x1, y1, x2, y2).
X1, Y1, X2, Y2 = 43, 971, 219, 1040
BEIJING_TZ = timezone(timedelta(hours=8))
NON_ALNUM = re.compile(r"[^0-9A-Za-z]")
WIDE_ROI = (0, 920, 520, 1080)
FIXED_ROI = (X1, Y1, X2, Y2)
# Common digit/letter confusions on the OSD font; applied only when the
# expected channel code is numeric.
DIGIT_CONFUSIONS = str.maketrans({"O": "0", "o": "0", "I": "1",
                                  "i": "1", "l": "1", "|": "1"})


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def expected_code(name: str) -> str:
    """'P40A2-087_1' -> 'P40A2-087'; '2544_1' -> '2544'."""
    stem = re.sub(r"_\d+$", "", name or "")
    if "-" in stem and re.search(r"[A-Za-z]", stem):
        return stem
    match = re.search(r"\d+", stem)
    return match.group(0) if match else stem


def canonical(text: str) -> str:
    """Compare codes ignoring case, spaces and separators (keeps letters)."""
    return NON_ALNUM.sub("", text or "").upper()


def parse_roi(text: str) -> tuple[int, int, int, int]:
    try:
        x1, y1, x2, y2 = (int(part) for part in text.split(","))
    except ValueError as exc:
        raise SystemExit("--roi must be x1,y1,x2,y2") from exc
    return x1, y1, x2, y2


def grab_frames(ffmpeg: str, url: str, timeout: float, target: Path,
                skip_frames: int = 10, count: int = 3):
    """Grab `count` consecutive frames with the ffmpeg CLI.

    HEVC streams start with frames whose references are missing, which decode
    to a grey image. Selecting frame N>=skip_frames avoids that. One RTSP
    connection is used for all frames (the stream token is single-use).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    for old in target.parent.glob(f"{target.stem}_*{target.suffix}"):
        old.unlink(missing_ok=True)
    target.unlink(missing_ok=True)

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-rtsp_transport", "tcp", "-i", url,
    ]
    if skip_frames > 0:
        cmd += ["-vf", rf"select=gte(n\,{skip_frames})"]
    pattern = None
    if count > 1:
        pattern = target.with_name(f"{target.stem}_%02d{target.suffix}")
        cmd += ["-frames:v", str(count), "-fps_mode", "passthrough",
                "-q:v", "2", "-y", str(pattern)]
    else:
        cmd += ["-frames:v", "1", "-q:v", "2", "-y", str(target)]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return []
    paths = (sorted(target.parent.glob(f"{target.stem}_*{target.suffix}"))
             if pattern else [target])
    frames = []
    for path in paths:
        if path.exists() and path.stat().st_size >= 1024:
            frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if frame is not None:
                frames.append(frame)
        path.unlink(missing_ok=True)
    return frames


class Monitor:
    def __init__(self, platform: Platform, reader, args):
        self.p = platform
        self.reader = reader
        self.args = args
        self.api_lock = threading.Lock()
        self.ocr_lock = threading.Lock()
        self.save_dir = args.save_dir
        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = args.ffmpeg or imageio_ffmpeg.get_ffmpeg_exe()
        self.allowlist = (ALLOW_ALNUM if args.allowlist == "alnum"
                          else ALLOW_DIGITS)
        self.roi = parse_roi(args.roi) if args.roi else None
        self.roi_map = {}
        if args.roi_file:
            with Path(args.roi_file).open(encoding="utf-8") as handle:
                self.roi_map = json.load(handle)

    def roi_options(self, frame, channel_code: str, expected: str):
        """ROIs to try, best first. Adaptive = narrow for numeric labels."""
        height, width = frame.shape[:2]
        custom = (self.roi_map.get(channel_code)
                  or self.roi_map.get("*"))
        if custom:
            x1, y1, x2, y2 = scale_box(custom, width, height)
            return [frame[y1:y2, x1:x2]]
        if self.roi:
            x1, y1, x2, y2 = scale_box(self.roi, width, height)
            return [frame[y1:y2, x1:x2]]

        mode = self.args.roi_mode
        if mode == "auto":
            return [detect_roi(frame)]
        if mode == "fixed":
            boxes = [FIXED_ROI]
        elif mode == "wide":
            boxes = [WIDE_ROI]
        else:  # adaptive
            boxes = ([FIXED_ROI, WIDE_ROI] if expected.isdigit()
                     else [WIDE_ROI, FIXED_ROI])
        scaled = [scale_box(box, width, height) for box in boxes]
        return [frame[y1:y2, x1:x2] for x1, y1, x2, y2 in scaled]

    @staticmethod
    def is_good(text: str, expected: str) -> bool:
        if not text:
            return False
        if expected.isdigit():
            return text.isdigit() and len(text) == len(expected)
        return any(ch.isalpha() for ch in text)

    def _start(self, code: str):
        with self.api_lock:
            body = self.p.post(START_VIDEO, {"data": {
                "dataType": "3",
                "streamType": self.args.stream_type,
                "channelId": code,
                "urlType": 1,
                "packtype": "Private",
                "optModule": "ADMIN_001031",
            }})
        data = body.get("data") or {}
        return body, data, pick_rtsp(data.get("url") or "",
                                     data.get("token") or "")

    def _stop(self, code: str, session) -> None:
        if not session:
            return
        with self.api_lock:
            self.p.post(STOP_VIDEO, {"data": {
                "channelId": code, "session": str(session),
                "optModule": "ADMIN_001031",
            }})

    def one(self, channel: dict) -> dict:
        code = channel["channelCode"]
        result = {
            "index": channel["index"],
            "channelCode": code,
            "name": channel["name"],
            "expected": expected_code(channel["name"]),
            "detected": "",
            "confidence": 0.0,
            "status": "CAPTURE_FAILED",
            "time": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
        }
        frames = []
        reason = ""
        for _ in range(1 + max(0, self.args.retries)):
            body, data, url = self._start(code)
            try:
                if not is_ok(body) or not url:
                    reason = f"StartVideo({body.get('code')})"
                    continue
                target = (self.save_dir / safe_name(channel["index"], code)
                          if self.save_dir else
                          Path(tempfile.gettempdir()) / f"osd_{channel['index']}.jpg")
                frames = grab_frames(self.ffmpeg, url, self.args.timeout,
                                     target, skip_frames=self.args.skip_frames,
                                     count=self.args.frames)
            finally:
                self._stop(code, data.get("session"))
            if frames:
                break
            reason = "capture timeout"

        if not frames:
            result["status"] = f"CAPTURE_FAILED({reason})" if reason else "CAPTURE_FAILED"
            return result

        expected = result["expected"]
        expected_len = len(expected) or None
        numeric = expected.isdigit()
        candidates = []
        with self.ocr_lock:
            for frame in frames:
                best = None
                for roi in self.roi_options(frame, code, expected):
                    text, conf, _src = read_number(
                        self.reader, roi, expected_len, self.allowlist,
                        numeric_expected=numeric)
                    if numeric:
                        text = text.translate(DIGIT_CONFUSIONS)
                    if best is None or conf > best[1]:
                        best = (text, conf, frame)
                    if self.is_good(text, expected):
                        break
                if best is not None:
                    candidates.append(best)

        # The OSD number is constant, so the most frequent reading wins.
        detected = majority([c[0] for c in candidates])
        matches = [c for c in candidates if detected and c[0] == detected]
        confidence = max((c[1] for c in matches), default=0.0)

        if self.save_dir:
            best = max(matches, key=lambda c: c[1]) if matches else candidates[0]
            cv2.imwrite(str(self.save_dir / safe_name(channel["index"], code)),
                        best[2])

        result["votes"] = [c[0] for c in candidates]
        result["detected"] = detected
        result["confidence"] = confidence
        if not detected:
            result["status"] = "NO_OCR"
        elif not result["expected"]:
            result["status"] = "NO_EXPECTED"
        elif canonical(detected) == canonical(result["expected"]):
            result["status"] = "OK"
        else:
            result["status"] = "MISMATCH"
        return result


def write_csv(path: Path, rows: list[dict], header: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        if header:
            writer.writerow(["时间", "编号", "通道编码", "通道名称", "期望",
                             "识别", "置信度", "状态"])
        for r in rows:
            writer.writerow([r["time"], r["index"], r["channelCode"],
                             r["name"], r["expected"], r["detected"],
                             f"{r['confidence']:.4f}", r["status"]])


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.getenv("EVO_BASE", ""))
    parser.add_argument("--user", default=os.getenv("EVO_USER", ""))
    parser.add_argument("--password", default=os.getenv("EVO_PASSWORD", ""))
    parser.add_argument("--token", default="")
    parser.add_argument("--user-id", default="1")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "out" / "streams.txt")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "output.txt")
    parser.add_argument("--csv", type=Path,
                        default=PROJECT_ROOT / "output" / "osd_rounds.csv")
    parser.add_argument("--save-dir", type=Path,
                        default=PROJECT_ROOT / "snapshots" / "osd20")
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--stream-type", choices=["1", "2"], default="1",
                        help="1=main stream (1080p), 2=sub stream")
    parser.add_argument("--frames", type=int, default=3,
                        help="frames per channel per round, majority-voted")
    parser.add_argument("--allowlist", choices=["digits", "alnum"],
                        default="alnum",
                        help="digits: 0-9 only; alnum: A-Z a-z 0-9 and dash")
    parser.add_argument("--roi-mode",
                        choices=["adaptive", "wide", "fixed", "auto"],
                        default="adaptive",
                        help="adaptive: narrow for numeric labels, wide for "
                             "alphanumeric; wide: (0,920)-(520,1080); "
                             "fixed: (43,971)-(219,1040)")
    parser.add_argument("--roi", default="",
                        help="fixed x1,y1,x2,y2; overrides --roi-mode")
    parser.add_argument("--roi-file", default="",
                        help="JSON map channelCode -> [x1,y1,x2,y2]")
    parser.add_argument("--retries", type=int, default=1,
                        help="retry a failed capture with a fresh token")
    parser.add_argument("--skip-frames", type=int, default=10,
                        help="decode N frames before saving (HEVC grey frames)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="ffmpeg timeout per capture (seconds)")
    parser.add_argument("--ffmpeg", default="",
                        help="override ffmpeg binary (default: imageio-ffmpeg)")
    parser.add_argument("--gpu", action="store_true", help="EasyOCR on GPU")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--verify-tls", action="store_true")
    args = parser.parse_args()
    if not args.base:
        parser.error("--base is required (or set EVO_BASE)")
    if not args.input.exists():
        raise SystemExit(f"input not found: {args.input}")

    channels = read_channels(args.input)
    if args.limit:
        channels = channels[:args.limit]
    if not channels:
        raise SystemExit("no channels in input file")

    p = Platform(args.base, args.user, args.password, args.token,
                 args.user_id, args.verify_tls)
    if not p.token:
        p.login()

    reader = easyocr.Reader(["en"], gpu=args.gpu, verbose=False)
    print(f"device: {reader.device}, channels: {len(channels)}, "
          f"interval: {args.interval:g}s, stream: main", flush=True)

    monitor = Monitor(p, reader, args)
    csv_path = args.csv
    csv_started = csv_path.exists() and csv_path.stat().st_size > 0
    round_no = 0

    while True:
        round_no += 1
        started = time.monotonic()
        print(f"--- round {round_no} ({now_beijing():%H:%M:%S}) ---", flush=True)

        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(monitor.one, ch): ch for ch in channels}
            for future in as_completed(futures):
                ch = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # one bad stream must not stop others
                    results.append({
                        "index": ch["index"], "channelCode": ch["channelCode"],
                        "name": ch["name"], "expected": expected_code(ch["name"]),
                        "detected": "", "confidence": 0.0,
                        "status": f"ERROR({type(exc).__name__})",
                        "time": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
                    })

        results.sort(key=lambda r: r["index"])
        with args.output.open("a", encoding="utf-8", buffering=1) as out:
            for r in results:
                out.write(f"{r['time']}\t{r['expected']}\t{r['detected']}\t"
                          f"{r['channelCode']}\t{r['name']}\t"
                          f"{r['confidence']:.3f}\t{r['status']}\n")
                print(f"  {r['index']:>3} {r['name']:<12} expected={r['expected']!r} "
                      f"detected={r['detected']!r} conf={r['confidence']:.2f} "
                      f"{r['status']}", flush=True)

        write_csv(csv_path, results, header=not csv_started)
        csv_started = True
        ok = sum(1 for r in results if r["status"] == "OK")
        print(f"round {round_no}: {ok}/{len(results)} OK, "
              f"{time.monotonic() - started:.1f}s", flush=True)

        if args.once:
            break
        elapsed = time.monotonic() - started
        sleep_for = args.interval - elapsed
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            print(f"warning: round took {elapsed:.1f}s > interval", flush=True)


if __name__ == "__main__":
    main()
