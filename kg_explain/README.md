# KG_Explain: 可解释知识图谱药物重定位系统

> 构建 **Drug → Target → Pathway → Disease** 多跳可解释路径，输出**机制级证据包**。
> 支持两种药物来源模式: **CT.gov 失败试验** (经典) 和 **基因签名反查** (跨疾病 repurposing)。

---

## 整体定位

```
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│  dsmeta_signature    │     │     SigReverse        │     │   KG_Explain         │
│                      │     │                       │     │   (本项目)           │
│  GEO 原始数据        │     │  disease_signature    │     │                      │
│  → 差异表达          │     │  → LINCS L1000 查询   │     │  药物来源 (二选一):  │
│  → Meta 分析         │     │  → CMap 反向评分      │     │  A) CT.gov 失败试验  │
│  → 疾病签名 JSON  ───┼────→│  → 药物排序           │     │  B) 基因签名反查 ────┤
│                      │  │  │                       │     │                      │
│                      │  │  │  (可选) 融合 KG 分数 ←┼─────┤  → Drug-Target       │
│                      │  │  └──────────────────────┘     │  → Target-Pathway    │
│                      │  │                                │  → Pathway-Disease   │
│                      │  └───────── disease_signature ───→│  → 安全 + 表型打分   │
└──────────────────────┘    meta.json (Signature 模式)     └──────────────────────┘
       造签名                     找反转药物                      找机制证据
     (自下而上)                 (表达谱匹配)                    (自上而下)

                         ┌──────────────────────┐
                         │  LLM+RAG 证据工程     │
                         │                      │
                         │  PubMed 文献挖掘      │
                         │  → LLM 证据提取      │
                         │  → 假设卡 + 评分      │
                         │  ← bridge_repurpose_   │
                         │    rag.csv (来自本项目)│
                         └──────────────────────┘
                               文献证据补充
```

四个项目**互补**：dsmeta 造签名 → SigReverse 找药 → KG_Explain 解释机制 → LLM+RAG 补充文献证据。

---

## 两种药物来源模式

### 模式 A: CT.gov 失败试验 (经典模式, `--drug-source ctgov`)

从 ClinicalTrials.gov 搜索目标疾病的失败/终止/撤回试验 → 提取药物 → 查靶点 → 找通路 → 关联疾病。

**适合**: 已知疾病领域内的药物重定位 (如"心血管药物治其他心血管病")。

**局限**: 药物来源被锁定在目标疾病领域，难以发现真正的跨疾病 repurposing。

### 模式 B: 基因签名反查 (Signature 模式, `--drug-source signature`) 🆕

从 dsmeta 输出的疾病基因签名 (up/down-regulated genes) 出发 → 反查 ChEMBL 中作用于这些基因的药物 (Phase II+) → 药物来自多个不同疾病领域 → 真正的跨疾病 drug repurposing。

**适合**: 发现来自其他疾病领域的药物，通过共享靶点/通路作用于目标疾病。

**优势**:
- 药物来自多个治疗领域 (自免、肿瘤、血液、眼科等)
- 自动标记已知适应症 (`is_known_indication`)
- 从机制层面解释"为什么这个药可能对目标疾病有效"

---

## 数据流

### 模式 A: CT.gov 失败试验模式 (10 步 + 排序)

