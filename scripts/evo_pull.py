"""Pull device names and live-stream (RTSP) URLs from a Dahua EVO / iConnection platform.

The platform exposes three different API families, and they are easy to mix up:

  * evo-oauth   /evo-apigw/evo-oauth/...        login -> access_token
  * evo-brm     /evo-apigw/evo-brm/...          organizations, devices, channels
                                                (the four API pages in the wiki)
  * admin/MTS   /evo-apigw/admin/API/MTS/...    StartVideo -> tokenized RTSP URL

`--with-streams` is optional because the live-stream endpoint is NOT part of the
evo-brm documentation.  It uses the MTS endpoint that the platform's web player
calls; if your platform differs, capture the real request in the browser
DevTools (F12 -> Network) and adjust START_VIDEO below.

RTSP tokens are valid for about 30 seconds and can be used only once, so bulk
collection should use --stream-mode template and playback should resolve a
fresh URL with scripts/evo_stream_url.py.

Examples
--------
Probe a platform (checks reachability, login and each API):

    python scripts/evo_pull.py --base https://10.0.0.10:8090 \
        --user admin --password 'xxx' --probe

Export devices + channels only:

    python scripts/evo_pull.py --base https://10.0.0.10:8090 \
        --user admin --password 'xxx' --outdir out

Export devices + channels + RTSP URLs (tokens are short-lived):

    python scripts/evo_pull.py ... --with-streams --stream-mode live

Reuse an access_token copied from the browser instead of logging in:

    python scripts/evo_pull.py --base ... --token 'eyJ...' --user-id 1 \
        --outdir out
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import urllib3
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ---------------------------------------------------------------------------
# Endpoints (all relative to --base, e.g. https://host:port)
# ---------------------------------------------------------------------------
DEVICE_PAGE = "/evo-apigw/evo-brm/1.2.0/device/subsystem/page"
CHANNEL_PAGE = "/evo-apigw/evo-brm/1.2.0/device/channel/subsystem/page"
DEVICE_DETAIL = "/evo-apigw/evo-brm/1.0.0/device/{device_code}"
TREE = "/evo-apigw/evo-brm/1.0.0/tree"

# Media transfer service used by the web live-preview page.
START_VIDEO = "/evo-apigw/admin/API/MTS/Video/StartVideo"
STOP_VIDEO = "/evo-apigw/admin/API/MTS/Video/StopVideo"

# Login endpoint variants seen on Dahua platforms; the first one that answers
# with a public key / token wins.
OAUTH_PREFIXES = ("/evo-apigw/evo-oauth", "/evo-apigw/evo-oauth/1.0.0")
TOKEN_EXPIRED_CODES = {"27001007", "28110015", "1002", "401"}

# evo-brm answers code "0"; admin/MTS answers code "1000".
OK_CODES = {"0", "1000"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def is_ok(body: dict) -> bool:
    code = str(body.get("code", "")).strip()
    if code in OK_CODES:
        return True
    return body.get("success") is True and code in ("", "None")


def body_error(body: dict) -> str:
    return (f"code={body.get('code')!r} errMsg={body.get('errMsg')!r} "
            f"desc={str(body.get('desc', ''))[:120]!r}")


_REACH_CACHE: dict[tuple[str, int], bool] = {}


def _reachable(url: str, timeout: float = 1.5) -> bool:
    """Can this machine open a TCP connection to the stream server?"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or 554
    if not host:
        return False
    key = (host, port)
    if key not in _REACH_CACHE:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                _REACH_CACHE[key] = True
        except OSError:
            _REACH_CACHE[key] = False
    return _REACH_CACHE[key]


