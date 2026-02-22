# SigReverse: 疾病 Signature ↔ LINCS/CMap 方向性反向评分引擎

> 输入「疾病 up/down 基因集」，输出「药物是否反向抵消疾病」的**定量排序**（含鲁棒性降权 + 可选融合 KG 机制分）。

---

## 整体定位

```
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│  dsmeta_signature    │     │    SigReverse         │     │     KG_Explain        │
│                      │     │    (本项目)           │     │                       │
│  GEO 原始数据        │     │                       │     │  CT.gov 失败试验     │
│  → 差异表达          │     │  disease_signature    │     │  → Drug-Target       │
│  → Meta 分析         │     │  → LINCS L1000 查询   │     │  → Target-Pathway    │
│  → 疾病签名 JSON  ───┼────→│  → CMap 4 阶段评分    │     │  → Pathway-Disease   │
│                      │     │  → 药物层鲁棒性聚合   │     │  → 安全 + 表型打分   │
│                      │     │                       │     │                       │
│                      │     │  (可选) 融合 KG 分数 ←┼─────┤                       │
└──────────────────────┘     └──────────────────────┘     └──────────────────────┘
       造签名                       找反转药物                    找机制证据
     (自下而上)                   (表达谱匹配)                  (自上而下)

                         ┌──────────────────────┐
                         │  LLM+RAG 证据工程     │
                         │                      │
                         │  PubMed 文献挖掘      │
                         │  → LLM 证据提取      │
                         │  → 假设卡 + 评分      │
                         └──────────────────────┘
                               文献证据补充
```

四个项目**互补**：dsmeta 造签名 → SigReverse 找药 → KG_Explain 解释机制 → LLM+RAG 补充文献证据。

> **2026-02-12**: 下游 KG_Explain 和 LLM+RAG 已新增不确定性量化 (Bootstrap CI)、数据泄漏审计、Schema 强制执行、Release Gate 等质量保障模块。融合时 KG 排名输出现包含 `ci_lower / ci_upper / confidence_tier` 列，可用于加权融合置信度。

---

## 数据流（13 步）