```
configs/
  ├── base.yaml                      基础配置 (API 端点、文件名)
  ├── diseases/atherosclerosis.yaml  疾病配置 (CT.gov 条件、ICD-10)
  └── versions/v5.yaml              V5 参数 (安全权重、表型加成)

    ▼ ─── Step 1: fetch ctgov ────────────────────────────────
    │  从 ClinicalTrials.gov 搜索失败/终止/撤回的临床试验
    │  • 状态: TERMINATED, WITHDRAWN, SUSPENDED
    │  • 提取: 药物名称 + 试验状态 + 停止原因
    │  • 干预类型过滤: DRUG + BIOLOGICAL (排除 DEVICE 等)
    ▼
  data/failed_trials_drug_rows.csv

    ▼ ─── Step 2: fetch rxnorm ───────────────────────────────
    │  RxNorm 药物名称标准化 (RxNav REST API)
    ▼
  data/drug_rxnorm_map.csv

    ▼ ─── Step 3: build canonical ────────────────────────────
    │  构建标准化药物名 (合并别名)
    ▼
  data/drug_canonical.csv

    ▼ ─── Step 4: fetch chembl ───────────────────────────────
    │  ChEMBL 药物 ID 映射
    │  • 先精确匹配 → 后模糊搜索 → 盐类→母体分子
    ▼
  data/drug_chembl_map.csv

    ▼ ─── Step 5: fetch targets ──────────────────────────────
    │  ChEMBL 药物-靶点关系 (Mechanism of Action)
    ▼
  data/edge_drug_target.csv

    ▼ ─── Step 6-10 + Ranking: (共享步骤, 见下方) ────────────
```

### 模式 B: 基因签名反查模式 (Signature) 🆕

```
输入: dsmeta_signature_pipeline/outputs/signature/disease_signature_meta.json
       (300 up-regulated + 300 down-regulated genes, 含 weight)

configs/
  ├── base.yaml
  ├── diseases/atherosclerosis.yaml
  └── versions/v5_signature.yaml     Signature 配置 (max_phase≥2)

    ▼ ─── Step 1: 基因签名 → 药物反查 ──────────────────────────
    │
    │  disease_signature_meta.json
    │  → 取 top 100 基因 (按 weight 排序, up + down)
    │  → Gene Symbol → UniProt Accession (MyGene.info)
    │  → UniProt → ChEMBL Target ID
    │  → ChEMBL Target → Drug Molecules (mechanism.json)
    │  → 筛选: max_phase ≥ 2 (Phase II 及以上)
    │
    │  同时输出兼容 Step 2-5 的占位文件:
    │  • drug_chembl_map.csv      (药物映射)
    │  • edge_drug_target.csv     (药物-靶点关系)
    │  • drug_canonical.csv       (标准名)
    │  • drug_rxnorm_map.csv      (占位, 空)
    │  • failed_trials_drug_rows.csv  (占位, 空)
    │  • failed_drugs_summary.csv     (标记 source=signature)
    ▼
  data/drug_from_signature.csv       完整反查结果 (药物+靶点+基因+方向+权重)
  data/drug_chembl_map.csv           药物映射 (兼容后续步骤)
  data/edge_drug_target.csv          药物-靶点边 (兼容后续步骤)

    ⏭ Step 2-5: 跳过 (Signature 模式已直接生成)

    ▼ ─── 共享步骤 (两种模式相同) ──────────────────────────────
```

### 共享步骤 (Step 6-10 + Ranking)

