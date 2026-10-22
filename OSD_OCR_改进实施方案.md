# OSD OCR 改进实施方案

> 目标：把当前“抓帧 → 通用 EasyOCR → 多分支投票”的流程，改造成  
> **固定 ROI → 行投影找 OSD 行 → 列投影切单字 → 实心二值化 → 专用数字分类器 → 置信度/一致性判定 → 低置信才复核**  
> 重点解决 `1→4`、`0↔9`、`3↔8`、漏字、背景拼接，以及“上一轮 OK、下一轮 MISMATCH”的抖动问题。

- 版本：v1.0
- 日期：2026-10-21
- 适用代码：`scripts/evo_osd_monitor.py`、`scripts/osd_ocr.py`、`scripts/osd_crop_binarize.py`、`scripts/predict_image.py`、`compose.yaml`
- 原则：先离线验证，再影子模式，最后灰度启用；不直接用 `expected` 强行纠正 OCR 结果。

## 0. 当前实现状态（2026-10-21）

已落地：

- `scripts/osd_digits.py`：固定 ROI、行投影、列切分、实心二值化、逐 cell EasyOCR；
- `scripts/evo_osd_monitor.py`：`--osd-profile-file`、`--cell-reader cells|legacy`、`--cell-decoder`、`--cell-min-conf`、`MISMATCH`（含串流 `reason=CROSSOVER`）/ `UNCERTAIN` / `NO_OSD` 状态、低置信回退；
- `config/osd_profiles.json`：每路 profile 模板，缺失时自动使用安全默认值；
- `scripts/predict_image.py --cell`：离线单图验证；
- `docs/osd_error_api.md`：补充新状态说明。

按需求**暂不实现**：专用数字分类器（0-9）。

后续可继续：`calibrate_osd_roi.py` 自动校准、影子模式、每轮异常二次复核队列。

---

## 1. 背景与结论

### 1.1 已确认的问题

对 `output/osd_error.txt` 中 `# round 1 2026-10-21 08:40:53` 之后的异常图片做人工标注，共 114 个唯一通道图片：

| 分类 | 数量 | 占比 |
|---|---:|---:|
| OCR 错（OSD 与 expected 一致，但 detected 错） | 52 | 45.6% |
| 标签不一致（OSD 本身与 expected 不同） | 61 | 53.5% |
| NO_OCR / 看不到 OSD | 1 | 0.9% |

按异常行统计（同一路两轮重复）：

| 分类 | 数量 | 占比 |
|---|---:|---:|
| OCR 错 | 58 | 38.7% |
| 标签不一致 | 90 | 60.0% |
| NO_OCR | 2 | 1.3% |

结论：

1. **超过一半的异常不是 OCR 的错**，而是 OSD 标签与通道名/期望编号不一致。这类问题要靠别名表、台账核对和串流检查解决，不能靠调 OCR。
2. 真正需要 OCR 解决的是 52 张 OCR 错，其中：
   - 单字符替换 28 张，最集中的是 **`1→4`（13 张）**，其次是 `3→8`、`9→0`；
   - 漏字 10 张；
   - 多字/背景拼接 4 张；
   - 严重错读/中文乱码 10 张。
3. 这些错误不是随机抖动，很多是确定性的输入问题：ROI 裁剪、空心二值化、背景线、整行 `readtext` 拼接。
4. **同轮复核只能兜底，不能作为主修复。** 如果 ROI/二值化一直是错的，复核多少次都会得到同样的错误结果。

### 1.2 目标

- 数字通道的 `1→4`、`0↔9`、`3→8` 在回归集上降到 0。
- 当前 52 张 OCR 错降到 ≤5 张。
- 61 张标签不一致不能被“修正”成 OK；真实串流仍然能及时报警。
- 正常通道不增加误报；异常确认仍然在同一轮内完成，不依赖跨轮去抖。
- 单路 OCR 延迟可控，不显著增加整轮耗时。

### 1.3 非目标

- 不试图用 OCR 解决通道名/台账错误；这类走别名表和数据治理。
- 不全局替换 EasyOCR；字母数字和中文通道继续保留 EasyOCR/CJK 回退。
- 不引入跨轮延迟告警；历史状态只用于展示和根因分析。

---

## 2. 现状与根因

### 2.1 当前链路

```text
StartVideo -> ffmpeg 抓 3 个关键帧
  -> roi_options(): detect_roi() 或 fixed/wide ROI
  -> read_number(): raw + binarize 两个分支
  -> readtext(): 把 crop 内所有文本框拼成一个字符串
  -> 每帧 x 每 ROI x 每分支投票
  -> majority() 取多数
  -> is_good() 只检查数字和长度
  -> 写 output.txt / osd_error.txt
```

### 2.2 根因表

