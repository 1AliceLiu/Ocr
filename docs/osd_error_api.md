# OSD 检测结果查询 API 使用说明

给需要**主动查询异常结果**的同学使用。数据来自监控脚本写出的
`output/osd_error.txt`（每轮只记录异常通道，不含取流超时）。

- 服务：`api`（FastAPI + uvicorn），代码在 `api/main.py`
- 默认地址：`http://<服务器IP>:8010`
- 交互式文档（可直接点按钮试）：`http://<服务器IP>:8010/docs`
- 本文档只讲"怎么查"；不需要懂后端，照抄下面的命令即可。

---

## 1. 启动 / 更新服务

```bash
cd /home/lsb/orc/orc

# 第一次启动，或改了 api/main.py 之后（必须 --build，代码是打进镜像的）
docker compose up -d --build api

# 看日志
docker compose logs -f api
```

端口由 `.env` 的 `API_PORT` 决定（宿主机端口 → 容器 8000）。本机 8000 已被别的项目占用，所以这里用 **8010**；如果启动时报 `port is already allocated`，改 `.env` 的 `API_PORT` 即可。

---

## 2. 鉴权

除 `/health` 和 `/docs` 外，所有接口都要带请求头：

```
X-API-Key: <API_KEY>
```

`API_KEY` 在 `.env` 里（`API_KEY=osd-...`）。如果 `.env` 里留空，则不需要鉴权。

---

## 3. 数据字段

每条异常记录对应 `osd_error.txt` 的一行：

| 字段 | 类型 | 含义 |
|---|---|---|
| `time` | string | 检测时间，北京时间 `YYYY-MM-DD HH:MM:SS` |
| `index` | int | 通道序号（`out/streams.txt` 里的编号） |
| `channelCode` | string | 通道编码，如 `1002484$1$0$0` |
| `name` | string | 通道名称，如 `1529_1` |
| `expected` | string | 期望编号（从通道名解析） |
| `detected` | string | 实际识别结果（可能含中文） |
| `confidence` | float | OCR 置信度 0~1；`0.99` 表示两种预处理结果一致 |
| `status` | string | 状态，见下表 |
| `round` | int | 第几轮检测 |

`status` 取值：

| 状态 | 含义 |
|---|---|
| `OK` | 识别结果与期望编号匹配 |
| `MISMATCH` | 识别到了，但和通道编号不匹配；串流也归入此状态 |
| `UNCERTAIN(...)` | 切字失败/低置信/多分支不一致，等待复核 |
| `NO_OSD` | 抓到了图，但在 OSD 区域看不到可读文字 |
| `CAPTURE_FAILED(...)` | 抓帧失败，括号里是原因（不属于 OSD 判定） |

> 注意：`CAPTURE_FAILED(capture timeout)` 属于取流超时，**不会**写进
> `osd_error.txt`（只留在 `output.txt`）。所以这个接口里不会出现它。
> `UNCERTAIN(...)` 也默认不写进 `osd_error.txt`，只留在 `output.txt` /
> `osd_rounds.csv`，避免低置信结果污染异常列表。

> 串流统一归入 `MISMATCH`，不再单独使用状态，也不会在
> `osd_error.txt` 里追加额外列；`osd_error.txt` 保持原来的 8 列格式。
> 控制台日志里仍会显示 `reason=CROSSOVER` 便于排查。

---

## 4. 接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/osd-error` | 异常明细，支持筛选、分页、排序，默认每页 5000 |
| GET | `/api/v1/osd-error/summary` | 按状态/通道汇总 + 最近几轮 |
| GET | `/api/v1/osd-error/rounds` | 每轮的开始时间、OK 数、异常数、耗时 |
| GET | `/api/v1/osd-error/channels/{channelCode}` | 某个通道的全部异常 |
| GET | `/api/v1/osd-error/status` | 文件是否新鲜、大小、记录数 |

---

## 5. 明细查询 `GET /api/v1/osd-error`