```
    ▼ ─── Step 6: Target Xref + Ensembl ──────────────────────
    │  ChEMBL Target → UniProt → Ensembl Gene ID
    ▼
  data/target_xref.csv
  data/target_chembl_to_ensembl_all.csv

    ▼ ─── Step 7: Target → Pathway (Reactome) ────────────────
    │  每个蛋白质 → 所参与的生物通路 (并行 API)
    ▼
  data/edge_target_pathway_all.csv

    ▼ ─── Step 8: Gene → Disease (OpenTargets) ────────────────
    │  GraphQL API v4, 过滤非疾病 ID (GO_/MP_)
    ▼
  data/edge_target_disease_ot.csv

    ▼ ─── Step 9: Build Edges ─────────────────────────────────
    │  gene_pathway + pathway_disease + trial_ae (聚合清洗)
    ▼
  data/edge_gene_pathway.csv
  data/edge_pathway_disease.csv
  data/edge_trial_ae.csv

    ▼ ─── Step 10: FAERS + Phenotypes + Known Indications ─────
    │  • FAERS 安全信号 (PRR, 严重 AE)
    │  • Disease → Phenotype (OpenTargets, min_score ≥ 0.3)
    │  • ChEMBL drug_indication → 已知适应症 (Signature 模式)
    ▼
  data/edge_drug_ae_faers.csv
  data/edge_disease_phenotype.csv
  data/drug_known_indications.csv    (Signature 模式新增)

    ▼ ─── Ranking: V5 排序 ────────────────────────────────────
    │
    │  核心公式:
    │    final = mechanism × exp(-w1×safety - w2×trial_penalty)
    │                      × (1 + w3×log1p(min(n_phenotype,10)))
    │
    │  Signature 模式额外输出:
    │    • is_known_indication: 是否为已知适应症 (标记, 不排除)
    │    • original_indications: 该药已批准的适应症列表
    │    • signature_genes: 触发反查的签名基因
    │    • n_signature_targets: 该药命中的签名靶点数
    ▼
  output/
  ├── drug_disease_rank_v5.csv      最终排序 (含 is_known_indication + CI 列)
  │                                 ci_lower, ci_upper, ci_width, confidence_tier, n_evidence_paths
  ├── evidence_paths_v5.jsonl       所有路径 (JSONL)
  ├── evidence_pack_v5/             每对证据包 (JSON)
  ├── bridge_repurpose_cross.csv   Direction A: 跨疾病迁移 bridge
  ├── bridge_origin_reassess.csv   Direction B: 原疾病重评估 bridge (generate_disease_bridge.py)
  └── pipeline_manifest.json        运行元数据 (计时、缓存、药物来源)
```

---

## 你需要提供什么

### CT.gov 模式
1. **疾病方向** — CT.gov 搜索条件 (如 "atherosclerosis")
2. **排序版本** — 推荐 V5

### Signature 模式
1. **疾病方向** — 疾病名称 (用于 OpenTargets 查询)
2. **签名文件** — dsmeta_signature_pipeline 输出的 `disease_signature_meta.json`
3. **排序版本** — 推荐 V5

---

## 你能得到什么

| 文件 | 说明 |
|------|------|
| `output/drug_disease_rank_v5.csv` | 药物-疾病排序 (final_score, mechanism, safety, is_known_indication) |
| `output/evidence_paths_v5.jsonl` | 所有 DTPD 路径 (每行一个 JSON) |
| `output/evidence_pack_v5/*.json` | ★ 每对药-疾病的完整证据包 |
| `output/bridge_repurpose_cross.csv` | Direction A: 跨疾病迁移 bridge (每药最高分疾病) |
| `output/bridge_origin_reassess.csv` | Direction B: 原疾病重评估 bridge (目标疾病 + 文献注入) |
| `output/pipeline_manifest.json` | 运行元数据 (计时、缓存命中率、配置摘要) |
| `data/drug_from_signature.csv` | (Signature) 反查结果 (药物+靶点+基因+权重) |
| `data/drug_known_indications.csv` | (Signature) 各药物已知适应症列表 |
| `data/edge_*.csv` | 所有中间边数据 (可复用) |

---

## 安装

```bash
pip install -r requirements.txt
mkdir -p data output cache
```

依赖: Python 3.12 + pandas + numpy + requests + tenacity + networkx + pyyaml + tqdm

---

## 运行

### CT.gov 模式 (经典)

```bash
# 完整管道
python -m kg_explain pipeline --disease atherosclerosis --version v5

# 仅排序 (假设数据已存在)
python -m kg_explain rank --version v5
```

### Signature 模式 (跨疾病 repurposing) 🆕

```bash
# 完整管道 — 从基因签名反查药物
python -m kg_explain pipeline \
  --disease atherosclerosis \
  --version v5 \
  --drug-source signature \
  --signature-path ../dsmeta_signature_pipeline/outputs/signature/disease_signature_meta.json
```

### 分步获取数据

