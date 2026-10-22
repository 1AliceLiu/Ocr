# OSD 检测结果看板（Demo）

一个独立的只读网页服务，用来浏览 `output/osd_error.txt` 里的异常检测结果。
**不修改 `osd20` 服务或 `scripts/evo_osd_monitor.py` 的任何代码/配置**，只读取它写出的文件。

## 启动

```bash
cd /home/lsb/orc/orc

# 第一次启动或修改 dashboard/ 下的代码后需要 --build
docker compose up -d --build osd-dashboard

# 看日志
docker compose logs -f osd-dashboard
```

打开：

```text
http://<服务器IP>:8011
```

内网实例**不需要密码**。端口默认 8011，可在 `.env` 里加
`DASHBOARD_PORT=xxxx` 覆盖。停止：

```bash
docker compose stop osd-dashboard
```

## 内网免密、公网要密码

看板实际上有两个独立实例：

```text
osd-dashboard         端口 8011，内网访问，不启用登录
osd-dashboard-public  只绑定 127.0.0.1:8012，启用登录，只给隧道用
```

所以内网同事直接打开 `http://<服务器IP>:8011` 就能看；外网访问走
`osd-dashboard-public`，必须先登录。

## 固定公网地址：Tailscale Funnel（推荐）

前提：服务器已安装 Tailscale 并登录，tailnet 已启用 Funnel / HTTPS。

1. 启动公网实例：

```bash
docker compose up -d osd-dashboard-public
```

2. 开启 Funnel（后台运行）：

```bash
tailscale funnel --bg 8012
```

3. 地址类似：

```text
https://<机器名>.<tailnet>.ts.net
```

同事不需要安装 Tailscale，直接浏览器打开即可，会看到登录页。

关闭 Funnel：

```bash
tailscale funnel --https=443 off
```

注意：

- 地址在机器名、tailnet 不变的情况下是固定的，服务器重启也不会变；
- **已经加入同一 tailnet 的设备**请使用 `http://100.118.168.16:8011`
  （内网免密），不要用 Funnel 域名；Tailscale 内会把该域名解析到
  Tailscale IP，可能和宿主机 443 端口上的其他服务冲突；
- 国内访问 `*.ts.net` 的速度取决于运营商网络。

## 临时公网地址：Cloudflare Quick Tunnel

如果只是想临时给同事看，也可以用 Cloudflare Quick Tunnel。它会给一个
临时的 `https://xxxx.trycloudflare.com` 地址，同事直接点开即可，不需要安装
任何客户端。

1. 在 `.env` 里设置公网实例的登录账号密码：

```bash
DASHBOARD_USER=osd
DASHBOARD_PASSWORD=换成你自己的强密码
```

2. 构建并启动内网实例、公网实例，再启动隧道：

```bash
docker compose up -d --build osd-dashboard osd-dashboard-public
docker compose up -d osd-dashboard-tunnel
```

3. 查看公网地址：

```bash
docker compose logs osd-dashboard-tunnel | grep trycloudflare
```

日志里会出现类似：

```text
https://purchasing-plate-preventing-price.trycloudflare.com
```

把地址、账号、密码发给同事即可。打开后是**页面内的登录表单**（不是浏览器
自带的 Basic Auth 弹窗），手机浏览器也能正常输入。

**注意：**

- Quick Tunnel 是临时的，隧道容器重启后地址会变化；
- 免费 Quick Tunnel 没有可用性保证，适合演示，不适合长期生产；
- 中国大陆访问 Cloudflare 的速度可能不稳定；如果同事打不开，可以改用
  一台有公网 IP 的服务器做反向代理/SSH 隧道，或使用 Tailscale 等私有网络；
- 停止公网访问：

```bash
docker compose stop osd-dashboard-tunnel
```

- 修改密码后需要重建公网实例：

```bash
docker compose up -d --force-recreate osd-dashboard-public
```

