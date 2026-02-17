# dsmeta-signature: Multi-GSE Meta Disease Signature Pipeline

> 从多个 GEO 数据集构建**鲁棒的疾病基因签名**，直接对接 SigReverse 做药物反向筛选。

---

## 整体定位

```
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│  dsmeta_signature    │     │     SigReverse        │     │     KG_Explain        │
│  (本项目)            │     │                       │     │                       │
│                      │     │  disease_signature    │     │  Drug → Target →      │
│  GEO 原始数据        │     │  → LINCS L1000 查询   │     │  Pathway → Disease    │
│  → 差异表达          │     │  → CMap 反向评分      │     │  (机制链路打分)       │
│  → Meta 分析         │     │  → 药物排序           │     │                       │
│  → 疾病签名 JSON  ───┼────→│                       │     │                       │
│                      │     │  (可选) 融合 KG 分数 ←┼─────┤                       │
└──────────────────────┘     └──────────────────────┘     └──────────────────────┘
         造签名                      找反转药物                   找机制证据
       (自下而上)                  (表达谱匹配)                 (自上而下)

                         ┌──────────────────────┐
                         │  LLM+RAG 证据工程     │
                         │                      │
                         │  PubMed 文献挖掘      │
                         │  → LLM 证据提取      │
                         │  → 假设卡 + 评分      │
                         │  → Release Gate 拦截  │
                         └──────────────────────┘
                               文献证据补充
```

四个项目**互补**：dsmeta 造签名 → SigReverse 找药 → KG_Explain 解释机制 → LLM+RAG 补充文献证据。

> **2026-02-12**: 下游 KG_Explain 新增 Bootstrap CI 不确定性量化、数据泄漏审计；LLM+RAG 新增 Schema 强制执行 + Release Gate。dsmeta 产出的签名质量直接影响全链路可信度。

---

## 数据流（10 步）

