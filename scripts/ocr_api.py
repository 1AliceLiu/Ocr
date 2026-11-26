"""HTTP API for single-channel OSD OCR.

Input is one data line from out/streams.txt (or the same fields as JSON):

    index  channelCode  deviceCode  name  seq  org  status  rtspUrl

The API returns the expected device name, the OCR reading and the OSD verdict.
It reuses the monitor's Monitor.one() so the algorithm matches osd20.
"""
from __future__ import annotations

import base64
import binascii
import csv
import logging
import os
import re
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import cv2
import easyocr
import numpy as np
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evo_osd_monitor import (  # noqa: E402
    DEFAULT_PROFILE_PATH,
    Monitor,
    WIDE_ROI,
    expected_code,
    load_device_names,
    roi_base,
)
from evo_pull import Platform  # noqa: E402
from osd_ocr import detect_roi, scale_box  # noqa: E402

log = logging.getLogger("ocr_api")

CHANNELS_FILE = Path(os.getenv("CHANNELS_FILE", "/work/out/channels.csv"))
OSD_PROFILE_FILE = Path(os.getenv("OSD_PROFILE_FILE", str(DEFAULT_PROFILE_PATH)))
OCR_API_KEY = os.getenv("OCR_API_KEY", "").strip()
OCR_GPU = os.getenv("OCR_GPU", "0").strip().lower() in ("1", "true", "yes", "on")
EVO_BASE = os.getenv("EVO_BASE", "").rstrip("/")
EVO_USER = os.getenv("EVO_USER", "")
EVO_PASSWORD = os.getenv("EVO_PASSWORD", "")
PLATFORM_CONFIGURED = bool(EVO_BASE and EVO_USER and EVO_PASSWORD)
SAMPLE_DIR = Path(os.getenv("OCR_SAMPLE_DIR", "/work/temp/ocr_api_osd"))
SAMPLE_LIMIT = max(0, int(os.getenv("OCR_SAMPLE_LIMIT", "10")))
DEBUG_DIR = Path(os.getenv("OCR_DEBUG_DIR", "/work/temp/ocr_api_debug"))
DEBUG_LIMIT = max(0, int(os.getenv("OCR_DEBUG_LIMIT", "10")))
_sample_lock = threading.Lock()
_debug_lock = threading.Lock()

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_key(key: str | None = Depends(api_key_header)) -> None:
    if not OCR_API_KEY:
        raise HTTPException(status_code=503, detail="OCR_API_KEY 未配置")
    if not key or not secrets.compare_digest(key, OCR_API_KEY):
        raise HTTPException(status_code=401, detail="X-API-Key 无效或缺失")


class OcrRequest(BaseModel):
    line: str | None = Field(
        default=None, max_length=16384,
        description="out/streams.txt 的一行数据（制表符分隔，8 列）")
    index: int | None = Field(default=None, ge=0)
    channelCode: str | None = Field(default=None, max_length=256)
    deviceCode: str | None = Field(default=None, max_length=256)
    name: str | None = Field(default=None, max_length=256)
    expected: str | None = Field(
        default=None, max_length=256,
        description="期望值；图片模式必填，流模式可选（默认查 channels.csv）")
    seq: str | None = Field(default=None, max_length=64)
    org: str | None = Field(default=None, max_length=512)
    status: str | None = Field(
        default=None, max_length=64,
        description="输出字段；请求里带会被忽略")
    detected: str | None = Field(
        default=None, max_length=256,
        description="输出字段；请求里带会被忽略")
    img: str | None = Field(
        default=None, max_length=20_000_000,
        description="图片模式：JPEG/PNG 的 base64 或 data:image/...;base64,...")
    url: str | None = Field(default=None, max_length=8192)
    source: Literal["auto", "platform", "url"] = "auto"


class OcrResponse(BaseModel):
    index: int
    channelCode: str
    name: str
    expected: str
    detected: str
    confidence: float
    status: str
    ok: bool
    crossover: bool = False
    reason: str = ""
    reader: str = ""
    source: str
    tookMs: int