- 如果 `.env` 里的 `DASHBOARD_PASSWORD` 为空，公网实例也不会要求登录；
  **不要**在这种状态下保持隧道开启。
- 公网实例没有映射宿主端口，不能直接通过 `http://<服务器IP>:8000` 访问；
  只能通过隧道访问，避免绕过登录。

## 页面功能

- **首页 = 当天**：默认显示北京时间今天的异常记录，数据来自
  `output/osd_error.txt`（只含异常通道，不含取流超时）。
- **首页顶部柱状图（Top 50）**：
  - 统计所有 `MISMATCH` 记录，按通道编码聚合；
  - 提供「设备名称 / 通道编码」切换：默认只显示设备名称柱状图，切换后只显示
    通道编码柱状图；统计逻辑和计数不变；
  - 横纵坐标标题已放大；
  - 点击图表打开大图，支持 +/−、重置、Ctrl/⌘+滚轮和双指捏合缩放；
    大图在 100% 时每 4 个显示一个标签，放大到 150% 时显示全部 50 个标签。
- **表格「样例」列**：当天的 `MISMATCH` 记录如果存在抓帧图片，会显示
  「查看图片」链接；点击后可查看大图并缩放。图片按轮次保存在
  `snapshots/osd_error/round_<N>_<时间>/`；历史归档不保存图片，因此历史
  日期的样例列为空。
- **按轮次分隔**：文件里的 `# round N <time>` / `# round N done:` 会解析成
  独立的轮次卡片，每轮之间有明显的间隔和汇总信息（正常数、异常数、取流超时、
  耗时）。跨午夜的轮次会标「跨天」。
- **查历史日期**：日期选择器可以切换到 `Logs/YYYYMMDD_osd_error.csv` 归档中
  的日期。归档本身没有轮次标记，页面会用 `output/osd_rounds.csv` 推断轮次边界
  （每个通道序号在一轮里只出现一次，重复出现即视为下一轮），推断结果会标
  「轮次推断」。
- **通道编码搜索**：仅当天可用，子串匹配 `channelCode`。切到历史日期后搜索框
  会禁用，后端也会拒绝历史日期的搜索请求。
- **底部分页**：默认每页 50 条，可选 20/50/100；分页栏在结果列表最底部，
  支持首页/上一页/页码/下一页/末页。
- **可分享 URL**：支持 `?date=2026-10-21&page=2`（历史日期）和
  `?q=1000304`（当天通道搜索）。页面上的操作会自动更新地址栏。
- **自动刷新**：无。需要最新数据时点右上角「刷新」。

## 数据来源

| 数据 | 路径 | 用途 |
|---|---|---|
| 当天异常 | `output/osd_error.txt` | 首页、当天搜索、轮次汇总 |
| 历史归档 | `Logs/*_osd_error.csv` | 历史日期查询 |
| 轮次推断 | `output/osd_rounds.csv` | 给历史归档记录分组（可选） |
| 通道/设备表 | `out/channels.csv` | 柱状图：通道编码 → 设备名称 |
| 异常抓帧 | `snapshots/osd_error/round_<N>_<时间>/<index>_<code>.jpg` | 当天表格「查看图片」 |

柱状图统计规则：

- 统计当天所有 `MISMATCH` 记录，按 `channelCode` 聚合，不做置信度过滤。
- 默认横轴显示通道在 `out/channels.csv` 中对应的设备名称；切换后横轴显示
  通道编码。两种展示使用完全相同的柱子和计数。
- `STREAM_TOP_N` 控制返回的 Top N（默认 50）。

所有文件都以只读方式挂载。看板不会写入、删除或轮转任何检测数据。

## 可调参数

