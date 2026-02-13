# Drug Repurposing Platform - 用户使用手册（最新版）

更新时间：2026-02-12
适用目录：`/Users/xinyueke/Desktop/Drug Repurposing`

---

## 1. 你在用的是什么系统

这个仓库包含 4 个互补模块：

1. `dsmeta_signature_pipeline`：从 GEO 多数据集构建疾病基因签名  
2. `sigreverse`：将疾病签名映射到 LINCS/CMap，筛选“反向表达”药物  
3. `kg_explain`：构建 Drug-Target-Pathway-Disease 机制链路并打分  
4. `LLM+RAG证据工程`：PubMed + LLM 抽取证据，输出 GO/MAYBE/NO-GO + 候选包 + 验证计划

> **2026-02-12 新增质量保障**: kg_explain 排名含 Bootstrap CI 置信区间；LLM+RAG Step7/8/9 自动 schema 校验 (ContractEnforcer)；Step8 Release Gate 自动拦截 NO-GO 药物；跨项目集成测试 12 个。共 848 tests 全通过。

---

## 2. 推荐使用路径

### 路径 A（全链路，推荐）
`dsmeta -> sigreverse -> kg_explain(signature) -> LLM+RAG`

### 路径 B（不跑基因签名）
`kg_explain(ctgov) -> LLM+RAG`

### 路径 C（只做文献证据）
直接跑 `LLM+RAG`（输入已有药物列表）。

---

## 3. 环境准备

建议每个模块单独虚拟环境。

### 3.1 dsmeta_signature_pipeline

```bash
cd "/Users/xinyueke/Desktop/Drug Repurposing/dsmeta_signature_pipeline"
mamba env create -f environment.yml
conda activate dsmeta
```

### 3.2 sigreverse

```bash
cd "/Users/xinyueke/Desktop/Drug Repurposing/sigreverse"
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3.3 kg_explain

```bash
cd "/Users/xinyueke/Desktop/Drug Repurposing/kg_explain"
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data output cache
```

注意：当前仓库结构下，`kg_explain` 入口用：

```bash
python -m src.kg_explain.cli --help
```

### 3.4 LLM+RAG证据工程

```bash
cd "/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程"
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Step6 需要 Ollama：

```bash
ollama serve
ollama pull qwen2.5:7b-instruct
ollama pull nomic-embed-text
```

---

## 4. 全链路最小可跑示例（Atherosclerosis）

### Step 1: dsmeta 造疾病签名

```bash
cd "/Users/xinyueke/Desktop/Drug Repurposing/dsmeta_signature_pipeline"
python run.py --config configs/athero_example.yaml
```

产物：
- `outputs/signature/disease_signature_meta.json`
- `outputs/signature/sigreverse_input.json`

### Step 2: sigreverse 反向找药

```bash
cd "/Users/xinyueke/Desktop/Drug Repurposing/sigreverse"
python scripts/run.py \
  --config configs/default.yaml \
  --in ../dsmeta_signature_pipeline/outputs/signature/sigreverse_input.json \
  --out data/output_atherosclerosis
```

产物：
- `data/output_atherosclerosis/drug_reversal_rank.csv`

### Step 3: kg_explain 机制证据（signature 模式）

```bash
cd "/Users/xinyueke/Desktop/Drug Repurposing/kg_explain"
python -m src.kg_explain.cli pipeline \
  --disease atherosclerosis \
  --version v5 \
  --drug-source signature \
  --signature-path ../dsmeta_signature_pipeline/outputs/signature/disease_signature_meta.json
```

产物：
- `output/drug_disease_rank_v5.csv`
- `output/bridge_repurpose_cross.csv`

### Step 4: LLM+RAG 证据工程（Step6-9）

```bash
cd "/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程"

python scripts/step6_evidence_extraction.py \
  --rank_in ../kg_explain/output/bridge_repurpose_cross.csv \
  --neg data/poolA_negative_drug_level.csv \
  --out output/step6_repurpose_cross \
  --target_disease atherosclerosis \
  --topn 50

python scripts/step7_score_and_gate.py \
  --input output/step6_repurpose_cross \
  --out output/step7_repurpose_cross \
  --strict_contract 1

python scripts/step8_candidate_pack.py \
  --step7_dir output/step7_repurpose_cross \
  --neg data/poolA_negative_drug_level.csv \
  --outdir output/step8_repurpose_cross \
  --target_disease atherosclerosis \
  --topk 5 \
  --include_explore 1 \
  --min_explore_slots 1 \
  --strict_contract 1

python scripts/step9_validation_plan.py \
  --step8_dir output/step8_repurpose_cross \
  --step7_dir output/step7_repurpose_cross \
  --outdir output/step9_repurpose_cross \
  --target_disease atherosclerosis \
  --strict_contract 1
```

