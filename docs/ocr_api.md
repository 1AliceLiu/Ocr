# OSD OCR HTTP API

单通道 OSD OCR：输入一张图片（`img`，base64/data URL）或
`out/streams.txt` 的一行，返回该通道的期望值、OCR 识别结果和状态。
算法复用 `scripts/evo_osd_monitor.py` 的 `Monitor.one()`，与 `osd20` 的判定一致。

服务：`ocr-api`（镜像 `easyocr-dev-ocr-api:latest`），默认端口 `8014`。
接口文档（Swagger UI）：`https://supervisor-cx5200-v5.tailb2db87.ts.net:8443/docs`
（直连：`http://<服务器>:8014/docs`）。

当前入口：

- **公网 OCR 算法 API（推荐，直接到算法服务，不是看板网页）**：
  `https://supervisor-cx5200-v5.tailb2db87.ts.net:8443`
  接口：`https://supervisor-cx5200-v5.tailb2db87.ts.net:8443/api/v1/ocr`
  这是 Tailscale Funnel 8443 直接代理到 `ocr-api`，和看板无关，
  **不需要看板登录**，只要 `X-API-Key`。
- **公网备用（如果公司网络封 8443）**：
  `https://supervisor-cx5200-v5.tailb2db87.ts.net/ocr`
  接口：`https://supervisor-cx5200-v5.tailb2db87.ts.net/ocr/api/v1/ocr`
  这是 443 上看板的 `/ocr` 反向代理，最终也是同一个 OCR 算法 API。
- Tailscale 直连：`http://100.118.168.16:8014`
- 内网直连：`http://192.168.1.25:8014`

注意：

- 看板网页是 `https://supervisor-cx5200-v5.tailb2db87.ts.net/`；
  不要用 `https://supervisor-cx5200-v5.tailb2db87.ts.net/api/v1/ocr`，
  那是看板路径，会返回 `401「需要登录」`。
- 8443 的 `/docs` 是直连 Swagger；443 的 `/ocr/docs` 可能因
  `/openapi.json` 路径不完整而显示不了，备用路径以本文档为准。
- 在服务器本机测试 8443 时，主机名会解析到 Tailscale IP，可能命中本地
  8443 的 MediaServer；请用外部网络，或加
  `--resolve supervisor-cx5200-v5.tailb2db87.ts.net:8443:103.84.155.153`。
  外部同事正常 DNS 即可。

## 鉴权

所有 OCR 请求都要带：

```http
X-API-Key: <OCR_API_KEY>
```

`OCR_API_KEY` 在服务器 `.env` 中，请通过私聊/密码管理器发给同事，不要写进
代码仓库或工单。服务端没配置 key 时接口返回 `503`。

1f95356b5df1726d7575d8362ac1f8e2136d452b85ef7201d8d98c96cb363fbf

## 图片模式（新）

`POST /api/v1/ocr`

请求字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 通道名称，例如 `2833_1`；用于 ROI/CJK 和默认 profile |
| `expected` | 是 | 期望值，例如 `2833` |
| `img` | 是 | JPEG/PNG 的 base64，或 `data:image/jpeg;base64,...`；完整画面，不要只裁 OSD 小图 |
| `channelCode` | 否 | 有就带上，用于查 `osd_profiles.json` 的专用 profile；不带时服务端按 `name`/`expected` 查 `channels.csv` 找通道编码，找不到才用 `name` |
| `detected` | 否 | 输出字段；请求里带会被忽略 |
| `status` | 否 | 输出字段；请求里带会被忽略 |

示例：

```json
{
  "name": "2833_1",
  "expected": "2833",
  "img": "data:image/jpeg;base64,/9j/4AAQSk..."
}
```

响应（`source` 为 `image`）：

```json
{
  "name": "2833_1",
  "expected": "2833",
  "detected": "28",
  "confidence": 0.99,
  "status": "MISMATCH",
  "ok": false,
  "source": "image",
  "tookMs": 4460
}
```

图片模式不需要 EVO 账号或 RTSP 地址；服务端直接对 `img` 跑 OCR。
单张图片只跑一遍，和 `osd20` 多帧多数投票的结果可能略有差异，状态通常一致。
响应不原样回传 `img`。

## 流模式（可选）

也可以直接发一行 `out/streams.txt` 数据。两种写法任选其一：

