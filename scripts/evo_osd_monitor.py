"""Every minute: grab a 1080p frame from the first N channels and OCR the OSD.

This is the token-aware version of rtsp_osd_monitor.py. The platform's RTSP
token lives ~30 seconds and is single-use, so each round asks MTS/StartVideo
for a fresh URL per channel and uses it immediately.

Default behaviour:
  * input  : out/streams.txt (first 20 channels)
  * stream : main stream (1920x1080 or larger), no resizing
  * ROI    : bottom-left corner, (43, 971) - (219, 1040) in 1080p coordinates
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

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Bottom-left OSD ROI for a 1920x1080 frame: (x1, y1, x2, y2).
X1, Y1, X2, Y2 = 43, 971, 219, 1040
BEIJING_TZ = timezone(timedelta(hours=8))
DIGITS = re.compile(r"\D")


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def expected_code(name: str) -> str:
    match = re.search(r"\d+", name or "")
    return match.group(0) if match else ""


def canonical(text: str) -> str:
    return DIGITS.sub("", text or "")


def crop_roi(frame):
    height, width = frame.shape[:2]
    if width < X2 or height < Y2:
        raise ValueError(f"frame too small for ROI: {width}x{height}")
    return frame[Y1:Y2, X1:X2]


def grab_frame(ffmpeg: str, url: str, timeout: float, target: Path,
               keep: bool, skip_frames: int = 10):
    """Grab one frame with the ffmpeg CLI (much faster than cv2 VideoCapture).

    HEVC streams start with frames whose references are missing, which decode
    to a grey image. Selecting frame N>=skip_frames avoids that.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-rtsp_transport", "tcp", "-i", url,
    ]
    if skip_frames > 0:
        cmd += ["-vf", rf"select=gte(n\,{skip_frames})"]
    cmd += ["-frames:v", "1", "-q:v", "2", "-y", str(target)]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if not target.exists() or target.stat().st_size < 1024:
        return None
    frame = cv2.imread(str(target), cv2.IMREAD_COLOR)
    if not keep:
        target.unlink(missing_ok=True)
    return frame


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
        frame = None
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
                frame = grab_frame(self.ffmpeg, url, self.args.timeout,
                                   target, keep=bool(self.save_dir),
                                   skip_frames=self.args.skip_frames)
            finally:
                self._stop(code, data.get("session"))
            if frame is not None:
                break
            reason = "capture timeout"

        if frame is None:
            result["status"] = f"CAPTURE_FAILED({reason})" if reason else "CAPTURE_FAILED"
            return result

        roi = crop_roi(frame)
        with self.ocr_lock:
            detections = self.reader.readtext(
                roi, allowlist="0123456789", detail=1, paragraph=False)
        detected = "".join(item[1] for item in detections).strip()
        confidence = min((float(item[2]) for item in detections), default=0.0)

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