核心结果 (Direction A: 跨疾病迁移)：
- `output/step7_repurpose_cross/step7_gating_decision.csv`
- `output/step8_repurpose_cross/step8_shortlist_topK.csv` (Release Gate 已自动移除 NO-GO 药物)
- `output/step9_repurpose_cross/step9_validation_plan.csv`

> **注意**: `--strict_contract 1` 启用 schema 强制校验。任何列缺失或类型错误会立即报错。设为 `0` 可降级为 warn-only 模式。Step8 的 Release Gate 会自动将 NO-GO 药物从 shortlist 移除并打印日志。

### Step 4b: 原疾病重评估 (Direction B)

回到原始目标疾病，评估"失败药物是否真的无效"：

```bash
# 生成原疾病重评估 bridge
cd "/Users/xinyueke/Desktop/Drug Repurposing/kg_explain"
python scripts/generate_disease_bridge.py \
  --disease atherosclerosis \
  --inject configs/inject_atherosclerosis.yaml \
  --out output/bridge_origin_reassess.csv

# Step6-9 (与 Direction A 独立)
cd "/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程"

python scripts/step6_evidence_extraction.py \
  --rank_in ../kg_explain/output/bridge_origin_reassess.csv \
  --neg data/poolA_negative_drug_level.csv \
  --out output/step6_origin_reassess \
  --target_disease atherosclerosis --topn 83

python scripts/step7_score_and_gate.py \
  --input output/step6_origin_reassess \
  --out output/step7_origin_reassess --strict_contract 1

python scripts/step8_candidate_pack.py \
  --step7_dir output/step7_origin_reassess \
  --outdir output/step8_origin_reassess \
  --target_disease atherosclerosis --topk 10 --include_explore 1

python scripts/step9_validation_plan.py \
  --step8_dir output/step8_origin_reassess \
  --step7_dir output/step7_origin_reassess \
  --outdir output/step9_origin_reassess \
  --target_disease atherosclerosis
```

核心结果 (Direction B: 原疾病重评估)：
- `output/step7_origin_reassess/step7_gating_decision.csv`
- `output/step8_origin_reassess/step8_shortlist_topK.csv`
- `output/step9_origin_reassess/step9_validation_plan.csv`

> **换疾病**: 只需改 `--disease` 参数和对应的 `--inject` 配置文件。输出文件名不变，会覆盖之前的结果。

---

## 5. 自定义药物注入（你最常用）

如果你有内部失败药物或未公开化合物，建议从 `screen_drugs.py` 开始。

### 5.1 准备私有药物 CSV

示例：`/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程/data/my_private_drugs.csv`

```csv
drug_name,phase,conditions,outcome,pvalue,notes
MyDrug-001,PHASE2,Atherosclerosis,NEGATIVE,p=0.08,Internal phase2 failed
MyDrug-002,PHASE3,Coronary Artery Disease,NEGATIVE,p=0.15,Terminated for futility
```

最少必须有 `drug_name` 列。

### 5.2 与 CT.gov 结果合并筛选

```bash
cd "/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程"
python scripts/screen_drugs.py \
  --disease atherosclerosis \
  --phases PHASE2 PHASE3 \
  --statuses COMPLETED TERMINATED WITHDRAWN SUSPENDED \
  --append-csv data/my_private_drugs.csv \
  --outdir data
```

该命令会输出：
- `data/poolA_trials.csv`
- `data/poolA_drug_level.csv`
- `data/drug_master.csv`
- `data/step6_rank.csv`（可直接喂给 Step6）
- `data/manual_review_queue.csv`
- `data/screen_manifest.json`

### 5.3 直接进入 Step6-9

```bash
python scripts/step6_evidence_extraction.py \
  --rank_in data/step6_rank.csv \
  --neg data/poolA_negative_drug_level.csv \
  --out output/step6_custom \
  --target_disease atherosclerosis \
  --topn 50
```