参数（全部可选）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `limit` | 5000 | 每页条数，1~5000 |
| `offset` | 0 | 跳过条数，用于翻页 |
| `status` | 无 | 状态**前缀**匹配，如 `MISMATCH`、`UNCERTAIN`、`NO_OSD`、`CAPTURE_FAILED` |
| `channel_code` | 无 | 通道编码，**子串**匹配 |
| `name` | 无 | 通道名称，**子串**匹配 |
| `expected` | 无 | 期望编号，精确匹配 |
| `round` | 无 | 轮次号 |
| `since` | 无 | 时间下界（含），如 `2026-10-20 00:00:00` |
| `until` | 无 | 时间上界（含） |
| `order` | `desc` | `desc` 最新在前 / `asc` 最早在前 |

```bash
# 不传 limit -> 默认 5000，返回全部（当前 13 条）
curl -s -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8010/api/v1/osd-error?status=MISMATCH"
```

返回示例（`items` 只截取 2 条示意，实际会返回全部 13 条）：

```json
{
  "total_records": 13,
  "updated_at": "2026-10-20 16:07:24",
  "age_seconds": 3,
  "count": 13,
  "total_matched": 13,
  "limit": 5000,
  "offset": 0,
  "page": 1,
  "total_pages": 1,
  "has_more": false,
  "next_offset": null,
  "order": "desc",
  "items": [
    {
      "time": "2026-10-20 15:56:37",
      "index": 405,
      "channelCode": "1002484$1$0$0",
      "name": "1529_1",
      "expected": "1529",
      "detected": "5284",
      "confidence": 0.357,
      "status": "MISMATCH",
      "round": 1
    },
    {
      "time": "2026-10-20 15:38:14",
      "index": 62,
      "channelCode": "1002983$1$0$0",
      "name": "2457_1",
      "expected": "2457",
      "detected": "1914",
      "confidence": 1.0,
      "status": "MISMATCH",
      "round": 1
    }
  ]
}
```

不传 `limit` 就返回全部（上限 5000 条/页）。超过 5000 条时看返回里的
`has_more` 和 `next_offset`，把 `next_offset` 当作下一次的 `offset` 继续取：

```bash
# 第 1 页（5000 条）
... /api/v1/osd-error?limit=5000&offset=0
# 第 2 页（用上一页返回的 next_offset）
... /api/v1/osd-error?limit=5000&offset=5000
```

`total_pages` 是总页数，`next_offset` 为 `null` 表示到底了。

---

## 6. 汇总 `GET /api/v1/osd-error/summary`

```bash
curl -s -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8010/api/v1/osd-error/summary"
```

```json
{
  "total": 13,
  "by_status": {"MISMATCH": 13},
  "top_channels": [
    {"channelCode": "1002983$1$0$0", "count": 1}
  ],
  "latest_round": {
    "round": 1,
    "started_at": "2026-10-20 15:34:34",
    "anomalies": 0,
    "records_in_file": 13
  },
  "recent_rounds": [ ... ],
  "file": "/work/output/osd_error.txt",
  "exists": true,
  "size": 1024,
  "updated_at": "2026-10-20 16:05:12",
  "age_seconds": 3
}
```

> `anomalies` 来自该轮结束时的汇总行；`records_in_file` 是文件里实际能数到的条数。
> 如果两者不一致，通常是这一轮还没跑完、或文件被手工清过。

---

## 7. 轮次 `GET /api/v1/osd-error/rounds`

```bash
curl -s -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8010/api/v1/osd-error/rounds"
```

```json
{
  "count": 1,
  "items": [
    {
      "round": 1,
      "started_at": "2026-10-20 15:34:34",
      "ok": 1400,
      "channels": 1521,
      "anomalies": 41,
      "timeouts_skipped": 276,
      "duration_seconds": 1382.4,
      "records_in_file": 41
    }
  ]
}
```

这一轮还没结束时，只有 `round` / `started_at` / `records_in_file`。

---

## 8. 单通道 `GET /api/v1/osd-error/channels/{channelCode}`

`channelCode` 里的 `$` 在 URL 里要写成 `%24`：

```bash
curl -s -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8010/api/v1/osd-error/channels/1002484%241%240%240"
```