| 现象 | 代码位置 | 根因 |
|---|---|---|
| `1→4` | `osd_ocr.py` `detect_roi()` / `scale_box()` | ROI 裁掉 `1` 的顶部/底部，或背景线把 `1` 补成 `4` |
| `9→0`、`0→9` | `osd_crop_binarize.py` `binarize_osd()` | 只保留白色边缘一圈，`9` 的环被破坏/闭合 |
| `3→8` | `binarize_osd()` + 宽 ROI | 背景斜线被空心轮廓保留，和 `3` 连成 `8` |
| 漏字 | `detect_roi()` / `readtext()` | ROI 右边界裁掉最后一位，或 CRAFT 检测框漏字 |
| 背景拼接 | `osd_ocr.py` `_read()` | `"".join(item[1] for item in detections)` 把背景数字/字母拼进结果 |
| 投票翻盘 | `evo_osd_monitor.py` `one()` | raw/binarize 不独立，平票按插入顺序；置信度不可靠 |
| 单轮即告警 | `evo_osd_monitor.py` `one()` | 没有确认/复核，单帧误读直接落盘 |
| 标签不一致 | `expected_code()` / `numbers_match()` | 用通道名猜期望，OSD 本身可能是 IPC/P40A2/中文/另一个编号 |

### 2.3 当前可用的可视化依据

根目录已有：

- `rowproj_demo_all.png`：固定搜索框 + 行投影 + 最终 OSD 行裁剪；
- `solid_binarize_demo_all.png`：原图 / 当前空心二值化 / 实心二值化对比。

这两张图可以直接作为实现和验收的视觉参考。

---

## 3. 目标架构

```text
                    ┌─────────────────────────────┐
                    │ 每路 OSD Profile（JSON）     │
                    │ roi / type / length / alias │
                    └──────────────┬──────────────┘
                                   │
frame ──> 固定 ROI 裁剪 ──> 白色字芯 mask ──> 行投影
                                   │
                                   ▼
                             OSD 行（单行）
                                   │
                                   ▼
                         列投影 / 连通域切单字
                                   │
                                   ▼
                        每个 cell 做实心二值化
                                   │
                                   ▼
                        专用数字分类器（0-9）
                                   │
                                   ▼
                 置信度 + margin + 多帧一致性判定
                     │            │             │
                     ▼            ▼             ▼
                    OK       MISMATCH      UNCERTAIN
                                  │             │
                                  │             ▼
                                  │      同轮低置信复核
                                  ▼
                    MISMATCH（串流 reason=CROSSOVER）
```

核心原则：

1. **Profile 驱动**：每路的 ROI、类型、长度来自配置，不靠 `expected_code()` 猜。
2. **先定位再识别**：行投影只负责“找到 OSD 行”，列投影负责“切成单字”。
3. **逐字分类**：数字通道用 10 类专用分类器，不再让整串 EasyOCR 决定每个字。
4. **不用 expected 强行纠正**：分类器高置信读到别的编号，就是 MISMATCH/串流，不能“修正”成 expected。
5. **低置信才复核**：复核只处理少量不确定样本，不延迟真实异常。
6. **串流优先告警**：检测值等于另一条通道的 expected，本身就是 MISMATCH/串流，稳定即报。

---

## 4. 数据与配置

### 4.1 每路 OSD Profile

新增 `config/osd_profiles.json`，不要破坏现有 `--roi-file` 的数组格式。

```json
{
  "version": 1,
  "defaults": {
    "roi": [43, 971, 219, 1040],
    "type": "digits"
  },
  "channels": {
    "1002913$1$0$2": {
      "roi": [43, 971, 219, 1040],
      "type": "digits",
      "length": 4,
      "alias": null
    },
    "1002983$1$0$0": {
      "roi": [0, 920, 520, 1080],
      "type": "alnum",
      "length": null,
      "pattern": "P40A2-\\d{3}",
      "alias": "P40A2-067"
    },
    "1001542$1$0$6": {
      "roi": [0, 920, 520, 1080],
      "type": "cjk",
      "length": null,
      "expected_label": "正門左"
    }
  }
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `roi` | 1080p 基准坐标；复用现有 `scale_box()` 按实际高度缩放；全景帧 x 不缩放 |
| `type` | `digits` / `alnum` / `cjk` |
| `length` | 数字通道的期望位数；用于切字和校验 |
| `pattern` | 字母数字通道的正则，例如 `P40A2-\d{3}` |
| `alias` | 已知的 OSD 标签/通道名别名 |
| `expected_label` | 中文通道的实际 OSD 文本 |

坐标约定：

- 1080p：直接使用；
- 1440p/4MP/5MP：按 `height/1080` 缩放 y 和 x；
- 全景（宽高比 > 2.5）：x 不缩放，y 仍然缩放；
- 如果是在实际分辨率上量的坐标，先换算回 1080p 基准。

### 4.1.1 没有 profile 时的降级策略（重要）

`--osd-profile-file` 必须是**可选的**，不能成为代码启动的硬依赖。没有 JSON 时，代码应自动生成一个默认 profile，保证 Phase 1 可以先跑起来。

```python
def default_profile(channel):
    expected = expected_code(channel["name"])

    if expected.isdigit():
        return {
            "roi": [43, 971, 219, 1040],   # 默认底部左侧搜索框
            "type": "digits",
            "length": len(expected),
            "alias": None,
        }

    if re.search(r"[A-Za-z]", expected):
        return {
            "roi": [0, 920, 520, 1080],
            "type": "alnum",
            "length": None,
            "pattern": None,
        }

    return {
        "roi": [0, 920, 520, 1080],
        "type": "cjk",
        "length": None,
        "expected_label": None,
    }