def parse_stream_line(line: str) -> dict:
    value = (line or "").strip("\r\n")
    if not value or value.lstrip().startswith("#"):
        raise HTTPException(
            status_code=400,
            detail="line 必须是数据行，不能是表头或注释")
    parts = value.split("\t")
    if len(parts) < 8:
        raise HTTPException(
            status_code=400,
            detail="line 需要 8 个制表符分隔字段："
                   "编号、通道编码、设备编码、通道名称、通道序号、组织、状态、RTSP地址")
    index_text = parts[0].strip()
    return {
        "index": int(index_text) if index_text.isdigit() else 0,
        "channelCode": parts[1].strip(),
        "deviceCode": parts[2].strip(),
        "name": parts[3].strip(),
        "seq": parts[4].strip(),
        "org": parts[5].strip(),
        "status": parts[6].strip(),
        "url": "\t".join(parts[7:]).strip(),
    }


def build_channel(req: OcrRequest) -> dict:
    if req.line is not None:
        channel = parse_stream_line(req.line)
    else:
        channel = {
            "index": req.index or 0,
            "channelCode": (req.channelCode or "").strip(),
            "deviceCode": (req.deviceCode or "").strip(),
            "name": (req.name or "").strip(),
            "seq": (req.seq or "").strip(),
            "org": (req.org or "").strip(),
            "status": (req.status or "").strip(),
            "url": (req.url or "").strip(),
        }
    if not channel["channelCode"]:
        raise HTTPException(status_code=400, detail="channelCode 不能为空")
    if not channel["name"]:
        raise HTTPException(status_code=400, detail="name（通道名称）不能为空")
    if len(channel["channelCode"]) > 256 or len(channel["name"]) > 256:
        raise HTTPException(status_code=400, detail="channelCode/name 过长")
    if len(channel["url"]) > 8192:
        raise HTTPException(status_code=400, detail="RTSP 地址过长")
    return channel


def load_name_codes(path: Path) -> dict[str, list[tuple[str, str]]]:
    mapping: dict[str, list[tuple[str, str]]] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="",
                       errors="replace") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("通道名称") or "").strip()
                code = (row.get("通道编码") or "").strip()
                device = (row.get("设备名称") or "").strip()
                if name and code:
                    mapping.setdefault(name, []).append((code, device))
    except OSError:
        return {}
    return mapping


def build_image_channel(req: OcrRequest) -> dict:
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="图片模式需要 name（通道名称）")
    return {
        "index": req.index or 0,
        "channelCode": (req.channelCode or "").strip(),
        "deviceCode": (req.deviceCode or "").strip(),
        "name": name,
        "seq": (req.seq or "").strip(),
        "org": (req.org or "").strip(),
        "status": (req.status or "").strip(),
        "url": "",
    }


def decode_image(value: str):
    raw = (value or "").strip()
    if raw.startswith("data:"):
        if "," not in raw:
            raise HTTPException(status_code=400, detail="img data URL 格式错误")
        raw = raw.split(",", 1)[1]
    raw = "".join(raw.split())
    raw += "=" * (-len(raw) % 4)
    try:
        blob = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="img 不是有效 base64") from exc
    if not blob:
        raise HTTPException(status_code=400, detail="img 为空")
    if len(blob) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片超过 10MB")
    frame = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=400, detail="无法解码图片（需要 JPEG/PNG）")
    if frame.shape[0] * frame.shape[1] > 20_000_000:
        raise HTTPException(status_code=400, detail="图片分辨率过大（上限 2000 万像素）")
    return frame


def osd_crop(frame):
    """Bottom-left OSD crop the same way the monitor's ROI detector sees it."""
    try:
        crop = detect_roi(frame, base=roi_base(frame))
    except Exception:
        crop = None
    if crop is None or crop.size == 0 or crop.shape[0] < 10 or crop.shape[1] < 20:
        x1, y1, x2, y2 = scale_box(WIDE_ROI, frame.shape[1], frame.shape[0])
        crop = frame[y1:y2, x1:x2]
    return crop


