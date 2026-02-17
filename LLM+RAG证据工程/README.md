# LLM+RAG 证据工程: PubMed 文献驱动的药物重定位证据管道

> 基于 **PubMed RAG + 本地 LLM (Ollama)** 的自动化证据工程，从文献中提取药物-疾病证据，多维评分后输出**假设卡片 + 验证方案**。

---

## 最近更新（2026-02-17）

- **工业级 TopN 调度（与主仓 runner 对齐）**:
  - 评分驱动 + 预算约束 + 质量门控 + 最多一次扩容（禁止无限扩容）
  - 默认 `TOPN_PROFILE=stable`，`TOPN_ORIGIN/TOPN_CROSS=auto`
  - 每次 run 产出 `topn_decision_*.json` 与 `topn_quality_*.json` 审计文件
- **Step6 预算参数升级** (`scripts/step6_evidence_extraction.py`):
  - `--pubmed_retmax` (默认 120)
  - `--pubmed_parse_max` (默认 60)
  - `--max_rerank_docs` (默认 40)
  - `--max_evidence_docs` (默认 12)
- `ops/quickstart.sh` 默认采用工业级自动 topn 策略（保持手动数字 topn 兼容）

- **Release Gate** (`scoring/release_gate.py`): Step8 自动拦截 NO-GO 药物, GO 比例检查, 人工审核质量门控 (kill/miss rate, IRR Kappa)
- **Contract Enforcer** (`contracts_enforcer.py`): Step7/8/9 所有输出 schema 强制校验 (strict 模式 raise / soft 模式 warn)
- **Audit Log** (`common/audit_log.py`): SHA256 不可篡改哈希链, 防篡改检测
- **Human Review Metrics** (`evaluation/human_review.py`): Kill rate, miss rate, IRR (Cohen's Kappa) 计算
- **Stratified Sampling** (`evaluation/stratified_sampling.py`): 按分数分层 + 门控决策平衡抽样
- **IAA Annotation** (`evaluation/annotation.py`): Cohen's Kappa 一致性 + 混淆矩阵
- **Monitoring Alerts** (`monitoring/alerts.py`): 可配置阈值规则 + JSONL 告警分发
- Step6/7/8/9 增加了**数据契约校验**与**run manifest**（可追溯输入/输出哈希、配置、git 状态）
- Step7 增加 **Exploit / Explore 双通道**，降低"为了匹配而匹配"造成的候选漏检
- `eval_extraction.py` 支持 holdout split；新增 `build_reject_audit_queue.py` 做拒绝样本回抽

---

## 工业级审核包（Human-in-the-loop）

说明：
- 人工审核是工业级的**必要条件**，但不是唯一条件。
- 仍需同时满足 CI 门禁、监控告警、可追溯治理和安全合规。

位置：`docs/quality/`

- `docs/quality/INDUSTRIAL_REVIEW_SOP.md`
- `docs/quality/review_log_template.csv`
- `docs/quality/adjudication_template.md`
- `docs/quality/release_decision_template.md`
- `docs/quality/issue_codebook.md`

每次 run 的最小审核流程：

1. 生成并冻结 Step6-9 manifests。  
2. 复制 `review_log_template.csv` 为 `review_log_<run_id>.csv`。  
3. Reviewer A/B 独立审核（先不互看）。  
4. 用 `adjudication_template.md` 记录分歧仲裁。  
5. 用 `release_decision_template.md` 做最终 `RELEASE/NO-RELEASE` 决策。  

---

## 整体定位

```
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│  dsmeta_signature    │     │     SigReverse        │     │     KG_Explain        │
│                      │     │                       │     │                       │
│  GEO 原始数据        │     │  disease_signature    │     │  CT.gov 失败试验     │
│  → 差异表达          │     │  → LINCS L1000 查询   │     │  → Drug-Target       │
│  → Meta 分析         │     │  → CMap 反向评分      │     │  → Target-Pathway    │
│  → 疾病签名 JSON     │     │  → 药物排序           │     │  → Pathway-Disease   │
└──────────────────────┘     └──────────────────────┘     └──────────────────────┘
       造签名                       找反转药物                    找机制证据
     (自下而上)                   (表达谱匹配)                  (自上而下)

                    ┌──────────────────────────────────┐
                    │      LLM+RAG 证据工程 (本项目)    │
                    │                                  │
                    │  bridge_repurpose_cross.csv ──→ 输入  │
                    │  (来自 KG_Explain)                │
                    │                                  │
                    │  PubMed 检索                     │
                    │  → BM25 + 语义重排序              │
                    │  → Ollama LLM 证据提取           │
                    │  → 多维打分 + 门控决策            │
                    │  → 假设卡 + 候选短名单            │
                    │  → 验证方案                       │
                    └──────────────────────────────────┘
                            文献证据补充
                          (PubMed 文本挖掘)
```

四个项目**互补**：dsmeta 造签名 → SigReverse 找药 → KG_Explain 解释机制 → LLM+RAG 补充文献证据。

---

## 数据流（Step 0 → Step 9）

```
data/seed_nct_list.csv  (种子 NCT ID 列表)
  └── 或 kg_explain/output/bridge_repurpose_cross.csv (来自 KG_Explain)

    ▼ ─── Step 0: build_pool ────────────────────────────────
    │  从种子 NCT ID 扩展试验池
    │  • CT.gov API 查询相关条件
    │  • 扩展到关联疾病层级
    │  • 本地缓存 (cache/ctgov/)
    ▼
  data/poolA_trials.csv            扩展后的试验池

    ▼ ─── Step 1-3: fetch_failed_drugs ──────────────────────
    │  提取失败试验药物
    │  • 解析试验方案中的药物名称
    │  • 获取试验状态 (TERMINATED, WITHDRAWN, FAILED)
    │  • 解析不良事件 (如可用)
    ▼
  data/poolA_trials_labeled.csv    标注后的试验 + 药物

    ▼ ─── Step 4: label_trials (AI 标注) ────────────────────
    │  AI 辅助标注试验结果
    │  • CT.gov + PubMed 联合查询试验结局
    │  • AI 标注: success / failure / unclear
    │  • 生成人工审核队列 (边界案例)
    ▼
  data/step4_final_trial_labels.csv  标注后的试验结果

    ▼ ─── Step 5: drug_normalize ─────────────────────────────
    │  药物名称标准化 + 聚合
    │  • 去除剂量/剂型/给药途径
    │  • 希腊字母转换 (α → alpha)
    │  • 别名分组 (aspirin, ASA → aspirin)
    │  • 生成稳定 drug_id (MD5 哈希)
    ▼
  data/
  ├── drug_master.csv              标准药物列表 (drug_id + canonical_name)
  ├── drug_alias_map.csv           别名映射
  └── poolA_drug_level.csv         药物级统计 (试验数、阶段等)

    ▼ ─── Step 6: PubMed RAG + LLM 证据提取 ★ 核心步骤 ────
    │  三阶段文献检索 + LLM 结构化提取
    │
    │  Stage 1: PubMed 多路检索
    │    • 多路 query: exact disease / endpoint+mechanism / cross-disease
    │    • 每路 ESearch → EFetch XML 解析，再合并去重
    │
    │  Stage 2: 排序
    │    • 各 route BM25 关键词匹配 (top 80)
    │    • route 列表先做 RRF 融合
    │    • (可选) Embedding 语义重排序 (Ollama nomic-embed-text, top 30)
    │    • 记录 route_coverage / cross_disease_hits 指标
    │
    │  Stage 3: LLM 结构化提取
    │    • 模型: Ollama qwen2.5:7b-instruct
    │    • 每篇文献提取:
    │      - direction: benefit / harm / neutral / unknown
    │      - model: human / animal / cell / unknown
    │      - endpoint: PLAQUE_IMAGING / CV_EVENTS / BIOMARKER / ...
    │      - mechanism: 1-2 句描述
    │      - confidence: 0-1 浮点
    │    • 幻觉检测: PMID 一致性 + 药物锚定 + 机制锚定
    │    • JSON 修复: 括号提取、markdown 剥离、逗号修复
    │    • 重试策略: 3 次, 温度递减 [0.2, 0.1, 0.0]
    ▼
  output/step6/
  ├── dossiers/                    每药证据档案
  │   ├── {drug_id}__{name}.json   结构化 JSON
  │   └── {drug_id}__{name}.md     可读 Markdown
  ├── step6_rank_v2.csv            证据排序 (含支持/反对计数)
  ├── step6_manifest.json          Step6 运行清单 (provenance)
  └── cache/pubmed/                PubMed 缓存 (PMID + XML + embedding)

    ▼ ─── Step 7: 多维评分 + 门控 ───────────────────────────
    │  5 维打分 (0-100 总分):
    │    • 证据强度 (0-30): 支持文献数量 × 质量
    │    • 机制合理性 (0-20): 有机制描述的文献比例
    │    • 可转化性 (0-20): 人体研究、临床阶段
    │    • 安全性 (0-20): 反向于危害证据
    │    • 实用性 (0-10): 药物可及性
    │
    │  门控决策:
    │    • 硬门控 (自动 NO-GO): 支持文献 < 2, 危害比 > 50%
    │    • 软门控: GO (≥60), MAYBE (40-60), NO-GO (<40)
    │
    │  生成假设卡片 (每药一张):
    │    • 假设摘要 + 机制叙述
    │    • 证据汇总 (支持/反对)
    │    • 下一步建议
    ▼
  output/step7/
  ├── step7_scores.csv             5 维评分
  ├── step7_gating_decision.csv    门控决策 (GO/MAYBE/NO-GO + 理由)
  ├── step7_cards.json             假设卡 (结构化)
  ├── step7_hypothesis_cards.md    假设卡 (可读)
  ├── step7_validation_plan.csv    初步验证方案
  └── step7_manifest.json          Step7 运行清单 (provenance)

    ▼ ─── Step 8: Release Gate + 候选短名单 + 打包 ──────────
    │  Release Gate (自动拦截):
    │  • NO-GO 药物自动从 shortlist 移除
    │  • GO 比例检查 (可配置 min_go_ratio)
    │  • ContractEnforcer 校验输出 schema
    │
    │  候选打包:
    │  • 按总分排序 → top-K 短名单
    │  • 从 bridge CSV 读取靶点信息 (targets + PDB/AlphaFold 标记)
    │  • 生成每药单页摘要 (one-pager, 含靶点结构表)
    │  • 打包 Excel 工作簿 (多 sheet, 含靶点结构表)
    ▼
  output/step8/
  ├── step8_shortlist_topK.csv     排序短名单 (含 targets + target_details 列)
  ├── step8_candidate_pack_from_step7.xlsx  Excel 报告 (短名单 + 每药靶点结构表)
  ├── step8_one_pagers_topK.md     Markdown 单页摘要 (含靶点/UniProt/PDB)
  └── step8_manifest.json          Step8 运行清单 (provenance)

    ▼ ─── Step 9: 验证方案 ──────────────────────────────────
    │  为每个候选药物生成:
    │  • 实验类型 (体外/动物/临床试验设计)
    │  • 关键研究问题
    │  • 读数指标 + 成功标准
    │  • 时间估算
    ▼
  output/step9/
  ├── step9_validation_plan.csv    验证方案 (CSV)
  ├── step9_validation_plan.md     验证方案 (Markdown)
  └── step9_manifest.json          Step9 运行清单 (provenance)
```

---

## 你需要提供什么

1. **药物池** — 以下任一方式:
   - 种子 NCT ID 列表 (`seed_nct_list.csv`) → Step 0 自动扩展
   - KG_Explain 的 bridge 文件 (`bridge_repurpose_cross.csv`) → 直接从 Step 6 开始
   - 手动药物列表 (`poolA_drug_level.csv`)

2. **Ollama 服务** — 本地运行 (Step 6 需要)
   - LLM 模型: `qwen2.5:7b-instruct`
   - Embedding 模型: `nomic-embed-text`

---

## 你能得到什么

| 文件 | 说明 |
|------|------|
| `output/step6/dossiers/*.json` | 每药证据档案 (文献引用 + 结构化提取) |
| `output/step6/step6_rank_v2.csv` | 证据排序 (支持/反对计数 + 置信度) |
| `output/step7/step7_gating_decision.csv` | 门控决策 (GO/MAYBE/NO-GO) |
| `output/step7/step7_cards.json` | 假设卡片 (每药机制假设 + 证据) |
| `output/step8/step8_shortlist_topK.csv` | 候选短名单 (含 targets + target_details + docking就绪字段) |
| `output/step8/step8_candidate_pack_from_step7.xlsx` | ★ Excel 候选报告 (每药 Sheet 含靶点结构表) |
| `output/step8/step8_one_pagers_topK.md` | Markdown 候选报告 (含靶点/UniProt/PDB 链接) |
| `output/step8/step8_manifest.json` | Step8 运行清单 (输入/输出哈希 + 配置) |
| `output/step9/step9_validation_plan.csv` | 验证方案 (实验设计 + 成功标准) |
| `output/step9/step9_manifest.json` | Step9 运行清单 (输入/输出哈希 + 配置) |

### Step8 靶点信息 (2026-02-17 工业版升级)

Step8 从 bridge CSV 自动读取靶点数据，并按 `PDB优先 + AlphaFold回退` 自动生成对接就绪字段，输出到三个地方:

1. **CSV** (`step8_shortlist_topK.csv`): 新增 `targets`、`target_details` 和 docking 字段:
   - `docking_primary_target_chembl_id`, `docking_primary_target_name`, `docking_primary_uniprot`
   - `docking_primary_structure_source`, `docking_primary_structure_provider`, `docking_primary_structure_id`
   - `docking_backup_targets_json` (默认主靶1+备选2)
   - `docking_feasibility_tier` (`READY_PDB` / `AF_FALLBACK` / `NO_STRUCTURE`)
   - `docking_target_selection_score`, `docking_risk_flags`, `docking_policy_version`
2. **Excel** (每药 Sheet): 新增靶点结构表
   - Target ChEMBL ID | Target Name | UniProt | Mechanism of Action | Structure Source | PDB Count | PDB IDs (top 5)
3. **Markdown** (`step8_one_pagers_topK.md`): 新增 `### Known targets (ChEMBL)` 段落，含 UniProt 链接和结构来源标记

Structure Source / Docking tier 含义:
- **PDB+AlphaFold**: 有实验晶体结构 → 优先选 PDB 做分子对接
- **AlphaFold_only**: 仅有 AI 预测结构 → 对接结果需谨慎
- **none**: 无结构 → 无法做对接

新增参数:
- `--bridge <path>` (可选, 自动检测 bridge CSV 位置)
- `--docking_primary_n` (默认 1)
- `--docking_backup_n` (默认 2)
- `--docking_structure_policy` (默认 `pdb_first`)
- `--docking_block_on_no_pdb` (默认 0, 非阻断降级)

---

## 安装

```bash
# 创建环境
python -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 安装 Ollama (Step 6 必需)
# macOS: brew install ollama
# 或从 https://ollama.ai 下载

# 拉取模型
ollama pull qwen2.5:7b-instruct
ollama pull nomic-embed-text
```

依赖: Python 3.10+ + pandas + requests + tqdm + openpyxl + python-dotenv + PyYAML + prometheus-client

---

## 运行

### 推荐启动（工业级，建议）

从主仓库根目录启动（推荐）：

```bash
cd "/Users/xinyueke/Desktop/Drug Repurposing"

# 单病种快速验证（默认 origin_only）
bash ops/quickstart.sh --single atherosclerosis

# 单病种 A+B 全跑（Cross + Origin）
RUN_MODE=dual bash ops/quickstart.sh --single atherosclerosis

# 24/7 常驻（工业级 topn 自动策略）
TOPN_PROFILE=stable RUN_MODE=dual bash ops/quickstart.sh --mode dual --run-only
```

说明：
1. 默认 `TOPN_ORIGIN/TOPN_CROSS=auto`，由策略自动决定 Stage1 topn。
2. 仅当 shortlist 质量不过线时触发 Stage2，且最多扩容一次。
3. 若要强制旧行为，仍可手动指定 `TOPN_ORIGIN=<int> TOPN_CROSS=<int>`。
4. 每个疾病 run 会写审计文件到 `runtime/work/<disease>/<run_id>/llm/`：
   - `topn_decision_origin_stage1.json` / `topn_quality_origin_stage1.json`
   - `topn_decision_cross_stage1.json` / `topn_quality_cross_stage1.json`
   - 对应 `*_stage2.json`（触发或跳过原因）

### 启动 Ollama (前提)

```bash
# 在单独终端中
ollama serve
```

### Step 6: PubMed RAG 证据提取 (核心)

```bash
python scripts/step6_evidence_extraction.py \
    --rank_in data/poolA_drug_level.csv \
    --neg data/poolA_negative_drug_level.csv \
    --out output/step6 \
    --target_disease atherosclerosis \
    --topn 14 \
    --pubmed_retmax 120 \
    --pubmed_parse_max 60 \
    --max_rerank_docs 40 \
    --max_evidence_docs 12
```

### Step 7: 评分 + 门控

```bash
python scripts/step7_score_and_gate.py \
    --input output/step6 \
    --out output/step7 \
    --strict_contract 1
```

### Step 8: 候选打包

```bash
python scripts/step8_candidate_pack.py \
    --step7_dir output/step7 \
    --neg data/poolA_negative_drug_level.csv \
    --outdir output/step8 \
    --topk 5 \
    --include_explore 1 \
    --min_explore_slots 1 \
    --strict_contract 1
```

### Step 9: 验证方案

```bash
python scripts/step9_validation_plan.py \
    --step8_dir output/step8 \
    --step7_dir output/step7 \
    --outdir output/step9 \
    --target_disease atherosclerosis \
    --strict_contract 1
```

---

## 对接其他项目

### ← KG_Explain (输入药物列表)

KG_Explain 产出两个方向的 bridge 文件：

```bash
# Direction A: 跨疾病迁移 (bridge_repurpose_cross.csv)
#   每药取全局最高分疾病 → 发现跨疾病 repurposing 候选
cd ../kg_explain
python -m src.kg_explain.cli pipeline --disease atherosclerosis --version v5
# → output/bridge_repurpose_cross.csv

cd ../LLM+RAG证据工程
python scripts/step6_evidence_extraction.py \
    --rank_in ../kg_explain/output/bridge_repurpose_cross.csv \
    --out output/step6_repurpose_cross \
    --target_disease atherosclerosis --topn 12 \
    --pubmed_retmax 120 --pubmed_parse_max 60 \
    --max_rerank_docs 40 --max_evidence_docs 12

# Direction B: 原疾病重评估 (bridge_origin_reassess.csv)
#   筛选目标疾病相关药物 → 评估"失败药物是否真的无效"
cd ../kg_explain
python scripts/generate_disease_bridge.py \
    --disease atherosclerosis \
    --inject configs/inject_atherosclerosis.yaml \
    --out output/bridge_origin_reassess.csv

cd ../LLM+RAG证据工程
python scripts/step6_evidence_extraction.py \
    --rank_in ../kg_explain/output/bridge_origin_reassess.csv \
    --out output/step6_origin_reassess \
    --target_disease atherosclerosis --topn 14 \
    --pubmed_retmax 120 --pubmed_parse_max 60 \
    --max_rerank_docs 40 --max_evidence_docs 12
```

两个方向的 Step7-9 各自独立运行，输出到 `step7_repurpose_cross/` 和 `step7_origin_reassess/` 等目录。
若希望使用自动 topn + 质量门控扩容，请走主仓 `ops/run_24x7_all_directions.sh` 或 `ops/quickstart.sh`。

### ← dsmeta (签名 → SigReverse → 药物列表)

```bash
# dsmeta 造签名 → SigReverse 找药 → 药物排序
# 将 SigReverse 的 drug_reversal_rank.csv 转为药物列表后使用
```

---

## 核心算法

### 三阶段文献排序 (Step 6)

```
PubMed 全文检索 (ESearch + EFetch)
  ↓
BM25 关键词匹配 → top 80 篇
  ↓
(可选) Ollama Embedding 语义重排序 → top 30 篇
  ↓
RRF 融合 (score = 1/(k + rank), k=60)
  ↓
top 30 → Ollama LLM 结构化提取
```

### LLM 提取 Schema

```json
{
  "direction": "benefit | harm | neutral | unknown",
  "model": "human | animal | cell | unknown",
  "endpoint": "自由文本端点标签 (例如 PLAQUE_IMAGING / CV_EVENTS / BIOMARKER)",
  "claim": "证据陈述",
  "confidence": "0-1 浮点数"
}
```

### 幻觉检测 (三层验证)

1. **PMID 一致性**: 提取的 PMID 必须存在于摘要/标题中
2. **药物锚定**: 药物名必须出现在摘要文本中
3. **机制锚定**: 机制描述必须引用具体发现，而非泛化陈述

### 5 维评分 (Step 7)

| 维度 | 分值 | 计算方式 |
|------|------|----------|
| 证据强度 | 0-30 | 支持文献数 × 2 - 危害文献 × 1 - 中性 × 0.5 |
| 机制合理性 | 0-20 | (有机制 PMID 数) / 阈值 × 20 |
| 可转化性 | 0-20 | 人体研究计数 + 临床阶段 + 疾病接近度 |
| 安全性 | 0-20 | 反向于危害证据 + 安全黑名单惩罚 |
| 实用性 | 0-10 | 药物可及性 + 生产可行性 |

### 门控规则

| 类型 | 条件 | 决策 |
|------|------|------|
| 硬门控 | 支持文献 < 2 | NO-GO |
| 硬门控 | 危害/(支持+危害) > 50% | NO-GO |
| 硬门控 | 唯一 PMID < 3 | NO-GO |
| 软门控 | 总分 ≥ 60 | GO |
| 软门控 | 40 ≤ 总分 < 60 | MAYBE |
| 软门控 | 总分 < 40 | NO-GO |

---

## 配置参考

<details>
<summary>环境变量 + 配置说明 (展开)</summary>

### 环境变量 (.env)

```bash
# PubMed API
NCBI_API_KEY=<your_ncbi_key>    # 有 key: 10 req/s, 无 key: 3 req/s
NCBI_DELAY=0.6                   # 请求间隔 (秒)
PUBMED_TIMEOUT=30                # 超时 (秒)
PUBMED_EFETCH_CHUNK=20           # 批量获取大小

# Ollama LLM
OLLAMA_HOST=http://localhost:11434
OLLAMA_EMBED_MODEL=nomic-embed-text
OLLAMA_LLM_MODEL=qwen2.5:7b-instruct
OLLAMA_TIMEOUT=600               # LLM 超时 (秒)
EMBED_BATCH_SIZE=16              # Embedding 批量大小

# 排序配置
BM25_TOPK=80                     # BM25 粗排
EMBED_TOPK=30                    # Embedding 精排
RRF_K=60                         # RRF 融合参数

# Feature flags
DISABLE_EMBED=0                  # 设 1 禁用 embedding
DISABLE_LLM=0                   # 设 1 使用规则提取
CROSS_DRUG_FILTER=1             # 过滤跨药物污染
PMID_STRICT=1                    # 严格 PMID 验证
FORCE_REBUILD=0                  # 强制重建缓存

# 评分阈值
TOPIC_MIN=0.30                   # 主题匹配最低比例
MIN_UNIQUE_PMIDS=2               # 最少唯一 PMID 数
```

### 评分配置 (ScoringConfig)

```python
@dataclass
class ScoringConfig:
    min_benefit: int = 10        # HIGH 置信度最低支持文献数
    topic_mismatch_threshold: float = 0.30
    high_confidence_min_pmids: int = 6
    med_confidence_min_pmids: int = 3
```

</details>

---

## 项目结构

```
LLM+RAG证据工程/
├── scripts/                         管道脚本
│   ├── step0_build_pool.py                   试验池扩展
│   ├── step1_3_fetch_failed_drugs.py         失败药物抓取
│   ├── step4_label_trials.py                 AI 标注
│   ├── step5_normalize_drugs.py              药物标准化
│   ├── step6_evidence_extraction.py          ★ PubMed RAG 核心
│   ├── step7_score_and_gate.py               评分 + 门控
│   ├── step8_candidate_pack.py               候选打包
│   ├── step9_validation_plan.py              验证方案
│   ├── eval_extraction.py                    抽取质量评估
│   └── build_reject_audit_queue.py           拒绝样本回抽复核
│
├── src/dr/                          核心模块
│   ├── common/                      通用工具
│   │   ├── text.py                 药物名标准化 (canonicalize_name)
│   │   ├── http.py                 HTTP 重试 (指数退避)
│   │   └── hashing.py             稳定哈希
│   ├── retrieval/                   数据获取
│   │   ├── pubmed.py              PubMedClient (ESearch + EFetch)
│   │   ├── ctgov.py               CTGovClient
│   │   └── cache.py               CacheManager (4 级缓存)
│   ├── evidence/                    证据提取
│   │   ├── extractor.py           LLMExtractor (JSON Schema 提取)
│   │   ├── ranker.py              BM25Ranker + HybridRanker
│   │   └── ollama.py              OllamaClient (LLM + Embedding)
│   ├── scoring/                     评分层
│   │   ├── scorer.py              DrugScorer (5 维)
│   │   ├── gating.py              GatingEngine (硬 + 软门控)
│   │   ├── cards.py               HypothesisCardBuilder
│   │   ├── validation.py          ValidationPlanner
│   │   ├── aggregator.py          试验→药物聚合 + 模糊匹配
│   │   └── release_gate.py        Release Gate (NO-GO 拦截 + GO 比例 + 人审质量)
│   ├── contracts_enforcer.py        Schema 强制校验 (strict raise / soft warn)
│   ├── evaluation/                  质量评估
│   │   ├── metrics.py             评估指标
│   │   ├── gold_standard.py       金标准框架
│   │   ├── annotation.py          IAA (Cohen's Kappa + 混淆矩阵)
│   │   ├── human_review.py        人工审核指标 (kill/miss rate, IRR)
│   │   └── stratified_sampling.py 分层抽样 (分数 × 门控决策平衡)
│   ├── common/                      通用工具
│   │   ├── text.py                药物名标准化
│   │   ├── http.py                HTTP 重试
│   │   ├── hashing.py             稳定哈希
│   │   ├── file_io.py             原子读写
│   │   ├── provenance.py          Run provenance (git hash + I/O hash)
│   │   └── audit_log.py           SHA256 不可篡改审计日志
│   └── monitoring/                  监控
│       ├── metrics.py             Prometheus 指标 (MetricsTracker)
│       └── alerts.py              可配置阈值告警 (JSONL 分发)
│
├── tests/                           测试 (截至 2026-02-11: 351 tests, 75.79% 覆盖率)
│   ├── unit/                       单元测试 (~200+)
│   │   ├── test_extractor.py      LLM 提取器 (96%)
│   │   ├── test_text.py           文本工具 (95%)
│   │   ├── test_scorer.py         评分器 (91%)
│   │   ├── test_gating.py         门控 (87%)
│   │   ├── test_ranker.py         排序器 (79%)
│   │   └── test_pubmed.py         PubMed (72%)
│   └── integration/                集成测试
│
├── monitoring/                      监控栈配置
│   ├── prometheus/                 Prometheus 配置
│   └── grafana/                    Grafana 面板 (14 panels)
│
├── data/                            输入数据
├── output/                          管道输出 (step6-step9)
├── cache/                           缓存 (gitignored)
│   ├── ctgov/                      CT.gov API 缓存
│   └── pubmed/                     PubMed XML/embedding 缓存
├── configs/
│   └── stop_words.yaml             药物名停用词
├── .env.example                     环境变量模板
├── requirements.txt                 依赖 (锁定版本)
├── requirements-dev.txt             开发依赖
├── docker-compose.monitoring.yml    Prometheus + Grafana
└── docs/                             文档与质量治理模板
```

---

## 运行性能

**典型运行 (~30 个药物, 动脉粥样硬化)**:

| 步骤 | 耗时 | 瓶颈 |
|------|------|------|
| Step 6 PubMed RAG | ~45 分钟 | LLM 提取 (~30min) |
| Step 7 评分 + 门控 | < 1 分钟 | — |
| Step 8 打包 | < 1 分钟 | — |
| Step 9 验证方案 | < 1 分钟 | — |

系统要求:
- RAM: 8GB+ (Ollama LLM + Embedding)
- CPU: 4+ 核心
- 磁盘: 50GB+ (缓存 + dossiers)
- 网络: 稳定连接到 PubMed (有速率限制)

---

## 监控 (可选)

内置 **Prometheus + Grafana** 监控栈:

```bash
# 1. 启动监控栈
docker-compose -f docker-compose.monitoring.yml up -d

# 2. 访问面板
# Grafana: http://localhost:3000 (admin/admin)
# Prometheus: http://localhost:9090
```

监控指标:
- `dr_pipeline_executions_total` — 管道执行计数 (成功/失败)
- `dr_pubmed_requests_total` — PubMed API 调用
- `dr_llm_extractions_total` — LLM 提取调用
- `dr_drug_scores` — 药物评分分布
- `dr_gating_decisions_total` — 门控决策 (GO/MAYBE/NO-GO)
- `dr_errors_total` — 错误计数 (按模块/类型)

---

## 测试

```bash
# 全部测试
pytest

# 带覆盖率报告
pytest --cov=src/dr --cov-report=html

# 特定模块
pytest tests/unit/test_scorer.py

# 集成测试
pytest -m integration
```

评估与反漏检：

```bash
# 抽取质量评估（含 holdout split）
python scripts/eval_extraction.py \
  --gold data/gold_standard/gold_standard_v1.csv \
  --dossier-dir output/step6/dossiers \
  --fields direction model endpoint \
  --holdout-ratio 0.2 \
  --split-key drug \
  --out output/eval/step6_eval_holdout.json
```

```bash
# 从 NO-GO / MAYBE-explore 抽样，做人工反漏检审核
python scripts/build_reject_audit_queue.py \
  --step7-dir output/step7 \
  --n 30 \
  --include-maybe-explore 1 \
  --out output/quality/reject_audit_queue.csv
```

覆盖率（截至 2026-02-12）：**501 tests 全通过**

| 模块 | 覆盖率 |
|------|--------|
| Extractor (LLM 提取) | 96% |
| Text Utils (文本处理) | 95% |
| Scorer (评分器) | 91% |
| Gating (门控) | 87% |
| Ranker (排序器) | 79% |
| PubMed (检索) | 72% |

---

## 常见问题

**Q: Ollama 超时或连接失败?**
A: 确保 `ollama serve` 在运行。检查 `OLLAMA_HOST` 环境变量。LLM 超时默认 600s (`OLLAMA_TIMEOUT`)，对于大文档可能需要增大。

**Q: PubMed 速率限制?**
A: 无 API key 限 3 req/s，有 key 限 10 req/s。设置 `NCBI_API_KEY` 环境变量。申请地址: https://www.ncbi.nlm.nih.gov/account/

**Q: 如何禁用 Embedding 重排序 (加速)?**
A: 设置 `DISABLE_EMBED=1`。只用 BM25 排序，速度更快但精度略低。

**Q: 为什么某些药物的 LLM 提取结果为空?**
A: 可能原因: (1) PubMed 无相关文献, (2) 文献与目标疾病不相关 (topic_match_ratio 低), (3) LLM 无法解析 JSON (查看日志)。设置 `PMID_STRICT=0` 可放宽验证。

**Q: 假设卡里的 "mechanism" 是 LLM 生成的吗?**
A: 是的，由 Ollama qwen2.5:7b 从文献摘要中提取。经过幻觉检测三层验证，但仍需人工审核。

**Q: 可以换用其他 LLM 吗?**
A: 可以。修改 `OLLAMA_LLM_MODEL` 环境变量。推荐 7B+ 参数的指令微调模型。更大模型 (如 13B/70B) 提取质量更好但更慢。

---

## 质量保障模块 (2026-02-12)

| 模块 | 文件 | 功能 |
|------|------|------|
| **Release Gate** | `scoring/release_gate.py` | NO-GO 拦截 + GO 比例 + 人审质量 (kill/miss rate, IRR) |
| **Contract Enforcer** | `contracts_enforcer.py` | Step7/8/9 输出 schema 强制校验 (strict/soft) |
| **Audit Log** | `common/audit_log.py` | SHA256 哈希链, 追加写, 防篡改检测 |
| **Human Review** | `evaluation/human_review.py` | Kill rate, miss rate, IRR (Cohen's Kappa) |
| **Stratified Sampling** | `evaluation/stratified_sampling.py` | 平衡抽样 (分数层 × 门控类型) |
| **IAA Annotation** | `evaluation/annotation.py` | Cohen's Kappa 一致性, 混淆矩阵 |
| **Gold Standard** | `evaluation/gold_standard.py` | 提取准确率评估框架 |
| **Monitoring Alerts** | `monitoring/alerts.py` | 可配置阈值规则 + JSONL 告警 |
| **Provenance** | `common/provenance.py` | Run manifest (git hash + I/O hash + config) |

Step7/8/9 流程中的 schema 校验:
```
Step7 → ContractEnforcer.check_step7_scores() + check_step7_gating()
Step8 → ReleaseGate.check_shortlist_composition() → 移除 NO-GO
      → ContractEnforcer.check_step8_shortlist()
Step9 → ContractEnforcer.check_step8_shortlist() (输入) + check_step9_plan() (输出)
```

---

## 免责声明
- 结果基于 PubMed 公开文献的自动化提取，覆盖范围受限于 PubMed 索引的期刊。
- LLM 提取可能存在幻觉 (已有三层检测，但无法完全消除)。
- 门控决策 (GO/MAYBE/NO-GO) 为启发式评分，不等于临床可行性判断。
- 假设卡和验证方案需专业人员审核后方可采用。
