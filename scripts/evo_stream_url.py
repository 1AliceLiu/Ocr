"""Print one fresh, playable RTSP URL for a channel.

On ICC platforms the RTSP token is valid for about 30 seconds and can be used
only once, so a URL collected in bulk is usually dead by the time you play it.
Call this right before opening the stream instead.

Usage:
    python scripts/evo_stream_url.py --channel '1003060$1$0$0'
    python scripts/evo_stream_url.py --channel '1003060$1$0$0' --stream-type 1

Connection settings come from the same options/environment variables as
evo_pull.py: --base/EVO_BASE, --user/EVO_USER, --password/EVO_PASSWORD.

The script leaves the media session open on purpose (stopping it would kill the
URL). Sessions expire on their own; use --stop to release it after printing.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evo_pull import START_VIDEO, STOP_VIDEO, Platform, is_ok, pick_rtsp  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.getenv("EVO_BASE", ""))
    parser.add_argument("--user", default=os.getenv("EVO_USER", ""))
    parser.add_argument("--password", default=os.getenv("EVO_PASSWORD", ""))
    parser.add_argument("--token", default="", help="reuse an access_token")
    parser.add_argument("--user-id", default="1")
    parser.add_argument("--channel", required=True, help="channelCode, e.g. 1003060$1$0$0")
    parser.add_argument("--stream-type", choices=["1", "2"], default="2",
                        help="1=main stream, 2=sub stream")
    parser.add_argument("--stop", action="store_true",
                        help="stop the session after printing (URL dies)")
    parser.add_argument("--verify-tls", action="store_true")
    args = parser.parse_args()
    if not args.base:
        parser.error("--base is required (or set EVO_BASE)")

    p = Platform(args.base, args.user, args.password, args.token,
                 args.user_id, args.verify_tls)
    if not p.token:
        p.login()

    body = p.post(START_VIDEO, {"data": {
        "dataType": "3",
        "streamType": args.stream_type,
        "channelId": args.channel,
        "urlType": 1,
        "packtype": "Private",
        "optModule": "ADMIN_001031",
    }})
    if not is_ok(body):
        raise SystemExit(f"StartVideo failed: code={body.get('code')!r} "
                         f"errMsg={body.get('errMsg')!r}")
    data = body.get("data") or {}
    url = pick_rtsp(data.get("url") or "", data.get("token") or "")
    if not url:
        raise SystemExit("StartVideo returned no rtsp url")
    print(url)

    if args.stop and data.get("session"):
        p.post(STOP_VIDEO, {"data": {"channelId": args.channel,
                                     "session": str(data["session"]),
                                     "optModule": "ADMIN_001031"}})


if __name__ == "__main__":
    main()
