# osd_error.txt 每日归档

`output/osd_error.txt` 只保留当天数据。删除前一天数据之前，先归档到：

```text
Logs/YYYYMMDD_osd_error.csv
```

例如 2026-10-21 删除 2026-10-20 的记录时，归档文件为：

```text
Logs/20261021_osd_error.csv
```

## 执行者

| 服务 | 行为 |
|---|---|
| `osd-error-prune` | **真正执行删除**：先归档要删除的行，再原地重写 `osd_error.txt` |
| `osd-daily` | 每天 06:30 刷新 streams 前，做一次同样的“待删除行”归档，不删除；作为 prune 服务异常时的保险 |

`osd-daily` 和 `osd-error-prune` 的归档使用同一个文件和去重逻辑，  
所以两边都归档同一批行时不会产生重复记录。

## CSV 格式

UTF-8 BOM，Excel 可直接打开：

```text
时间,编号,通道编码,通道名称,期望,识别,置信度,状态
2026-10-20 23:00:01,1,c1,n1,e1,d1,1.000,MISMATCH
```

只归档数据行，不写 `# round ...` 注释行。  
重复的完全相同行会去重。

## 抓帧图片

`osd20` 现在把每一轮的异常抓帧保存到单独的轮次目录，不再覆盖：

```text
snapshots/osd_error/round_0004_20261022_125603/0394_1002484_1_0_0.jpg
```

`osd-error-prune` 在删除 `osd_error.txt` 旧记录时，会同步删除这些记录对应的
轮次图片；如果整个轮次目录被删空，目录也会被移除。归档 CSV 仍然只保存
8 列数据，不会保存图片路径或图片文件。

清理还会按图片文件的抓取时间做一次兜底扫描：早于保留日期的图片会直接删除，
即使它对应的状态（例如 `UNCERTAIN`）从未写入 `osd_error.txt`。跨午夜轮次中
当天抓取的图片会保留，避免误删当天数据。

相关参数：

```bash
--image-dir PATH   # 默认 snapshots/osd_error
--no-images        # 只清理 osd_error.txt，不删除抓帧图片
```

## 安全规则

- 归档失败时，`osd-error-prune` **不会删除**原文件，下一轮轮询重试；
- `--dry-run` 只显示将删除/归档多少行，不写文件；
- `--no-archive` 可临时关闭归档，只删除；
- 历史 `osd_error.txt` 行如果带额外列，归档只取前 8 列，保持 CSV 结构稳定。

## 手动命令

```bash
# 只预览
docker compose exec osd-error-prune \
  python /work/scripts/prune_osd_error.py --once --dry-run

# 立即执行一次：归档 + 删除
docker compose exec osd-error-prune \
  python /work/scripts/prune_osd_error.py --once

# 关闭归档，只删除（不推荐）
docker compose exec osd-error-prune \
  python /work/scripts/prune_osd_error.py --once --no-archive
```

## 相关代码

- `scripts/prune_osd_error.py`：归档 + 删除
- `scripts/daily_refresh.py`：每日安全归档
- `Logs/`：归档目录（运行时生成，已加入 `.gitignore`）
