"""Grab a 1080p keyframe from every channel and OCR the OSD, round after round.

Results are written to disk as soon as each channel finishes (line-buffered),
so output.txt / osd_error.txt grow in real time instead of at round end.

Token-aware: the platform's RTSP token lives ~30 seconds and is single-use, so
each round asks MTS/StartVideo for a fresh URL per channel and uses it
immediately.

Default behaviour:
  * input  : out/streams.txt (all channels)
  * stream : main stream (1920x1080 or larger), no resizing
  * frames : keyframes only (`-skip_frame nokey`), 1 per channel by default;
             `--frames N` majority-votes N keyframes (slower: one GOP each)
  * ROI    : adaptive by default: narrow (43,971)-(219,1040) for numeric
             labels, wide (0,920)-(520,1080) for alphanumeric codes; the boxes
             are scaled to the real frame height; override with --roi/--roi-file
  * chars  : alphanumeric by default (P40A2-087); --allowlist digits for 0-9
  * output : output.txt, one tab-separated line per channel per round:
             time  expected  detected  channelCode  channelName  confidence  status
             `--output-rotate-hours N` empties output.txt every N hours.
  * extra  : output/osd_rounds.csv (same data with a header)
             snapshots/osd20/<index>_<code>.jpg (latest frame, overwritten)

Anomaly handling (everything not OK = MISMATCH / NO_OCR / NO_EXPECTED / CAPTURE_FAILED):
  * `--error-log PATH`  appends the abnormal rows of each round to PATH
    (e.g. output/osd_error.txt), preceded by a `# round N <time>` marker.
    `CAPTURE_FAILED(capture timeout)` rows are NOT written there: a timeout is a
    stream/network problem, not an OSD anomaly (they stay in output.txt).
  * `--error-dir DIR`   keeps the full frame of each abnormal channel in DIR
    (e.g. snapshots/osd_error), one `<index>_<code>.jpg` per channel, overwritten
    every round. Channels that fail before a frame exists (CAPTURE_FAILED) have
    no image, only a log line.
  * `--no-save`         do not keep frames of healthy channels anywhere.

Status values: OK, MISMATCH, NO_OCR, NO_EXPECTED, CAPTURE_FAILED.

Run it inside the easyocr container (it has cv2 + easyocr):

    docker compose exec -e EVO_BASE=... -e EVO_USER=... -e EVO_PASSWORD=... \
        easyocr python /work/scripts/evo_osd_monitor.py --once

    # continuous
    docker compose exec -e ... easyocr python /work/scripts/evo_osd_monitor.py
"""
from __future__ import annotations

import argparse
import contextlib
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
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Bottom-left OSD ROI for a 1920x1080 frame: (x1, y1, x2, y2).
X1, Y1, X2, Y2 = 43, 971, 219, 1040
FIXED_ROI = (X1, Y1, X2, Y2)
WIDE_ROI = (0, 920, 520, 1080)

# The OSD is anchored bottom-left and keeps roughly the same pixel height on
# 4MP/5MP/panorama cameras, so these boxes are measured from the bottom edge.
OSD_X_MAX = 300          # right edge of the bottom box, capped at 12% of width
OSD_X_FRAC = 0.12
OSD_BOTTOM_MARGIN = 8    # pixels to ignore at the very bottom
OSD_HEIGHT = 150         # box height above that margin
ROI_BASE_HEIGHT = 180    # search band for content-based ROI detection
ROI_BASE_WIDTH = 760

BEIJING_TZ = timezone(timedelta(hours=8))
NON_ALNUM = re.compile(r"[^0-9A-Za-z]")
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


def scale_box(box: tuple[int, int, int, int], width: int,
              height: int) -> tuple[int, int, int, int]:
    """Scale a 1080p reference box to the real frame, with a little margin.

    Some 4MP/5MP cameras draw the OSD a few pixels further left than a strict
    h/1080 scaling predicts, so the left/top edges are padded.
    """
    factor = height / 1080.0
    x1, y1, x2, y2 = box
    x1 = max(0, int(x1 * factor) - 20)
    y1 = max(0, int(y1 * factor) - 10)
    x2 = min(width, int(x2 * factor))
    y2 = min(height, int(y2 * factor))
    return x1, y1, x2, y2


def crop_roi(frame):
    """Bottom-anchored fallback box (works for 1080p/1440p/4MP/panorama)."""
    height, width = frame.shape[:2]
    x2 = min(int(width * OSD_X_FRAC), OSD_X_MAX)
    y2 = height - OSD_BOTTOM_MARGIN
    y1 = max(0, height - OSD_HEIGHT)
    if x2 < 40 or y2 - y1 < 20:
        raise ValueError(f"frame too small for ROI: {width}x{height}")
    return frame[y1:y2, 0:x2]


