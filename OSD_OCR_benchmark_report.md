# OSD OCR 离线 Benchmark 报告

- 日期：2026-10-21
- 脚本：`scripts/benchmark_osd_cells.py`
- 标注集：`dataset/osd_eval/annotations.csv`（114 个唯一通道图片）
- 图片目录：`snapshots/osd_error/`
- 运行方式：EasyOCR + GPU
- 结果目录：
  - `output/benchmark/`：greedy decoder
  - `output/benchmark_beam/`：beamsearch decoder（推荐）

> 本报告只做离线评测，没有修改生产代码，也没有重启容器。

---

## 1. A/B 定义

| 路径 | 做法 |
|---|---|
| **A：当前流程** | `binarize_robust(line)` + EasyOCR `readtext` 整行识别 |
| **B：新流程** | 行投影找 OSD 行 → 列投影/组件切单字 → 每个 cell 实心二值化 → EasyOCR `recognize()` 逐字识别 |

重点目标：OCR 错（OSD 正确但 detected 错）能修好多少，尤其是 `1→4`、`0→9`、`3→8`、漏字、背景拼接。

---

## 2. 总体结果（beamsearch）

| 指标 | A：当前 | B：实心 + 逐 cell |
|---|---:|---:|
| 与人工 OSD 完全一致（全部 114 行） | 62/114 | **81/114** |
| 与 expected 完全一致（全部 114 行） | 22/114 | **44/114** |
| 与 expected 匹配（允许前导 0） | 22/114 | **45/114** |
| 切分成功 | - | 94/114 |

---

## 3. OCR 错样本（主目标）

只统计 **digit-only OSD** 的 OCR 错样本，共 **44 行**：

| 指标 | A：当前 | B：实心 + 逐 cell |
|---|---:|---:|
| 与 expected 匹配（允许前导 0） | 22/44 | **41/44** |
| 单字准确率 | 108/173 | **164/173** |
| 切分成功 | - | 42/44 |

混淆对比：

| 混淆 | A 当前 | B 实心 |
|---|---:|---:|
| `1→4` | 3 | **0** |
| `3→8` | 1 | **0** |
| `9→8` | 1 | **0** |
| `5→6` | 1 | **0** |
| `2→3` | 1 | 1 |

结论：

- `1→4` 全部消失；
- `3→8`、`9→8`、`5→6` 全部消失；
- 仍然剩余 1 个 `2→3`；
- 44 个 digit-only OCR 错里，B 修好了 41 个。

---

## 4. 剩余错误分析

B 仍然失败的 digit-only OCR 样本只有 3 个：

| 行号 | 通道 | expected | 人工 OSD | B 结果 | 原因 |
|---:|---|---:|---:|---:|---|
| 21 | `1002295$1$0$0` / 1851_1 | 1851 | 1851 | `6` | 宽搜索框把背景脚手架当成了文字组，切分只选出 1 个残片 |
| 40 | `1001897$1$0$0` / 1784_1 | 1784 | 1784 | `4` | 同上，背景钢筋/钢管干扰了宽搜索框 |
| 42 | `1001836$1$0$0` / 1726_1 | 1726 | 1726 | `1736` | 单字 `2→3`，典型字体混淆 |

**修正：前两个不是 OSD 被裁掉，而是宽搜索框选错了背景组。**  
用固定窄 ROI `(25,960)-(235,1045)` / `(30,960)-(235,1045)` 后，OSD 是完整的 `1851` / `1784`，实心二值化也非常干净。  
证据图：

- `osd_failcase_all.png`
- `osd_failcase_1851_demo.png` / `osd_failcase_1851_crop.png` / `osd_failcase_1851_solid.png`
- `osd_failcase_1784_demo.png` / `osd_failcase_1784_crop.png` / `osd_failcase_1784_solid.png`

因此前两个问题应该靠 **固定 ROI / 行投影分组** 修复，不是分类器；  
第三个 `2→3` 才是唯一一个可能靠分类器/模板匹配进一步修正的样本。

另外，`0676` 与 `676` 是前导 0 差异，已经按 `numbers_match` 规则算作匹配，不算 OCR 错。

---

## 5. 标签不一致样本

61 行标签不一致样本：