```

使用方式必须是：

```python
profile = self.profile_map.get(code) or default_profile(channel)
```

**不要写 `self.profile_map[code]`**，否则没有 JSON 时直接 KeyError，代码跑不起来。

没有 profile 时的影响：

| 能力 | 没有 profile 时 |
|---|---|
| 默认底部左侧 OSD 通道 | 可以正常工作 |
| 行投影、列切分、实心二值化 | 可以正常工作 |
| `expected` 是纯数字的通道 | 可以用数字分类器 |
| `expected` 是字母数字的通道 | 回退 EasyOCR |
| 中文通道 | 回退 CJK 模型 |
| 特殊 ROI（全景/4MP/特殊位置） | 可能不准，需要后续校准 |
| 通道名是数字、OSD 实际是 `P40A2-xxx` / 中文 | 默认会按数字处理，需要回退 + 人工确认 |
| 标签不一致/串流 | 仍然会报，但无法自动区分类型，需要别名表 |

结论：

- **不影响代码先跑起来**，只要把 profile 设计成可选；
- **影响最终准确率和特殊通道覆盖率**，尤其是标签不一致、字母数字、中文、特殊分辨率通道；
- 推荐顺序：先用 `default_profile()` 跑 Phase 1，再用 `calibrate_osd_roi.py` 自动生成大部分 profile，只人工复核低置信/异常通道。

### 4.2 数据集目录

```text
dataset/
  osd_calib/                    # 一次性校准帧
    <index>_<channelCode>.jpg
  osd_digits/                   # 单字分类数据集
    train/0/ ... train/9/
    val/0/   ... val/9/
    test/0/  ... test/9/
    hard_negatives/             # 1↔4、0↔9、3↔8 难例
  osd_eval/
    annotations.csv             # 114 张人工标注，作为回归集
    frames/                     # 对应异常帧（可复制自 snapshots/osd_error）
```

标注规范：

- 只标注“看到的一个字”，不要把整行当一个样本；
- `0/9/4/6/8` 的内部孔洞是字形的一部分，标注时不要当作噪声；
- 背景线穿过数字的样本要保留，标签仍然是数字本身；
- 按**通道**划分 train/val/test，不能按帧随机划分，否则同一路的多帧会泄漏。

### 4.3 一次性校准帧

建议新增 `scripts/calibrate_osd_roi.py`：

```text
输入：out/streams.txt
流程：
  1. 每路抓 1 帧（复用 StartVideo/StopVideo）；
  2. 用当前 detect_roi + 行投影给出候选 OSD 行；
  3. 保存整帧、行裁剪、候选 ROI；
  4. 输出 profiles.json；
  5. 低置信/切字失败的通道进入人工复核队列。
```

建议参数：

```bash
python /work/scripts/calibrate_osd_roi.py \
  --input /work/out/streams.txt \
  --out /work/config/osd_profiles.json \
  --frames-dir /work/dataset/osd_calib \
  --limit 0 \
  --workers 8
```

### 4.4 合成数据

真实数据不足时，用 OSD 字体合成：

- 白字 + 黑描边；
- 随机背景：水泥、天空、白墙、栏杆、纹理；
- 随机穿过数字的直线/斜线；
- 随机亮度、对比度、gamma、模糊、JPEG、噪声、缩放、平移；
- 重点生成：
  - `1` 带背景斜线 / 顶部被裁；
  - `0` 和 `9` 的上半部分被背景线干扰；
  - `3` 右侧被线连上；
  - `4` 的横笔被弱化；
  - `0/9/4/6/8` 的孔洞完整保留。

---

## 5. 核心算法

### 5.1 固定 ROI + 行投影

#### 5.1.1 目标

在底部左侧固定搜索框内，找到 OSD 所在的那一条水平带，避免背景栏杆、杂物、上方文字进入 OCR。

#### 5.1.2 伪代码

```python
def find_osd_line(frame, profile):
    x1, y1, x2, y2 = scale_box(profile["roi"], frame.shape[1], frame.shape[0])
    crop = frame[y1:y2, x1:x2]

    # 1. 灰度拉伸 + 白色字芯 mask
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = percentile_stretch(gray, 2, 98)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    white = ((gray >= 210) & (hsv[:, :, 1] <= 110)).astype(np.uint8) * 255

    # 2. 连通域过滤：只保留像字形的白色字芯
    keep = filter_glyph_components(
        white,
        min_h=30, max_h=55,
        min_w=7,  max_w=35,
        min_area=150, max_area=900,
        min_aspect=0.2, max_aspect=0.85,
        require_dark_border=True,
    )

    # 3. 行投影
    row_proj = (keep > 0).sum(axis=1)
    threshold = max(3, int(0.20 * row_proj.max()))
    runs = contiguous_runs(row_proj >= threshold)
    runs = merge_runs(runs, max_gap=2)
    runs = [r for r in runs if 12 <= r.height <= 70]
    band = select_lowest_strong_run(runs, keep)

    # 4. 可选：列分组后再做一次行投影细化
    line = crop[band.start:band.stop, :]
    return line, band, keep