```
configs/athero_example.yaml
  ├── GSE ID 列表 (GSE28829, GSE43292)
  └── case/control 标注规则

    ▼ ─── Step 1: fetch_geo (R) ───────────────────────────────
    │  从 GEO/NCBI 下载表达矩阵 + 样本元数据
    │  • 自动选择最大平台（多平台 GSE 取样本最多的）
    │  • 本地缓存：已下载的 GSE 不会重复下载
    ▼
  work/geo/<GSE>/
  ├── expr.tsv       表达矩阵 (探针 × 样本)
  └── pheno.tsv      样本元数据 (含 platform_id)

    ▼ ─── Step 2: de_limma (R) ────────────────────────────────
    │  每个 GSE 独立做差异表达分析
    │  • regex 自动标注 case/control（或手动 GSM 列表）
    │  • PCA outlier 检测 + 去除
    │  • limma: design → lmFit → eBayes → topTable
    │  • 输出: logFC, t-statistic, p-value, SE
    ▼
  work/de/<GSE>/
  ├── de.tsv              差异表达结果 (探针级)
  └── pheno_labeled.tsv   标注后的样本表

    ▼ ─── Step 3: probe_to_gene (Python) ★ 关键步骤 ──────────
    │  跨平台探针 ID → HGNC 基因符号映射
    │
    │  为什么需要？
    │    GSE28829 用 GPL570 (探针: 1007_s_at)
    │    GSE43292 用 GPL6244 (探针: 7892501)
    │    不同平台的探针 ID 完全不同 → 不映射就零重叠
    │
    │  做了什么？
    │    1. 从 GEO FTP 自动下载 GPL 注释文件 (首次下载，后续缓存)
    │    2. 解析 probe_id → Gene Symbol 映射
    │    3. 多探针→同基因: DE 保留 max|t|, 表达保留 max variance
    │    4. 原始文件自动备份 (*.probe_backup)
    │
    │  效果: 基因重叠率 0% → 74.6% (17,487 共享基因)
    ▼
  work/de/<GSE>/de.tsv          feature_id 变为基因符号
  work/geo/<GSE>/expr.tsv       feature_id 变为基因符号
  data/cache/gpl_annotations/   GPL 注释文件缓存

    ▼ ─── Step 4: meta_effects (R) ────────────────────────────
    │  跨数据集 Meta 分析
    │  • 对每个基因: metafor::rma.uni (DL 随机效应模型)
    │  • 方向一致性过滤 (sign_concordance ≥ 0.7)
    │  • 计算: meta_logFC, meta_z, meta_p, tau2, I2
    │  • FDR 校正 (Benjamini-Hochberg)
    ▼
  outputs/signature/
  └── gene_meta.tsv     每基因一行，含所有 meta 统计量

    ▼ ─── Step 5: rank_aggregate (Python + R) ─────────────────
    │  秩聚合 (RobustRankAggreg)
    │  • 每个 GSE 独立排名 → 构建 rank_matrix
    │  • RRA 聚合 → 鲁棒排名
    │  • Ensemble: 0.7 × meta_rank + 0.3 × rra_rank
    ▼
  outputs/signature/
  ├── rank_aggregation/
  │   ├── rank_matrix.tsv    基因 × GSE 排名矩阵
  │   └── rra.tsv            RRA 聚合分数
  └── gene_meta_ensemble.tsv 增强版 meta 表 (含 ensemble_rank)

    ▼ ─── Step 6: fetch_genesets (Python) ─────────────────────
    │  下载通路基因集
    ▼
  work/genesets/
  ├── reactome.gmt         Reactome 通路
  └── wikipathways.gmt     WikiPathways 通路

    ▼ ─── Step 7: gsea_fgsea (R) ──────────────────────────────
    │  每个 GSE × 每个基因集库 运行 fgsea
    │  • t-stat 排名 → permutation → NES + p-value
    ▼
  work/gsea/
  ├── GSE28829__reactome.tsv
  ├── GSE28829__wikipathways.tsv
  ├── GSE43292__reactome.tsv
  └── GSE43292__wikipathways.tsv

    ▼ ─── Step 8: pathway_meta (Python) ───────────────────────
    │  通路层跨数据集合并
    │  • 方向一致性过滤 (concordance ≥ 0.7)
    │  • Stouffer's Z 合并 (signed)
    │  • BH FDR 校正
    ▼
  outputs/pathways/
  ├── reactome_meta.tsv       通路层 meta 结果
  └── wikipathways_meta.tsv

    ▼ ─── Step 9: make_signature_json (Python) ────────────────
    │  生成最终疾病签名
    │  • 优先用 ensemble 表，空则回退到 gene_meta
    │  • sign_concordance 过滤
    │  • 取 top-N up + top-N down (默认各 300)
    │  • 方向自动验证 (up 的 mean logFC > down 的)
    ▼
  outputs/signature/
  ├── up_genes.txt                  300 个上调基因
  ├── down_genes.txt                300 个下调基因
  ├── disease_signature_meta.json   带权重 + QC + 溯源
  └── sigreverse_input.json ──────→ 直接喂给 SigReverse ✓
      {
        "name": "atherosclerosis_meta_signature",
        "up":   ["C2", "CYTH4", "WDFY4", ...],
        "down": ["ROCK2", "SERAC1", "DNAJB4", ...],
        "meta": {"source": "dsmeta_signature_pipeline"}
      }

    ▼ ─── Step 10: make_report (Python) ───────────────────────
    │  QC 汇总报告
    ▼
  outputs/reports/
  └── qc_summary.html
      (基因数、FDR 分布、sign concordance、I2、Top 通路)
```

---

## 你需要提供什么

只需要两件事，其余全部自动化：

1. **GSE ID 列表** — 你想用哪些 GEO 数据集
2. **Case/Control 标注** — regex 规则或显式 GSM 列表

### 手动配置

```yaml
# configs/athero_example.yaml (核心部分)
geo:
  gse_list:
    - "GSE28829"
    - "GSE43292"

labeling:
  mode: "regex"
  regex_rules:
    GSE28829:
      case:    { any: ["advanced atherosclerotic"] }
      control: { any: ["early atherosclerotic"] }
    GSE43292:
      case:    { any: ["tissue: atheroma plaque"] }
      control: { any: ["tissue: macroscopically intact"] }
```