```bash
# CT.gov 模式
python -m kg_explain fetch ctgov --condition atherosclerosis
python -m kg_explain fetch rxnorm
python -m kg_explain fetch chembl
python -m kg_explain fetch targets
python -m kg_explain fetch pathways
python -m kg_explain fetch diseases
python -m kg_explain fetch faers
python -m kg_explain fetch phenotypes

# Signature 模式 (单独反查)
python -m kg_explain fetch signature \
  --signature-path ../dsmeta_signature_pipeline/outputs/signature/disease_signature_meta.json
```

### 构建中间边

```bash
python -m kg_explain build gene-pathway
python -m kg_explain build pathway-disease
python -m kg_explain build trial-ae
```

### 评估 (需要金标准)

```bash
python -m kg_explain benchmark --version v5 --gold gold_standard.csv --ks 5,10,20
```

---

## 对接其他项目

### → SigReverse (融合机制分)

```bash
# KG_Explain 产出排序
python -m kg_explain pipeline --disease atherosclerosis --version v5

# SigReverse 融合 KG 分数
cd ../sigreverse
python scripts/run_fusion_with_kg.py \
    --sigreverse-csv data/output/drug_reversal_rank.csv \
    --kg-csv ../kg_explain/output/drug_disease_rank_v5.csv \
    --faers-csv ../kg_explain/data/edge_drug_ae_faers.csv \
    --disease atherosclerosis \
    --out data/output/fused_rank.csv
```

### → LLM+RAG 证据工程 (补充文献证据)

两个方向的 bridge 文件，分别喂给 LLM+RAG:

```bash
# Direction A: 跨疾病迁移 (bridge_repurpose_cross.csv)
cd ../LLM+RAG证据工程
python scripts/step6_evidence_extraction.py \
    --rank_in ../kg_explain/output/bridge_repurpose_cross.csv \
    --out output/step6_repurpose_cross \
    --target_disease atherosclerosis --topn 50

# Direction B: 原疾病重评估 (bridge_origin_reassess.csv)
python scripts/step6_evidence_extraction.py \
    --rank_in ../kg_explain/output/bridge_origin_reassess.csv \
    --out output/step6_origin_reassess \
    --target_disease atherosclerosis --topn 83
```

### 原疾病重评估 (Direction B)

从 V3 排序中提取目标疾病相关药物，评估"失败药物是否真的对原疾病无效"。

```bash
# 通用脚本 — 换疾病只改 --disease 参数
python scripts/generate_disease_bridge.py \
    --disease atherosclerosis \
    --inject configs/inject_atherosclerosis.yaml \
    --out output/bridge_origin_reassess.csv

# 换其他疾病
python scripts/generate_disease_bridge.py \
    --disease "type 2 diabetes" \
    --out output/bridge_origin_reassess.csv
```

文献注入配置 (`configs/inject_<disease>.yaml`):
```yaml
- name: colchicine
  endpoint_type: CV_EVENTS
  note: "COLCOT/LoDoCo2"
- name: canakinumab
  endpoint_type: CV_EVENTS
  note: "CANTOS"
```

---

## 数据源

| 数据源 | 模块 | 用途 | API |
|--------|------|------|-----|
| **CT.gov** | `datasources/ctgov.py` | 失败临床试验 | ClinicalTrials.gov API v2 |
| **RxNorm** | `datasources/rxnorm.py` | 药物名标准化 | RxNav REST API |
| **ChEMBL** | `datasources/chembl.py` | 药物→靶点映射 | ChEMBL API |
| **Reactome** | `datasources/reactome.py` | 靶点→通路关系 | Reactome ContentService |
| **OpenTargets** | `datasources/opentargets.py` | 基因→疾病 + 表型 | GraphQL API v4 |
| **FDA FAERS** | `datasources/faers.py` | 药物不良事件 | openFDA API |
| **Signature** 🆕 | `datasources/signature.py` | 基因签名→药物反查 | MyGene.info + ChEMBL |

---

## 版本演进