```

#### 5.1.3 关键规则

- 优先“最靠下的强投影段”，不是全局最大值；
- 段宽度要像一行字（≥ 2×字高）；
- 如果有时间戳/多行文字，选最底部且字数/宽度符合 `profile["length"]` 的那一行；
- 行上下各留 `0.35 × 字高` 的 padding；
- 列左右各留 `0.6 × 字高` 的 padding。

#### 5.1.4 验收

- `rowproj_demo_all.png` 中 5 个例子的行带都正确；
- 对 114 张回归图，OSD 行定位成功率 ≥99%；
- 不再出现 tight ROI 只框到背景、完全没框到数字的情况。

---

### 5.2 列投影切单字

#### 5.2.1 伪代码

```python
def segment_digits(line, expected_len, keep_mask):
    col_proj = (keep_mask > 0).sum(axis=0)
    threshold = max(2, int(0.05 * line.shape[0]))
    runs = contiguous_runs(col_proj >= threshold)
    runs = merge_runs(runs, max_gap=int(0.15 * glyph_height))
    cells = [line[:, a:b] for a, b in runs]

    cells = [c for c in cells if is_glyph_like(c)]
    if expected_len and len(cells) != expected_len:
        # 仅作为 fallback：按行宽等分；不要无条件使用
        return None, "SEGMENT_MISMATCH"
    return cells, "OK"
```

#### 5.2.2 规则

- 每个 cell 高度在 `0.6~1.2 × 中位字高`；
- 宽度在 `0.2~1.0 × 字高`；
- 面积不能太小；
- 去掉左右边缘的细长竖线；
- 如果切出的字数 != `profile["length"]`：
  - 不要强行按 expected 等分；
  - 可以先尝试等分作为 fallback；
  - 仍不一致则返回 `SEGMENT_MISMATCH`，回退 EasyOCR，并标记 `UNCERTAIN` 或“可能标签不一致/串流”。

---

### 5.3 实心二值化

#### 5.3.1 目标

保留完整白色字芯，得到实心黑字白底；不要只保留靠近黑色描边的白色边缘。

> 注意：**不要填掉 `0/9/4/6/8` 的内部孔洞**。  
> 内部白洞是字形的一部分；填掉会把 `0` 变成实心块、把 `9` 变成实心块。

#### 5.3.2 伪代码

```python
def solid_binarize(cell):
    gray = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY)
    gray = percentile_stretch(gray, 2, 98)
    hsv = cv2.cvtColor(cell, cv2.COLOR_BGR2HSV)

    # 1. 白色字芯
    white = ((gray >= 210) & (hsv[:, :, 1] <= 110)).astype(np.uint8) * 255

    # 2. 暗描边
    dark = (gray <= 100).astype(np.uint8) * 255
    near_dark = cv2.dilate(dark, np.ones((5, 5), np.uint8))

    # 3. 连通域过滤：保留完整字芯，而不是只保留边缘
    keep = np.zeros_like(white)
    for comp in connected_components(white):
        if not glyph_like(comp):
            continue
        boundary = comp - erode(comp, 3x3)
        if (boundary & near_dark).sum() / boundary.sum() >= 0.9:
            keep[comp] = 255

    # 4. 膨胀 1px 把黑色描边包进来，然后反色
    return 255 - cv2.dilate(keep, np.ones((2, 2), np.uint8), iterations=1)