### 自动配置（推荐，2026-02-16 新增）

使用 `auto_discover_geo.py` 自动搜索 GEO、检测 case/control、生成候选配置：

```bash
# 1. 自动搜索（~1分钟/疾病，纯规则无 LLM）
cd .. && python ops/auto_discover_geo.py --disease "heart failure" --write-yaml --out-dir ops/geo_curation

# 2. AI 辅助审核（把 discovery_log.txt 喂给 LLM）
cat ops/geo_curation/heart_failure/discovery_log.txt
# → 复制给 Claude/ChatGPT，问："请审核这些 heart failure 的 GSE 选择"
# AI 检查：数据类型(mRNA?)、疾病匹配、组织/细胞类型、regex正确性、重复、研究设计

# 3. 应用审核 — 编辑生成的 YAML 移除不合格 GSE
#    修改 configs/<disease>.yaml 的 geo.gse_list 和 labeling.regex_rules
#    如果审核后 GSE < 2 → 从 disease_list_day1_dual.txt 移除

# 4. 生成正式 dsmeta 配置
python ops/generate_dsmeta_configs.py \
    --geo-dir ops/geo_curation \
    --config-dir dsmeta_signature_pipeline/configs

# 5. 验证（运行 Step1-2 检查样本数）
bash ops/precheck_dual_dsmeta.sh
```

**AI 审核检查清单（喂给 LLM 的 6 项检查）：**

| 检查项 | 不合格 → 移除 | 实际案例 |
|--------|--------------|---------|
| 数据类型 | 非 mRNA 表达谱（16S rRNA, circRNA, tRNA-derived sRNA） | coronary: GSE242047 是 16S rRNA |
| 疾病匹配 | GSE 研究的是其他疾病/完全不同的病种 | hypertension: 3/4 个 GSE 是 PAH |
| 组织/细胞 | 细胞系、成纤维细胞、非靶器官 | cardiomyopathy: GSE133754 是成纤维细胞 |
| Case/Control | regex 匹配了错误的分组 | 比较两个亚型而非 disease vs healthy |
| 重复 | 同一数据集出现两次 | MI: '48060' = GSE48060 |
| 研究设计 | 非 case-control 转录组（药物处理、方法学等） | MI: GSE220865 是效力测试 |

**自动发现的工作原理：**
- 调用 NCBI E-utilities 搜索 GEO（esearch → esummary）
- 硬编码过滤：Human only, expression profiling, ≥6 samples
- 排除：cell line, animal model, miRNA, methylation, single-cell
- 正则匹配 case/control（在 title/source/characteristics 字段上）
- 打分：样本数(30) + 平衡性(20) + 平台质量(20) + 分类置信度(20) + 时效(10)
- 选 top-5 并生成 `candidate_config.yaml`
- 自动输出路线推荐（`route_recommendation.txt`），告诉你这个疾病应该走哪条路线

**推荐 3-5 个 GSE/疾病**，至少 2 个才能做 meta-analysis。

**GSE 数量不足怎么办？**

| 情况 | 建议 |
|------|------|
| 0 个 GSE | 跳过 dsmeta 管线，该疾病只走 Direction B（CT.gov → KG → LLM） |
| 1 个 GSE | 可以跑但结果为低可信度（无跨实验验证），建议手动补搜或只走 Direction B |
| ≥2 个 GSE | 正常跑，meta-analysis 和 RRA 都能发挥作用 |

> `generate_dsmeta_configs.py --update-disease-list` 只会把 ≥2 GSE 的疾病加入 dual 列表。

---

## 你能得到什么

| 文件 | 说明 |
|------|------|
| `outputs/signature/gene_meta.tsv` | 每基因 meta logFC/z/p/FDR/I2/sign concordance |
| `outputs/signature/gene_meta_ensemble.tsv` | 增强版 (meta + RRA 联合排名) |
| `outputs/signature/up_genes.txt` / `down_genes.txt` | Top-N 上/下调基因列表 |
| `outputs/signature/disease_signature_meta.json` | 详细签名 (含权重、QC、溯源) |
| `outputs/signature/sigreverse_input.json` | **直接喂给 SigReverse** |
| `outputs/pathways/*_meta.tsv` | 通路层 meta 结果 |
| `outputs/reports/qc_summary.html` | QC 汇总报告 |