| 版本 | 路径类型 | 新增能力 |
|------|----------|----------|
| V1 | Drug → Disease | CT.gov conditions 直接关联 |
| V2 | Drug → Target → Disease | + ChEMBL 靶点机制 |
| V3 | Drug → Target → Pathway → Disease | + Reactome 通路 (核心 DTPD) |
| V4 | V3 + Evidence Pack | + 每对证据包 (JSON) |
| **V5** | **完整可解释路径** | **+ FAERS 安全信号 + 疾病表型加成** |
| **V5-Sig** 🆕 | **V5 + Signature 药物来源** | **+ 基因签名反查 + 已知适应症标记** |

---

## V5 评分公式

```
final_score = mechanism_score
              × exp(-w1 × safety_penalty - w2 × trial_penalty)
              × (1 + w3 × log1p(min(n_phenotype, 10)))

机制分 (V3 DTPD 路径):
  path_score = (1 + support_gene_boost × n_support_genes)
               × pathway_score
               × exp(-hub_penalty × target_degree)

其中:
  w1 = 0.3 (safety_penalty_weight)
  w2 = 0.2 (trial_failure_penalty, Signature 模式设为 0)
  w3 = 0.1 (phenotype_overlap_boost)
  support_gene_boost = 0.15
  hub_penalty = 1.0
  每对药-疾病保留 top-10 paths
```

---

## V5 证据包格式

每个 `evidence_pack_v5/{drug}__{disease}.json` 包含:

```json
{
  "drug": "tofacitinib citrate",
  "disease": {"id": "EFO_0000685", "name": "rheumatoid arthritis"},
  "scores": {
    "final": 10.58,
    "mechanism": 8.54,
    "safety_penalty": 0.0,
    "trial_penalty": 0.0
  },
  "explainable_paths": [
    {
      "type": "DTPD",
      "path_score": 2.15,
      "nodes": [
        {"type": "Drug",    "id": "tofacitinib citrate"},
        {"type": "Target",  "id": "CHEMBL2148"},
        {"type": "Pathway", "id": "R-HSA-449147", "name": "Signaling by Interleukins"},
        {"type": "Disease", "id": "EFO_0000685", "name": "rheumatoid arthritis"}
      ],
      "explanation": "tofacitinib citrate targets CHEMBL2148 (JAK3), which..."
    }
  ],
  "safety_signals": [...],
  "trial_evidence": [],
  "phenotypes": [...]
}
```

---

## Signature 模式反查流程

```
Gene Symbol (e.g. JAK3, BTK from disease signature)
  → MyGene.info: /v3/query?q=JAK3&species=human&fields=uniprot,ensembl
  → UniProt Accession (e.g. P52333)
  → ChEMBL: /target.json?target_components__accession=P52333
  → target_chembl_id (e.g. CHEMBL2148)
  → ChEMBL: /mechanism.json?target_chembl_id=CHEMBL2148
  → 所有药物分子 + mechanism_of_action
  → ChEMBL: /molecule/{id}.json → max_phase ≥ 2 (Phase II+)
  → 候选药物列表

同时:
  → ChEMBL: /drug_indication.json?molecule_chembl_id=X
  → 已知适应症列表 → is_known_indication 标记
```

---

## 配置参考

<details>
<summary>完整配置项说明 (展开)</summary>

### base.yaml — 基础配置

```yaml
# API 端点
api:
  ctgov: "https://clinicaltrials.gov/api/v2/studies"
  rxnorm: "https://rxnav.nlm.nih.gov/REST/approximateTerm.json"
  chembl: "https://www.ebi.ac.uk/chembl/api/data"
  reactome: "https://reactome.org/ContentService"
  opentargets: "https://api.platform.opentargets.org/api/v4/graphql"
  faers: "https://api.fda.gov/drug/event.json"

# HTTP 设置
http:
  timeout: 60
  max_retries: 5
  page_size: 200

# 排序参数
rank:
  topk_paths_per_pair: 10
  topk_pairs_per_drug: 50
  hub_penalty_lambda: 1.0
  support_gene_boost: 0.15
```

