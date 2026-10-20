"""Grab one frame from every video channel and build an HTML picture wall.

Input is the tab-separated file produced by evo_pull.py:

    # 编号  通道编码  设备编码  通道名称  通道序号  组织  状态  RTSP地址
    1       1003060$1$0$0 ... rtsp://...?token=<TOKEN>

Each row is one channel. For each row the script asks MTS/StartVideo for a
fresh token (they live ~30s and are single-use), lets ffmpeg grab one frame,
then releases the session. Outputs:

    snapshots/0001_1003060_1_0_0.jpg
    snapshots/report.csv
    snapshots/index.html          <- open this in a browser

Examples:
    python scripts/evo_snapshot.py --limit 20 --workers 4
    python scripts/evo_snapshot.py --workers 6 --skip-existing
"""
from __future__ import annotations

import argparse
import csv
import html
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evo_pull import (  # noqa: E402
    START_VIDEO,
    STOP_VIDEO,
    Platform,
    is_ok,
    pick_rtsp,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        for path in Path(local).glob(
                "Microsoft/WinGet/Packages/Gyan.FFmpeg_*/**/bin/ffmpeg.exe"):
            return str(path)
    raise SystemExit("ffmpeg not found; install it or add it to PATH")


def read_channels(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 8:
                continue
            rows.append({
                "index": int(parts[0]) if parts[0].isdigit() else len(rows) + 1,
                "channelCode": parts[1],
                "deviceCode": parts[2],
                "name": parts[3],
                "seq": parts[4],
                "org": parts[5],
                "status": parts[6],
                "url": parts[7],
            })
    rows.sort(key=lambda r: r["index"])
    return rows


def safe_name(index: int, code: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z@_-]+", "_", code)
    return f"{index:04d}_{cleaned}.jpg"


class Snapshotter:
    def __init__(self, platform: Platform, ffmpeg: str, outdir: Path,
                 stream_type: str, width: int, timeout: int, skip_existing: bool):
        self.p = platform
        self.ffmpeg = ffmpeg
        self.outdir = outdir
        self.images = outdir / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.stream_type = stream_type
        self.width = width
        self.timeout = timeout
        self.skip_existing = skip_existing
        self.api_lock = threading.Lock()
        self.results: list[dict] = []
        self.result_lock = threading.Lock()

    def _start(self, code: str) -> tuple[str, str, dict]:
        with self.api_lock:
            body = self.p.post(START_VIDEO, {"data": {
                "dataType": "3",
                "streamType": self.stream_type,
                "channelId": code,
                "urlType": 1,
                "packtype": "Private",
                "optModule": "ADMIN_001031",
            }})
        data = body.get("data") or {}
        url = pick_rtsp(data.get("url") or "", data.get("token") or "")
        return str(body.get("code")), url, data

    def _stop(self, code: str, session) -> None:
        if not session:
            return
        with self.api_lock:
            self.p.post(STOP_VIDEO, {"data": {
                "channelId": code, "session": str(session),
                "optModule": "ADMIN_001031",
            }})

    def _grab(self, url: str, target: Path) -> tuple[bool, str]:
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-rtsp_transport", "tcp", "-i", url, "-frames:v", "1",
        ]
        if self.width > 0:
            cmd += ["-vf", f"scale={self.width}:-2"]
        cmd += ["-q:v", "4", "-y", str(target)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return False, f"ffmpeg timeout after {self.timeout}s"
        if proc.returncode != 0 or not target.exists() or target.stat().st_size < 1024:
            err = (proc.stderr or "").strip().splitlines()
            return False, (err[-1] if err else f"ffmpeg exit {proc.returncode}")
        return True, ""

    def one(self, row: dict) -> dict:
        target = self.images / safe_name(row["index"], row["channelCode"])
        result = {**row, "file": "", "error": "", "ok": False}

        if self.skip_existing and target.exists() and target.stat().st_size > 1024:
            result.update(ok=True, file=str(target.relative_to(self.outdir)))
            with self.result_lock:
                self.results.append(result)
            return result

        code, url, data = self._start(row["channelCode"])
        try:
            if not url:
                result["error"] = f"StartVideo code={code}"
                return result
            ok, err = self._grab(url, target)
            if ok:
                result.update(ok=True, file=str(target.relative_to(self.outdir)))
            else:
                result["error"] = err
            return result
        finally:
            self._stop(row["channelCode"], data.get("session"))
            with self.result_lock:
                self.results.append(result)


def write_report(outdir: Path, results: list[dict]) -> None:
    path = outdir / "report.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["编号", "通道编码", "通道名称", "设备编码", "组织",
                         "结果", "图片", "错误"])
        for r in results:
            writer.writerow([r["index"], r["channelCode"], r["name"],
                             r["deviceCode"], r["org"],
                             "成功" if r["ok"] else "失败",
                             r["file"], r["error"]])
    print(f"wrote {path}")