后续 Step7-9 命令同上，把输入目录换为 `output/step6_custom`。

---

## 6. 工业化人工审核与放行

审核模板目录：
`/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程/docs/quality`

每个 run 必做：

1. 冻结 Step6-9 的 manifest  
2. `review_log_template.csv` 复制为 `review_log_<run_id>.csv`  
3. Reviewer A/B 独立审核  
4. `adjudication_<run_id>.md` 做分歧仲裁  
5. `release_decision_<run_id>.md` 写最终放行决定

说明：人工审核是工业级必要条件，不是充分条件。还需要 CI 门禁、监控告警、可追溯与合规一起成立。

### 6.1 自动化质量门控（2026-02-12 新增）

除人工审核外，管道现已内置以下自动门控：

| 门控 | 位置 | 行为 |
|------|------|------|
| **ContractEnforcer** | Step7/8/9 | 校验输出 CSV schema (列名/类型)，strict 模式下缺列即报错 |
| **Release Gate** | Step8 | 自动拦截 NO-GO 药物、检查 GO 比例、可对接人审质量指标 |
| **Bootstrap CI** | kg_explain V5 | 排名附带 95% 置信区间，`confidence_tier=LOW` 的排名需谨慎解读 |
| **Leakage Audit** | kg_explain temporal | 检测 train/test 数据泄漏 (drug/disease/pair 三级) |

这些门控与人工审核互补，形成 **"自动拦截 + 人工决策"** 双层保障。

---

## 7. gold_standard_v1.csv 怎么接入

`gold_standard_v1.csv` 不是 Step0-9 主流程输入。  
它用于 Step6 抽取质量评估脚本：

```bash
cd "/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程"
python scripts/eval_extraction.py \
  --gold data/gold_standard/gold_standard_v1.csv \
  --dossier-dir output/step6_repurpose_cross/dossiers \
  --fields direction model endpoint \
  --holdout-ratio 0.2 \
  --split-key drug \
  --out output/eval/step6_eval_repurpose_cross.json
```

你的人工审核结论，应该定期增量合并到：
`data/gold_standard/gold_standard_v1.csv`，再跑评估。

反漏检审核（推荐每个 run 都做）：

```bash
python scripts/build_reject_audit_queue.py \
  --step7-dir output/step7_repurpose_cross \
  --n 30 \
  --include-maybe-explore 1 \
  --out output/quality/reject_audit_queue_kg_bridge.csv
```

---

## 8. 常见问题

### Q1. Step6 很慢怎么办？
- 先缩小输入药物数（`--rank_in` 只放 TopN 药）。
- 降低 `--topn`（例如 20-30）。
- 先不做完整评测，先跑出 step7/8 做筛选。

### Q2. `kg_explain` 为什么 `python -m kg_explain` 不可用？
- 当前仓库结构下请用：`python -m src.kg_explain.cli ...`

### Q3. 我审核完是不是“直接保存”就行？
- 不是。保存后还要：
  1) 形成 release 决策单  
  2) 将最终标注并入 `gold_standard_v1.csv`  
  3) 运行 `eval_extraction.py` 形成量化评估闭环

---

## 9. 你应该优先看的结果文件

1. `/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程/output/step7_*/step7_gating_decision.csv`  
2. `/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程/output/step8_*/step8_shortlist_topK.csv`  
3. `/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程/output/step9_*/step9_validation_plan.csv`  
4. `/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程/output/eval/*.json`

---

## 10. 关联文档

- 全项目统一入口：`/Users/xinyueke/Desktop/Drug Repurposing/README.md`
- LLM+RAG 子项目：`/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程/README.md`
- 工业审核模板：`/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程/docs/quality/README.md`
- dsmeta 子项目：`/Users/xinyueke/Desktop/Drug Repurposing/dsmeta_signature_pipeline/README.md`
- sigreverse 子项目：`/Users/xinyueke/Desktop/Drug Repurposing/sigreverse/README.md`
- kg_explain 子项目：`/Users/xinyueke/Desktop/Drug Repurposing/kg_explain/README.md`
- 人工判断检查清单：`/Users/xinyueke/Desktop/Drug Repurposing/HUMAN_JUDGMENT_CHECKLIST.md`
- 工业化分析报告：`/Users/xinyueke/Desktop/Drug Repurposing/LLM+RAG证据工程/docs/INDUSTRIAL_READINESS_REPORT.md`