### 1. 直接发一行 streams.txt 数据

`line` 用制表符分隔，必须 8 列；表头/`#` 注释行会返回 `400`。

```json
{
  "line": "1\t1003511$1$0$0\t1003511\t2823_1\t0\tHSK/HSK C2 90天云存\t在线\trtsp://...token=<TOKEN>",
  "source": "platform"
}
```

`line` 里的 RTSP 地址可以是 `out/streams.txt` 的模板（含 `<TOKEN>`）；
`source=platform` 时服务端会用自己的 EVO 账号重新取一个短期 RTSP 地址，
不使用这一列。

### 2. 发结构化字段

```json
{
  "index": 1,
  "channelCode": "1003511$1$0$0",
  "deviceCode": "1003511",
  "name": "2823_1",
  "seq": "0",
  "org": "HSK/HSK C2 90天云存",
  "status": "在线",
  "url": "",
  "source": "platform"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `line` | 二选一 | 一整行 streams.txt 数据，优先级高于结构化字段 |
| `channelCode` | 结构化必填 | 通道编码，例如 `1003511$1$0$0` |
| `name` | 结构化必填 | 通道名称，例如 `2823_1`；期望值按 `out/channels.csv` 的 `设备名称` 取 |
| `expected` | 否 | 可选，覆盖 `channels.csv` 里的设备名称 |
| `url` | `source=url` 时必填 | 可直接播放的新鲜 RTSP 地址，不能含 `<TOKEN>` |
| `source` | 否 | `auto`（默认）/ `platform` / `url` |

`source` 规则：

- `platform`：服务端用 `EVO_BASE/EVO_USER/EVO_PASSWORD` 调 StartVideo 取新地址，
  忽略请求里的 `url`；适合直接发 streams.txt 的模板行。
- `url`：直接用请求里的 `url`；该地址必须新鲜、可播放，token 通常约 30 秒失效。
- `auto`：服务端配置了 EVO 账号时用 `platform`，否则用 `url`。

## 响应

```json
{
  "index": 1,
  "channelCode": "1003511$1$0$0",
  "name": "2823_1",
  "expected": "2823",
  "detected": "2823",
  "confidence": 0.6867,
  "status": "OK",
  "ok": true,
  "crossover": false,
  "reason": "",
  "reader": "cells",
  "source": "platform",
  "tookMs": 25320
}
```

| 字段 | 说明 |
|---|---|
| `expected` | 图片模式取请求的 `expected`；流模式取 `out/channels.csv` 中该通道的 `设备名称`，找不到时回退到通道名称的尾部编号 |
| `detected` | OCR 识别结果，空字符串表示没读到 |
| `confidence` | OCR 置信度，0–1 |
| `status` | `OK` / `MISMATCH` / `NO_OSD` / `UNCERTAIN(...)` / `CAPTURE_FAILED(...)` |
| `ok` | `status == "OK"` |
| `crossover` | 识别值是否匹配了另一台设备的期望值（仅诊断用，仍算 MISMATCH） |
| `reason` | `CROSSOVER` 等诊断原因 |
| `reader` | `cells` / `legacy` / `cells+legacy` |
| `source` | 实际输入方式：`image` / `platform` / `url` |
| `tookMs` | 本次 OCR 耗时（毫秒） |

状态含义：

- `OK`：识别值与期望值一致。
- `MISMATCH`：读到 OSD，但与期望值不一致。
- `NO_OSD`：画面里没有找到 OSD 文本。
- `UNCERTAIN(...)`：画面可读性不足，不能下结论；括号内是原因。
- `CAPTURE_FAILED(...)`：仅流模式；RTSP 取流失败（地址过期、设备离线、网络等），
  不是 OSD 判定，换一条新流再试。

## 示例

图片模式（Python，推荐；避免 base64 超出 shell 参数长度）：

```python
import base64, requests