### versions/v5_signature.yaml — Signature 模式参数

```yaml
mode: v5

signature:
  max_phase: 2          # Phase II+ 药物
  max_genes: 100        # 前100个签名基因
  gene_source: both     # up + down 都用

rank:
  safety_penalty_weight: 0.3
  trial_failure_penalty: 0.0    # Signature 模式无试验数据
  phenotype_overlap_boost: 0.1

faers:
  min_report_count: 5
  min_prr: 1.5
```

### diseases/atherosclerosis.yaml — 疾病配置

```yaml
condition: "atherosclerosis"
drug_filter:
  include: [DRUG, BIOLOGICAL]
  exclude: [DEVICE, PROCEDURE, BEHAVIORAL, DIETARY_SUPPLEMENT]
icd10: [I70, I25, I73.9]
mesh_terms:
  - Atherosclerosis
  - Coronary Artery Disease
  - Peripheral Arterial Disease
```

</details>

---

## 项目结构

```
kg_explain/
├── src/kg_explain/                  源代码
│   ├── __init__.py                 版本 0.7.0
│   ├── __main__.py                 入口
│   ├── cli.py                      命令行 (pipeline + fetch + rank + build)
│   ├── config.py                   配置加载 + 验证
│   ├── cache.py                    HTTP 缓存 (TTL + 并发)
│   ├── utils.py                    工具函数 (concurrent_map, CSV I/O)
│   ├── graph.py                    NetworkX 知识图谱构建
│   ├── datasources/                7 个数据源模块
│   │   ├── ctgov.py               CT.gov 失败试验
│   │   ├── rxnorm.py              RxNorm 药物名标准化
│   │   ├── chembl.py              ChEMBL 药物→靶点映射
│   │   ├── reactome.py            Reactome 通路 (并行)
│   │   ├── opentargets.py         OpenTargets 基因→疾病
│   │   ├── faers.py               FAERS 不良事件 (PRR)
│   │   └── signature.py           🆕 基因签名→药物反查
│   ├── builders/                   边构建
│   │   └── edges.py               gene_pathway, pathway_disease, trial_ae
│   ├── rankers/                    排序算法 V1-V5
│   │   ├── v1.py ~ v5.py         各版本排序器
│   │   ├── base.py                hub_penalty 等共享工具
│   │   ├── uncertainty.py         Bootstrap CI 不确定性量化 (1000x 重采样)
│   │   └── __init__.py            run_pipeline 调度器
│   ├── evaluation/                 评估模块
│   │   ├── metrics.py             Hit@K, MRR, P@K, AP, NDCG@K, AUROC
│   │   ├── benchmark.py           Gold-standard 评估 + 报告 (含 CI + leakage 段)
│   │   ├── external_benchmarks.py Hetionet CtD 外部验证
│   │   ├── temporal_split.py      时间分割验证 (集成 leakage audit)
│   │   └── leakage_audit.py       数据泄漏审计 (drug/disease/pair 重叠检测)
│   └── governance/                 治理模块
│       ├── quality_gate.py        指标阈值门控 + 回归容忍检查
│       ├── registry.py            模型版本注册 (config hash + data hash + metrics)
│       └── regression.py          回归测试套件 (固定 input/output fixtures)
│
├── configs/                        配置文件
│   ├── base.yaml                  通用设置
│   ├── diseases/
│   │   └── atherosclerosis.yaml   疾病方向
│   └── versions/
│       ├── v5.yaml                V5 参数 (CT.gov 模式)
│       └── v5_signature.yaml      🆕 V5 参数 (Signature 模式)
│
├── data/                           中间数据 (~20+ 个 CSV)
├── output/                         最终输出
├── cache/                          HTTP 缓存 (gitignored)
├── tests/                          测试文件
├── requirements.txt               依赖
└── pytest.ini                     测试配置
```

---