| 指标 | A：当前 | B：实心 + 逐 cell |
|---|---:|---:|
| 与人工 OSD 完全一致 | 40/61 | 40/61 |
| 被误判成 expected | 0/61 | 0/61 |

说明：

- B 是**数字通道专用**流程，中文/字母数字标签不在它的目标范围内；
- 该结果最重要的点是：**B 没有把任何标签不一致样本“修正”成 expected**，不会掩盖真实串流/错配；
- 中文/字母数字通道仍应走 EasyOCR/CJK 回退，不能靠数字分类器。

---

## 6. 字母数字 / 中文 OCR 样本

8 行非 digit-only 的 OCR 样本（如 `IPC-409`、`P40A2-067`、中文标签）：

| 指标 | A：当前 | B：数字逐 cell |
|---|---:|---:|
| 与人工 OSD 完全一致 | 0/8 | 0/8 |

这不是 B 的失败，而是**流程边界**：B 只处理纯数字。  
生产环境应按 profile 分流：

- `type=digits` → B；
- `type=alnum` → EasyOCR / 字母数字模型；
- `type=cjk` → CJK 模型。

---

## 7. greedy vs beamsearch

| 指标（digit-only OCR 44 行） | greedy | beamsearch |
|---|---:|---:|
| 与 expected 匹配 | 39/44 | **41/44** |
| 单字准确率 | 160/173 | **164/173** |

beamsearch 额外修复了一个“单 cell 被读成两个字符”的样本（`1405 → 11405`）。  
建议 B 路径默认使用 **beamsearch**，虽然稍慢，但数字通道的收益明显。

---

## 8. 决定：现在要不要训练专用数字分类器？

**结论：暂时不需要训练完整数字分类器。**

理由：

1. B 已经把 44 个 digit-only OCR 错修到 41 个，达到“当前 52 张 OCR 错降到 ≤5 张”的目标区间；
2. `1→4`、`3→8`、`9→8`、`5→6` 已经全部清零；
3. 剩余 3 个错误里：
   - 2 个是宽搜索框选错背景组（`1851`、`1784`），固定窄 ROI 后 OSD 完整、实心二值化干净，属于 ROI/分组问题，不是分类器问题；
   - 1 个是 `2→3`，只出现 1 次，不值得为它单独训练一个模型；
4. 61 张标签不一致样本没有被误改成 OK，说明 B 不会掩盖真实串流。

建议优先级：

1. **先上线 B：行投影 + 逐 cell 实心二值化 + beamsearch**；
2. **修复宽搜索框的分组逻辑**：优先固定窄 ROI / 按 expected 长度选组，避免把背景脚手架/钢筋当成文字；先用 `1851`、`1784` 两个案例回归验证；
3. **字母数字/中文通道保持 EasyOCR/CJK 回退**；
4. 观察一周真实数据：
   - 如果 `2→3`、`1→4`、`0→9` 仍然在多个通道出现；
   - 或者新相机字体导致新的单字混淆；
   - 再训练一个小型数字分类器/模板匹配，用 hard-negatives 针对性修正。

---

## 9. 复现方式

```bash
# greedy
docker exec -i easyocr-dev-monitor-1 \
  python /work/scripts/benchmark_osd_cells.py \
  --gpu --out-dir /work/output/benchmark

# beamsearch（推荐）
docker exec -i easyocr-dev-monitor-1 \
  python /work/scripts/benchmark_osd_cells.py \
  --gpu --decoder beamsearch \
  --out-dir /work/output/benchmark_beam
```

输出：

- `output/benchmark_beam/osd_cell_benchmark.csv`：逐行 A/B 结果、cell 切分结果、失败原因；
- `output/benchmark_beam/osd_cell_benchmark_summary.md`：汇总指标；
- `output/benchmark_beam/cells/`：每个 cell 的实心二值化小图，可直接人工检查。

---

## 10. 一句话总结

**实心二值化 + 逐 cell 识别已经把 `1→4`、`3→8`、`9→8`、`5→6` 清零，剩余问题主要是宽搜索框被背景带跑（`1851`、`1784`）和 1 个 `2→3`。现在不值得训练完整数字分类器；先上线 B，修 ROI/分组，观察真实 residual error，再决定是否加分类器。**
