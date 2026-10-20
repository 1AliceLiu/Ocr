"""Grab a 1080p frame from every channel and OCR the OSD, round after round.

Results are written to disk as soon as each channel finishes (line-buffered),
so output.txt / osd_error.txt grow in real time instead of at round end.

This is the token-aware version of rtsp_osd_monitor.py. The platform's RTSP
token lives ~30 seconds and is single-use, so each round asks MTS/StartVideo
for a fresh URL per channel and uses it immediately.

Default behaviour:
  * input  : out/streams.txt (all channels)
  * stream : main stream (1920x1080 or larger), no resizing
  * frame  : only keyframes are decoded (`-skip_frame nokey`), because joining a
             live stream mid-GOP yields grey frames with missing references
  * ROI    : bottom-left corner, anchored to the bottom edge, roughly
             0..300 x (h-150)..(h-8); works for 1080p/1440p/4MP/panorama
  * output : output.txt, one tab-separated line per channel per round:
             time  expected  detected  channelCode  channelName  confidence  status
             `--output-rotate-hours N` empties output.txt every N hours.
  * extra  : output/osd_rounds.csv (same data with a header)
             snapshots/osd20/<index>_<code>.jpg (latest frame, overwritten)

Anomaly handling (everything not OK = MISMATCH / NO_OCR / NO_EXPECTED / CAPTURE_FAILED):
  * `--error-log PATH`  appends the abnormal rows of each finished round to PATH
    (e.g. output/osd_error.txt), preceded by a `# round N <time>` marker.
  * `--error-dir DIR`   keeps the full frame of each abnormal channel in DIR
    (e.g. snapshots/osd_error), one `<index>_<code>.jpg` per channel, overwritten
    every round. Channels that fail before a frame exists (CAPTURE_FAILED) have
    no image, only a log line.
  * `--no-save`         do not keep frames of healthy channels anywhere.

Status values: OK, MISMATCH, NO_OCR, NO_EXPECTED, CAPTURE_FAILED.

Run it inside the easyocr container (it has cv2 + easyocr):

    docker compose exec -e EVO_BASE=... -e EVO_USER=... -e EVO_PASSWORD=... \
        easyocr python /work/scripts/evo_osd_monitor.py --once

    # continuous, every 60 seconds
    docker compose exec -e ... easyocr python /work/scripts/evo_osd_monitor.py
"""
from __future__ import annotations

import argparse
import contextlib
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

# OSD reference box on a 1920x1080 frame: x 43..219, y 971..1040.
# The OSD is anchored bottom-left and keeps roughly the same pixel height on
# 4MP/5MP/panorama cameras, so the ROI is taken relative to the bottom edge
# with some margin instead of using absolute 1080p coordinates.
OSD_X_MAX = 300          # right edge of the box, also capped at 12% of width
OSD_X_FRAC = 0.12
OSD_BOTTOM_MARGIN = 8    # pixels to ignore at the very bottom
OSD_HEIGHT = 150         # box height above that margin
# Below this EasyOCR confidence a keyframe is considered bad and another one is
# grabbed (a blurred keyframe or one captured mid OSD update).
OCR_MIN_CONF = 0.60
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
    x2 = min(int(width * OSD_X_FRAC), OSD_X_MAX)
    y2 = height - OSD_BOTTOM_MARGIN
    y1 = max(0, height - OSD_HEIGHT)
    if x2 < 40 or y2 - y1 < 20:
        raise ValueError(f"frame too small for ROI: {width}x{height}")
    return frame[y1:y2, 0:x2]