```

#### 5.3.3 参数

- 白色阈值：`gray >= 210`，饱和度 `<= 110`；
- 字芯过滤：高度 30~55、宽度 7~35、面积 150~900、宽高比 0.2~0.85；
- 暗描边：`gray <= 100`，膨胀 5×5；
- 边界靠近暗描边的比例：≥0.9；
- 最后膨胀 1px，反色。

这些参数以 1080p 为基准，其他分辨率按字高缩放。

#### 5.3.4 验收

- `1954`：当前空心 `9` 变成完整实心 `9`；
- `2326`：背景线不再把 `3` 补成 `8`；
- `2436`、`1702`、`1510`：背景杂物被排除；
- `0/9/4/6/8` 的内部孔洞仍然保留。

---

### 5.4 专用数字分类器

#### 5.4.1 模型

建议小 CNN，输入 32×32 灰度：

```text
Conv3x3(16) + BN + ReLU + MaxPool
Conv3x3(32) + BN + ReLU + MaxPool
Conv3x3(64) + BN + ReLU
Flatten / GAP
FC(64) + Dropout(0.3)
FC(10)
```

参数量约 10 万，CPU 可跑，GPU 更快。

#### 5.4.2 训练

- 损失：交叉熵 + label smoothing 0.05；
- 类别权重：`1/4/0/9` 样本少时适当加权；
- 数据增强：亮度、对比度、gamma、模糊、运动模糊、JPEG、噪声、平移/缩放、随机背景线；
- 按通道划分 train/val/test；
- 先合成预训练，再用真实单字微调；
- 每轮做 hard-negative mining，重点补：
  - `1↔4`
  - `0↔9`
  - `3↔8`
  - `8↔0`
  - `5↔6`

#### 5.4.3 推理

```python
def read_digits(frame, profile, model):
    line, band, mask = find_osd_line(frame, profile)
    cells, reason = segment_digits(line, profile.get("length"), mask)
    if cells is None:
        return None, 0.0, "SEGMENT_MISMATCH"

    preds = []
    for cell in cells:
        x = cv2.resize(solid_binarize(cell), (32, 32))
        prob = model.predict_proba(x)      # softmax, 10 类
        preds.append(prob)

    text = "".join(str(int(p.argmax())) for p in preds)
    score = min(float(p.max()) for p in preds)
    margin = min(float(sorted(p)[-1] - sorted(p)[-2]) for p in preds)
    return text, {"score": score, "margin": margin}, "DIGITS"
```

#### 5.4.4 判定阈值

- `score >= 0.95` 且 `margin >= 0.3`：接受；
- `score` 或 `margin` 不够：不硬判，进入回退/复核；
- 所有字都高置信但 `text != expected`：大概率是真实标签不一致/串流，报 MISMATCH；
- 低置信且回退 EasyOCR 也不稳定：`UNCERTAIN`。

---

### 5.5 置信度与一致性判定

| 条件 | 状态 |
|---|---|
| 数字分类器高置信，`text == expected` | `OK` |
| 数字分类器高置信，`text != expected`，且 `text` 等于另一条通道的 expected | `MISMATCH`（`reason=CROSSOVER`），优先告警 |
| 数字分类器高置信，`text != expected`，其他情况 | `MISMATCH` |
| 切字失败 / 低置信，EasyOCR 回退高置信且等于 expected | `OK`（`reader=easyocr`） |
| 切字失败 / 低置信，两个分支不一致 | `UNCERTAIN` → 同轮复核 |
| 看不到 OSD | `NO_OSD` |
| 抓帧失败 | `CAPTURE_FAILED` |

OSD 判定只有 **4 个状态**：`OK`、`MISMATCH`、`UNCERTAIN`、`NO_OSD`。  
串流不再单独使用状态，统一归入 `MISMATCH`，用 `reason=CROSSOVER` 区分。

规则：

1. **不要用 expected 强行纠正分类器输出。** 否则 `1→4` 会被“修正”成 expected，真正的串流也会被掩盖。
2. 高置信稳定读到别的编号 → 不是 OCR 随机错，是标签不一致/串流。
3. 低置信/多分支不一致 → 才进入复核。
4. EasyOCR 的 `confidence` 只作参考，不作为唯一置信度；主要看分类器 `score/margin` 和多帧一致性。

---

### 5.6 同轮低置信复核

> 不做跨轮去抖。复核在同一轮内完成，避免 80 分钟一轮造成的告警延迟。

流程：

```text
主扫描：
  对每路执行 read_digits / EasyOCR 回退
  如果 OK -> 直接写 OK
  如果高置信 MISMATCH（含串流 reason=CROSSOVER）-> 进入待确认队列
  如果 UNCERTAIN -> 进入复核队列

主扫描结束后（或使用空闲 worker）：
  对复核队列重新抓 2~3 帧（新 token）
  换一种独立路径：
    - 固定窄 ROI vs 宽 ROI
    - raw 灰度 vs 实心二值化
    - 数字分类器 vs EasyOCR/beamsearch
  两次一致 -> 确认
  第二次读到 expected -> 丢弃误报
  仍不一致 -> 第三次或标 UNCERTAIN