def roi_base(frame):
    """Bottom-left band searched by the content-based ROI detector."""
    height, width = frame.shape[:2]
    return (0, max(0, height - ROI_BASE_HEIGHT), min(width, ROI_BASE_WIDTH), height)


def is_capture_timeout(status: str) -> bool:
    """True for `CAPTURE_FAILED(capture timeout)`.

    A timeout means the stream could not be opened/decoded in time (network,
    platform load, camera offline), not that the OSD is wrong. Those rows stay in
    output.txt but are kept out of osd_error.txt, so that file only lists
    channels whose picture was actually inspected.
    """
    return status.startswith("CAPTURE_FAILED") and "capture timeout" in status


def is_logged_anomaly(status: str) -> bool:
    return status != "OK" and not is_capture_timeout(status)


def grab_frames(ffmpeg: str, url: str, timeout: float, target: Path,
                skip_frames: int = 10, count: int = 3,
                keyframes: bool = True):
    """Grab `count` frames with the ffmpeg CLI, one RTSP connection.

    `keyframes=True` decodes only keyframes: joining a live HEVC/H.264 stream
    mid-GOP makes the leading pictures reference frames that were never
    received ("Could not find ref with POC ..."), which decode to grey blocks
    and make OCR fail. A keyframe is a self-contained picture, so every captured
    frame is complete. `keyframes=False` keeps the old "decode everything, then
    skip the first N frames" behaviour.

    All frames share one connection because the stream token is single-use.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    for old in target.parent.glob(f"{target.stem}_*{target.suffix}"):
        old.unlink(missing_ok=True)
    target.unlink(missing_ok=True)

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-rtsp_transport", "tcp",
    ]
    if keyframes:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-i", url]
    if not keyframes and skip_frames > 0:
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
        self.error_dir = args.error_dir
        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)
        if self.error_dir:
            self.error_dir.mkdir(parents=True, exist_ok=True)
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
        custom = (self.roi_map.get(channel_code)
                  or self.roi_map.get("*"))
        height, width = frame.shape[:2]
        if custom:
            x1, y1, x2, y2 = custom
            return [frame[y1:y2, x1:x2]]
        if self.roi:
            x1, y1, x2, y2 = self.roi
            return [frame[y1:y2, x1:x2]]

        mode = self.args.roi_mode
        if mode == "auto":
            return [detect_roi(frame, base=roi_base(frame))]
        if mode == "bottom":
            return [crop_roi(frame)]
        if mode == "fixed":
            boxes = [scale_box(FIXED_ROI, width, height)]
        elif mode == "wide":
            boxes = [scale_box(WIDE_ROI, width, height)]
        else:  # adaptive
            narrow = scale_box(FIXED_ROI, width, height)
            wide = scale_box(WIDE_ROI, width, height)
            boxes = ([narrow, wide] if expected.isdigit() else [wide, narrow])
        return [frame[y1:y2, x1:x2] for x1, y1, x2, y2 in boxes]

    @staticmethod
    def is_good(text: str, expected: str) -> bool:
        if not text:
            return False
        if expected.isdigit():
            return text.isdigit() and len(text) == len(expected)
        return any(ch.isalpha() for ch in text)

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
        expected_len = len(expected) or None
        numeric = expected.isdigit()

        frames: list = []
        candidates: list[tuple[str, float, object]] = []
        reason = ""
        # Capture retries (no frame at all) and quality retries (empty read or
        # wrong digit count) are counted separately: a dead stream must not burn
        # the OCR budget, and a bad read must not turn into a long capture loop.
        capture_left = 1 + max(0, self.args.retries)
        quality_left = max(0, self.args.ocr_retries)
        while capture_left > 0:
            capture_left -= 1
            body, data, url = self._start(code)
            try:
                if not is_ok(body) or not url:
                    reason = f"StartVideo({body.get('code')})"
                    continue
                target = (self.save_dir / safe_name(channel["index"], code)
                          if self.save_dir else
                          Path(tempfile.gettempdir()) / f"osd_{channel['index']}.jpg")
                frames = grab_frames(self.ffmpeg, url, self.args.timeout, target,
                                     skip_frames=self.args.skip_frames,
                                     count=self.args.frames,
                                     keyframes=self.args.frame_source == "key")
            finally:
                self._stop(code, data.get("session"))
            if not frames:
                reason = "capture timeout"
                continue

            candidates = []
            with self.ocr_lock:
                for frame in frames:
                    best = None
                    for roi in self.roi_options(frame, code, expected):
                        if roi.size == 0:
                            continue
                        try:
                            text, conf, _source = read_number(
                                self.reader, roi, expected_len, self.allowlist,
                                numeric_expected=numeric,
                                mag_ratio=self.args.mag_ratio)
                        except Exception:  # one bad crop must not kill the channel
                            continue
                        if numeric:
                            text = text.translate(DIGIT_CONFUSIONS)
                        if best is None or conf > best[1]:
                            best = (text, conf, frame)
                        if self.is_good(text, expected):
                            break
                    if best is not None:
                        candidates.append(best)

            detected = majority([c[0] for c in candidates])
            if detected and self.is_good(detected, expected):
                break
            reason = "low quality OCR"
            if quality_left > 0:
                quality_left -= 1
                capture_left += 1  # spend one more capture on reading quality

        if not frames:
            result["status"] = f"CAPTURE_FAILED({reason})" if reason else "CAPTURE_FAILED"
            return result

        # The OSD number is constant, so the most frequent reading wins.
        detected = majority([c[0] for c in candidates])
        matches = [c for c in candidates if detected and c[0] == detected]
        confidence = max((c[1] for c in matches), default=0.0)
        result["votes"] = [c[0] for c in candidates]
        result["detected"] = detected
        result["confidence"] = confidence
        if not detected:
            result["status"] = "NO_OCR"
        elif not expected:
            result["status"] = "NO_EXPECTED"
        elif canonical(detected) == canonical(expected):
            result["status"] = "OK"
        else:
            result["status"] = "MISMATCH"

        # Keep the frame that produced the winning reading (falls back to the
        # first captured frame when nothing was read).
        if matches:
            best_frame = max(matches, key=lambda c: c[1])[2]
        elif candidates:
            best_frame = candidates[0][2]
        else:
            best_frame = frames[0]
        if self.save_dir:
            cv2.imwrite(str(self.save_dir / safe_name(channel["index"], code)),
                        best_frame)
        if self.error_dir and result["status"] != "OK":
            self.save_anomaly(channel, best_frame)
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
    parser.add_argument("--no-save", action="store_true",
                        help="do not keep frames of healthy channels")
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
    parser.add_argument("--frames", type=int, default=None,
                        help="frames per channel per round, majority-voted "
                             "(default: 1 with --frame-source key, else 3)")
    parser.add_argument("--frame-source", choices=["key", "all"], default="key",
                        help="key: decode keyframes only (complete pictures, "
                             "default); all: decode everything and skip "
                             "--skip-frames frames")
    parser.add_argument("--allowlist", choices=["digits", "alnum"],
                        default="alnum",
                        help="digits: 0-9 only; alnum: A-Z a-z 0-9 and dash")
    parser.add_argument("--roi-mode",
                        choices=["adaptive", "wide", "fixed", "auto", "bottom"],
                        default="adaptive",
                        help="adaptive: narrow for numeric labels, wide for "
                             "alphanumeric; wide: (0,920)-(520,1080); "
                             "fixed: (43,971)-(219,1040); auto: content-based "
                             "detection; bottom: bottom-anchored box")
    parser.add_argument("--roi", default="",
                        help="fixed x1,y1,x2,y2; overrides --roi-mode")
    parser.add_argument("--roi-file", default="",
                        help="JSON map channelCode -> [x1,y1,x2,y2]")
    parser.add_argument("--retries", type=int, default=1,
                        help="retry a failed capture with a fresh token")
    parser.add_argument("--ocr-retries", type=int, default=1,
                        help="grab another set of frames when the reading is "
                             "empty or has the wrong length")
    parser.add_argument("--skip-frames", type=int, default=10,
                        help="only with --frame-source all: decode N frames "
                             "before saving")
    parser.add_argument("--mag-ratio", type=float, default=1.0,
                        help="EasyOCR mag_ratio (image is already upscaled 3x)")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="ffmpeg timeout per capture (seconds); a keyframe "
                             "may take a full GOP to arrive")
    parser.add_argument("--ffmpeg", default="",
                        help="override ffmpeg binary (default: imageio-ffmpeg)")
    parser.add_argument("--gpu", action="store_true", help="EasyOCR on GPU")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--verify-tls", action="store_true")
    args = parser.parse_args()
    if args.frames is None:
        args.frames = 1 if args.frame_source == "key" else 3
    if args.frames < 1:
        parser.error("--frames must be >= 1")
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
          f"interval: {args.interval:g}s, stream: {args.stream_type}, "
          f"frames: {args.frames} ({args.frame_source}), "
          f"roi: {args.roi_mode}, chars: {args.allowlist}", flush=True)

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
        logged = 0
        timeouts = 0
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
                    if is_capture_timeout(r["status"]):
                        timeouts += 1
                    elif err_fh and r["status"] != "OK":
                        logged += 1
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
                             f"{logged} anomaly logged, "
                             f"{timeouts} capture timeout skipped, "
                             f"{time.monotonic() - started:.1f}s\n")

        print(f"round {round_no}: {ok}/{len(results)} OK, {logged} anomaly, "
              f"{timeouts} capture timeout, {time.monotonic() - started:.1f}s",
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
