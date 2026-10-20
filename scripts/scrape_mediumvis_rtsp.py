"""Scrape all channels and their RTSP URLs from the MediumVis/Dahua platform.

The live preview page obtains an RTSP URL per channel through
`admin/API/MTS/Video/StartVideo`, which creates a per-channel stream session.
A token from one channel cannot be used for another channel.

Typical usage:
    python scripts/scrape_mediumvis_rtsp.py --user system --password '...' \
        --output rtsp_streams.txt

Notes:
  * RTSP URLs contain a session token. By default the script keeps the sessions
    alive so the URLs stay usable; use --stop-sessions to release them.
  * Offline channels are listed without an RTSP URL.
"""
import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

DEFAULT_BASE = "https://cloudstorage.mediumvis.com:8092"
TREE_TYPE = "001;00;1,82"


class Platform:
    def __init__(self, base: str, user: str, password: str):
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.http = requests.Session()
        self.token = ""
        self.refresh_token = ""
        self.user_id = "1"

    def _headers(self) -> dict:
        return {
            "Authorization": f"bearer {self.token}",
            "userId": self.user_id,
            "user-client": "1",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept-Language": "zh-CN",
            "clientTimezoneOffset": "28800000",
            "X-Subject-TimezoneAddress": "Asia/Shanghai",
            "X-Subject-DSTEnable": "0",
        }

    def login(self) -> None:
        key_res = self.http.get(
            f"{self.base}/evo-apigw/evo-oauth/oauth/public-key",
            params={"t": int(time.time() * 1000)}, timeout=30,
        )
        key_res.raise_for_status()
        pub_b64 = key_res.json()["data"]["publicKey"]
        public_key = serialization.load_der_public_key(base64.b64decode(pub_b64))
        encrypted = base64.b64encode(
            public_key.encrypt(self.password.encode("utf-8"), padding.PKCS1v15())
        ).decode()

        res = self.http.post(
            f"{self.base}/evo-apigw/evo-oauth/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "username": self.user,
                "password": encrypted,
                "grant_type": "password",
                "client_id": "web_client",
                "client_secret": "web_client",
                "public_key": pub_b64,
                "verifyCodeFlag": "0",
                "rememberFlag": "0",
            },
            timeout=30,
        )
        res.raise_for_status()
        body = res.json()
        if not body.get("success"):
            raise SystemExit(f"login failed: {body}")
        self._store_token(body["data"])

    def _store_token(self, data: dict) -> None:
        self.token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.user_id = str(data.get("userId", self.user_id))

    def refresh(self) -> None:
        if not self.refresh_token:
            self.login()
            return
        res = self.http.post(
            f"{self.base}/evo-apigw/evo-oauth/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
                "client_id": "web_client",
                "client_secret": "web_client",
            },
            timeout=30,
        )
        body = res.json()
        if body.get("success"):
            self._store_token(body["data"])
        else:
            self.login()

    def post(self, path: str, payload: dict, retry: bool = True) -> dict:
        res = self.http.post(
            f"{self.base}{path}", headers=self._headers(),
            json=payload, timeout=60,
        )
        try:
            body = res.json()
        except ValueError:
            body = {"code": res.status_code, "desc": res.text[:200]}
        if retry and (res.status_code == 401
                      or str(body.get("code")) in {"27001007", "28110015"}):
            self.refresh()
            return self.post(path, payload, retry=False)
        return body


def tree_page(p: Platform, node_id: str | None) -> list[dict]:
    payload = {"checkNodes": [], "menu": 0, "type": TREE_TYPE}
    if node_id:
        payload["id"] = node_id
        payload["checkStat"] = 1
    body = p.post("/evo-apigw/admin/API/org_dev/tree", payload)
    if str(body.get("code")) != "1000":
        raise RuntimeError(f"tree {node_id or 'root'} failed: {body}")
    return body.get("data", {}).get("value") or []


def collect_channels(p: Platform, sleep_s: float):
    nodes: dict[str, dict] = {}
    queue: list[str] = []
    visited: set[str] = set()

    for node in tree_page(p, None):
        nodes[node["id"]] = node
        if node.get("nodeType") == "org" and node.get("isParent"):
            queue.append(node["id"])

    queue = [i for i in queue if i != "001"]
    while queue:
        node_id = queue.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        for node in tree_page(p, node_id):
            nodes[node["id"]] = node
            if node.get("nodeType") == "org" and node.get("isParent") and node["id"] not in visited:
                queue.append(node["id"])
        if sleep_s:
            time.sleep(sleep_s)
    return nodes


def org_path(nodes: dict[str, dict], org_id: str) -> str:
    parts = []
    current = org_id
    seen = set()
    while current and current not in seen:
        seen.add(current)
        node = nodes.get(current)
        if not node:
            break
        parts.append(node.get("name", ""))
        current = node.get("pId", "")
    return "/".join(reversed(parts))