def write_gallery(outdir: Path, results: list[dict], total: int) -> None:
    ok = sum(1 for r in results if r["ok"])
    cards = []
    for r in sorted(results, key=lambda x: x["index"]):
        if r["ok"]:
            media = (f'<a href="{html.escape(r["file"])}" target="_blank">'
                     f'<img loading="lazy" src="{html.escape(r["file"])}" '
                     f'alt="{html.escape(r["name"])}"></a>')
            badge = '<span class="ok">正常</span>'
        else:
            media = '<div class="fail">无画面</div>'
            badge = f'<span class="bad">{html.escape(r["error"][:60])}</span>'
        cards.append(f"""
        <figure>
          {media}
          <figcaption>
            <b>{html.escape(r["name"])}</b>
            <code>{html.escape(r["channelCode"])}</code>
            <small>{html.escape(r["org"]) or html.escape(r["deviceCode"])}</small>
            {badge}
          </figcaption>
        </figure>""")

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>视频通道快照 ({ok}/{total})</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; padding: 24px; background: #0f1115; color: #e6e6e6;
         font: 14px/1.4 "Segoe UI", "Microsoft YaHei", sans-serif; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  p.meta {{ color: #9aa0a6; margin: 0 0 20px; }}
  .grid {{ display: grid; gap: 14px;
           grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }}
  figure {{ margin: 0; background: #181b21; border: 1px solid #262a33;
            border-radius: 10px; overflow: hidden; }}
  figure img, .fail {{ display: block; width: 100%; aspect-ratio: 4/3;
                       object-fit: cover; background: #000; }}
  .fail {{ display: flex; align-items: center; justify-content: center;
           color: #777; }}
  figcaption {{ padding: 8px 10px 10px; display: flex; flex-direction: column;
                gap: 2px; }}
  figcaption b {{ font-size: 13px; }}
  figcaption code {{ color: #7fb3ff; font-size: 11px; }}
  figcaption small {{ color: #8b93a1; font-size: 11px; }}
  .ok {{ color: #51cf66; font-size: 11px; }}
  .bad {{ color: #ff6b6b; font-size: 11px; }}
</style>
</head>
<body>
  <h1>视频通道快照</h1>
  <p class="meta">共 {total} 路，已抓取 {ok} 路，未抓到 {total - ok} 路 · 点击图片看原图</p>
  <div class="grid">{''.join(cards)}</div>
</body>
</html>
"""
    path = outdir / "index.html"
    path.write_text(page, encoding="utf-8")
    print(f"wrote {path}")


def gallery_from_disk(outdir: Path, channels: list[dict]) -> None:
    """Rebuild index.html/report.csv from images already on disk (no network)."""
    by_index = {c["index"]: c for c in channels}
    results = []
    for path in sorted((outdir / "images").glob("*.jpg")):
        match = re.match(r"(\d+)_", path.name)
        if not match:
            continue
        index = int(match.group(1))
        row = by_index.get(index, {
            "index": index, "channelCode": path.stem, "name": "",
            "deviceCode": "", "org": "", "seq": "",
        })
        results.append({**row, "ok": True, "error": "",
                        "file": str(path.relative_to(outdir))})
    write_report(outdir, results)
    write_gallery(outdir, results, len(channels))
    print(f"gallery rebuilt from {len(results)} images on disk")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.getenv("EVO_BASE", ""))
    parser.add_argument("--user", default=os.getenv("EVO_USER", ""))
    parser.add_argument("--password", default=os.getenv("EVO_PASSWORD", ""))
    parser.add_argument("--token", default="")
    parser.add_argument("--user-id", default="1")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "out" / "streams.txt")
    parser.add_argument("--outdir", type=Path, default=PROJECT_ROOT / "snapshots")
    parser.add_argument("--limit", type=int, default=0, help="first N channels")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--stream-type", choices=["1", "2"], default="2")
    parser.add_argument("--width", type=int, default=480,
                        help="scale to this width; 0 keeps the native size")
    parser.add_argument("--timeout", type=int, default=25, help="per-stream seconds")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--gallery-only", action="store_true",
                        help="rebuild index.html from images already on disk "
                             "(no login, no network)")
    parser.add_argument("--verify-tls", action="store_true")
    args = parser.parse_args()
    if not args.input.exists():
        raise SystemExit(f"input not found: {args.input}")

    channels = read_channels(args.input)
    if args.gallery_only:
        gallery_from_disk(args.outdir, channels)
        return
    if not args.base:
        parser.error("--base is required (or set EVO_BASE)")
    if args.limit:
        channels = channels[:args.limit]
    if not channels:
        raise SystemExit("no channels in input file")
    print(f"channels to capture: {len(channels)}", flush=True)

    p = Platform(args.base, args.user, args.password, args.token,
                 args.user_id, args.verify_tls)
    if not p.token:
        p.login()

    snap = Snapshotter(p, find_ffmpeg(), args.outdir, args.stream_type,
                       args.width, args.timeout, args.skip_existing)
    started = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(snap.one, row): row for row in channels}
        for future in as_completed(futures):
            done += 1
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = {**row, "ok": False, "file": "",
                          "error": f"exception: {exc}"}
                with snap.result_lock:
                    snap.results.append(result)
            if done % 5 == 0 or done == len(channels):
                ok = sum(1 for r in snap.results if r["ok"])
                print(f"progress {done}/{len(channels)} ok={ok} "
                      f"({time.time() - started:.0f}s)", flush=True)

    results = sorted(snap.results, key=lambda r: r["index"])
    write_report(args.outdir, results)
    write_gallery(args.outdir, results, len(results))
    ok = sum(1 for r in results if r["ok"])
    print(f"done: {ok}/{len(results)} captured in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