```
输入: disease_signature.json (up/down 基因集)
  ├── 来源 1: CREEDS 公共数据库 (828+ 疾病签名, 一键下载)
  ├── 来源 2: dsmeta_signature_pipeline (sigreverse_input.json)
  └── 来源 3: 手动文献整理

    ▼ ─── Step 1: 加载签名 ──────────────────────────────────
    │  读取 JSON → 验证 up/down 基因列表
    │  • 签名大小检查 (推荐 100-300 / 方向)
    │  • 空列表 / 重复基因警告
    ▼

    ▼ ─── Step 2: LINCS L1000 查询 ──────────────────────────
    │  LDP3 API (LINCS Data Portal 3)
    │  • 解析基因符号 → LINCS entity ID
    │  • 缺失基因记录 (missing_up / missing_down)
    │  • 缓存: data/cache/lincs/ (FileCache, TTL=168h)
    ▼
  [内存: gene_id_map, missing genes]

    ▼ ─── Step 3: 签名级 enrichment ─────────────────────────
    │  对每个 LINCS signature 做 enrichment:
    │  • z-up: up 基因在该签名中的富集分
    │  • z-down: down 基因在该签名中的富集分
    │  • 每条签名 = 某药 × 某细胞系 × 某浓度 × 某时间
    ▼

    ▼ ─── Step 4-7: CMap 4 阶段评分 ─────────────────────────
    │
    │  Stage 1 — ES (Enrichment Score):
    │    对 up/down 分别计算类 KS 富集分
    │
    │  Stage 2 — WTCS (Weighted Tau Connectivity Score):
    │    WTCS = (ES_up − ES_down) / 2
    │    符号门控: 仅保留 "up 被压低 & down 被升高" 的签名
    │    方向不一致 → WTCS = 0 (核心反向假设)
    │
    │  Stage 3 — NCS (Normalized Connectivity Score):
    │    按参考分布归一化 (分正/负分别归一)
    │
    │  Stage 4 — Tau (百分位):
    │    在全库中百分位化 → Tau ∈ [-100, 100]
    │    Tau < -90: 强反向, 高优先级
    ▼

    ▼ ─── Step 8: 剂量-响应分析 (可选) ──────────────────────
    │  同药同细胞系: 按浓度排序
    │  • 单调性检验 (Spearman ρ)
    │  • 非单调药物标记 (可能是噪声)
    ▼

    ▼ ─── Step 9: 药物名标准化 ──────────────────────────────
    │  LINCS pert_name → 标准化药物名
    │  • PubChem CID → InChIKey (化学通用标识符)
    │  • UniChem 跨库引用 (ChEMBL, DrugBank)
    │  • BRD-ID / 别名 / 品牌名去重
    ▼

    ▼ ─── Step 10: 鲁棒性聚合 ───────────────────────────────
    │  同一药的多 context (细胞系 × 浓度 × 时间) 聚合:
    │  • p_reverser: 跨 context "反向" 的一致性比例
    │  • 中位强度 + IQR 波动
    │  • 鲁棒性降权: 一致性越低 → 最终分越弱
    │
    │  final_reversal_score = median_score × p_reverser × coverage_weight
    ▼

    ▼ ─── Step 11: 统计检验 + FDR ───────────────────────────
    │  • 每药 bootstrap p-value
    │  • Benjamini-Hochberg FDR 校正
    │  • FDR < 0.05 → 显著反向
    ▼

    ▼ ─── Step 12: QC + 毒性检测 ────────────────────────────
    │  • 签名级 QC: FDR pass rate, 方向一致性
    │  • 毒性/应激假阳性检测:
    │    n_sigs ≥ 10 & p_reverser ≥ 0.80 & median_strength ≥ 25
    │    → 标记 possible_toxicity_confounder
    ▼

    ▼ ─── Step 13: 融合 (可选) ──────────────────────────────
    │  加权融合 3 路分数:
    │  • 50% SigReverse 反向分
    │  • 30% KG_Explain 机制分 (drug_disease_rank.csv)
    │  • 20% FAERS 安全分 (edge_drug_ae_faers.csv)
    │
    │  药物名匹配策略 (跨项目):
    │    1. 精确匹配 (大小写不敏感)
    │    2. 组合药拆分 ("aspirin+ticagrelor" → 分别匹配)
    │    3. 盐类/剂型去除 ("fasudil hydrochloride" → "fasudil")
    │    4. 品牌→通用名 ("jardiance" → "empagliflozin")
    ▼
  data/output/
  ├── drug_reversal_rank.csv       ★ 药物排序 (final_reversal_score)
  ├── signature_level_details.csv  签名层明细 (每条 context)
  └── run_manifest.json            运行元数据 (参数 + 缺失基因 + 时间戳)
```

---

## 你需要提供什么

**疾病基因签名** — 以下任一方式:

### 方式一: CREEDS 公共数据库 (推荐)

```bash
# 列出所有可用疾病
python scripts/fetch_disease_signature.py --list

# 搜索 + 自动合并多个 GEO 数据集 (推荐)
python scripts/fetch_disease_signature.py --disease atherosclerosis \
    --merge --auto --top-n 200
```

### 方式二: dsmeta_signature_pipeline (自定义 meta-analysis)

```bash
cd ../dsmeta_signature_pipeline
python run.py --config configs/athero_example.yaml
# → outputs/signature/sigreverse_input.json
```

### 方式三: 手动编写 JSON

```json
{
  "name": "atherosclerosis",
  "up": ["IL1B", "TNF", "CCL2", "MMP9", "VCAM1", "ICAM1"],
  "down": ["KLF2", "NOS3", "ABCA1", "PPARGC1A"],
  "meta": {"source": "manual_literature", "pmids": ["12345678"]}
}
```

---

## 你能得到什么

| 文件 | 说明 |
|------|------|
| `drug_reversal_rank.csv` | ★ 药物排序 (final_reversal_score 越负越好) |
| `signature_level_details.csv` | 签名层明细 (cell/dose/time + z-up/z-down + reverser) |
| `run_manifest.json` | 运行元数据 (参数 + 缺失基因统计 + 数据源信息) |
| `fusion_rank.csv` | (可选) 融合排序 (SigReverse + KG + FAERS) |

