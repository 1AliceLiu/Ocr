"""Grab a 1080p keyframe from every channel and OCR the OSD, round after round.

Results are written to disk as soon as each channel finishes (line-buffered),
so output.txt / osd_error.txt grow in real time instead of at round end.

Token-aware: the platform's RTSP token lives ~30 seconds and is single-use, so
each round asks MTS/StartVideo for a fresh URL per channel and uses it
immediately.

Default behaviour:
  * input  : out/streams.txt (all channels)
  * stream : main stream (1920x1080 or larger), no resizing
  * frames : keyframes only (`-skip_frame nokey`), 3 per channel by default,
             majority-voted (each keyframe costs one GOP -> slower, steadier)
  * ROI    : numeric channels use a per-channel profile ROI (default bottom-left
             320x100 at 1080p), row projection finds the OSD line, and cells are
             split by column projection.  `--cell-reader legacy` restores the
             old content-based ROI/whole-line path.  `--roi` / `--roi-file`
             still override the profile.
  * chars  : alphanumeric by default (P40A2-087); --allowlist digits for 0-9
             Chinese labels (traditional 繁體 and simplified 简体) are read by a
             fallback model when the latin read is empty; --cjk off disables it
  * cells  : for numeric channels, each cell keeps the whole white glyph fill,
             is inverted to solid black on white, and is read with EasyOCR
             `recognize()`; a low-confidence/segment failure falls back to the
             legacy whole-line reader.
  * vote   : every reading (3 frames x ROIs x 2 preprocessing variants) is one
             vote; for numeric codes only readings with the expected digit count
             vote, so '72436' cannot outvote the real '2436'
  * decoder: --decoder greedy (default) | beamsearch (slower, steadier);
             --cell-decoder defaults to beamsearch for per-cell reads
  * output : output.txt, one tab-separated line per channel per round:
             time  expected  detected  channelCode  channelName  confidence  status
             `--output-rotate-hours N` empties output.txt every N hours.
  * extra  : output/osd_rounds.csv (same data with a header)
             snapshots/osd20/<index>_<code>.jpg (latest frame, overwritten)

Anomaly handling (OSD verdicts: OK / MISMATCH / UNCERTAIN / NO_OSD; capture
failures are separate):
  * `--error-log PATH`  appends the abnormal rows of each round to PATH
    (e.g. output/osd_error.txt), preceded by a `# round N <time>` marker.
    `CAPTURE_FAILED(capture timeout)` rows are NOT written there: a timeout is a
    stream/network problem, not an OSD anomaly (they stay in output.txt).
  * `--error-dir DIR`   keeps the full frame of each abnormal channel in DIR
    (e.g. snapshots/osd_error), one `<index>_<code>.jpg` per channel, inside a
    per-round folder `round_<N>_<YYYYMMDD_HHMMSS>`. Frames are therefore kept
    for every round instead of being overwritten. Channels that fail before a
    frame exists (CAPTURE_FAILED) have no image, only a log line.
  * `--no-save`         do not keep frames of healthy channels anywhere.

OSD status values: OK, MISMATCH, UNCERTAIN, NO_OSD.
`CAPTURE_FAILED(...)` is a capture failure, not an OSD verdict.
Crossover is reported as MISMATCH, with `crossover=True` / `reason=CROSSOVER`.

Matching rule for OK: both the channel code and the scan are reduced to their
LAST contiguous digit group (letters/separators ignored), so 'MB_ZM_DO_CE_0679'
-> '0679' and 'P4042-032' -> '032'. Alphanumeric codes need the same number;
numeric codes also match when the scanned number contains them contiguously;
a leading 0 is optional. e.g. '0679' matches '679'/'X-0679'; '2436' matches
'12436', not '436'.

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
    scale_box,
)
from osd_digits import (  # noqa: E402
    default_profile,
    load_profiles,
    read_digits,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_PATH = PROJECT_ROOT / "config" / "osd_profiles.json"

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
# CJK ideographs: the OSD label is sometimes Chinese (traditional or simplified).
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# Common digit/letter confusions on the OSD font; applied only when the
# expected channel code is numeric.
DIGIT_CONFUSIONS = str.maketrans({"O": "0", "o": "0", "I": "1",
                                  "i": "1", "l": "1", "|": "1"})


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def expected_code(name: str) -> str:
    """'P40A2-087_1' -> 'P40A2-087'; '2544_1' -> '2544'; 'MB_ZM_DO_CE_0673' -> '0673'."""
    name = name or ""
    stem = re.sub(r"_\d+$", "", name)
    # Keep the trailing number when the stem has no digits left: the last part
    # of 'MB_ZM_DO_CE_0673' *is* the code, not a sequence suffix.
    if not re.search(r"\d", stem):
        stem = name
    if "-" in stem and re.search(r"[A-Za-z]", stem):
        return stem
    match = re.search(r"\d+", stem)
    return match.group(0) if match else stem


def canonical(text: str) -> str:
    """Compare codes ignoring case, spaces and separators (keeps letters)."""
    return NON_ALNUM.sub("", text or "").upper()


def has_cjk(text: str) -> bool:
    return bool(CJK.search(text or ""))


DIGITS_RUN = re.compile(r"\d+")


def digits_of(text: str) -> str:
    return "".join(DIGITS_RUN.findall(text or ""))


def tail_digits(text: str) -> str:
    """Last contiguous digit group: 'P4042-032' -> '032'."""
    runs = DIGITS_RUN.findall(text or "")
    return runs[-1] if runs else ""


def numbers_match(expected: str, detected: str) -> bool:
    """Compare a channel code with the scanned OSD label by their numbers.

    Both sides are reduced to their LAST contiguous digit group, letters and
    separators are ignored:

        MB_ZM_DO_CE_0679  -> 0679
        P4042-032         -> 032

    * code with letters and digits: the two trailing numbers must be the same
      ('0679' matches '0679', 'X-0679'; it does not match '032').
    * code with digits only: the scanned number may carry extra digits around
      it, as long as all the code digits appear contiguously ('2436' matches
      '12436' and '24361', not '436').
    * a leading 0 is optional on both sides, so '0679' and '679' are the same
      code ("有0无0都算对").
    """
    code = tail_digits(expected)
    scanned = tail_digits(detected)
    if not code or not scanned:
        return False

    def same(a: str, b: str) -> bool:
        return a == b or (a.lstrip("0") or a) == (b.lstrip("0") or b)

    if same(code, scanned):
        return True
    if re.search(r"[A-Za-z]", expected or ""):
        return False
    # pure numeric code: allow extra digits around it in the scan
    return code in scanned or (code.lstrip("0") or code) in scanned.lstrip("0")


def parse_roi(text: str) -> tuple[int, int, int, int]:
    try:
        x1, y1, x2, y2 = (int(part) for part in text.split(","))
    except ValueError as exc:
        raise SystemExit("--roi must be x1,y1,x2,y2") from exc
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
    scale = height / 1080.0
    return (0, max(0, height - round(ROI_BASE_HEIGHT * scale)),
            min(width, round(ROI_BASE_WIDTH * scale)), height)


def is_capture_timeout(status: str) -> bool:
    """True for `CAPTURE_FAILED(capture timeout)`.

    A timeout means the stream could not be opened/decoded in time (network,
    platform load, camera offline), not that the OSD is wrong. Those rows stay in
    output.txt but are kept out of osd_error.txt, so that file only lists
    channels whose picture was actually inspected.
    """
    return status.startswith("CAPTURE_FAILED") and "capture timeout" in status


def is_logged_anomaly(status: str) -> bool:
    """Rows that should reach osd_error.txt.

    UNCERTAIN is intentionally kept out of the error log: it means the picture
    was not readable enough to make a claim.  It still appears in output.txt and
    osd_rounds.csv for diagnostics.
    """
    return (status != "OK"
            and not is_capture_timeout(status)
            and not status.startswith("UNCERTAIN"))


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
        self.error_root = args.error_dir
        self.error_dir = None
        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)
        if self.error_root:
            self.error_root.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = args.ffmpeg or imageio_ffmpeg.get_ffmpeg_exe()
        self.allowlist = (ALLOW_ALNUM if args.allowlist == "alnum"
                          else ALLOW_DIGITS)
        self.roi = parse_roi(args.roi) if args.roi else None
        self.roi_map = {}
        if args.roi_file:
            with Path(args.roi_file).open(encoding="utf-8") as handle:
                self.roi_map = json.load(handle)

        self.profile_data = load_profiles(args.osd_profile_file)
        self.profiles = self.profile_data.get("channels", {})
        self.profile_defaults = self.profile_data.get("defaults", {})
        self.expected_codes: set[str] = set()
        # Chinese fallback models. EasyOCR cannot mix ch_sim and ch_tra in one
        # model ("Chinese_tra is only compatible with English"), so there is one
        # reader per script; both are only used when the latin read is empty.
        self.cjk_readers = []
        for lang, wanted in (("ch_sim", args.cjk in ("sim", "both")),
                             ("ch_tra", args.cjk in ("tra", "both"))):
            if not wanted:
                continue
            try:
                self.cjk_readers.append(
                    easyocr.Reader([lang, "en"], gpu=args.gpu, verbose=False))
            except Exception as exc:  # missing model / download failure
                print(f"  warn: cannot load {lang} OCR model: {exc}", flush=True)

    def roi_options(self, frame, channel_code: str, expected: str):
        """ROIs to try, best first. Adaptive = narrow for numeric labels."""
        height, width = frame.shape[:2]
        custom = (self.roi_map.get(channel_code)
                  or self.roi_map.get("*"))
        height, width = frame.shape[:2]
        if custom:
            x1, y1, x2, y2 = scale_box(custom, width, height)
            return [frame[y1:y2, x1:x2]]
        if self.roi:
            x1, y1, x2, y2 = scale_box(self.roi, width, height)
            return [frame[y1:y2, x1:x2]]

        mode = self.args.roi_mode
        if mode == "auto":
            # Tight crop around the detected text; the wide box still gets a
            # vote in case the detector picked the wrong blob.
            rois = []
            try:
                tight = detect_roi(frame, base=roi_base(frame))
            except Exception:
                tight = None
            if (tight is not None and tight.size
                    and tight.shape[0] >= 10 and tight.shape[1] >= 20):
                rois.append(tight)
            wide = scale_box(WIDE_ROI, width, height)
            rois.append(frame[wide[1]:wide[3], wide[0]:wide[2]])
            return rois
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

    def profile_for(self, channel: dict, expected: str) -> dict:
        """Per-channel OSD profile, with safe defaults when no JSON exists."""
        code = channel["channelCode"]
        profile = self.profiles.get(code)
        if not profile:
            profile = default_profile(channel.get("name", ""), expected,
                                      self.profile_defaults)
        else:
            profile = dict(profile)
            base = default_profile(channel.get("name", ""), expected,
                                   self.profile_defaults)
            base.update(profile)
            profile = base

        # CLI overrides win over the profile file, matching the old --roi /
        # --roi-file behaviour.
        if self.roi:
            profile["roi"] = list(self.roi)
        custom = self.roi_map.get(code) or self.roi_map.get("*")
        if custom:
            profile["roi"] = list(custom)

        if not profile.get("roi"):
            profile["roi"] = list(default_profile(
                channel.get("name", ""), expected)["roi"])
        if expected.isdigit():
            profile.setdefault("type", "digits")
            profile.setdefault("length", len(expected))
        else:
            profile.setdefault("type", "alnum")
        profile["expected"] = expected
        return profile

    @staticmethod
    def is_good(text: str, expected: str) -> bool:
        if not text:
            return False
        if has_cjk(text):
            # A Chinese label is a valid reading; do not burn recaptures on it.
            return True
        if expected.isdigit():
            return (text.isdigit()
                    and (len(text) == len(expected)
                         or numbers_match(expected, text)))
        return any(ch.isalpha() for ch in text)

    def read_cjk(self, frame, channel_code: str, expected: str, best):
        """Fallback for OSD labels that are Chinese (traditional/simplified).

        The primary English model only knows latin characters and digits, so a
        Chinese label comes back empty or as latin garbage. Keep the best of the
        existing read and the ch_sim/ch_tra readings, ranked by:
          1. contains the expected code (so '2438-全景' still matches '2438')
          2. contains CJK at all (a Chinese label is the whole point)
          3. OCR confidence
        """
        wanted = canonical(expected)
        candidates = [best] if best else []
        for reader in self.cjk_readers:
            for roi in self.roi_options(frame, channel_code, expected):
                if roi.size == 0:
                    continue
                try:
                    text, conf, _source = read_number(
                        reader, roi, None, None, numeric_expected=False,
                        mag_ratio=self.args.mag_ratio,
                        decoder=self.args.decoder)
                except Exception:
                    continue
                if text:
                    candidates.append((text, conf, frame))
        if not candidates:
            return best

        def rank(candidate):
            text, conf, _frame = candidate
            return (bool(wanted) and wanted in canonical(text),
                    has_cjk(text), conf)

        return max(candidates, key=rank)

    def start_round(self, round_no: int, round_time: str) -> None:
        """Create a per-round folder so anomaly frames are not overwritten."""
        if not self.error_root:
            self.error_dir = None
            return
        stamp = round_time.replace("-", "").replace(":", "").replace(" ", "_")
        self.error_dir = self.error_root / f"round_{round_no:04d}_{stamp}"
        self.error_dir.mkdir(parents=True, exist_ok=True)

    def save_anomaly(self, channel: dict, frame) -> None:
        """Keep the full frame of a channel whose status is not OK."""
        if frame is None or not self.error_dir:
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

    def _read_legacy_frame(self, frame, code: str, channel_name: str,
                           expected: str, expected_len: int | None, numeric: bool,
                           extra_rois=None):
        """One frame through the old whole-line EasyOCR path.

        Returns (best, votes); best is (text, confidence, frame).
        """
        best = None
        votes: list[str] = []
        rois = list(extra_rois or []) + self.roi_options(frame, code, expected)
        for roi in rois:
            if roi.size == 0:
                continue
            try:
                text, conf, _source, variant_votes = read_number(
                    self.reader, roi, expected_len, self.allowlist,
                    numeric_expected=numeric,
                    mag_ratio=self.args.mag_ratio,
                    decoder=self.args.decoder,
                    return_votes=True)
            except Exception:  # one bad crop must not kill the channel
                continue
            if numeric:
                text = text.translate(DIGIT_CONFUSIONS)
                variant_votes = [v.translate(DIGIT_CONFUSIONS)
                                 for v in variant_votes]
            votes.extend(variant_votes)
            if best is None or conf > best[1]:
                best = (text, conf, frame)

        # Chinese-only labels read as latin garbage ("TT") or empty with the
        # latin model; a Chinese channel name is the other strong hint.
        latin_text = best[0] if best else ""
        if (self.cjk_readers and
                (has_cjk(channel_name) or
                 not any(ch.isdigit() for ch in latin_text))):
            best = self.read_cjk(frame, code, expected, best)
        return best, votes

    @staticmethod
    def matches_expected(detected: str, expected: str) -> bool:
        if not detected or not expected:
            return False
        return (canonical(detected) == canonical(expected)
                or numbers_match(expected, detected))

    def is_other_expected(self, detected: str, expected: str) -> bool:
        """True when `detected` matches a different channel's expected code."""
        if not detected:
            return False
        det_canon = canonical(detected)
        for code in self.expected_codes:
            if not code or code == expected:
                continue
            if canonical(code) == det_canon or numbers_match(code, detected):
                return True
        return False

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
        profile = self.profile_for(channel, expected)
        expected_len = profile.get("length") or len(expected) or None
        numeric = expected.isdigit()
        cell_mode = (self.args.cell_reader == "cells"
                     and profile.get("type") == "digits"
                     and numeric)
        cell_meta: dict = {}
        legacy_used = False

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
            votes: list[str] = []
            cell_meta = {}
            legacy_used = False
            with self.ocr_lock:
                for frame in frames:
                    best = None
                    if cell_mode:
                        cell_result = read_digits(
                            self.reader, frame, profile,
                            decoder=self.args.cell_decoder)
                        cell_text = cell_result.get("text", "")
                        cell_conf = float(cell_result.get("confidence", 0.0))
                        # Keep the best cell result across frames; a later bad
                        # frame must not turn a good read into UNCERTAIN.
                        if (not cell_meta
                                or (cell_text
                                    and cell_conf >= float(cell_meta.get(
                                        "confidence", 0.0)))):
                            cell_meta = cell_result
                        cell_matches = self.matches_expected(cell_text, expected)
                        cell_extra = (
                            bool(cell_result.get("extra_glyphs"))
                            and bool(cell_text) and cell_text.isdigit())
                        plausible = (
                            bool(cell_text)
                            and cell_text.isdigit()
                            and (cell_matches or cell_extra
                                 or (expected_len is not None
                                     and len(cell_text) == expected_len
                                     and cell_conf >= self.args.cell_min_conf)))

                        # A second, independent opinion is the legacy whole-line
                        # reader, not the raw/core/solid variants: those variants
                        # all feed the same EasyOCR recognition model and often
                        # share the same 5->6 bias.  Run legacy when the cell
                        # read does not match expected, or when it is implausible.
                        need_legacy = (not plausible) or (bool(cell_text)
                                                          and not cell_matches)
                        if not need_legacy:
                            votes.append(cell_text)
                            best = (cell_text, cell_conf, frame)
                        else:
                            extra = []
                            profile_roi = profile.get("roi")
                            if profile_roi:
                                px1, py1, px2, py2 = scale_box(
                                    tuple(profile_roi), frame.shape[1], frame.shape[0])
                                extra.append(frame[py1:py2, px1:px2])
                            legacy_best, legacy_votes = self._read_legacy_frame(
                                frame, code, channel["name"], expected,
                                expected_len, numeric, extra_rois=extra)
                            if legacy_votes:
                                legacy_used = True
                            legacy_matches = (
                                legacy_best is not None
                                and self.matches_expected(legacy_best[0], expected))
                            high_conf_legacy = (
                                legacy_matches
                                and legacy_best[1] >= 0.9
                                and (not plausible
                                     or legacy_best[1] >= cell_conf + 0.1))
                            if high_conf_legacy:
                                # Prefer the high-confidence independent reader
                                # that actually matches expected.  Do not count
                                # the low-confidence cell vote in this case.
                                votes.extend(legacy_votes)
                                best = legacy_best
                            else:
                                if plausible:
                                    votes.append(cell_text)
                                    best = (cell_text, cell_conf, frame)
                                votes.extend(legacy_votes)
                                if (legacy_best is not None
                                        and (best is None
                                             or legacy_best[1] > best[1])):
                                    best = legacy_best
                    else:
                        best, legacy_votes = self._read_legacy_frame(
                            frame, code, channel["name"], expected,
                            expected_len, numeric)
                        votes.extend(legacy_votes)

                    if best is not None:
                        candidates.append(best)

            # Pure numeric codes: only readings with the expected digit count
            # vote ('72436' / '43' must not outvote the real '2436').
            pool = votes
            if numeric and expected_len:
                exact = [v for v in votes
                         if v.isdigit() and len(v) == expected_len]
                if exact:
                    pool = exact
            detected = majority(pool) or majority([c[0] for c in candidates])
            if not has_cjk(detected):
                cjk_reads = [c for c in candidates if has_cjk(c[0])]
                if cjk_reads:
                    detected = max(cjk_reads, key=lambda c: c[1])[0]
            if detected and self.is_good(detected, expected):
                break
            reason = "low quality OCR"
            if quality_left > 0:
                quality_left -= 1
                capture_left += 1  # spend one more capture on reading quality

        if not frames:
            result["status"] = f"CAPTURE_FAILED({reason})" if reason else "CAPTURE_FAILED"
            return result

        # detected comes from the variant vote pool computed above; keep it.
        matches = [c for c in candidates if detected and c[0] == detected]
        confidence = max((c[1] for c in matches),
                         default=max((c[1] for c in candidates), default=0.0))
        result["votes"] = [c[0] for c in candidates]
        result["detected"] = detected
        result["confidence"] = confidence
        result["reader"] = ("cells+legacy" if cell_mode and legacy_used
                            else "cells" if cell_mode else "legacy")
        result["segment_reason"] = cell_meta.get("reason", "") if cell_mode else ""
        result["cell_texts"] = cell_meta.get("cell_texts", []) if cell_mode else []
        result["roi"] = cell_meta.get("roi") if cell_mode else None
        result["extra_glyphs"] = bool(cell_meta.get("extra_glyphs"))
        cell_reason = result["segment_reason"]
        cell_ok = (
            cell_mode
            and cell_reason in ("OK", "BOX_FALLBACK")
            and bool(detected)
            and detected.isdigit()
            and expected_len is not None
            and len(detected) == expected_len
        ) or (
            cell_mode
            and bool(result["extra_glyphs"])
            and bool(detected)
            and detected.isdigit()
        )

        result["crossover"] = False
        result["reason"] = ""
        if not expected:
            result["status"] = "UNCERTAIN(NO_EXPECTED)"
        elif not detected:
            if cell_mode and cell_reason in ("NO_MASK", "NO_BAND", "NO_GROUP",
                                             "EMPTY_ROI"):
                result["status"] = "NO_OSD"
            elif cell_mode and cell_reason.startswith("CELL_COUNT"):
                result["status"] = f"UNCERTAIN({cell_reason})"
            else:
                result["status"] = "UNCERTAIN(NO_OCR)"
        elif (canonical(detected) == canonical(expected)
              or numbers_match(expected, detected)):
            result["status"] = "OK"
        else:
            # Crossover is still MISMATCH.  Keep the flag only for diagnostics
            # and operator context; it is not a separate status label.
            if self.is_other_expected(detected, expected):
                result["crossover"] = True
                result["reason"] = "CROSSOVER"
            if cell_mode and not cell_ok and not legacy_used:
                result["status"] = f"UNCERTAIN({cell_reason or 'LOW_CONF'})"
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
                        help="keep frames of abnormal channels here, one "
                             "subfolder per round (e.g. "
                             "/work/snapshots/osd_error)")
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
    parser.add_argument("--frames", type=int, default=3,
                        help="frames per channel per round, majority-voted "
                             "(each keyframe costs one GOP)")
    parser.add_argument("--frame-source", choices=["key", "all"], default="key",
                        help="key: decode keyframes only (complete pictures, "
                             "default); all: decode everything and skip "
                             "--skip-frames frames")
    parser.add_argument("--allowlist", choices=["digits", "alnum"],
                        default="alnum",
                        help="digits: 0-9 only; alnum: A-Z a-z 0-9 and dash")
    parser.add_argument("--cjk", choices=["off", "sim", "tra", "both"],
                        default="both",
                        help="also try Chinese OCR (ch_sim/ch_tra) when the "
                             "latin read is empty; both = traditional and "
                             "simplified (two models, EasyOCR cannot mix them)")
    parser.add_argument("--roi-mode",
                        choices=["auto", "adaptive", "wide", "fixed", "bottom"],
                        default="auto",
                        help="auto: tight crop around the detected text (plus a "
                             "wide-box vote); adaptive: narrow for numeric "
                             "labels, wide for alphanumeric; wide: "
                             "(0,920)-(520,1080); fixed: (43,971)-(219,1040); "
                             "bottom: bottom-anchored box")
    parser.add_argument("--roi", default="",
                        help="fixed x1,y1,x2,y2; overrides --roi-mode")
    parser.add_argument("--roi-file", default="",
                        help="JSON map channelCode -> [x1,y1,x2,y2]")
    parser.add_argument("--osd-profile-file", default=str(DEFAULT_PROFILE_PATH),
                        help="optional per-channel OSD profile JSON "
                             "(roi/type/length/alias); missing file is OK")
    parser.add_argument("--cell-reader", choices=["cells", "legacy"],
                        default="cells",
                        help="cells: row projection + per-cell solid mask + "
                             "EasyOCR recognize for numeric channels; legacy: "
                             "old whole-line readtext path")
    parser.add_argument("--cell-decoder", choices=["greedy", "beamsearch"],
                        default="beamsearch",
                        help="EasyOCR decoder for per-cell recognition "
                             "(default: beamsearch)")
    parser.add_argument("--cell-min-conf", type=float, default=0.7,
                        help="minimum per-cell OCR confidence before falling "
                             "back to the legacy whole-line reader")
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
    parser.add_argument("--decoder", choices=["greedy", "beamsearch"],
                        default="greedy",
                        help="EasyOCR decoder; beamsearch is slower but usually "
                             "more accurate on thin/hollow OSD fonts")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="ffmpeg timeout per capture (seconds); every "
                             "keyframe may take a full GOP to arrive")
    parser.add_argument("--ffmpeg", default="",
                        help="override ffmpeg binary (default: imageio-ffmpeg)")
    parser.add_argument("--gpu", action="store_true", help="EasyOCR on GPU")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--verify-tls", action="store_true")
    args = parser.parse_args()
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
          f"roi: {args.roi_mode}, chars: {args.allowlist}, cjk: {args.cjk}, "
          f"reader: {args.cell_reader}/{args.cell_decoder}, "
          f"profiles: {args.osd_profile_file}", flush=True)

    monitor = Monitor(p, reader, args)
    monitor.expected_codes = {expected_code(ch["name"]) for ch in channels}
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
        monitor.start_round(round_no, round_time)
        print(f"--- round {round_no} ({now_beijing():%H:%M:%S}) ---", flush=True)

        # Stream every result to disk as soon as its channel is done, instead of
        # buffering the whole round (1521 channels = minutes of silence).
        results: list[dict] = []
        ok = 0
        logged = 0
        timeouts = 0
        uncertain = 0
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
                    elif r["status"].startswith("UNCERTAIN"):
                        uncertain += 1
                    elif err_fh and is_logged_anomaly(r["status"]):
                        logged += 1
                        # Keep osd_error.txt at the original 8-column schema:
                        # time index channelCode name expected detected confidence status
                        err_fh.write(f"{r['time']}\t{r['index']}\t{r['channelCode']}\t"
                                     f"{r['name']}\t{r['expected']}\t{r['detected']}\t"
                                     f"{r['confidence']:.3f}\t{r['status']}\n")
                    if r["status"] == "OK":
                        ok += 1
                    extra = f" reason={r['reason']}" if r.get("reason") else ""
                    print(f"  {r['index']:>3} {r['name']:<12} expected={r['expected']!r} "
                          f"detected={r['detected']!r} conf={r['confidence']:.2f} "
                          f"{r['status']}{extra}", flush=True)

            if err_fh:
                err_fh.write(f"# round {round_no} done: {ok}/{len(results)} OK, "
                             f"{logged} anomaly logged, "
                             f"{timeouts} capture timeout skipped, "
                             f"{time.monotonic() - started:.1f}s\n")
                if uncertain:
                    err_fh.write(f"# round {round_no} uncertain: {uncertain}\n")

        print(f"round {round_no}: {ok}/{len(results)} OK, {logged} anomaly, "
              f"{timeouts} capture timeout, {uncertain} uncertain, "
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