def save_osd_sample(frame, name: str, expected: str, result: dict) -> None:
    """Keep at most SAMPLE_LIMIT bottom-left OSD crops for debugging."""
    if SAMPLE_LIMIT <= 0:
        return
    try:
        crop = osd_crop(frame)
        if crop is None or crop.size == 0:
            return
        safe_name = re.sub(r'[^0-9A-Za-z@_-]+', '_', name or 'channel')[:40]
        safe_exp = re.sub(r'[^0-9A-Za-z@_-]+', '_', expected or '')[:30]
        safe_status = re.sub(
            r'[^0-9A-Za-z_-]+', '_', str(result.get('status') or ''))[:30]
        with _sample_lock:
            SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
            existing = list(SAMPLE_DIR.glob('osd_*.jpg'))
            if len(existing) >= SAMPLE_LIMIT:
                return
            seq = len(existing) + 1
            stamp = time.strftime('%Y%m%d_%H%M%S')
            path = SAMPLE_DIR / (
                f'osd_{seq:02d}_{safe_name}_exp{safe_exp}_{stamp}_{safe_status}.jpg')
            cv2.imwrite(str(path), crop)
    except Exception as exc:  # debug capture must never fail the API
        log.warning('cannot save OSD sample: %s', exc)


def _box(value):
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            return tuple(int(v) for v in value)
        except (TypeError, ValueError):
            return None
    return None