```

时间成本：

- 异常通常约 100 路；
- 8 并发下再抓 2~3 帧、重跑 OCR，约 **1~3 分钟**；
- 不影响正常通道，不需要等下一轮。

复核原则：

- 复核必须使用**不同的独立路径**；重复同一套 ROI + 二值化 + EasyOCR 不叫复核。
- 真实串流/标签错配会稳定复现，复核后仍然报警；
- 随机 OCR 误读会在复核中被 expected 纠正，从而丢弃。

---

### 5.7 标签不一致与串流

这两类不是同一个问题，必须分开：

| 类型 | 特征 | 处理 |
|---|---|---|
| 标签不一致 | OSD 稳定是另一个值，但该值不属于现有通道，或属于同一设备的不同名字 | 加入 `alias` / 修正台账；不再重复告警 |
| 串流 / 映射错误 | 检测值等于另一条通道的 expected，且两路画面/OSD 互相矛盾 | 状态仍为 `MISMATCH`，记 `reason=CROSSOVER`，立即告警并复核两路 |
| 同一设备多通道 | 两个 channelCode 指向同一 camera，OSD 一样 | 建立重复映射表，按业务规则处理 |

串流处理建议：

1. 如果 3 个关键帧、2 个独立路径都稳定读到同一个“别的编号”，直接告警；
2. 同时复核“被检测通道”和“编号匹配到的那条通道”；
3. 如果两条通道返回同一路画面/同一个 OSD，基本确认串流或重复映射；
4. 不要因为“可能是 OCR 错”而延迟真实串流告警。

---

## 6. 代码改造清单

### 6.1 新增文件

#### `scripts/osd_digits.py`

核心模块：

```python
find_osd_line(frame, profile) -> (line, band, mask)
segment_digits(line, expected_len, mask) -> (cells, reason)
solid_binarize(cell) -> mask
DigitClassifier.load(path)
read_digits(frame, profile, classifier) -> (text, metrics, reason)
```

#### `scripts/calibrate_osd_roi.py`

一次性校准：

- 输入 `out/streams.txt`；
- 抓帧、行投影、保存候选 ROI；
- 输出 `config/osd_profiles.json`；
- 低置信通道进入人工复核队列。

#### `scripts/train_osd_digits.py`

训练脚本：

- 读取 `dataset/osd_digits`；
- 训练 10 类 CNN；
- 输出 `models/osd_digits.pt` 和 `models/osd_digits.json`（版本、阈值、输入尺寸）；
- 支持 `--hard-negatives` 追加难例。

#### `scripts/eval_osd_digits.py`

离线评测：

- 读取 `dataset/osd_eval/annotations.csv`；
- 对 114 张图跑新流程；
- 输出整串准确率、单字准确率、`1↔4`/`0↔9`/`3↔8` 混淆矩阵、标签不一致保持率。

### 6.2 修改 `scripts/evo_osd_monitor.py`

新增参数：

```text
--osd-profile-file PATH      # 默认 config/osd_profiles.json
--digit-model PATH           # 默认 models/osd_digits.pt
--shadow                     # 影子模式：只记录，不改变告警
--recheck-frames N           # 复核抓帧数，默认 3
--recheck-workers N          # 复核并发，默认沿用 workers
--recheck-roi MODE           # 复核使用的独立 ROI 路径
```

`Monitor.__init__`：

```python
self.profile_map = load_profiles(args.osd_profile_file)
self.digit_model = DigitClassifier.load(args.digit_model) if args.digit_model else None
```

`Monitor.roi_options`：

- 优先使用 `profile["roi"]`；
- 没有 profile 时保持现有 `--roi-file` / `--roi-mode` 回退；
- 不要删除旧逻辑，方便灰度回滚。

`Monitor.one`：

```python
profile = self.profile_map.get(code)
if profile and profile["type"] == "digits" and self.digit_model:
    text, metrics, reason = read_digits(frame, profile, self.digit_model)
    if text and metrics["score"] >= 0.95 and metrics["margin"] >= 0.3:
        detected = text
        reader = "digits"
    else:
        detected, conf, source = read_number(...)  # 现有 EasyOCR 回退
        reader = "easyocr"
else:
    detected, conf, source = read_number(...)
    reader = "easyocr"