---

## 安装

```bash
mamba env create -f environment.yml
conda activate dsmeta
```

依赖: Python 3.11 + R 4.3+ + Bioconductor (limma, GEOquery, fgsea) + metafor + RobustRankAggreg

---

## 运行

### 完整流程

```bash
python run.py --config configs/athero_example.yaml
```

### 部分重跑

```bash
# 只跑 step 3-9 (跳过 GEO 下载和 DE)
python run.py --config configs/athero_example.yaml --from-step 3 --to-step 9

# 只重新生成签名 JSON (step 9)
python run.py --config configs/athero_example.yaml --from-step 9

# 预览会跑哪些步骤
python run.py --config configs/athero_example.yaml --dry-run
```

### 单独跑探针映射

```bash
python scripts/02b_probe_to_gene.py --config configs/athero_example.yaml --workdir work
```

---

## 对接 SigReverse

```bash
# dsmeta 产出签名
python run.py --config configs/athero_example.yaml

# SigReverse 消费签名，找反转药物
cd ../sigreverse
python scripts/run.py \
    --config configs/default.yaml \
    --in ../dsmeta_signature_pipeline/outputs/signature/sigreverse_input.json \
    --out data/output_atherosclerosis/
```

---

## 配置参考

<details>
<summary>完整配置项说明 (展开)</summary>

```yaml
project:
  name: "my_disease"       # 项目名称
  outdir: "outputs"        # 输出目录
  workdir: "work"          # 中间文件目录
  seed: 13                 # 随机种子 (fgsea permutation 等)

geo:
  gse_list: ["GSE12345"]   # GEO Series ID 列表
  prefer_series_matrix: true

labeling:
  mode: "regex"            # "regex" 或 "explicit"
  regex_rules:             # mode=regex 时使用
    GSE12345:
      case:    { any: ["disease"] }
      control: { any: ["control"] }
  explicit:                # mode=explicit 时使用
    GSE12345:
      case_gsm: ["GSM001", "GSM002"]
      control_gsm: ["GSM003", "GSM004"]

de:
  method: "limma"
  covariates: []           # 可选: ["age", "sex"]
  qc:
    remove_outliers: true
    pca_outlier_z: 3.5

probe_to_gene:             # ★ 跨平台关键步骤
  enable: true             # 默认开启
  skip_if_gene_symbols: true  # 如果 ID 已经是基因符号则跳过

meta:
  model: "random"          # "random" (推荐) 或 "fixed"
  min_sign_concordance: 0.7
  flag_i2_above: 0.6
  top_n: 300               # 每方向取多少基因

rank_aggregation:
  enable: true
  method: "rra"            # "rra" 或 "mean"
  ensemble:
    enable: true
    w_meta: 0.7
    w_rra: 0.3

genesets:
  enable_reactome: true
  enable_wikipathways: true
  enable_kegg: false

gsea:
  method: "fgsea"
  min_size: 15
  max_size: 500
  nperm: 10000

pathway_meta:
  method: "stouffer"
  min_concordance: 0.7

report:
  enable: true
```

</details>

---

## 项目结构