### drug_reversal_rank.csv 字段

| 字段 | 说明 |
|------|------|
| `final_reversal_score` | 最终反向分 (越负越"反向抵消疾病") |
| `p_reverser` | 跨 context "反向" 一致性比例 |
| `n_signatures` | 该药命中的签名数 (context 数) |
| `median_strength(reverser_only)` | 仅 reverser 子集的强度中位数 |
| `iqr_strength(reverser_only)` | 强度波动 (越大越不稳定) |
| `possible_toxicity_confounder` | 毒性/应激假阳性提示 (启发式) |

---

## 安装

```bash
pip install -r requirements.txt
```

(可选) 输出 parquet: `pip install pyarrow`

依赖: Python 3.10+ + pandas + numpy + requests + scipy + pyyaml + tqdm

---

## 运行

### 标准运行

```bash
python scripts/run.py \
    --config configs/default.yaml \
    --in data/input/disease_signature.json \
    --out data/output/
```

### 一键运行 (自动从 CREEDS 获取)

```bash
python scripts/run.py \
    --config configs/default.yaml \
    --fetch atherosclerosis \
    --out data/output_atherosclerosis/
```

`--fetch` 自动执行: CREEDS 搜索 → 合并多 GEO 签名 → 运行完整管道。

### 完整工作流示例 (动脉粥样硬化)

```bash
# Step 1: 获取签名
python scripts/fetch_disease_signature.py \
    --disease atherosclerosis --merge --auto \
    --out data/input/disease_signature_atherosclerosis.json

# Step 2: 运行 SigReverse
python scripts/run.py \
    --config configs/default.yaml \
    --in data/input/disease_signature_atherosclerosis.json \
    --out data/output_atherosclerosis/

# Step 3 (可选): 融合 KG 分数
python scripts/run_fusion_with_kg.py \
    --sigreverse-csv data/output_atherosclerosis/drug_reversal_rank.csv \
    --kg-csv ../kg_explain/output/drug_disease_rank.csv \
    --faers-csv ../kg_explain/data/edge_drug_ae_faers.csv \
    --disease atherosclerosis \
    --out data/output_atherosclerosis/fusion_rank.csv
```

---

## 对接其他项目

### ← dsmeta_signature_pipeline (签名输入)

```bash
# dsmeta 产出签名
cd ../dsmeta_signature_pipeline
python run.py --config configs/athero_example.yaml

# SigReverse 消费签名
cd ../sigreverse
python scripts/run.py \
    --config configs/default.yaml \
    --in ../dsmeta_signature_pipeline/outputs/signature/sigreverse_input.json \
    --out data/output_atherosclerosis/
```

### ← KG_Explain (融合机制分)

```bash
# 融合 KG 机制分 + FAERS 安全分
python scripts/run_fusion_with_kg.py \
    --sigreverse-csv data/output/drug_reversal_rank.csv \
    --kg-csv ../kg_explain/output/drug_disease_rank.csv \
    --faers-csv ../kg_explain/data/edge_drug_ae_faers.csv \
    --disease atherosclerosis \
    --out data/output/fusion_rank.csv
```

---

## CMap 4 阶段评分算法

```
     up 基因集          down 基因集
         │                   │
    ┌────▼────┐         ┌────▼────┐
    │ ES_up   │         │ ES_down │     Stage 1: Enrichment Score
    └────┬────┘         └────┬────┘
         │                   │
    ┌────▼───────────────────▼────┐
    │ WTCS = (ES_up − ES_down)/2  │     Stage 2: 方向一致性门控
    │ 符号门控: 不一致 → WTCS=0    │     (up被压低 & down被升高)
    └────────────┬────────────────┘
                 │
    ┌────────────▼────────────────┐
    │ NCS = WTCS / ref_distribution│    Stage 3: 归一化
    │ 正负分别归一                  │
    └────────────┬────────────────┘
                 │
    ┌────────────▼────────────────┐
    │ Tau = percentile(NCS)        │    Stage 4: 百分位化
    │ ∈ [-100, 100]                │    Tau < -90: 强反向
    └──────────────────────────────┘
```