| 位置 | 变量 | 默认 | 说明 |
|---|---|---|---|
| `compose.yaml` | `DASHBOARD_PORT` | `8011` | 内网实例的宿主机端口 |
| `compose.yaml` | `DASHBOARD_PUBLIC_PORT` | `8012` | 公网实例绑定的回环端口（只给隧道用） |
| `.env` | `DASHBOARD_USER` | `osd` | **公网实例**的登录用户名 |
| `.env` | `DASHBOARD_PASSWORD` | 空 | **公网实例**的登录密码；留空表示不启用登录 |
| `compose.yaml` | `DASHBOARD_SESSION_HOURS` | `12` | 公网实例登录有效期（小时） |
| `compose.yaml` | `HISTORY_ROUNDS` | `1` | 是否用 `osd_rounds.csv` 推断历史轮次；`0` 关闭 |
| `compose.yaml` | `DASHBOARD_PAGE_SIZE` | `50` | 默认每页条数 |
| `compose.yaml` | `STREAM_TOP_N` | `50` | 柱状图 Top N |

改完后重新执行：

```bash
docker compose up -d --build osd-dashboard osd-dashboard-public
```

## 目录

```text
dashboard/
├── main.py            FastAPI 路由 + 登录会话 + 静态页面
├── data.py            文件解析、轮次分组、分页（仅标准库）
├── static/
│   ├── index.html
│   ├── login.html     公网实例的登录页
│   ├── app.css
│   └── app.js
├── Dockerfile
├── requirements.txt
└── README.md
```

## 接口（Demo 自用）

- `GET /api/dashboard/dates`：有数据的日期列表和当天记录数。
- `GET /api/dashboard/results?date=YYYY-MM-DD&page=1&page_size=50&channel_code=...`
  ：分页结果，按轮次分组返回。`channel_code` 只允许 `date` 等于今天。
- `GET /api/dashboard/frame?index=...&channel_code=...&frame_dir=round_...`
  ：返回该轮该通道的异常抓帧图片；`frame_dir` 可省略，兼容旧的平铺图片。
- `GET /health`：健康检查。
- `GET /docs`：FastAPI 自动文档。

## 已知限制

- 历史轮次是推断的：归档 CSV 只存异常行，不存 `# round` 注释；没有
  `output/osd_rounds.csv` 时历史页会退化成按时间排列。
- 看板只展示异常记录，`output/osd_error.txt` 本身不包含 OK 的通道和取流超时。
- 内网实例按设计**没有登录**，只应暴露在可信内网；不要把 8011 端口直接映射到公网。
- 公网实例有登录页，但会话只存在进程内存里；容器重启后需要重新登录。
- 「发生通道」统计的是所有 `MISMATCH` 记录数，**不等于**监控进程内部的
  `reason=CROSSOVER` 数量；`osd_error.txt` 没有保存这个字段。
- 历史日期的柱状图使用**当前** `out/channels.csv` 做通道 → 设备名称映射；
  如果后来通道改名或下线，历史统计可能和当天看到的名称不完全一致。
- 设备名称模式与通道编码模式使用同一批柱子（相同计数、相同位置）；同一台设备
  有多个通道进入 Top 50 时，设备名称会重复出现。
- 抓帧图片按轮次保存在
  `snapshots/osd_error/round_<N>_<YYYYMMDD_HHMMSS>/<index>_<code>.jpg`，不再
  覆盖。旧数据仍可能使用平铺的 `<index>_<code>.jpg`，页面会兼容读取。
- 历史归档仍然只保存 8 列 CSV，不保存图片；`osd-error-prune` 删除
  `osd_error.txt` 旧记录时，会同时删除对应轮次目录里的旧抓帧图片。
- 变更前遗留的平铺 `<index>_<code>.jpg` 不参与轮次目录清理；确认没有旧页面
  依赖后可以手动删除以节省磁盘。
- 历史归档由 `osd-error-prune` 跨天生成，所以只能查询已经有归档文件的日期。

查看 Funnel 状态：
tailscale funnel status
关闭固定公网地址：
tailscale funnel --https=443 off
重新开启：
tailscale funnel --bg 8012
修改密码：
# 编辑 .env 里的 DASHBOARD_PASSWORD
docker compose up -d --force-recreate osd-dashboard-public