```
dsmeta_signature_pipeline/
├── run.py                          主编排器 (10 步)
├── configs/
│   ├── template.yaml               配置模板
│   ├── atherosclerosis.yaml        动脉粥样硬化 (人工审核)
│   └── athero_example.yaml         示例配置
│   └── <disease>.yaml              (由 auto_discover_geo + generate_dsmeta_configs 自动生成)
├── scripts/
│   ├── 01_fetch_geo.R              GEO 数据下载
│   ├── 02_de_limma.R               limma 差异表达
│   ├── 02b_probe_to_gene.py        ★ 探针→基因映射 (新增)
│   ├── 03_meta_effects.R           跨数据集 Meta 分析
│   ├── 04_rank_aggregate.py        秩聚合 (Python 部分)
│   ├── 04b_rra.R                   RobustRankAggreg (R 部分)
│   ├── 05_fetch_genesets.py        基因集下载
│   ├── 06_gsea_fgsea.R             fgsea GSEA
│   ├── 07_pathway_meta.py          通路层 Meta 分析
│   ├── 08_make_signature_json.py   生成签名 JSON
│   └── 09_make_report.py           QC 报告
├── tests/
│   ├── test_pathway_meta.py        Stouffer/BH-FDR 测试
│   ├── test_rank_aggregate.py      秩聚合测试
│   └── test_probe_to_gene.py       探针映射测试 (新增)
├── data/cache/                     GPL 注释缓存 (自动)
├── work/                           中间文件
├── outputs/                        最终输出
└── environment.yml                 conda 环境
```

---

## 常见问题

**Q: meta 分析结果全是空的 (gene_meta.tsv 没有 logFC)**
A: 大概率是跨平台探针 ID 不重叠。确保 `probe_to_gene.enable: true`（默认已开启）。运行 step 3 后会打印重叠率，应该 > 50%。

**Q: 如何查看两个 GSE 用了什么平台？**
A: 查看 `work/geo/<GSE>/pheno.tsv` 的 `platform_id` 列，或直接在 GEO 网站上看。

**Q: 可以用 RNA-seq 数据吗？**
A: 可以，但 limma 需要 log-normalized 的表达矩阵。如果是 counts，建议先用 DESeq2/edgeR 做 DE，再从 step 3 开始跑。RNA-seq 的 feature_id 通常已是基因符号，`probe_to_gene` 会自动检测并跳过映射。

**Q: 只有一个 GSE 能跑吗？**
A: 能跑。meta 分析会退化为单数据集统计，但没有跨数据集验证。sign_concordance 和 I2 指标不可用。建议至少 2 个 GSE。

**Q: 怎么快速找到适合的 GSE？**
A: 用 `auto_discover_geo.py`。它会自动搜索 NCBI GEO、过滤不适合的数据集、检测 case/control、打分排名，输出候选配置。每个疾病约 1 分钟。详见上方"自动配置"章节。

**Q: auto_discover_geo 生成的配置可以直接用吗？**
A: 高置信度（confidence=high）的通常可以。但建议至少看一眼 `discovery_log.txt` 确认 GSE 选择合理、case/control regex 正确。低置信度（含 TODO 占位符）的必须手动审核。

**Q: sigreverse_input.json 的基因数量多少合适？**
A: 推荐每方向 100-300 个。通过 `meta.top_n` 配置。太少统计功效不足，太多信噪比下降。

---

## 免责声明
- 结果质量强依赖 case/control 标注准确性和数据集质量。
- 跨平台探针映射使用 GEO 官方 GPL 注释，覆盖率约 65-85%，部分探针无法映射。
- 通路分析结果仅供筛选参考，不等于生物学验证。

---

## 下游质量保障链 (2026-02-12)

dsmeta 签名是整条管道的起点，签名质量直接影响下游所有结果。目前下游已建立完整质量保障:

```
dsmeta 签名 → SigReverse 反向评分 → KG_Explain (含 Bootstrap CI + 泄漏审计)
                                   → LLM+RAG (含 Schema 强制 + Release Gate)
                                   → 跨项目集成测试 (接口兼容性验证)
```

| 下游模块 | 项目 | 与 dsmeta 的关系 |
|----------|------|-----------------|
| Bootstrap CI | kg_explain | KG 排名附带置信区间，签名噪声大 → CI 宽 → confidence_tier=LOW |
| 数据泄漏审计 | kg_explain | 确保评估集无训练泄漏 |
| Schema 强制执行 | LLM+RAG | Step6→Step9 全链路 schema 自动校验 |
| Release Gate | LLM+RAG | shortlist 自动拦截 NO-GO 药物，防止低质量候选进入验证 |
| 跨项目集成测试 | 根目录 | 12 个测试验证 kg_explain ↔ LLM+RAG 接口兼容性 |