```json
{
  "channelCode": "1002484$1$0$0",
  "count": 1,
  "total_matched": 1,
  "items": [ ... 同上的记录 ... ]
}
```

没有记录时返回 `404`。

---

## 9. 文件新鲜度 `GET /api/v1/osd-error/status`

```bash
curl -s -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8010/api/v1/osd-error/status"
```

```json
{
  "status": "ok",
  "stale_after_seconds": 180,
  "total_records": 13,
  "file": "/work/output/osd_error.txt",
  "exists": true,
  "size": 1024,
  "updated_at": "2026-10-20 16:05:12",
  "age_seconds": 3
}
```

`status=stale` 只表示**文件超过 3×MONITOR_INTERVAL 秒没更新**，不代表服务挂了。
`osd_error.txt` 只在有异常时才写，**连续几轮没有异常、或者正在跑一轮的中途，
文件长时间不动是正常的**。要看整体进度请用 `output.txt` 对应的
`/api/v1/status` 或监控容器日志。

---

## 10. Python 调用示例

```python
import os
import requests

API = "http://127.0.0.1:8010"
HEADERS = {"X-API-Key": os.environ["API_KEY"]}

resp = requests.get(f"{API}/api/v1/osd-error",
                    headers=HEADERS,
                    params={"status": "MISMATCH", "limit": 50},
                    timeout=10)
resp.raise_for_status()
data = resp.json()

for item in data["items"]:
    print(item["time"], item["channelCode"], item["name"],
          item["expected"], "->", item["detected"])
```

---

## 11. 常见问题

**Q: 返回空列表？**
`osd_error.txt` 只记录异常，全 OK 时就没有记录；另外确认 `--error-log` 指向
`/work/output/osd_error.txt`（compose 里已配置）。

**Q: 为什么 `capture timeout` 不在结果里？**
按需求，取流超时不算 OSD 异常，只写 `output.txt`。需要的话可以另开接口。

**Q: 文件会一直变大吗？**
不会。新增了一个 `osd-error-prune` 服务，**每天跨天时**把 `osd_error.txt` 里
前一天及更早的记录删掉，只保留当天：

```bash
docker compose ps osd-error-prune
docker compose logs -f osd-error-prune       # 每次跨天打印 kept=/dropped=

# 手动试跑（只看会删多少，不动文件）
docker compose exec osd-error-prune \
  python /work/scripts/prune_osd_error.py --once --dry-run

# 立即清理一次
docker compose exec osd-error-prune \
  python /work/scripts/prune_osd_error.py --once
```

- 默认只保留**当天**；想多留几天，在 compose 的 command 里加 `--keep-days 3`；
- **跨午夜的轮次不会被切断**：只要这一轮还有当天的记录，`# round N <时间>`
  头和 `# round N done:` 尾都会保留；
- 清理是**原地改写同一个文件**（truncate + write），监控进程正开着这个文件
  也不会把后续记录写丢（用 `os.replace` 才会）；
- 判断"当天"按记录里的北京时间（UTC+8），与机器时区无关；
- 服务重启后会立刻按当天规则清理一次，然后每 5 分钟检查日期是否变化。

`output.txt` 仍按 `--output-rotate-hours 3` 清空，两者互不影响。

**Q: 改了 `api/main.py` 为什么没生效？**
api 的代码是打进镜像的，必须 `docker compose up -d --build api`。

**Q: 有没有 Swagger/Postman 导入？**
`http://<IP>:8010/docs`（Swagger UI）或 `http://<IP>:8010/openapi.json`。

---

## 12. 附：`output.txt` 的旧接口（保留）

`output.txt` 记录**每一路每一轮**的结果（不只异常），对应接口：

| 路径 | 说明 |
|---|---|
| `/api/v1/errors` | 全部记录，按时间倒序，支持 `limit`/`expected`/`detected`/`since` |
| `/api/v1/summary` | 按期望编号汇总 |
| `/api/v1/status` | `output.txt` 的新鲜度 |

注意 `output.txt` 每 3 小时会被清空（`--output-rotate-hours 3`），
需要长期留存的异常历史请用 `osd_error.txt` 那一组接口。
