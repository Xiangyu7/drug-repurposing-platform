# SigReverse — 疾病 Signature ↔ LINCS/CMap 方向性反向评分引擎（独立项目）

> 目标：输入「疾病 up/down 基因集」，输出「药物是否反向抵消疾病」的**定量排序**（含鲁棒性降权）。
> 设计原则：输入输出固定、可复现、可缓存、尽量自动化。

## 1) 你能得到什么
运行一次会产出 3 个文件（默认在 `data/output/`）：
- `drug_reversal_rank.csv`：**药你层排序**（final_reversal_score 越负越好）
- `signature_level_details.csv`：**签名层明细**（cell/dose/time + z-up/z-down + 是否 reverser）
- `run_manifest.json`：本次运行参数、缺失基因统计、时间戳、数据源信息（可复现）

## 2) 安装
推荐新建虚拟环境后安装依赖：

```bash
pip install -r requirements.txt
```

（可选）如果你想输出 parquet：再装 `pyarrow`。

## 3) 准备输入
把疾病签名写到：`data/input/disease_signature.json`

示例：
```json
{
  "name": "atherosclerosis",
  "up": ["IL1B", "TNF", "CCL2", "VCAM1"],
  "down": ["KLF2", "NOS3", "ABCA1", "PPARGC1A"],
  "meta": {
    "source": "GEO/CREEDS/DE",
    "note": "case/control definition here"
  }
}
```

## 4) 一键运行
```bash
python scripts/run.py --config configs/default.yaml --in data/input/disease_signature.json --out data/output/
```

## 5) 输出字段解释（drug_reversal_rank.csv）
- `final_reversal_score`：最终反向分数（越负越“反向抵消疾病”）
- `p_reverser`：同一药在多 context 下“反向”的比例
- `n_signatures`：该药命中的签名数量（context 数）
- `median_strength(reverser_only)`：仅在 reverser 子集中计算的强度中位数
- `iqr_strength(reverser_only)`：强度波动（越大越不稳定）
- `possible_toxicity_confounder`：可能的“毒性/应激”假阳性提示（启发式）

## 6) 和你 KG 项目的对接（最干净）
KG 项目只需要把 `drug_reversal_rank.csv` 按药名（建议统一为 drug_normalized）做一次 left join，
再用 `final_reversal_score` 做主排序锚点，KG/证据工程做解释与排雷。

---

# 免责声明
- 这是一个“方向性+鲁棒性”的定量筛选引擎，不等于临床有效性结论。
- 结果强依赖疾病 signature 质量（case/control 定义必须靠谱）。