```

`is_good`：

- 不再只检查“数字且长度等于 expected”；
- 数字分类器高置信结果直接接受；
- 低置信/切字失败进入复核，不要 `break` 后直接写 MISMATCH。

OSD 状态常量（只有 4 个）：

```text
OK
MISMATCH          # 串流也是 MISMATCH，附加 reason=CROSSOVER
UNCERTAIN(...)
NO_OSD
```

抓帧失败单独使用 `CAPTURE_FAILED(...)`，不属于 OSD 判定。

保存异常帧：

- 同时保存整帧、OSD 行、单字 cell；
- 文件名带 round/时间，避免 `snapshots/osd_error` 只保留最后一次异常导致无法回溯；
- 例如：
  ```text
  snapshots/osd_error/<round>/<index>_<code>_frame.jpg
  snapshots/osd_error/<round>/<index>_<code>_line.png
  snapshots/osd_error/<round>/<index>_<code>_digits.png
  ```

### 6.3 修改 `compose.yaml`

`osd20` 的 command 增加：

```yaml
- --osd-profile-file
- /work/config/osd_profiles.json
- --digit-model
- /work/models/osd_digits.pt
- --recheck-frames
- "3"
```

灰度阶段可以加：

```yaml
- --shadow
```

回滚：

- 删除 `--digit-model` 或设 `--shadow`；
- 现有 EasyOCR 路径仍然可用。

### 6.4 API 与文档

如果新增 `UNCERTAIN` / `NO_OSD`（串流仍归 `MISMATCH`）：

- 更新 `api/main.py` 的状态解析和汇总；
- 更新 `docs/osd_error_api.md` 的状态表；
- 保持旧字段兼容：
  - `osd_error.txt` 固定为原来的 8 列：
    `time index channelCode name expected detected confidence status`
  - `reader` / `segment_reason` / `roi` / `reason` 只保留在内存和控制台日志，
    不写入 `osd_error.txt`，避免破坏现有 API 和查询。
  - 如需长期排查，可另开独立的 debug CSV，不要改主日志格式。

### 6.5 保留回退

- 字母数字通道：EasyOCR / 36 类模型；
- 中文通道：CJK 模型；
- 数字分类器低置信：EasyOCR 回退；
- 新模型异常：自动回退旧路径，并记录 `reader=fallback`。

---

## 7. 实施阶段

### Phase 0：数据和基线（1~2 天）

任务：

- 把 114 张人工标注整理到 `dataset/osd_eval/annotations.csv`（当前临时文件：`/tmp/opencode/osd_annotation.csv`）；
- 复制对应异常帧到 `dataset/osd_eval/frames/`；
- 复制根目录的 `rowproj_demo_all.png`、`solid_binarize_demo_all.png` 作为视觉基线；
- 统计当前基线：整串准确率、`1→4`、`0→9`、`3→8`、漏字、背景拼接；
- 抓取一次性校准帧。

交付：

- `dataset/osd_eval/annotations.csv`
- `dataset/osd_calib/`
- 基线报告 `output/osd_eval_baseline.md`

验收：

- 114 张图都能对应到通道和 expected；
- 基线数字可复现。

### Phase 1：定位和预处理（2~3 天）

任务：

- 实现 `find_osd_line()`、`segment_digits()`、`solid_binarize()`；
- 先不接分类器，用现有 EasyOCR 跑回归集；
- 对比：
  - 当前流程；
  - 固定 ROI + 行投影；
  - + 列切分；
  - + 实心二值化。

交付：

- `scripts/osd_digits.py`（先只含定位/切分/二值化）
- 新回归结果

验收：

- 漏字、背景拼接明显下降；
- `1→4`/`3→8` 有改善；
- 标签不一致不被误改成 OK。

### Phase 2：数字分类器（3~5 天）

任务：

- 生成单字数据集；
- 合成难例；
- 训练第一版 CNN；
- hard-negative mining 2~3 轮；
- 离线评测混淆矩阵。

交付：

- `dataset/osd_digits/`
- `models/osd_digits.pt`
- `output/osd_digits_eval.md`

验收：

- 单字准确率 ≥99.8%；
- `1↔4`、`0↔9` 在回归集上为 0；
- 52 张 OCR 错降到 ≤5 张。

### Phase 3：集成与影子模式（2~3 天）

任务：

- 接入 `evo_osd_monitor.py`；
- 加 `--shadow`，新旧流程同时跑；
- 只记录差异，不改变告警；
- 观察 1~2 天。

交付：

- 影子对比日志
- 差异分析报告

验收：

- 正常通道不增加误报；
- 标签不一致仍然报 MISMATCH；
- 串流样本仍然能报；
- 异常复核耗时在可接受范围。

### Phase 4：灰度上线（1~2 天）

任务：

- 先对纯数字通道启用数字分类器；
- 字母数字/CJK 继续 EasyOCR；
- 保留 `--shadow` 开关；
- 观察一周。

验收：

- `osd_error.txt` 中 OCR 错占比明显下降；
- `1→4`、`0→9` 不再出现；
- 真实串流仍能及时报警；
- 整轮耗时增加不超过可接受范围。

### Phase 5：持续迭代（每周）

- 收集新的 `1↔4`、`0↔9`、`3↔8` 错分；
- 加入 hard negatives；
- 重新训练、影子验证、灰度发布；
- 更新 profile 和别名表。

---

## 8. 评测方案

### 8.1 回归集

使用 `dataset/osd_eval/annotations.csv` 的 114 张图：

- 52 张 OCR 错；
- 61 张标签不一致；
- 1 张 NO_OCR。

### 8.2 指标

| 指标 | 目标 |
|---|---:|
| 整串准确率（数字通道） | ≥99.5% |
| 单字准确率 | ≥99.8% |
| `1→4` | 0 |
| `0↔9` | 0 |
| `3→8` | 0 |
| 当前 52 张 OCR 错修复数 | ≥47 |
| 标签不一致保持率 | 100% 仍报 MISMATCH（串流带 reason=CROSSOVER） |
| 正常通道误报率 | 不高于当前 |
| 异常复核耗时 | ≤3 分钟/轮 |

### 8.3 混淆矩阵

必须输出 10×10 混淆矩阵，重点看：

```text
1 -> 4
4 -> 1
0 -> 9
9 -> 0
3 -> 8
8 -> 3
```

### 8.4 影子模式对比

同一批帧同时跑：

- 当前 EasyOCR 流程；
- 新流程（行投影 + 切分 + 实心二值化 + 分类器）；
- 记录：
  - `detected_current`
  - `detected_new`
  - `score/margin`
  - `status_current`
  - `status_new`
  - `reason`

只有满足以下条件才启用：

- 新流程在正常帧上不增加误报；
- 新流程在 52 张 OCR 错上修复 ≥47；
- 61 张标签不一致仍然报异常；
- 串流样本仍然报异常。

---

## 9. 上线与运维

### 9.1 灰度顺序

1. 影子模式：只记录，不改告警；
2. 纯数字通道启用；
3. 观察一周；
4. 字母数字通道继续 EasyOCR；
5. 稳定后再考虑 36 类模型统一数字/字母。

### 9.2 监控指标

- 每轮 OK / MISMATCH / UNCERTAIN / NO_OSD 数量；串流看 `reason=CROSSOVER`；
- 每路 `score/margin` 分布；
- 每路 `SEGMENT_MISMATCH` 次数；
- 每路 OCR 结果变化频率；
- 复核纠正率（复核后变 OK 的比例）；
- 1↔4、0↔9、3↔8 的每周统计。

### 9.3 回滚

```text
--shadow                          # 只记录，不改变告警
--digit-model ""                  # 关闭数字分类器，回退 EasyOCR
--osd-profile-file ""             # 回退现有 ROI 逻辑
```

### 9.4 重训练节奏

- 每周收集新异常；
- 人工确认后加入 hard negatives；
- 重新训练；
- 影子验证 1 天；
- 灰度发布；
- 保留上一版模型，异常时回滚。

---

## 10. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 背景线和数字粘连 | 切字/分类失败 | 单 cell 实心二值化 + 边界暗描边过滤 + 低置信复核 |
| `0/9/4` 孔洞被填掉 | 字形错误 | **不要填大孔洞**；保留完整字芯 |
| ROI 校准漂移 | 漏字/框错 | 定期重校准；profile 版本化；低置信回退 |
| 新相机字体/分辨率 | 分类器不准 | 按分辨率/字体分模型；hard-negative 重训练 |
| expected 本身错误 | 误报 | 标签不一致走别名/台账，不强行纠正 |
| 串流被复核掩盖 | 漏报 | 稳定串流立即告警；复核两路；状态仍为 `MISMATCH`，记 `reason=CROSSOVER` |
| 分类器过拟合 | 新场景变差 | 按通道划分数据集；合成+真实混合；持续难例挖掘 |
| 性能下降 | 整轮变慢 | 小 CNN；只对异常复核；批处理；GPU |
| 新状态破坏 API | 查询异常 | 保持旧字段；追加字段；更新 API 文档 |

---

## 11. 附录

### 11.1 当前关键代码位置

| 文件 | 作用 |
|---|---|
| `scripts/evo_osd_monitor.py` | 抓帧、ROI、OCR、投票、写日志 |
| `scripts/osd_ocr.py` | `detect_roi`、`read_number`、`binarize_robust` |
| `scripts/osd_crop_binarize.py` | `binarize_osd` 白芯/暗边过滤 |
| `scripts/predict_image.py` | 离线单图验证 |
| `compose.yaml` | `osd20` 服务参数 |
| `output/osd_rounds.csv` | 每轮结果 |
| `output/osd_error.txt` | 异常日志 |
| `snapshots/osd_error/` | 异常帧 |
| `api/main.py` | 异常查询 API |

### 11.2 参考可视化

- `rowproj_demo_all.png`：行投影定位示例；
- `solid_binarize_demo_all.png`：空心 vs 实心二值化示例；
- `rowproj_roi_*.png`：行投影裁出的 OSD 行；
- `solid_binarize_roi_*.png`：实心二值化结果。

### 11.3 关键参数默认值（1080p 基准）

| 参数 | 默认值 |
|---|---|
| 搜索框高度 | 180 px |
| 搜索框宽度 | 700~760 px |
| 白色阈值 | gray ≥ 210，sat ≤ 110 |
| 字芯高度 | 30~55 px |
| 字芯宽度 | 7~35 px |
| 字芯面积 | 150~900 |
| 字芯宽高比 | 0.2~0.85 |
| 行投影阈值 | max(3, 0.20 × 最大投影) |
| 行带高度 | 12~70 px |
| 列分组 gap | ≤0.15 × 字高 |
| 行 padding | 0.35 × 字高 |
| 列 padding | 0.6 × 字高 |
| 分类器输入 | 32×32 灰度 |
| 接受阈值 | score ≥ 0.95，margin ≥ 0.3 |

### 11.4 状态定义

| 状态 | 含义 |
|---|---|
| `OK` | 高置信且等于 expected |
| `MISMATCH` | 高置信但 OSD != expected；串流也归入此状态，附加 `reason=CROSSOVER` |
| `UNCERTAIN(...)` | 低置信/切字失败/多分支不一致，等待复核 |
| `NO_OSD` | 看不到 OSD |
| `CAPTURE_FAILED(...)` | 抓帧失败（不属于 OSD 判定） |

---

## 12. 一句话总结

**先把“框、行、字、二值化”做 deterministic，再用专用数字分类器做逐字识别；置信度不够才复核，绝不用 expected 强行纠正。**  
这样 `1→4`、`0→9`、`3→8`、漏字、背景拼接会被从输入和模型两侧同时解决，而真正的串流/标签不一致仍然能及时报警。