def _draw_box(image, box, color, label, offset=(0, 0)) -> None:
    if box is None:
        return
    ox, oy = offset
    x1, y1, x2, y2 = box
    x1, y1, x2, y2 = x1 - ox, y1 - oy, x2 - ox, y2 - oy
    h, w = image.shape[:2]
    x1, x2 = max(0, min(w - 1, x1)), max(0, min(w - 1, x2))
    y1, y2 = max(0, min(h - 1, y1)), max(0, min(h - 1, y2))
    if x2 <= x1 or y2 <= y1:
        return
    cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
    cv2.putText(image, label, (x1 + 3, max(12, y1 + 12)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)


def save_debug_sample(frame, channel: dict, expected: str, result: dict,
                      profile: dict) -> None:
    """Keep 10 MISMATCH samples as wide/profile/cell crops + metadata."""
    if DEBUG_LIMIT <= 0:
        return
    if str(result.get('status') or '').upper() != 'MISMATCH':
        return
    try:
        h, w = frame.shape[:2]
        safe_name = re.sub(r'[^0-9A-Za-z@_-]+', '_',
                           channel.get('name') or 'channel')[:40]
        safe_exp = re.sub(r'[^0-9A-Za-z@_-]+', '_', expected or '')[:30]
        profile_box = _box(profile.get('roi'))
        if profile_box:
            profile_box = tuple(scale_box(profile_box, w, h))
        wide_box = tuple(scale_box(WIDE_ROI, w, h))
        cell_box = _box(result.get('roi'))
        with _debug_lock:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            existing = list(DEBUG_DIR.glob('wide_*.jpg'))
            if len(existing) >= DEBUG_LIMIT:
                return
            seq = len(existing) + 1
            stamp = time.strftime('%Y%m%d_%H%M%S')
            base = f'{seq:02d}_{safe_name}_exp{safe_exp}_{stamp}_MISMATCH'

            wx1, wy1, wx2, wy2 = wide_box
            wide = frame[wy1:wy2, wx1:wx2].copy()
            if not wide.size:
                return
            _draw_box(wide, profile_box, (0, 255, 0), 'profile',
                      offset=(wx1, wy1))
            _draw_box(wide, cell_box, (0, 255, 255), 'cells',
                      offset=(wx1, wy1))
            cv2.imwrite(str(DEBUG_DIR / f'wide_{base}.jpg'), wide)

            if profile_box:
                px1, py1, px2, py2 = profile_box
                pcrop = frame[py1:py2, px1:px2]
                if pcrop.size:
                    cv2.imwrite(str(DEBUG_DIR / f'profile_{base}.jpg'), pcrop)
            if cell_box:
                cx1, cy1, cx2, cy2 = cell_box
                ccrop = frame[cy1:cy2, cx1:cx2]
                if ccrop.size:
                    cv2.imwrite(str(DEBUG_DIR / f'cell_{base}.jpg'), ccrop)

            meta = DEBUG_DIR / 'meta.csv'
            new = not meta.exists()
            with meta.open('a', encoding='utf-8', newline='') as fh:
                writer = csv.writer(fh)
                if new:
                    writer.writerow([
                        'seq', 'time', 'name', 'channelCode', 'expected',
                        'detected', 'status', 'confidence', 'reader',
                        'inputW', 'inputH', 'wide', 'profile', 'cell',
                        'cell_texts'])
                writer.writerow([
                    seq, time.strftime('%Y-%m-%d %H:%M:%S'),
                    channel.get('name', ''), channel.get('channelCode', ''),
                    expected, result.get('detected', ''),
                    result.get('status', ''), result.get('confidence', ''),
                    result.get('reader', ''), w, h,
                    ','.join(str(v) for v in wide_box),
                    ','.join(str(v) for v in profile_box) if profile_box else '',
                    ','.join(str(v) for v in cell_box) if cell_box else '',
                    '|'.join(result.get('cell_texts') or []),
                ])
    except Exception as exc:  # debug capture must never fail the API
        log.warning('cannot save debug sample: %s', exc)


class Engine:
    def __init__(self) -> None:
        self._call_lock = threading.Lock()
        self._monitor: Monitor | None = None
        self._names_key: tuple[int, int] | None = None
        self._names: dict[str, str] = {}
        self._name_codes: dict[str, list[tuple[str, str]]] = {}
        self._platform_error = ""

    def load(self) -> None:
        args = SimpleNamespace(
            save_dir=None,
            error_dir=None,
            ffmpeg="",
            allowlist="alnum",
            roi="",
            roi_file="",
            osd_profile_file=str(OSD_PROFILE_FILE),
            cjk="both",
            gpu=OCR_GPU,
            roi_mode="auto",
            stream_type="1",
            frame_source="key",
            frames=3,
            skip_frames=10,
            timeout=30.0,
            retries=1,
            ocr_retries=1,
            cell_reader="cells",
            cell_decoder="beamsearch",
            cell_min_conf=0.7,
            mag_ratio=1.0,
            decoder="greedy",
        )
        platform = None
        if PLATFORM_CONFIGURED:
            platform = Platform(EVO_BASE, EVO_USER, EVO_PASSWORD)
            try:
                platform.login()
                log.info("EVO platform login ok")
            except (Exception, SystemExit) as exc:
                self._platform_error = f"{type(exc).__name__}: {exc}"
                log.error("EVO platform login failed: %s", self._platform_error)
        reader = easyocr.Reader(["en"], gpu=OCR_GPU, verbose=False)
        self._monitor = Monitor(platform, reader, args)
        self._refresh_names()
        log.info("OCR engine ready (gpu=%s, platform=%s)",
                 OCR_GPU, bool(platform and not self._platform_error))

    def _refresh_names(self) -> None:
        monitor = self._monitor
        if monitor is None:
            return
        try:
            stat = CHANNELS_FILE.stat()
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = None
        if key != self._names_key:
            self._names = load_device_names(CHANNELS_FILE)
            self._name_codes = load_name_codes(CHANNELS_FILE)
            self._names_key = key
        monitor.device_names = self._names
        monitor.expected_codes = set(self._names.values())

    def resolve_code(self, name: str, expected: str) -> str:
        rows = self._name_codes.get(name, [])
        if not rows:
            return name
        if len(rows) == 1:
            return rows[0][0]
        matches = [code for code, device in rows if device == expected]
        return matches[0] if len(matches) == 1 else name

    def run(self, channel: dict, source: str,
            expected: str | None = None) -> dict:
        if self._monitor is None:
            raise HTTPException(status_code=503, detail="OCR 引擎尚未就绪")
        with self._call_lock:
            self._refresh_names()
            expected = (expected or self._names.get(channel["channelCode"])
                        or expected_code(channel["name"]))
            self._monitor.expected_codes.add(expected)
            if source == "platform":
                if not PLATFORM_CONFIGURED:
                    raise HTTPException(
                        status_code=503,
                        detail="服务端未配置 EVO_BASE/EVO_USER/EVO_PASSWORD")
                if self._platform_error:
                    raise HTTPException(
                        status_code=503,
                        detail=f"大华平台登录失败：{self._platform_error}")
                return self._monitor.one(channel, expected=expected)
            return self._monitor.one(channel, url=channel["url"],
                                     expected=expected)

    def run_image(self, channel: dict, frame, expected: str) -> dict:
        if self._monitor is None:
            raise HTTPException(status_code=503, detail="OCR 引擎尚未就绪")
        with self._call_lock:
            self._refresh_names()
            if not channel.get("channelCode"):
                channel = dict(channel)
                channel["channelCode"] = self.resolve_code(
                    channel["name"], expected)
            self._monitor.expected_codes.add(expected)
            return self._monitor.one_image(channel, frame, expected=expected)

    def profile_for(self, channel: dict, expected: str) -> dict:
        if self._monitor is None:
            return {}
        return self._monitor.profile_for(channel, expected)

    @property
    def platform_ready(self) -> bool:
        return PLATFORM_CONFIGURED and not self._platform_error


engine = Engine()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    engine.load()
    yield


app = FastAPI(
    title="OSD OCR API",
    version="1.1.0",
    description="单通道 OSD OCR：输入图片（img）或 out/streams.txt 的一行，"
                "返回期望值、识别结果和状态。",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["*"],
)


@app.get("/health", summary="Liveness probe (no auth)")
def health() -> dict:
    return {
        "status": "ok",
        "apiKeyConfigured": bool(OCR_API_KEY),
        "imageMode": True,
        "platformConfigured": PLATFORM_CONFIGURED,
        "platformReady": engine.platform_ready,
    }


def choose_source(req: OcrRequest, channel: dict) -> str:
    if req.source in ("platform", "url"):
        source = req.source
    else:
        source = "platform" if PLATFORM_CONFIGURED else "url"
    if source == "url":
        url = channel["url"]
        if not url.startswith("rtsp://"):
            raise HTTPException(
                status_code=400,
                detail="source=url 需要可播放的 rtsp:// 地址")
        if "<TOKEN>" in url.upper():
            raise HTTPException(
                status_code=400,
                detail="RTSP 地址是模板（含 <TOKEN>），"
                       "请用 source=platform 或提供带新 token 的地址")
    return source


@app.post("/api/v1/ocr", response_model=OcrResponse,
          dependencies=[Depends(require_key)],
          summary="OCR one image or one channel line")
def ocr(req: OcrRequest) -> dict:
    started = time.monotonic()
    try:
        if req.img is not None:
            if req.line is not None:
                raise HTTPException(
                    status_code=400, detail="img 与 line 只能二选一")
            channel = build_image_channel(req)
            expected = (req.expected or "").strip()
            if not expected:
                raise HTTPException(
                    status_code=400, detail="图片模式需要 expected")
            frame = decode_image(req.img)
            source = "image"
            result = engine.run_image(channel, frame, expected)
            debug_channel = dict(channel)
            debug_channel["channelCode"] = (
                result.get("channelCode") or channel["channelCode"])
            profile = engine.profile_for(debug_channel, expected)
            save_debug_sample(
                frame, debug_channel, expected, result, profile)
            save_osd_sample(frame, channel["name"], expected, result)
        else:
            channel = build_channel(req)
            source = choose_source(req, channel)
            expected = (req.expected or "").strip() or None
            result = engine.run(channel, source, expected=expected)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("OCR failed")
        raise HTTPException(
            status_code=500, detail="OCR 内部错误，请查看服务日志") from exc
    return {
        "index": result.get("index", channel["index"]),
        "channelCode": result.get("channelCode", channel["channelCode"]),
        "name": result.get("name", channel["name"]),
        "expected": result.get("expected", ""),
        "detected": result.get("detected", ""),
        "confidence": float(result.get("confidence") or 0.0),
        "status": result.get("status", ""),
        "ok": result.get("status") == "OK",
        "crossover": bool(result.get("crossover")),
        "reason": result.get("reason", "") or "",
        "reader": result.get("reader", "") or "",
        "source": source,
        "tookMs": int((time.monotonic() - started) * 1000),
    }