## 运行性能

### CT.gov 模式 (V5, atherosclerosis)
- 总耗时: **~4.6 分钟** (278s, 缓存热启动)
- 输入药物: 46 个 (来自失败试验)
- 输出药-疾病对: 54,000+

### Signature 模式 (V5, atherosclerosis) 🆕
- 总耗时: **~5.8 分钟** (349s, 缓存热启动)
- 输入基因: 100 个 (from disease signature)
- 发现药物: 31 个 (max_phase ≥ 4, 来自 8 个签名基因靶点)
- 药物类别: JAK 抑制剂、BTK 抑制剂、干扰素、强心苷、抗 CD52 等
- 输出药-疾病对: 1,550
- 已知适应症标记: 200 对

---

## 常见问题

**Q: CT.gov 模式和 Signature 模式有什么区别?**
A: CT.gov 从失败试验获取药物 (同疾病领域内)，Signature 从基因签名反查药物 (跨疾病领域)。后者更适合发现"用 A 疾病的药治 B 疾病"的 repurposing 候选。

**Q: max_phase ≥ 2 包括哪些药物?**
A: Phase II (临床 II 期)、Phase III (临床 III 期)、Phase IV (已上市)。设为 2 可以包含更多候选药物。

**Q: is_known_indication 是怎么判断的?**
A: 通过 ChEMBL `drug_indication` API 查询每个药物的所有已知适应症 (EFO/MESH ID)，与排名中的 disease ID 做交叉匹配。匹配上的标记为 True。

**Q: 为什么 100 个签名基因只有少数产出药物?**
A: 大多数疾病基因尚无已批准/在研药物靶向。这是正常的 — "可成药靶点" (druggable targets) 在人类基因组中占少数。

**Q: 如何添加新的疾病方向?**
A: 在 `configs/diseases/` 下创建新 YAML，指定 `condition` (CT.gov 搜索词)。Signature 模式还需要该疾病的 `disease_signature_meta.json`。

**Q: 组合药 (如 "aspirin+ticagrelor") 分数为什么偏高?**
A: 组合药靶点多于单药，机制分被放大。V5 已按组分数量归一化。

---

## 质量保障模块 (2026-02-12)

| 模块 | 文件 | 功能 |
|------|------|------|
| **Bootstrap CI** | `rankers/uncertainty.py` | 1000x 重采样置信区间, HIGH/MEDIUM/LOW 分层 |
| **Leakage Audit** | `evaluation/leakage_audit.py` | Drug/disease/pair 三级泄漏检测 |
| **Temporal Split** | `evaluation/temporal_split.py` | 按年份切割 train/test, 自动集成 leakage audit |
| **External Benchmark** | `evaluation/external_benchmarks.py` | Hetionet CtD 金标准, 6+ 指标 |
| **Quality Gate** | `governance/quality_gate.py` | 指标阈值 + 回归容忍 (baseline 对比) |
| **Model Registry** | `governance/registry.py` | config hash + data hash + metrics 快照 |
| **Regression Suite** | `governance/regression.py` | 固定 fixture 回归测试 |

V5 排序后自动附加 Bootstrap CI 列:
- `ci_lower` / `ci_upper`: 95% 置信区间
- `ci_width`: 区间宽度
- `confidence_tier`: HIGH (<0.10) / MEDIUM (<0.25) / LOW (>=0.25)
- `n_evidence_paths`: 该 pair 的证据路径数

benchmark 报告自动包含 Uncertainty Summary + Data Leakage Audit 段落。

**测试**: 335 tests 全通过

---

## 免责声明
- 结果基于公开数据库 (CT.gov, ChEMBL, Reactome, OpenTargets, FAERS) 的自动化整合，不等于临床验证。
- Hub 靶点 (如激酶家族) 已惩罚但仍可能引入噪声。
- FAERS 安全信号为启发式，需结合专业判断。
- 已知适应症标记基于 ChEMBL 数据，可能不完整。