def grab_frame(ffmpeg: str, url: str, timeout: float, target: Path,
               keep: bool):
    """Grab one complete frame with the ffmpeg CLI (faster than cv2 VideoCapture).

    `-skip_frame nokey` decodes only keyframes: joining a live HEVC/H.264 stream
    mid-GOP makes the first decoded pictures reference frames that were never
    received ("Could not find ref with POC ..."), which look like grey blocks and
    make OCR fail. A keyframe is a self-contained picture, so the first one is
    already a stable, fully decoded frame.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-rtsp_transport", "tcp", "-skip_frame", "nokey", "-i", url,
    ]
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
        self.error_dir = args.error_dir
        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)
        if self.error_dir:
            self.error_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = args.ffmpeg or imageio_ffmpeg.get_ffmpeg_exe()

    def save_anomaly(self, channel: dict, frame) -> None:
        """Keep the full frame of a channel whose status is not OK."""
        if frame is None:
            return
        path = self.error_dir / safe_name(channel["index"], channel["channelCode"])
        try:
            if not cv2.imwrite(str(path), frame):
                raise OSError("cv2.imwrite returned False")
        except Exception as exc:  # a bad frame must not abort the round
            print(f"  warn: cannot save anomaly frame {path}: {exc}", flush=True)

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

    def ocr(self, frame):
        """Read the OSD digits in the bottom-left corner of one frame."""
        roi = crop_roi(frame)
        with self.ocr_lock:
            detections = self.reader.readtext(
                roi, allowlist="0123456789", detail=1, paragraph=False,
                mag_ratio=self.args.mag_ratio)
        text = "".join(item[1] for item in detections).strip()
        confidence = min((float(item[2]) for item in detections), default=0.0)
        return text, confidence

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
        expected = result["expected"]
        target_len = len(expected)
        frame = None
        reason = ""
        best = None  # (score, text, confidence, frame): best read of this channel
        # Capture retries (no frame), plus quality retries: when a keyframe still
        # gives no digits, the wrong number of digits or a low confidence, we grab
        # another keyframe and keep the best read. This is the "stable frame"
        # selection: a single keyframe can be blurred or mid-fade on some cameras.
        attempts = 1 + max(0, self.args.retries) + max(0, self.args.ocr_retries)
        for _ in range(attempts):
            body, data, url = self._start(code)
            try:
                if not is_ok(body) or not url:
                    reason = f"StartVideo({body.get('code')})"
                    continue
                target = (self.save_dir / safe_name(channel["index"], code)
                          if self.save_dir else
                          Path(tempfile.gettempdir()) / f"osd_{channel['index']}.jpg")
                frame = grab_frame(self.ffmpeg, url, self.args.timeout,
                                   target, keep=bool(self.save_dir))
            finally:
                self._stop(code, data.get("session"))
            if frame is None:
                reason = "capture timeout"
                continue

            text, confidence = self.ocr(frame)
            # prefer non-empty, then the digit count closest to the channel name,
            # then the highest confidence
            score = (bool(text),
                     -abs(len(text) - target_len) if text else -99,
                     confidence)
            if best is None or score > best[0]:
                best = (score, text, confidence, frame)
            if text and (not target_len or
                         (len(text) == target_len and confidence >= OCR_MIN_CONF)):
                break
            reason = "low quality OCR"

        if best is None:
            result["status"] = f"CAPTURE_FAILED({reason})" if reason else "CAPTURE_FAILED"
            return result

        _, detected, confidence, frame = best
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

        if self.error_dir and result["status"] != "OK":
            self.save_anomaly(channel, frame)
        return result


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
    parser.add_argument("--error-dir", type=Path, default=None,
                        help="keep frames of abnormal channels here "
                             "(e.g. /work/snapshots/osd_error)")
    parser.add_argument("--error-log", type=Path, default=None,
                        help="append abnormal channels of each round to this "
                             "file (e.g. /work/output/osd_error.txt)")
    parser.add_argument("--output-rotate-hours", type=float, default=0.0,
                        help="empty --output every N hours (0 = never)")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--stream-type", choices=["1", "2"], default="1",
                        help="1=main stream (1080p), 2=sub stream")
    parser.add_argument("--retries", type=int, default=1,
                        help="retry a failed capture with a fresh token")
    parser.add_argument("--ocr-retries", type=int, default=1,
                        help="grab another keyframe when the first read looks bad "
                             "(no digits, wrong digit count, low confidence)")
    parser.add_argument("--mag-ratio", type=float, default=2.0,
                        help="EasyOCR mag_ratio applied to the OSD crop")
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="ffmpeg timeout per capture (seconds); a keyframe "
                             "may take a full GOP to arrive")
    parser.add_argument("--ffmpeg", default="",
                        help="override ffmpeg binary (default: imageio-ffmpeg)")
    parser.add_argument("--gpu", action="store_true", help="EasyOCR on GPU")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--verify-tls", action="store_true")
    args = parser.parse_args()
    if args.no_save:
        args.save_dir = None
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
    for path in (args.output, csv_path, args.error_log):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
    rotate_seconds = max(0.0, args.output_rotate_hours) * 3600.0
    output_since = time.monotonic()
    round_no = 0

    while True:
        round_no += 1
        if rotate_seconds and time.monotonic() - output_since >= rotate_seconds:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text("", encoding="utf-8")
            output_since = time.monotonic()
            print(f"cleared {args.output} (every {args.output_rotate_hours:g}h)",
                  flush=True)
        started = time.monotonic()
        round_time = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
        print(f"--- round {round_no} ({now_beijing():%H:%M:%S}) ---", flush=True)

        # Stream every result to disk as soon as its channel is done, instead of
        # buffering the whole round (1521 channels = minutes of silence).
        results: list[dict] = []
        ok = 0
        with contextlib.ExitStack() as stack:
            out = stack.enter_context(
                args.output.open("a", encoding="utf-8", buffering=1))
            csv_fh = stack.enter_context(
                csv_path.open("a", encoding="utf-8-sig", newline=""))
            writer = csv.writer(csv_fh)
            if not csv_started:
                writer.writerow(["时间", "编号", "通道编码", "通道名称", "期望",
                                 "识别", "置信度", "状态"])
                csv_started = True
            err_fh = (stack.enter_context(
                args.error_log.open("a", encoding="utf-8", buffering=1))
                if args.error_log else None)
            if err_fh:
                err_fh.write(f"# round {round_no} {round_time}\n")

            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                futures = {pool.submit(monitor.one, ch): ch for ch in channels}
                for future in as_completed(futures):
                    ch = futures[future]
                    try:
                        r = future.result()
                    except Exception as exc:  # one bad stream must not stop others
                        r = {
                            "index": ch["index"], "channelCode": ch["channelCode"],
                            "name": ch["name"], "expected": expected_code(ch["name"]),
                            "detected": "", "confidence": 0.0,
                            "status": f"ERROR({type(exc).__name__})",
                            "time": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    results.append(r)
                    out.write(f"{r['time']}\t{r['expected']}\t{r['detected']}\t"
                              f"{r['channelCode']}\t{r['name']}\t"
                              f"{r['confidence']:.3f}\t{r['status']}\n")
                    writer.writerow([r["time"], r["index"], r["channelCode"],
                                     r["name"], r["expected"], r["detected"],
                                     f"{r['confidence']:.4f}", r["status"]])
                    csv_fh.flush()
                    if err_fh and r["status"] != "OK":
                        err_fh.write(f"{r['time']}\t{r['index']}\t{r['channelCode']}\t"
                                     f"{r['name']}\t{r['expected']}\t{r['detected']}\t"
                                     f"{r['confidence']:.3f}\t{r['status']}\n")
                    if r["status"] == "OK":
                        ok += 1
                    print(f"  {r['index']:>3} {r['name']:<12} expected={r['expected']!r} "
                          f"detected={r['detected']!r} conf={r['confidence']:.2f} "
                          f"{r['status']}", flush=True)

            if err_fh:
                err_fh.write(f"# round {round_no} done: {ok}/{len(results)} OK, "
                             f"{len(results) - ok} anomaly, "
                             f"{time.monotonic() - started:.1f}s\n")

        print(f"round {round_no}: {ok}/{len(results)} OK, "
              f"{len(results) - ok} anomaly, {time.monotonic() - started:.1f}s",
              flush=True)

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
