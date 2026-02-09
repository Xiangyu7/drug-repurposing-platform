# Drug Repurposing Platform — 使用指南

> 本指南面向需要手动输入自定义药物（如临床 II/III 期失败但未公开的药物）的用户。

---

## 目录

1. [平台架构总览](#1-平台架构总览)
2. [环境准备](#2-环境准备)
3. [Step 0：药物筛选（screen_drugs.py）](#3-step-0药物筛选)
4. [快速开始：自定义药物注入](#4-快速开始自定义药物注入)
5. [模块一：dsmeta_signature_pipeline（疾病签名）](#5-模块一dsmeta_signature_pipeline)
6. [模块二：sigreverse（反向签名匹配）](#6-模块二sigreverse)
7. [模块三：kg_explain（知识图谱解释）](#7-模块三kg_explain)
8. [模块四：LLM+RAG 证据工程](#8-模块四llmrag-证据工程)
9. [自定义药物完整流程（端到端）](#9-自定义药物完整流程端到端)
10. [输出文件说明](#10-输出文件说明)
11. [常见问题](#11-常见问题)

---

## 1. 平台架构总览

```
                     ┌──────────────────────────┐
                     │  你的自定义药物列表       │
                     │  (临床失败/未公开药物)    │
                     └────────┬─────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
    ┌─────────────┐  ┌──────────────┐  ┌──────────────┐
    │ sigreverse  │  │  kg_explain  │  │  LLM+RAG     │
    │ 反向签名匹配│  │  知识图谱    │  │  证据工程    │
    └──────┬──────┘  └──────┬───────┘  └──────┬───────┘
           │                │                  │
           └────────────────┼──────────────────┘
                            ▼
                   ┌─────────────────┐
                   │ 最终候选药物列表 │
                   │ + 假说卡片      │
                   │ + 验证方案      │
                   └─────────────────┘
```

**四个模块可以独立运行，也可以串联使用。**
对于手动注入自定义药物，最常用的入口是 **模块四（LLM+RAG）**。

---

## 2. 环境准备

### 2.1 Python 环境

每个模块有独立的虚拟环境。推荐使用各自的 `.venv`：

```bash
# LLM+RAG 模块（最常用）
cd "LLM+RAG证据工程"
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# kg_explain 模块
cd ../kg_explain
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# sigreverse 模块
cd ../sigreverse
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2.2 Ollama（LLM+RAG 模块必需）

```bash
# 安装 Ollama (macOS)
brew install ollama

# 启动服务
ollama serve

# 拉取所需模型
ollama pull qwen2.5:7b-instruct
ollama pull nomic-embed-text
```

### 2.3 R 环境（仅 dsmeta 模块需要）

```bash
# 需要 R >= 4.0 + 以下包：
# limma, GEOquery, fgsea, data.table
```

---

## 3. Step 0：药物筛选（screen_drugs.py）

**这是整个管道的起点**——从 ClinicalTrials.gov 按疾病/阶段/状态筛选药物，并支持混入你自己的未公开药物。

脚本位置：`LLM+RAG证据工程/scripts/screen_drugs.py`

### 3.1 按疾病筛选（最常用）

```bash
cd "LLM+RAG证据工程"

# 筛选动脉粥样硬化的 Phase 2/3 失败试验
python scripts/screen_drugs.py --disease atherosclerosis
```

脚本会自动：
1. 搜索 CT.gov 匹配疾病关键词的 PHASE2/PHASE3 试验
2. 拉取每条试验的详情（干预措施、结果、主要终点）
3. 自动标注结果为 NEGATIVE / POSITIVE / MIXED / UNCLEAR
4. 提取药物名（去除安慰剂/对照）
5. 生成去重后的药物主表 + Step6 可直接使用的输入文件

### 3.2 灵活筛选

```bash
# 只看 Phase 3 + 终止的试验
python scripts/screen_drugs.py \
  --disease atherosclerosis \
  --phases PHASE3 \
  --statuses TERMINATED

# 按药物名搜（已知药物）
python scripts/screen_drugs.py --drug colchicine

# 联合搜索：疾病 + 药物
python scripts/screen_drugs.py --disease atherosclerosis --drug darapladib

# 心衰的 Phase 2/3/4
python scripts/screen_drugs.py \
  --disease "heart failure" \
  --phases PHASE2 PHASE3 PHASE4

# 快速测试（只拉 50 条）
python scripts/screen_drugs.py --disease atherosclerosis --max-studies 50

# 跳过详情拉取（更快，但信息不完整）
python scripts/screen_drugs.py --disease atherosclerosis --skip-details
```

**参数说明**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--disease` | *(无)* | 疾病关键词 (CT.gov condition query) |
| `--drug` | *(无)* | 药物关键词 (CT.gov intervention query) |
| `--phases` | `PHASE2 PHASE3` | 试验阶段。可选: `PHASE1` `PHASE2` `PHASE3` `PHASE4` |
| `--statuses` | `COMPLETED TERMINATED WITHDRAWN SUSPENDED` | 试验状态 |
| `--max-studies` | `500` | 最大拉取数量 |
| `--append-csv` | *(无)* | 追加自定义药物 CSV（见下节） |
| `--outdir` | `data/` | 输出目录 |
| `--skip-details` | `false` | 跳过详情拉取 |

### 3.3 混入你自己的未公开药物

准备一个简单的 CSV 文件 `data/my_private_drugs.csv`：

```csv
drug_name,phase,conditions,outcome,pvalue,notes
MyDrug-001,PHASE2,Atherosclerosis,NEGATIVE,p=0.08,Internal Phase 2 failed on primary endpoint
MyDrug-002,PHASE3,Coronary Artery Disease,NEGATIVE,p=0.15,Terminated for futility
CompoundX,PHASE2,Heart Failure,UNCLEAR,,Early stage compound
```

**必需列**: `drug_name`（其他列可选但建议填写）

**可选列**:

| 列名 | 说明 | 示例 |
|------|------|------|
| `drug_name` | 药物名称（**必需**） | `MyDrug-001` |
| `nctId` | 试验编号（无则自动生成） | `NCT12345678` 或 `PRIV001` |
| `phase` | 试验阶段 | `PHASE2` / `PHASE3` |
| `conditions` / `disease` | 适应症 | `Atherosclerosis` |
| `outcome` | 结果标签 | `NEGATIVE` / `POSITIVE` / `UNCLEAR` |
| `pvalue` | p 值 | `p=0.08` |
| `status` | 试验状态 | `TERMINATED` |
| `sponsor` | 赞助方 | `Your Company` |
| `title` | 试验标题 | `Phase 2 Study of MyDrug-001` |
| `primary_outcome` | 主要终点 | `CIMT Change from Baseline` |
| `timeframe` | 观察时间 | `12 months` |
| `pmids` | 相关 PubMed ID | `30639340;28476871` |
| `notes` | 备注 | 任意文本 |

然后运行：

```bash
python scripts/screen_drugs.py \
  --disease atherosclerosis \
  --append-csv data/my_private_drugs.csv
```

这会把 CT.gov 搜到的公开药物和你的私有药物**合并到同一个输出**中。

### 3.4 输出文件

```
data/
├── poolA_trials.csv            ← 试验级别记录（含结果标签、p值）
├── poolA_drug_level.csv        ← 药物级别记录（下游 Step4-6 的输入）
├── drug_master.csv             ← 去重药物主表（drug_id + canonical_name）
├── step6_rank.csv              ← Step6 证据提取的直接输入
├── manual_review_queue.csv     ← 需要你人工复核的试验（⚠ 重要！）
└── screen_manifest.json        ← 运行参数 + 统计摘要
```

### 3.5 人工复核

运行结束后，**务必检查** `manual_review_queue.csv`。这是自动标注为 NEGATIVE/UNCLEAR/MIXED 的试验——你需要快速浏览确认：

1. 结果标签是否正确（NEGATIVE vs POSITIVE 有时仅凭文本难以判断）
2. 药物名归一化是否正确（如 "darapladib 160mg" → "darapladib"）
3. 是否有你想排除的药物（安慰剂/对照漏网之鱼）

### 3.6 完整工作流示例

```bash
cd "LLM+RAG证据工程"

# Step 0: 筛选药物
python scripts/screen_drugs.py \
  --disease atherosclerosis \
  --phases PHASE2 PHASE3 \
  --append-csv data/my_private_drugs.csv \
  --outdir data

# (人工) 检查 manual_review_queue.csv

# Step 6: 证据提取
python scripts/step6_evidence_extraction.py \
  --rank_in data/step6_rank.csv \
  --neg data/poolA_drug_level.csv \
  --out output/step6 \
  --target_disease atherosclerosis

# Step 7: 打分 + GO/NO-GO
python scripts/step7_score_and_gate.py \
  --input output/step6 \
  --out output/step7

# Step 8: 候选报告
python scripts/step8_candidate_pack.py \
  --step7_dir output/step7 \
  --neg data/poolA_drug_level.csv \
  --outdir output/step8 \
  --target_disease atherosclerosis \
  --topk 5
```

### 3.7 扩展版：多源筛选（screen_drugs_extended.py）

从 **6 个公开数据源** 拉取候选药物，按 **7 维证据** 交叉评分：

| # | 数据源 | 提供什么 | 是否需要注册 |
|---|--------|----------|-------------|
| 1 | ClinicalTrials.gov | 失败/完成的 Phase 2/3 试验药物 | 否 |
| 2 | OpenTargets | 疾病-靶点遗传关联 (GWAS) | 否 |
| 3 | ChEMBL | 靶点→药物反查（已知 MOA） | 否 |
| 4 | OpenTargets (已知药物) | 该疾病已有的临床药物 | 否 |
| 5 | **repoDB** | 药物重定位金标准：已验证的正样本(Approved) + 负样本(Failed) | 否 (Figshare) |
| 6 | **TTD** | 靶点可药性 + Drug-Target-Disease 三元组 | 否 (直接下载) |
| 7 | 用户自定义 CSV | 你的未公开药物 | — |

```bash
# 标准用法（自动搜索全部 6 个数据源）
python scripts/screen_drugs_extended.py --disease atherosclerosis

# 指定 OpenTargets 疾病 ID（更精确）
python scripts/screen_drugs_extended.py --disease atherosclerosis --disease-id EFO_0003914

# 混入私有药物
python scripts/screen_drugs_extended.py --disease atherosclerosis --append-csv data/my_drugs.csv

# 只用部分数据源（快速测试）
python scripts/screen_drugs_extended.py --disease atherosclerosis --sources ctgov,chembl

# 跳过 repoDB/TTD（网络受限时）
python scripts/screen_drugs_extended.py --disease atherosclerosis --sources ctgov,opentargets,chembl
```

**交叉评分维度**（7 维，满分 100）:

| 维度 | 满分 | 含义 |
|------|------|------|
| 多源覆盖 | 20 | 同时出现在多个数据源的药物得分更高（每源 +5） |
| 遗传证据 | 20 | 靶点有 GWAS/基因组学证据支持（临床成功率翻倍） |
| 临床阶段 | 15 | 已批准(15) > Phase3(12) > Phase2(8) |
| **repoDB 金标准** | **15** | 正样本 +15（已在其他适应症获批）；负样本 -10（已失败） |
| **TTD 可药性** | **15** | Successful target +15；Clinical target +8 |
| 试验数量 | 10 | CT.gov 中有更多试验记录 |
| 已知机制 | 5 | 有明确的药理机制描述 |

> **repoDB 的价值**: 如果一个药物在其他适应症已获 FDA 批准（repoDB 正样本），说明安全性已验证，重定位成功率更高。反之，如果同样的 drug-indication 对已被标记为失败（负样本），则需要格外谨慎。

> **TTD 的价值**: 如果药物的靶点被标注为 "Successful target"（已有其他药物通过该靶点获批），说明靶点可药性已验证，降低靶点风险。

**优先看**: `drugs_ranked_summary.csv` 中 `cross_score` 最高的、`n_sources >= 2` 且 `has_genetic_evidence = True` 的药物。repoDB `+` 标记的药物特别值得关注。

---

## 4. 快速开始：自定义药物注入

**最快的方式**：跳过模块一二三，直接从模块四（LLM+RAG）开始。

你只需要准备两个 CSV 文件：

### 文件 1：`drug_master.csv`（药物主表）

```csv
drug_id,canonical_name
DCUSTOM00001,darapladib
DCUSTOM00002,losmapimod
DCUSTOM00003,your_drug_name
```

- `drug_id`：任意唯一标识符，格式 `D` + 10 字符（字母数字均可）
- `canonical_name`：药物规范名称（小写，去除剂型信息）

**自动生成 drug_id 的方法**（可选）：

```python
import hashlib

def make_drug_id(name):
    """与管道内部算法一致的 ID 生成"""
    h = hashlib.sha1(name.encode("utf-8")).hexdigest()
    return "D" + h[:10].upper()

# 示例
print(make_drug_id("darapladib"))   # → D8A3F...
print(make_drug_id("losmapimod"))   # → D2B7C...
```

### 文件 2：`poolA_negative_drug_level.csv`（试验信息）

```csv
nctId,briefTitle,overallStatus,phase,studyType,leadSponsor,conditions,drug_raw,drug_normalized,intervention_type,arm_label,arm_type,role,is_candidate_drug,is_combo,outcome_label_final,confidence_final,evidence_source,primary_outcome_title,primary_outcome_timeframe,primary_outcome_pvalues,notes_ctgov,pubmed_pmids,notes_pubmed
NCT00000001,My Phase 3 Trial of DrugX,TERMINATED,PHASE3,INTERVENTIONAL,Pharma Inc,Atherosclerosis,DrugX 100mg,drugx,DRUG,DrugX arm,EXPERIMENTAL,OTHER,1,0,NEGATIVE,MED,MANUAL_ENTRY,Primary Endpoint,12 months,p=0.15,Manual entry,,
```

**最小必需列**（其他列可留空）：

| 列名 | 说明 | 示例 |
|------|------|------|
| `nctId` | 试验编号（如无公开编号可自编） | `NCT00000001` 或 `PRIV001` |
| `drug_raw` | 原始药物名称 | `DrugX 100mg tablet` |
| `drug_normalized` | 归一化名称（小写，去剂型） | `drugx` |
| `phase` | 试验阶段 | `PHASE2` / `PHASE3` |
| `conditions` | 适应症 | `Atherosclerosis` |
| `overallStatus` | 试验状态 | `TERMINATED` / `COMPLETED` |
| `intervention_type` | 干预类型 | `DRUG` |
| `is_candidate_drug` | 是否候选药 | `1` |

然后直接跳到 [第 8 节](#8-模块四llmrag-证据工程) 运行 Step 6-8。

---

## 5. 模块一：dsmeta_signature_pipeline

### 功能

从 GEO 公共微阵列数据构建疾病基因表达签名（哪些基因在疾病中上调/下调）。

### 运行命令

```bash
cd dsmeta_signature_pipeline

python run.py --config configs/your_disease.yaml
```

**可选参数**：

| 参数 | 说明 | 默认值 |
|------|------|-------|
| `--config` | 配置文件路径（**必需**） | - |
| `--from-step` | 从第几步开始 | 1 |
| `--to-step` | 到第几步结束 | 9 |
| `--dry-run` | 只显示步骤，不执行 | false |

### 配置文件模板

复制并编辑 `configs/template.yaml`：

```yaml
project:
  name: "my_disease_signature"
  outdir: "outputs"
  workdir: "work"
  seed: 13

geo:
  gse_list:            # 填入你的 GEO 数据集编号
    - "GSE12345"
    - "GSE67890"

labeling:
  mode: "regex"        # 自动标注模式
  regex_rules:
    GSE12345:
      case:
        any: ["disease", "patient"]
      control:
        any: ["healthy", "control"]
    GSE67890:
      case:
        any: ["disease"]
      control:
        any: ["normal"]

meta:
  model: "random"      # 随机效应元分析
  top_n: 300           # 取 top 300 上调 + 300 下调基因

gsea:
  nperm: 10000         # 置换次数
```

### 输出

```
outputs/signature/
├── sigreverse_input.json     ← 给 sigreverse 用的输入文件
├── disease_signature_meta.json
├── up_genes.txt
└── down_genes.txt
```

### 与自定义药物的关系

**此模块不涉及药物输入**——它只生成疾病签名。如果你已经知道目标疾病的差异基因，可以手动创建签名 JSON 跳过此模块。

---

## 6. 模块二：sigreverse

### 功能

将疾病签名与 LINCS L1000 药物扰动数据库匹配，找出能"反转"疾病表达模式的候选药物。

### 运行命令

```bash
cd sigreverse

python scripts/run.py \
  --config configs/default.yaml \
  --in data/input/disease_signature.json \
  --out data/output/my_disease/
```

**可选参数**：

| 参数 | 说明 | 默认值 |
|------|------|-------|
| `--config` | 配置文件（**必需**） | - |
| `--in` | 疾病签名 JSON（**必需**） | - |
| `--out` | 输出目录（**必需**） | - |
| `--no-stats` | 跳过统计显著性计算 | false |
| `--no-cmap` | 跳过 CMap 4 阶段流程 | false |
| `--no-dr` | 跳过剂量反应分析 | false |

### 输入文件格式

`disease_signature.json`（来自 dsmeta 输出或手动创建）：

```json
{
  "name": "atherosclerosis",
  "up": ["IL1B", "TNF", "CCL2", "VCAM1", "MMP9"],
  "down": ["KLF2", "NOS3", "ABCA1", "PPARGC1A"],
  "meta": {
    "source": "manual_curation",
    "note": "从文献整理的动脉粥样硬化差异基因"
  }
}
```

**要求**：
- `up` 和 `down` 为基因 symbol 列表（HUGO 标准命名）
- 每个方向建议 >= 50 个基因，最佳 150-300 个
- `meta` 为可选元数据

### 输出

```
data/output/my_disease/
├── drug_reversal_rank.csv      ← 药物反转评分排名
├── signature_level_details.csv ← 签名级别详情
└── run_manifest.json           ← 运行元数据
```

### 与自定义药物的关系

**此模块的药物来自 LINCS 数据库（自动检索），不支持直接注入自定义药物。**
如果你的药物恰好在 LINCS L1000 数据库中，它会自动被检索到。

---

## 7. 模块三：kg_explain

### 功能

构建 Drug → Target → Pathway → Disease 知识图谱，为候选药物提供机制解释路径 + 安全性信号。

### 运行命令

```bash
cd kg_explain

# 完整管道（自动获取数据 + 排名）
python -m kg_explain pipeline --disease atherosclerosis --version v5

# 跳过数据获取（只重新排名）
python -m kg_explain pipeline --disease atherosclerosis --version v5 --skip-fetch

# 查看详细日志
python -m kg_explain pipeline --disease atherosclerosis --version v5 -v
```

**可选参数**：

| 参数 | 说明 | 默认值 |
|------|------|-------|
| `--disease` | 疾病名称 | atherosclerosis |
| `--version` | 排名算法版本（v1-v5） | v5 |
| `--skip-fetch` | 跳过数据获取 | false |
| `-v` | 显示 DEBUG 日志 | false |

### 配置文件

三层 YAML 配置，自动合并：

```
configs/
├── base.yaml                    # 基础配置（API 端点、HTTP 参数）
├── diseases/
│   └── atherosclerosis.yaml    # 疾病特定配置
└── versions/
    └── v5.yaml                  # V5 排名参数
```

**添加新疾病**：创建 `configs/diseases/your_disease.yaml`：

```yaml
disease:
  name: "Your Disease"
  condition: "your disease name"   # CT.gov 搜索关键词

trial_filter:
  statuses:
    - TERMINATED
    - WITHDRAWN
    - SUSPENDED
```

### 输出

```
output/
├── drug_disease_rank_v5.csv      ← 带机制解释的药物排名
├── evidence_paths_v5.jsonl       ← 所有解释路径
└── pipeline_manifest.json        ← 运行元数据
```

### 与自定义药物的关系

**此模块自动从 ClinicalTrials.gov 检索失败试验中的药物。**
如果你的药物在 CT.gov 上有公开试验记录（即使失败），它会被自动检索。

如果你的药物**未公开**，可以手动提供 `data/edge_drug_target.csv`：

```csv
drug_normalized,drug_raw,molecule_chembl_id,target_chembl_id,mechanism_of_action
your_drug,Your Drug,CHEMBL123456,CHEMBL789,Your mechanism description
```

---

## 8. 模块四：LLM+RAG 证据工程

**这是注入自定义药物的主要入口。**

### 8.1 准备自定义药物输入

#### 方式 A：从零开始（最灵活）

创建 `data/step6_rank.csv`：

```csv
drug_id,canonical_name
DCUSTOM00001,darapladib
DCUSTOM00002,losmapimod
DCUSTOM00003,evacetrapib
```

只需要 `drug_id` 和 `canonical_name` 两列。Step 6 会自动：
1. 用药物名搜索 PubMed
2. BM25 + 语义重排检索相关文献
3. LLM 提取每篇文献的证据

#### 方式 B：从 CT.gov 试验数据开始（有试验记录）

如果你有试验的 NCT 编号，可以利用完整管道：

**Step 1：准备 `poolA_negative_drug_level.csv`**

```csv
nctId,briefTitle,overallStatus,phase,studyType,leadSponsor,conditions,drug_raw,drug_normalized,intervention_type,is_candidate_drug
NCT00799903,STABILITY Trial,TERMINATED,PHASE3,INTERVENTIONAL,GSK,Atherosclerosis,Darapladib,darapladib,DRUG,1
NCT01000727,SOLID-TIMI 52,COMPLETED,PHASE3,INTERVENTIONAL,GSK,Acute Coronary Syndrome,Darapladib 160mg,darapladib,DRUG,1
PRIV00001,Internal Phase 2 Study,TERMINATED,PHASE2,INTERVENTIONAL,Your Company,Atherosclerosis,YourDrug-001,yourdrug001,DRUG,1
```

**对于未公开试验**：
- `nctId` 填自定义编号（如 `PRIV00001`）
- `overallStatus` 填实际状态
- 其他可选字段尽量填写，缺失可留空

**Step 2：运行 Step 5（药物名归一化）**

```bash
cd "LLM+RAG证据工程"

# 编辑 scripts/step5_normalize_drugs.py 中的 IN_FILE 路径指向你的文件
# 或直接把文件放到 data/poolA_negative_drug_level.csv

python scripts/step5_normalize_drugs.py
```

输出：
- `data/drug_master.csv` — 药物主表
- `data/drug_alias_map.csv` — 别名映射
- `data/negative_drug_summary.csv` — 汇总统计
- `data/manual_alias_review_queue.csv` — **需要人工检查的模糊匹配**

> **重要**：检查 `manual_alias_review_queue.csv`，确认药物名没有被错误合并。

### 8.2 运行 Step 6：证据提取

```bash
cd "LLM+RAG证据工程"

# 确保 Ollama 在运行
ollama serve &

# 运行证据提取
python scripts/step6_evidence_extraction.py \
  --rank_in data/step6_rank.csv \
  --neg data/poolA_negative_drug_level.csv \
  --out output/step6 \
  --target_disease atherosclerosis \
  --topn 20
```

**参数说明**：

| 参数 | 说明 | 默认值 |
|------|------|-------|
| `--rank_in` | 药物列表 CSV | `data/step6_rank.csv` |
| `--neg` | CT.gov 试验数据 CSV | `data/poolA_negative_drug_level.csv` |
| `--out` | 输出目录 | `output/step6` |
| `--target_disease` | 目标疾病名 | `atherosclerosis` |
| `--topn` | 处理前 N 个药物 | 50 |

**环境变量（可选）**：

```bash
# LLM 配置
export OLLAMA_HOST=http://localhost:11434
export OLLAMA_LLM_MODEL=qwen2.5:7b-instruct
export OLLAMA_EMBED_MODEL=nomic-embed-text

# PubMed 配置（有 API key 更快）
export NCBI_API_KEY=your_api_key_here

# 功能开关
export DISABLE_EMBED=0    # 设为 1 禁用语义重排
export DISABLE_LLM=0      # 设为 1 禁用 LLM 提取（仅 BM25 排序）
export CROSS_DRUG_FILTER=1 # 过滤跨药物 PubMed 结果
```

**输出**：

```
output/step6/
├── step6_rank_v2.csv                          ← 带证据计数的排名
├── dossiers/
│   ├── DCUSTOM00001__darapladib.json         ← 结构化证据档案
│   ├── DCUSTOM00001__darapladib.md           ← 可读 Markdown 档案
│   ├── DCUSTOM00002__losmapimod.json
│   └── ...
└── cache/                                     ← PubMed 缓存
```

### 8.3 运行 Step 7：打分 + GO/NO-GO 门控

```bash
python scripts/step7_score_and_gate.py \
  --input output/step6 \
  --out output/step7
```

**输出**：

```
output/step7/
├── step7_scores.csv              ← 五维评分
├── step7_gating_decision.csv     ← GO / MAYBE / NO-GO 决策
├── step7_cards.json              ← 假说卡片（结构化）
├── step7_hypothesis_cards.md     ← 假说卡片（可读）
└── step7_validation_plan.csv     ← 验证方案
```

### 8.4 运行 Step 8：候选药物精选

```bash
python scripts/step8_candidate_pack.py \
  --step7_dir output/step7 \
  --neg data/poolA_negative_drug_level.csv \
  --outdir output/step8 \
  --target_disease atherosclerosis \
  --topk 5
```

**参数说明**：

| 参数 | 说明 | 默认值 |
|------|------|-------|
| `--step7_dir` | Step 7 输出目录 | `output/step7` |
| `--topk` | 精选前 K 个候选 | 3 |
| `--prefer_go` | 优先选 GO 决策的药物 | 1 |

**输出**：

```
output/step8/
├── step8_shortlist_top5.csv                ← Top-5 候选列表
├── step8_candidate_pack_from_step7.xlsx    ← 详细 Excel 报告
└── step8_one_pagers_top5.md                ← 单页摘要（可直接阅读）
```

---

## 9. 自定义药物完整流程（端到端）

以下是注入 3 个自定义药物的完整操作步骤：

### Step 1：准备药物列表

```bash
cd "LLM+RAG证据工程"

# 创建药物主表
cat > data/step6_rank.csv << 'EOF'
drug_id,canonical_name
DCUSTOM00001,darapladib
DCUSTOM00002,losmapimod
DCUSTOM00003,your_secret_drug
EOF

# 创建试验信息（可选，尽量提供）
cat > data/poolA_negative_drug_level.csv << 'EOF'
nctId,briefTitle,overallStatus,phase,studyType,leadSponsor,conditions,drug_raw,drug_normalized,intervention_type,is_candidate_drug,primary_outcome_title,primary_outcome_pvalues
NCT00799903,STABILITY Trial of Darapladib,TERMINATED,PHASE3,INTERVENTIONAL,GSK,Atherosclerosis,Darapladib,darapladib,DRUG,1,Major Coronary Events,p=0.34
NCT01145560,LATITUDE Trial of Losmapimod,COMPLETED,PHASE3,INTERVENTIONAL,GSK,Acute Coronary Syndrome,Losmapimod,losmapimod,DRUG,1,CV Death or MI,p=0.12
PRIV001,Internal Phase 2 of SecretDrug,TERMINATED,PHASE2,INTERVENTIONAL,Your Company,Atherosclerosis,SecretDrug-001,your_secret_drug,DRUG,1,CIMT Change,p=0.08
EOF
```

### Step 2：启动 Ollama

```bash
ollama serve &
# 等待几秒确认启动
curl http://localhost:11434/api/tags
```

### Step 3：运行证据提取

```bash
python scripts/step6_evidence_extraction.py \
  --rank_in data/step6_rank.csv \
  --neg data/poolA_negative_drug_level.csv \
  --out output/my_custom_run \
  --target_disease atherosclerosis \
  --topn 10
```

> 每个药物约需 2-5 分钟（取决于 PubMed 文献量和 LLM 速度）

### Step 4：打分 + 门控

```bash
python scripts/step7_score_and_gate.py \
  --input output/my_custom_run \
  --out output/my_custom_step7
```

### Step 5：生成候选报告

```bash
python scripts/step8_candidate_pack.py \
  --step7_dir output/my_custom_step7 \
  --neg data/poolA_negative_drug_level.csv \
  --outdir output/my_custom_step8 \
  --target_disease atherosclerosis \
  --topk 3
```

### Step 6：查看结果

```bash
# 查看 GO/MAYBE/NO-GO 决策
cat output/my_custom_step7/step7_gating_decision.csv

# 查看假说卡片（人类可读）
cat output/my_custom_step7/step7_hypothesis_cards.md

# 查看 Top-3 候选单页报告
cat output/my_custom_step8/step8_one_pagers_top3.md
```

---

## 10. 输出文件说明

### 10.1 证据档案（Dossier）

每个药物生成一个 JSON + 一个 Markdown 档案：

**JSON 结构**：
```
{
  "drug_id": "DCUSTOM00001",
  "canonical_name": "darapladib",
  "total_pmids": 15,
  "supporting_evidence": [
    {
      "pmid": "30639340",
      "direction": "benefit",     ← benefit / harm / neutral
      "model": "human",           ← human / animal / cell
      "endpoint": "CV_EVENTS",
      "confidence": "HIGH",
      "mechanism": "Lp-PLA2 inhibition reduces plaque inflammation",
      "claim": "Darapladib reduced coronary events..."
    }
  ],
  "harm_evidence": [...],
  "neutral_evidence": [...]
}
```

### 10.2 假说卡片

```
# darapladib — GO
总分: 72/100

| 维度 | 分数 |
|------|------|
| 证据强度 (0-30) | 24 |
| 机制合理性 (0-20) | 16 |
| 可转化性 (0-20) | 14 |
| 安全适配 (0-20) | 12 |
| 实用性 (0-10) | 6 |

机制假说: Lp-PLA2 抑制可减少...
下一步: 建议开展 Phase 2b 剂量爬坡试验...
```

### 10.3 门控决策

| 决策 | 含义 | 总分范围 |
|------|------|---------|
| **GO** | 推荐进入验证阶段 | >= 60 |
| **MAYBE** | 需要更多证据，进入观察队列 | 40-59 |
| **NO-GO** | 不推荐（证据不足或安全风险） | < 40 |

**硬门控**（无论总分多高，触发即 NO-GO）：
- benefit 论文 < 2 篇
- 总 PMID < 3 篇
- harm 比例 > 50%
- 命中安全黑名单药物

---

## 11. 常见问题

### Q: 我的药物没有公开的 NCT 编号怎么办？

在 `poolA_negative_drug_level.csv` 中使用自定义编号（如 `PRIV001`）。系统不会去 CT.gov 验证编号真实性。

### Q: 我的药物在 PubMed 上没有文献怎么办？

Step 6 会显示 `pubmed_total_articles=0`，后续打分会因证据不足得到较低分数（可能 NO-GO）。你可以：
1. 尝试使用药物的别名或通用名搜索
2. 在 `--target_disease` 参数中调整疾病关键词
3. 手动创建 dossier JSON 文件

### Q: 如何更换目标疾病？

所有模块中替换疾病关键词：
- **dsmeta**: 修改 config YAML 中的 GEO 数据集和标注规则
- **sigreverse**: 创建新的 disease_signature.json
- **kg_explain**: `--disease your_disease` + 新建 `configs/diseases/your_disease.yaml`
- **LLM+RAG**: `--target_disease your_disease`

### Q: 如何调整 GO/NO-GO 阈值？

修改 `src/dr/scoring/gating.py` 中的 `GatingConfig` 默认值，或在调用时传入自定义配置。详见 `HUMAN_JUDGMENT_CHECKLIST.md`。

### Q: 运行一个药物大约需要多长时间？

- PubMed 检索：10-30 秒
- BM25 + 语义重排：5-15 秒
- LLM 证据提取：1-3 分钟（取决于文献数量和模型速度）
- 打分 + 门控：< 1 秒
- **总计约 2-5 分钟/药物**

### Q: 如何保护未公开药物的隐私？

- 所有处理在本地完成（Ollama 本地部署）
- PubMed 查询会暴露药物名称（不可避免），但不会暴露试验数据
- CT.gov API 不会被调用（除非你提供真实 NCT 编号）
- 建议：对高度保密药物使用内部代号运行，仅在最终报告中替换真名