---

## 融合配置

```yaml
# configs/default.yaml
fusion:
  enabled: true
  weights:
    reversal: 0.50       # SigReverse 反向分权重
    kg_explain: 0.30     # KG 机制分权重
    safety: 0.20         # FAERS 安全分权重
  kg_scores_path: "../kg_explain/output/drug_disease_rank.csv"
  disease_filter: "atherosclerosis"
  safety_scores_path: "../kg_explain/data/edge_drug_ae_faers.csv"
```

融合支持的数据格式:
- **KG_Explain**: 自动检测 `drug`/`drug_normalized` 列, 使用 `final_score`, 同药取 max
- **FAERS**: 支持 `safety_score` 列 或 `report_count` → 自动转 `log(total+1)`

---

## 配置参考

<details>
<summary>完整配置项说明 (展开)</summary>

```yaml
# configs/default.yaml
lincs:
  api_base: "https://ldp3.cloud"
  timeout: 60
  max_retries: 3

signature:
  min_genes_per_direction: 50      # 最少每方向基因数
  optimal_min: 150                  # 推荐最少值

scoring:
  sign_gate: true                   # WTCS 方向门控
  fdr_threshold: 0.05               # FDR 阈值

robustness:
  min_contexts: 3                   # 最少 context 数
  coverage_weight: true             # 覆盖率加权

toxicity_flag:
  enabled: true
  min_signatures: 10                # 最少签名数
  min_p_reverser: 0.80              # reverser 比例阈值
  min_median_strength: 25.0         # 中位强度阈值

drug_standardization:
  enabled: true                     # PubChem + UniChem 标准化
  cache_dir: "data/cache/drugs"

dose_response:
  enabled: true                     # 剂量-响应分析
  monotonicity_threshold: 0.6       # Spearman ρ 阈值

fusion:
  enabled: false                    # 默认关闭
  weights:
    reversal: 0.50
    kg_explain: 0.30
    safety: 0.20

cache:
  dir: "data/cache"
  default_ttl_hours: 168            # 7 天
```

</details>

---

## 项目结构

```
sigreverse/
├── configs/
│   └── default.yaml                   主配置文件
├── scripts/
│   ├── run.py                         ★ 主流程 (13 步, 845 行)
│   ├── fetch_disease_signature.py     CREEDS 签名获取 (467 行)
│   ├── run_fusion_with_kg.py          KG 融合脚本 (430 行)
│   ├── validate_pipeline.py           管道验证
│   ├── validate_multi_disease.py      多疾病批量验证
│   └── test_api.py                    API 连通测试
├── sigreverse/                        核心模块 (7,457 行)
│   ├── __init__.py                    版本号 + 公共接口
│   ├── io.py                          输入输出 (JSON/CSV/parquet)
│   ├── ldp3_client.py                 LDP3 API 客户端 (LINCS 查询)
│   ├── cmap_algorithms.py             ★ CMap 4 阶段评分 (738 行)
│   ├── scoring.py                     签名层评分 + 方向分类
│   ├── dose_response.py               剂量-响应分析 (474 行)
│   ├── drug_standardization.py        药物名标准化 (PubChem/UniChem)
│   ├── robustness.py                  鲁棒性聚合 + 降权 (430 行)
│   ├── statistics.py                  Bootstrap + FDR (447 行)
│   ├── qc.py                          QC + 毒性检测 (270 行)
│   ├── cache.py                       FileCache (TTL + 统计, 298 行)
│   └── fusion.py                      多源融合排序 (489 行)
├── data/
│   ├── input/                         疾病签名 JSON
│   ├── cache/                         CREEDS + LINCS + 药物缓存
│   └── output/                        运行结果
├── tests/                             测试
├── requirements.txt                   依赖
└── README.md
```

---

## 签名获取工具

内置 CREEDS (828+ 疾病签名) 一键获取:

```bash
# 列出所有可用疾病
python scripts/fetch_disease_signature.py --list

# 搜索疾病 (交互模式)
python scripts/fetch_disease_signature.py --disease atherosclerosis

# 自动合并 + 指定基因数 (推荐)
python scripts/fetch_disease_signature.py --disease atherosclerosis \
    --merge --auto --top-n 200

# 指定输出
python scripts/fetch_disease_signature.py --disease "breast cancer" \
    --merge --out data/input/breast_cancer_sig.json
```

合并策略: `frequency × mean_abs_fold_change` — 出现在越多数据集 + 效应值越大的基因排越前，自动去除方向矛盾基因。

---

## 常见问题

**Q: CREEDS 没有我要的疾病怎么办?**
A: 用 `--list` 看所有可用疾病。如果没有，用 dsmeta_signature_pipeline 从 GEO 自行做 meta-analysis，或从文献手动整理 JSON。

**Q: 签名基因数量多少合适?**
A: 推荐每方向 100-300 个基因。太少 (<50) 统计功效不足，太多 (>500) 信噪比下降。`--top-n 200` 是好的默认值。

**Q: SigReverse 和 KG_Explain 必须一起用吗?**
A: 不用。SigReverse 可以独立运行。融合只是可选增强 — 当两套方法都指向同一个药时，置信度更高。

**Q: 为什么有些药的 `possible_toxicity_confounder` 是 True?**
A: 这些药可能通过诱导应激反应来"反向"疾病基因，而非真正的治疗作用。判断标准: n_sigs ≥ 10 且 p_reverser ≥ 0.80 且 median_strength ≥ 25。需结合文献判断。

**Q: LINCS API 连接超时?**
A: 运行 `python scripts/test_api.py` 检查 API 连通性。LINCS L1000 API 偶尔不稳定，可调大 `timeout` 和 `max_retries`。缓存命中后不再需要网络。

**Q: 融合时药物名匹配不上怎么办?**
A: 融合脚本有 4 级匹配策略 (精确 → 组合药拆分 → 盐类去除 → 品牌→通用名)。如仍无法匹配，手动在 `run_fusion_with_kg.py` 的 `BRAND_TO_GENERIC` 字典中添加映射。

---

## 免责声明
- 这是一个"方向性 + 鲁棒性"的定量筛选引擎，不等于临床有效性结论。
- 结果强依赖疾病 signature 质量（case/control 定义必须靠谱）。
- CREEDS 数据来源于社区众包标注的 GEO 数据集，质量参差不齐，建议合并多个数据集（`--merge`）以提升鲁棒性。
- CMap/LINCS 数据库覆盖有限 (~20,000 化合物)，未收录的药物无法评估。
- 毒性/应激假阳性检测为启发式，需结合文献和实验验证。

---

## 下游质量保障集成 (2026-02-12)

SigReverse 的融合输出可对接下游 KG_Explain + LLM+RAG 的完整质量保障链:

| 下游模块 | 项目 | 作用 |
|----------|------|------|
| Bootstrap CI 不确定性量化 | kg_explain | KG 排名附带 95% 置信区间，融合时可按 `confidence_tier` 加权 |
| 数据泄漏审计 | kg_explain | 确保 KG 评估无 train/test 泄漏 |
| Schema 强制执行 | LLM+RAG | Step6→Step9 全链路 schema 自动校验 |
| Release Gate | LLM+RAG | Step8 shortlist 自动拦截 NO-GO 药物 |
| 跨项目集成测试 | 根目录 | 验证 kg_explain ↔ LLM+RAG 接口兼容性 |

融合 KG 分数时，`drug_disease_rank.csv` 新增字段:

| 字段 | 说明 |
|------|------|
| `ci_lower` | Bootstrap 95% CI 下界 |
| `ci_upper` | Bootstrap 95% CI 上界 |
| `ci_width` | CI 宽度 (越小越确定) |
| `confidence_tier` | HIGH (<0.10) / MEDIUM (<0.25) / LOW (≥0.25) |
| `n_evidence_paths` | 支撑该排名的证据路径数 |