def pick_rtsp(raw: str, token: str) -> str:
    """StartVideo returns several pipe-separated URLs (LAN + WAN).

    Pick the first one this machine can actually reach; the platform often
    lists an internal 172.16.x/10.x address first even when you connect from
    the internet.
    """
    urls = [u for u in str(raw or "").split("|") if u.startswith("rtsp://")]
    candidates = [u for u in urls
                  if "127.0.0.1" not in u and "localhost" not in u] or urls
    chosen = next((u for u in candidates if _reachable(u)), "")
    if not chosen:
        chosen = (candidates or [""])[0]
    if not chosen:
        return ""
    if "token=" in chosen:
        return chosen
    return chosen + ("&" if "?" in chosen else "?") + "token=" + str(token)


def write_csv(path: Path, rows: list[dict], fields: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([label for _, label in fields])
        for row in rows:
            writer.writerow([row.get(key, "") for key, _ in fields])
    print(f"wrote {path} ({len(rows)} rows)")


# ---------------------------------------------------------------------------
# Platform client
# ---------------------------------------------------------------------------
class Platform:
    def __init__(self, base: str, user: str = "", password: str = "",
                 token: str = "", user_id: str = "1",
                 verify_tls: bool = False):
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.token = token
        self.user_id = str(user_id or "1")
        self.refresh_token = ""
        self.verify = verify_tls
        self.http = requests.Session()
        if not verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # -- headers ----------------------------------------------------------
    def headers(self, json_body: bool = True) -> dict:
        headers = {
            "Authorization": f"bearer {self.token}",
            "userId": self.user_id,
            "user-client": "1",
            "Accept-Language": "zh-CN",
            "clientTimezoneOffset": "28800000",
            "X-Subject-TimezoneAddress": "Asia/Shanghai",
            "X-Subject-DSTEnable": "0",
        }
        if json_body:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        return headers

    # -- raw requests -----------------------------------------------------
    def get(self, path: str, params: dict | None = None,
            retry: bool = True) -> tuple[int, dict]:
        res = self.http.get(self.base + path, params=params,
                            headers=self.headers(False), verify=self.verify,
                            timeout=30)
        body = self._load(res)
        if retry and self._needs_refresh(res, body):
            self.refresh()
            return self.get(path, params, retry=False)
        return res.status_code, body

    def post(self, path: str, payload: dict, retry: bool = True) -> dict:
        res = self.http.post(self.base + path, headers=self.headers(), json=payload,
                             verify=self.verify, timeout=60)
        body = self._load(res)
        if retry and self._needs_refresh(res, body):
            self.refresh()
            return self.post(path, payload, retry=False)
        return body

    @staticmethod
    def _load(res: requests.Response) -> dict:
        try:
            return res.json()
        except ValueError:
            return {"success": False, "code": res.status_code,
                    "errMsg": res.text[:200]}

    def _needs_refresh(self, res: requests.Response, body: dict) -> bool:
        if not self.user or not self.password:
            return False
        return res.status_code == 401 or str(body.get("code")) in TOKEN_EXPIRED_CODES

    # -- login ------------------------------------------------------------
    def login(self) -> None:
        if self.token:
            return
        if not (self.user and self.password):
            raise SystemExit("need --token or --user/--password")

        pub_b64, prefix = "", ""
        for candidate in OAUTH_PREFIXES:
            for suffix in ("/oauth/public-key", "/public-key"):
                try:
                    res = self.http.get(
                        self.base + candidate + suffix,
                        params={"t": int(time.time() * 1000)},
                        verify=self.verify, timeout=20)
                    data = self._load(res).get("data") or {}
                    if data.get("publicKey"):
                        pub_b64, prefix = data["publicKey"], candidate
                        break
                except requests.RequestException:
                    continue
            if pub_b64:
                break

        forms = []
        if pub_b64:
            # Preferred: the web client encrypts the password with the
            # platform's RSA public key.
            forms.append({
                "username": self.user,
                "password": self._encrypt(pub_b64),
                "grant_type": "password",
                "client_id": "web_client",
                "client_secret": "web_client",
                "public_key": pub_b64,
                "verifyCodeFlag": "0",
                "rememberFlag": "0",
            })
        else:
            # Older builds accept the raw password or an MD5 digest.
            forms.append({"username": self.user, "password": self.password,
                          "grant_type": "password", "client_id": "web_client",
                          "client_secret": "web_client"})
            forms.append({"username": self.user,
                          "password": hashlib.md5(self.password.encode()).hexdigest().upper(),
                          "grant_type": "password", "client_id": "web_client",
                          "client_secret": "web_client"})

        prefixes = [prefix] if prefix else list(OAUTH_PREFIXES)
        last = {}
        for cand in prefixes:
            for form in forms:
                try:
                    res = self.http.post(
                        self.base + cand + "/oauth/token",
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        data=form, verify=self.verify, timeout=30)
                except requests.RequestException as exc:
                    last = {"errMsg": str(exc)}
                    continue
                body = self._load(res)
                last = body
                if body.get("data", {}).get("access_token"):
                    self._store(body["data"])
                    print(f"login ok ({cand}), user={self.user} userId={self.user_id}")
                    return
        raise SystemExit(f"login failed: {body_error(last)}")

    def refresh(self) -> None:
        if not self.refresh_token:
            self.token = ""
            self.login()
            return
        body = {}
        for cand in OAUTH_PREFIXES:
            try:
                res = self.http.post(
                    self.base + cand + "/oauth/token",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data={"refresh_token": self.refresh_token,
                          "grant_type": "refresh_token",
                          "client_id": "web_client", "client_secret": "web_client"},
                    verify=self.verify, timeout=30)
                body = self._load(res)
            except requests.RequestException:
                continue
            if body.get("data", {}).get("access_token"):
                self._store(body["data"])
                return
        self.token = ""
        self.login()

    def _store(self, data: dict) -> None:
        self.token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.user_id = str(data.get("userId", self.user_id))

    def _encrypt(self, pub_b64: str) -> str:
        der = base64.b64decode(pub_b64)
        try:
            key = serialization.load_der_public_key(der)
        except ValueError:
            key = serialization.load_pem_public_key(pub_b64.encode())
        return base64.b64encode(
            key.encrypt(self.password.encode("utf-8"), padding.PKCS1v15())
        ).decode()

    # -- high level -------------------------------------------------------
    def paged(self, path: str, payload: dict, page_size: int) -> list[dict]:
        rows: list[dict] = []
        page = 1
        while True:
            body = self.post(path, {**payload, "pageNum": page,
                                    "pageSize": page_size})
            if not is_ok(body):
                raise RuntimeError(f"{path} page {page} failed: {body_error(body)}")
            data = body.get("data") or {}
            batch = data.get("pageData") or []
            rows.extend(batch)
            total_pages = int(data.get("totalPage") or 0)
            total_rows = int(data.get("totalRows") or 0)
            if not batch:
                break
            if total_pages:
                if page >= total_pages:
                    break
            else:
                # Some builds omit totalPage; fall back to row count/short page.
                if total_rows and len(rows) >= total_rows:
                    break
                if len(batch) < page_size:
                    break
            page += 1
            time.sleep(0.05)
        return rows

    def tree(self, root: str, type_prefix: str) -> dict[str, dict]:
        """Breadth-first walk of the org/device/channel tree."""
        nodes: dict[str, dict] = {}
        queue = [root]
        visited: set[str] = set()
        while queue:
            node_id = queue.pop(0)
            if node_id in visited:
                continue
            visited.add(node_id)
            body = self.post(TREE, {"id": node_id, "checkStat": 1,
                                    "type": f"{type_prefix};;1"})
            if not is_ok(body):
                if node_id == root:
                    raise RuntimeError(f"tree failed: {body_error(body)}")
                continue
            for node in (body.get("data") or {}).get("value") or []:
                nodes[node["id"]] = node
                if (node.get("nodeType") == "org" and node.get("isParent")
                        and node["id"] not in visited):
                    queue.append(node["id"])
            time.sleep(0.05)
        return nodes


# ---------------------------------------------------------------------------
# Collection steps
# ---------------------------------------------------------------------------
def org_path(nodes: dict[str, dict], org_id: str) -> str:
    parts, current, seen = [], org_id, set()
    while current and current not in seen:
        seen.add(current)
        node = nodes.get(current)
        if not node:
            break
        parts.append(node.get("name", ""))
        current = node.get("pId", "")
    return "/".join(reversed(parts))


def collect_devices(p: Platform, source: str, page_size: int,
                    root: str) -> tuple[list[dict], dict[str, dict]]:
    nodes: dict[str, dict] = {}
    devices: list[dict] = []
    if source in ("auto", "page"):
        try:
            devices = p.paged(DEVICE_PAGE, {"showChildNodeData": 1}, page_size)
            print(f"device page API: {len(devices)} devices", flush=True)
        except Exception as exc:  # noqa: BLE001 - fall back to the tree
            if source == "page":
                raise
            print(f"device page API failed ({exc}); falling back to tree")
    if not devices:
        nodes = p.tree(root, root)
        devices = [n for n in nodes.values() if n.get("nodeType") == "dev"]
        print(f"device tree API: {len(devices)} devices")
    return devices, nodes


def collect_channels(p: Platform, devices: list[dict], nodes: dict[str, dict],
                     source: str, page_size: int) -> list[dict]:
    channels: list[dict] = []
    if source in ("auto", "page"):
        try:
            channels = p.paged(CHANNEL_PAGE, {"includeSubOwnerCodeFlag": True},
                               page_size)
            print(f"channel page API: {len(channels)} channels", flush=True)
        except Exception as exc:  # noqa: BLE001
            if source == "page":
                raise
            print(f"channel page API failed ({exc}); deriving from devices")
    if channels:
        return channels

    if not nodes:
        # Ask the device detail API for units/channels of every device.
        for dev in devices:
            code = dev.get("deviceCode")
            if not code:
                continue
            status, body = p.get(DEVICE_DETAIL.format(device_code=code))
            if not is_ok(body):
                continue
            for unit in (body.get("data") or {}).get("units") or []:
                for ch in unit.get("channels") or []:
                    ch = dict(ch)
                    ch.setdefault("deviceCode", code)
                    ch.setdefault("unitType", unit.get("unitType"))
                    channels.append(ch)
            time.sleep(0.05)
    else:
        channels = [n for n in nodes.values() if n.get("nodeType") == "ch"]
    print(f"channels: {len(channels)}")
    return channels


def is_video_channel(channel: dict) -> bool:
    """unitType 1 is a video unit; 3/4 are alarm inputs/outputs, 7 access, 14 barrier."""
    unit_type = channel.get("unitType")
    return unit_type is None or str(unit_type) == "1"


def fetch_streams(p: Platform, channels: list[dict], stream_type: str,
                  mode: str, delay: float, stop_sessions: bool) -> list[dict]:
    results: list[dict] = []
    sessions: list[tuple[str, str]] = []
    ok = failed = 0
    online = [c for c in channels
              if int(c.get("isOnline") or 0) == 1 and is_video_channel(c)]

    for index, ch in enumerate(online, 1):
        code = ch.get("channelCode") or ch.get("id")
        body = p.post(START_VIDEO, {"data": {
            "dataType": "3",
            "streamType": stream_type,
            "channelId": code,
            "urlType": 1,
            "packtype": "Private",
            "optModule": "ADMIN_001031",
        }})
        data = body.get("data") or {}
        raw = data.get("url") or data.get("urls") or ""
        token = str(data.get("token", ""))
        if is_ok(body) and raw:
            template = pick_rtsp(raw, "<TOKEN>")
            live = pick_rtsp(raw, token)
            results.append({"channelCode": code,
                            "rtsp": template if mode == "template" else live,
                            "status": "ok"})
            ok += 1
            if data.get("session"):
                if mode == "template":
                    # The template only needs the URL shape, so release the
                    # session immediately and leave the token placeholder.
                    stop_stream(p, code, data["session"])
                else:
                    sessions.append((code, str(data["session"])))
        else:
            results.append({"channelCode": code, "rtsp": "",
                            "status": f"取流失败({body.get('code')})"})
            failed += 1

        if index % 20 == 0 or index == len(online):
            print(f"streams {index}/{len(online)} ok={ok} failed={failed}", flush=True)
        if delay:
            time.sleep(delay)

    if stop_sessions:
        for code, session in sessions:
            stop_stream(p, code, session)
        print(f"stopped {len(sessions)} sessions; live URLs are now invalid")
    elif mode == "live" and sessions:
        print(f"kept {len(sessions)} media sessions open; "
              f"run with --stop-sessions to release them")

    return results


def stop_stream(p: Platform, channel_code: str, session: str) -> None:
    p.post(STOP_VIDEO, {"data": {"channelId": channel_code,
                                 "session": session,
                                 "optModule": "ADMIN_001031"}})


# ---------------------------------------------------------------------------
# Probe mode
# ---------------------------------------------------------------------------
def probe(p: Platform) -> None:
    print(f"base: {p.base}")
    try:
        res = p.http.get(p.base, verify=p.verify, timeout=15)
        print(f"  GET /                      HTTP {res.status_code}")
    except requests.RequestException as exc:
        print(f"  GET /                      unreachable: {exc}")

    for cand in OAUTH_PREFIXES:
        for suffix in ("/oauth/public-key", "/public-key"):
            try:
                res = p.http.get(p.base + cand + suffix, verify=p.verify, timeout=10)
                data = p._load(res).get("data") or {}
                if data.get("publicKey"):
                    print(f"  public key                 {cand}{suffix}")
            except requests.RequestException:
                pass

    if p.user and p.password:
        try:
            p.login()
            print(f"  token                      ok ({len(p.token)} chars)")
        except SystemExit as exc:
            print(f"  token                      {exc}")
            return

    checks = [
        ("device page", DEVICE_PAGE, {"showChildNodeData": 1}),
        ("channel page", CHANNEL_PAGE, {"includeSubOwnerCodeFlag": True}),
        ("device tree", TREE, {"id": "001", "type": "001;;1", "checkStat": 1}),
    ]
    for label, path, payload in checks:
        try:
            body = p.post(path, {**payload, "pageNum": 1, "pageSize": 1}
                          if path.endswith("/page") else payload)
            data = body.get("data") or {}
            rows = data.get("pageData") or data.get("value") or []
            print(f"  {label:<25} ok={is_ok(body)} rows={len(rows)} "
                  f"{'' if is_ok(body) else body_error(body)}")
        except requests.RequestException as exc:
            print(f"  {label:<25} error: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.getenv("EVO_BASE", ""),
                        help="https://host:port (or set EVO_BASE)")
    parser.add_argument("--user", default=os.getenv("EVO_USER", ""))
    parser.add_argument("--password", default=os.getenv("EVO_PASSWORD", ""),
                        help="prefer the EVO_PASSWORD environment variable")
    parser.add_argument("--token", default="",
                        help="reuse an access_token instead of logging in")
    parser.add_argument("--user-id", default="1")
    parser.add_argument("--outdir", type=Path, default=Path("out"))
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--source", choices=["auto", "page", "tree"], default="auto",
                        help="device/channel source, default auto (page then tree)")
    parser.add_argument("--root", default="001",
                        help="root org code for the tree API (logical org: L01)")
    parser.add_argument("--with-streams", action="store_true",
                        help="also call StartVideo and save RTSP URLs")
    parser.add_argument("--stream-type", choices=["1", "2"], default="2",
                        help="1=main stream, 2=sub stream")
    parser.add_argument("--stream-mode", choices=["template", "live"],
                        default="template",
                        help="template: save <TOKEN> placeholder; live: save "
                             "short-lived tokenized URLs")
    parser.add_argument("--stop-sessions", action="store_true",
                        help="live mode: release media sessions when done "
                             "(the saved URLs stop working)")
    parser.add_argument("--delay", type=float, default=0.1,
                        help="pause between StartVideo calls")
    parser.add_argument("--limit", type=int, default=0,
                        help="only the first N online channels (testing)")
    parser.add_argument("--probe", action="store_true",
                        help="check endpoints and exit")
    parser.add_argument("--verify-tls", action="store_true",
                        help="verify HTTPS certificates (off for self-signed)")
    args = parser.parse_args()
    if not args.base:
        parser.error("--base is required (or set EVO_BASE)")

    p = Platform(args.base, args.user, args.password, args.token,
                 args.user_id, args.verify_tls)

    if args.probe:
        probe(p)
        return

    if not p.token:
        p.login()

    devices, nodes = collect_devices(p, args.source, args.page_size, args.root)
    channels = collect_channels(p, devices, nodes, args.source, args.page_size)
    if not channels:
        print("no channels found; check the account's org permissions")
        return

    dev_by_code = {d.get("deviceCode"): d for d in devices}
    for ch in channels:
        ch["_deviceName"] = dev_by_code.get(ch.get("deviceCode"), {}).get("deviceName", "")
        ch["_org"] = org_path(nodes, ch.get("pId", "")) if nodes else ""

    write_csv(args.outdir / "devices.csv", devices, [
        ("deviceCode", "设备编码"), ("deviceName", "设备名称"),
        ("deviceSn", "设备标识码"), ("deviceCategory", "设备大类"),
        ("deviceType", "设备小类"), ("deviceIp", "设备IP"),
        ("devicePort", "设备端口"), ("ownerCode", "组织编码"),
        ("isOnline", "在线"), ("sleepStat", "休眠"),
        ("deviceManufacturer", "厂商"), ("subSystem", "子系统"),
    ])
    write_csv(args.outdir / "channels.csv", channels, [
        ("channelCode", "通道编码"), ("channelName", "通道名称"),
        ("deviceCode", "设备编码"), ("_deviceName", "设备名称"),
        ("unitType", "单元类型"), ("channelSeq", "通道序号"),
        ("channelType", "通道类型"), ("cameraType", "摄像头类型"),
        ("ownerCode", "组织编码"), ("_org", "组织路径"),
        ("isOnline", "在线"), ("stat", "启用"), ("access", "已接入"),
    ])

    if not args.with_streams:
        return

    online = [c for c in channels
              if int(c.get("isOnline") or 0) == 1 and is_video_channel(c)]
    if args.limit:
        online = online[:args.limit]
    if not online:
        print("no online channels, nothing to stream")
        return
    if args.stream_mode == "live" and len(online) > 10:
        print("note: RTSP tokens are single-use and valid for about 30 seconds; "
              "for bulk collection use --stream-mode template and resolve each "
              "URL right before playing (scripts/evo_stream_url.py)")

    streams = fetch_streams(p, online, args.stream_type,
                            args.stream_mode, args.delay,
                            stop_sessions=args.stop_sessions)
    by_code = {s["channelCode"]: s for s in streams}

    args.outdir.mkdir(parents=True, exist_ok=True)
    out = args.outdir / "streams.txt"
    with out.open("w", encoding="utf-8", newline="") as fh:
        fh.write("# RTSP 地址来自 MTS/StartVideo；模板模式的 <TOKEN> 由平台动态下发，"
                 "与通道绑定且短期有效\n")
        fh.write("# 编号\t通道编码\t设备编码\t通道名称\t通道序号\t组织\t状态\tRTSP地址\n")
        for index, ch in enumerate(online, 1):
            code = ch.get("channelCode") or ch.get("id")
            s = by_code.get(code, {})
            row = [str(index), code, ch.get("deviceCode", ""),
                   ch.get("channelName", ch.get("name", "")),
                   str(ch.get("channelSeq", "")), ch.get("_org", ""),
                   "在线" if s.get("rtsp") else s.get("status", "取流失败"),
                   s.get("rtsp", "")]
            fh.write("\t".join(row) + "\n")
    print(f"wrote {out} ({len(streams)} rows)")


if __name__ == "__main__":
    sys.exit(main())