def pick_rtsp(raw: str, token: str) -> str:
    urls = [u for u in str(raw or "").split("|") if u.startswith("rtsp://")]
    public = [
        u for u in urls
        if "127.0.0.1" not in u and "localhost" not in u and "172.16." not in u
    ]
    chosen = (public or urls or [""])[0]
    if not chosen:
        return ""
    if "token=" in chosen:
        return chosen
    return chosen + ("&" if "?" in chosen else "?") + "token=" + str(token)


def template_rtsp(raw: str) -> str:
    """Same as pick_rtsp, but with a placeholder for the short-lived token."""
    url = pick_rtsp(raw, "<TOKEN>")
    return url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] / "rtsp_streams.txt")
    parser.add_argument("--stream-type", choices=["1", "2"], default="2",
                        help="1=main stream, 2=sub stream (live preview default)")
    parser.add_argument("--limit", type=int, default=0,
                        help="only the first N channels (for testing)")
    parser.add_argument("--delay", type=float, default=0.1,
                        help="pause between StartVideo calls (seconds)")
    parser.add_argument("--mode", choices=["template", "live"], default="template",
                        help="template=save URL with <TOKEN> placeholder and stop "
                             "sessions; live=save tokenized URLs (tokens expire)")
    parser.add_argument("--stop-sessions", action="store_true",
                        help="live mode: stop sessions after collecting (tokens die)")
    args = parser.parse_args()

    p = Platform(args.base, args.user, args.password)
    print("logging in...")
    p.login()
    print("collecting channel tree...")
    nodes = collect_channels(p, sleep_s=0.1)
    channels = [n for n in nodes.values() if n.get("nodeType") == "ch"]
    channels.sort(key=lambda c: (org_path(nodes, c.get("pId", "")), c.get("name", ""), c["id"]))
    if args.limit:
        channels = channels[:args.limit]
    online = [c for c in channels if c.get("isOnline") == 1]
    print(f"channels: {len(channels)}, online: {len(online)}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    sessions: list[tuple[str, str]] = []
    ok = 0
    failed = 0

    with args.output.open("w", encoding="utf-8", newline="") as out:
        out.write("# 说明: RTSP 地址中的 <TOKEN> 需调用 "
                  "admin/API/MTS/Video/StartVideo 动态获取；token 与通道绑定，"
                  "空闲约 1 分钟即失效。\n")
        out.write("# 编号\t通道编码\t设备编码\t通道序号\t组织\t状态\tRTSP地址\n")
        out.flush()
        for index, channel in enumerate(channels, 1):
            name = channel.get("name", "")
            channel_id = channel["id"]
            org = org_path(nodes, channel.get("pId", ""))
            status = "在线" if channel.get("isOnline") == 1 else "离线"
            url = ""

            if channel.get("isOnline") == 1:
                body = p.post("/evo-apigw/admin/API/MTS/Video/StartVideo", {
                    "data": {
                        "dataType": "3",
                        "streamType": args.stream_type,
                        "channelId": channel_id,
                        "urlType": 1,
                        "packtype": "Private",
                        "optModule": "ADMIN_001031",
                    },
                })
                data = body.get("data") or {}
                if str(body.get("code")) == "1000" and data.get("url"):
                    session = data.get("session")
                    if args.mode == "template":
                        url = template_rtsp(data["url"])
                        if session:
                            p.post("/evo-apigw/admin/API/MTS/Video/StopVideo", {
                                "data": {"channelId": channel_id,
                                         "session": str(session),
                                         "optModule": "ADMIN_001031"},
                            })
                    else:
                        url = pick_rtsp(data["url"], data.get("token", ""))
                        if session:
                            sessions.append((channel_id, str(session)))
                    status = "在线"
                    ok += 1
                else:
                    status = f"取流失败({body.get('code')})"
                    failed += 1

            row = [name, channel_id, channel.get("deviceCode", ""),
                   str(channel.get("channelSeq", "")), org, status, url]
            out.write("\t".join(row) + "\n")
            out.flush()

            if index % 20 == 0 or index == len(channels):
                print(f"progress {index}/{len(channels)} ok={ok} failed={failed}")
            if channel.get("isOnline") == 1 and args.delay:
                time.sleep(args.delay)

    print(f"wrote {args.output}")
    print(f"rtsp ok: {ok}, failed: {failed}, sessions kept: {len(sessions)}")

    if args.stop_sessions and sessions:
        print("stopping stream sessions...")
        for channel_id, session in sessions:
            p.post("/evo-apigw/admin/API/MTS/Video/StopVideo", {
                "data": {"channelId": channel_id, "session": session,
                         "optModule": "ADMIN_001031"},
            })
        print("sessions stopped; tokens in the output are no longer valid")


if __name__ == "__main__":
    main()