BASE = "https://supervisor-cx5200-v5.tailb2db87.ts.net:8443"
# 如果 8443 被网络封：BASE = "https://supervisor-cx5200-v5.tailb2db87.ts.net/ocr"
# 直连（仅 Tailscale/内网）：BASE = "http://100.118.168.16:8014"
img = base64.b64encode(open("frame.jpg", "rb").read()).decode()
r = requests.post(
    BASE + "/api/v1/ocr",
    headers={"X-API-Key": "<OCR_API_KEY>"},
    json={"name": "2833_1", "expected": "2833", "img": img},
    timeout=120,
)
r.raise_for_status()
print(r.json())
```

流模式（curl）：

```bash
KEY='<OCR_API_KEY>'
BASE='https://supervisor-cx5200-v5.tailb2db87.ts.net:8443'
# 如果 8443 被网络封：BASE='https://supervisor-cx5200-v5.tailb2db87.ts.net/ocr'
# 直连（仅 Tailscale/内网）：BASE='http://100.118.168.16:8014'

# 直接发一行 streams.txt 数据（平台模式）
curl -s "$BASE/api/v1/ocr" \
  -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"line":"1\t1003511$1$0$0\t1003511\t2823_1\t0\tHSK/HSK C2 90天云存\t在线\trtsp://...token=<TOKEN>","source":"platform"}'

# 结构化字段 + 新鲜 RTSP 地址
curl -s "$BASE/api/v1/ocr" \
  -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"channelCode":"1003511$1$0$0","name":"2823_1","url":"rtsp://...","source":"url"}'
```

Python：

```python
import requests

BASE = "https://supervisor-cx5200-v5.tailb2db87.ts.net:8443"
# 如果 8443 被网络封：BASE = "https://supervisor-cx5200-v5.tailb2db87.ts.net/ocr"
r = requests.post(
    BASE + "/api/v1/ocr",
    headers={"X-API-Key": "<OCR_API_KEY>"},
    json={"line": line_from_streams_txt, "source": "platform"},
    timeout=120,
)
r.raise_for_status()
print(r.json())
```

## 限制与注意

- 一次只处理一个通道/图片；服务端串行执行 OCR，不提供批量接口。
- 流模式单次通常 10–60 秒，图片模式通常几秒；客户端超时建议 ≥120 秒。
- 图片模式只收完整画面（JPEG/PNG），不要只裁 OSD 小图；base64 解码后
  上限 10MB、2000 万像素。`img` 与 `line` 只能二选一。
- 服务端不保存截图，不写 `output/`，不重启设备，只做 OCR/取帧。
- `source=platform` 需要服务器能登录 EVO 平台；`source=url` 不需要平台账号，
  但调用方要自己保证 URL 新鲜。
- 流模式的期望值取 `out/channels.csv` 的 `设备名称`，不是通道名称本身；例如
  `2823_1` 的期望值是 `2823`。图片模式直接用请求里的 `expected`。
- RTSP 地址和 OCR 结果可能包含业务信息，请只在受控渠道里传递。
- 接口文档：`/docs`；OpenAPI：`/openapi.json`。

## 常见错误

| 现象 | 原因 / 解决 |
|---|---|
| `401 {"detail":"需要登录"}` | 用了看板根路径，例如 `https://supervisor.../api/v1/ocr`；改用 `https://supervisor...:8443/api/v1/ocr`，或备用的 `https://supervisor.../ocr/api/v1/ocr` |
| `401 {"detail":"X-API-Key 无效或缺失"}` | OCR key 错误或没带；在 `.env` 的 `OCR_API_KEY` 里取，单独发送 |
| 连接 `100.118.168.16` 或 `192.168.1.25` 超时 | 不在 Tailscale/内网；改用公网 `https://supervisor...:8443`，或备用 `https://supervisor.../ocr` |
| `502 {"detail":"OCR API 不可达..."}` | 仅 443 备用路径；看板代理到 `ocr-api` 失败，检查 `docker compose ps ocr-api` 和 `docker compose logs --tail 50 ocr-api` |
| `503 OCR_API_KEY 未配置` | 服务端 `.env` 没有 `OCR_API_KEY`；配置后重启 `ocr-api` |

## 维护

```bash
# 代码在 scripts/ocr_api.py，通过 volume 挂载；改完重启：
docker compose restart ocr-api

# Dockerfile / 依赖变更：
docker compose build ocr-api && docker compose up -d --force-recreate ocr-api

# 查看日志：
docker compose logs -f ocr-api
```

API 容器只挂载 `scripts/`、`out/`、`config/`（只读）和 EasyOCR 模型卷，
不挂载仓库根目录，因此读不到 `.env`。
