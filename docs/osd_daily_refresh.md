# 每天 09:00 自动刷新 streams.txt 并重启 OSD 检测

`evo_osd_monitor.py` 只在**启动时读一次** `out/streams.txt`，所以改了列表必须重启
监控进程才会生效。这个服务把两件事串起来：

1. 每天 **北京时间 09:00** 调用 `scripts/evo_pull.py --with-streams --stream-mode template`
   重新拉取通道列表；
2. 校验新列表（行数、是否比旧表小太多），通过后**原子替换** `out/streams.txt`，
   同时更新 `out/devices.csv`、`out/channels.csv`；
3. 通过 Docker API 重启 `osd20` 容器，下一轮就用新列表。

代码：`scripts/daily_refresh.py`；服务：compose 里的 `osd-daily`。

刷新前还会把 `output/osd_error.txt` 中“按保留规则将要删除的旧行”归档到
`Logs/YYYYMMDD_osd_error.csv`，作为 `osd-error-prune` 的保险；归档本身不删除
数据。详见 `docs/osd_error_archive.md`。

---

## 1. 启动

```bash
cd /home/lsb/orc/orc
docker compose up -d osd-daily
docker compose logs -f osd-daily
```

只会创建/启动这一个服务，不会动 osd20。

启动后日志会打印下一次执行时间：

```
2026-10-21 10:12:03 next refresh at 09:00 Beijing (in 22.80 h)
```

## 2. 手动验证（推荐先跑这个）

```bash
# 只拉取 + 校验，不替换文件、不重启（最安全）
docker compose exec osd-daily \
  python /work/scripts/daily_refresh.py --once --dry-run

# 执行一次完整流程：替换 streams.txt 并重启 osd20
docker compose exec osd-daily \
  python /work/scripts/daily_refresh.py --once

# 只更新 streams.txt，不重启（下一轮开始前你自己重启）
docker compose exec osd-daily \
  python /work/scripts/daily_refresh.py --once --no-restart
```

正常输出示例：

```
2026-10-21 09:00:02 pull: /opt/conda/bin/python /work/scripts/evo_pull.py --outdir /tmp/osd-refresh-abcd1234 --with-streams --stream-mode template
...
2026-10-21 09:00:31 rows: new=1521 current=1521
2026-10-21 09:00:31 replaced /work/out/streams.txt (1521 rows)
2026-10-21 09:00:31 replaced /work/out/devices.csv
2026-10-21 09:00:31 replaced /work/out/channels.csv
2026-10-21 09:00:33 docker: restarted osd20 (a1b2c3d4e5f6)
```

## 3. 安全措施

| 情况 | 行为 |
|---|---|
| 平台登录失败 / 拉取报错 | **保留旧** `streams.txt`，不重启 |
| 新列表行数 < `--min-rows`（默认 100） | 拒绝替换 |
| 新列表 < 当前行数的 `--min-ratio`（默认 50%） | 拒绝替换 |
| 替换方式 | 先写 `*.tmp` 再 `os.replace`，不会出现半个文件 |
| `devices.csv` / `channels.csv` 缺失或为空 | 保留旧文件，但不影响 `streams.txt` 的替换和重启 |

失败只写日志，不会影响正在跑的 osd20。

## 4. 可调参数

在 `compose.yaml` 的 `osd-daily` 服务里：

```yaml
    command:
      - python
      - /work/scripts/daily_refresh.py
      - --watch
      - --time
      - "${REFRESH_TIME:-09:00}"     # 改 .env 的 REFRESH_TIME 即可
```

脚本还支持：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--min-rows` | 100 | 少于这个行数视为拉取异常 |
| `--min-ratio` | 0.5 | 新表小于旧表这个比例时拒绝 |
| `--service` | osd20 | 要重启的 compose 服务名 |
| `--no-restart` | 关 | 只更新文件 |

额外参数会原样传给 `evo_pull.py`，例如：

```bash
docker compose exec osd-daily python /work/scripts/daily_refresh.py --once -- \
  --stream-type 1 --page-size 200
```

## 5. 注意事项

- **会中断当前这一轮**：重启 osd20 时，正在进行的那一轮会从头开始，已经检测的通道会重跑。
  每天 09:00 执行一次，影响可控；如果希望避开，改 `REFRESH_TIME`。
- **挂载了 `/var/run/docker.sock`**：`osd-daily` 需要它来重启 osd20。
  这等于给了该容器控制 Docker 的权限，仅限本机可信环境；不接受的话用下面的 cron 方案。
- **时区**：脚本固定按 UTC+8 判断，不依赖容器/宿主时区。
- **只覆盖 `out/` 下的三个文件**：`streams.txt`、`devices.csv`、`channels.csv`。
  拉取时使用系统临时目录（`/tmp/osd-refresh-*`），不会在项目里创建 `out.new`
  之类的文件夹，完成后临时目录自动删除。
- **通道数超过 1521**：osd20 的 compose 参数是 `--limit 1521`，只会检测前 1521 路。
  要全量需要把那一行改成 `--limit 0`（属于 osd20 配置，我没动）。

## 6. 不想用容器的话（宿主机 cron）

```bash
# 每天 09:00（宿主机时区需为 Asia/Shanghai，或用 01:00 UTC）
0 9 * * * cd /home/lsb/orc/orc && \
  docker compose run --rm -T osd-daily python /work/scripts/daily_refresh.py --once >> /var/log/osd-refresh.log 2>&1
```

`--once` 结束后容器自动删除；重启 osd20 仍然通过 Docker API（cron 在宿主机上，
也可以直接 `docker compose restart osd20`，二选一）。
