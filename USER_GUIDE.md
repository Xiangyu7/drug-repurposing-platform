# Drug Repurposing Platform - 用户使用手册（最新版）

更新时间：2026-02-21
适用目录：`/Users/xinyueke/Desktop/Drug Repurposing`

---

## 1. 你在用的是什么系统

这个仓库包含 5 个互补模块 + 1 套运维工具链：

1. `dsmeta_signature_pipeline`：从 GEO microarray 多数据集构建疾病基因签名（主签名源）
2. `archs4_signature_pipeline`：从 ARCHS4 RNA-seq 构建疾病基因签名（备选签名源，dsmeta失败时自动回退）
3. `sigreverse`：将疾病签名映射到 LINCS/CMap，筛选"反向表达"药物
4. `kg_explain`：构建 Drug-Target-Pathway-Disease 机制链路并打分，标注靶点结构（PDB/AlphaFold/UniProt）
5. `LLM+RAG证据工程`：PubMed + LLM 抽取证据，输出 GO/MAYBE/NO-GO + 候选包（含分子对接就绪评估）+ 验证计划
6. `ops/` 运维工具链：一键启动（`start.sh`）+ 状态查看 + A/B交叉验证。底层脚本统一收入 `ops/internal/`

> **2026-02-21 新增**: ARCHS4 备选签名管线（RNA-seq）; Step8 新增 `alphafold_structure_id` 列（即使有PDB也展示AF ID）; A+B 路线交叉验证（`compare_ab_routes.py`）; 空签名自动回退机制。
>
> **2026-02-16 新增运维工具链**: `auto_discover_geo.py` 自动搜索 GEO 数据集 + 检测 case/control；`generate_dsmeta_configs.py` 批量生成 dsmeta 配置；`start.sh` 一键环境检查/安装/启动。

---

## 2. 推荐使用路径

### Direction A（跨疾病迁移，探索型）
```
dsmeta/archs4 -> sigreverse -> kg_explain(signature) -> LLM+RAG Step6-9
签名源优先级: dsmeta > archs4 > OT-only (自动回退)
```

### Direction B（原疾病重评估，稳健型）
```
screen_drugs(CT.gov) -> kg_explain(ctgov) -> LLM+RAG Step6-9
```

### Dual Mode（A+B 并行，推荐生产模式）
```
同时跑 A + B → compare_ab_routes.py 交叉验证
两路线都推荐的药物 = 最高可信度
```

### 路径 C（只做文献证据）
直接跑 `LLM+RAG`（输入已有药物列表）。

---

## 2.0 启动速查（只看这个就够了）

```bash
cd "/Users/xinyueke/Desktop/Drug Repurposing"

# ① 第一次用 — 装环境（只需运行一次）
bash ops/start.sh setup

# ② 跑单个疾病试试看（最常用）
bash ops/start.sh run atherosclerosis

# ③ 正式批量跑（后台常驻）
bash ops/start.sh start

# ④ 查看运行状态
bash ops/check_status.sh
```

### 可选参数（按需加）

```bash
# 想跑 A+B 两条路线（默认只跑 B）
bash ops/start.sh run atherosclerosis --mode dual

# 检查环境有没有问题（不运行）
bash ops/start.sh check
```

> 底层脚本（`runner.sh`、`env_guard.py`、`topn_policy.py` 等）已移入 `ops/internal/`，`start.sh` 已经封装了它们，你不需要直接调用。

### 添加新疾病到 Direction A

之前需要手动查 GEO、手动写 dsmeta YAML，现在自动化了：

```bash
# Step 1: 自动搜索 GEO 数据集（纯规则，无 LLM，~1分钟）
python ops/internal/auto_discover_geo.py --disease "heart failure" --write-yaml --out-dir ops/internal/geo_curation

# Step 2: 查看路线推荐（决定是否走 Direction A）
cat ops/internal/geo_curation/heart_failure/route_recommendation.txt
#   DIRECTION_B_ONLY        → 没有 GEO 数据，跳过 Direction A
#   DIRECTION_A_LOW_CONFIDENCE → 只有 1 个 GSE，建议手动补搜或只走 B
#   DIRECTION_A_GOOD/IDEAL  → 可以走 Direction A

# Step 3: AI 辅助审核（把 discovery_log.txt 喂给 LLM 审核）
cat ops/internal/geo_curation/heart_failure/discovery_log.txt
#   → 复制内容给 Claude/ChatGPT，问："请审核这些 heart failure 的 GSE 选择"
#   AI 会检查：数据类型、疾病匹配、组织/细胞类型、case/control regex、重复、研究设计

# Step 4: 应用审核结果 — 编辑 YAML 移除不合格 GSE
#   打开 dsmeta_signature_pipeline/configs/<disease>.yaml
#   ① 从 geo.gse_list 中删掉不合格的 GSE ID
#   ② 从 labeling.regex_rules 中删掉对应的标注块
#   如果审核后 GSE < 2 个 → 从 ops/internal/disease_list_day1_dual.txt 移除该疾病

# Step 5: 生成 dsmeta config + 更新 dual 列表（仅 ≥2 GSE 的疾病会被加入）
python ops/internal/generate_dsmeta_configs.py \
    --geo-dir ops/internal/geo_curation \
    --config-dir dsmeta_signature_pipeline/configs \
    --update-disease-list

# Step 6: 验证（跑 dsmeta Step1-2 检查样本数）
bash ops/start.sh check --mode dual

# Step 7: 启动
bash ops/start.sh start --mode dual
```

### AI 审核检查清单

把 `ops/internal/geo_curation/<disease>/discovery_log.txt` 喂给 LLM（Claude/ChatGPT），让它检查以下 6 项：

| 检查项 | 不合格标准 | 实际案例 |
|--------|-----------|---------|
| 数据类型 | 不是标准 mRNA 表达谱 | GSE242047 = 16S rRNA 微生物测序 |
| 疾病匹配 | GSE 研究的是其他疾病 | hypertension 搜到了 3 个 PAH（肺动脉高压） |
| 组织/细胞 | 用的是细胞系或非靶器官组织 | GSE133754 = 成纤维细胞，不是心肌组织 |
| Case/Control | regex 匹配了错误的分组 | 比较两个亚型而非疾病 vs 健康 |
| 重复 | 同一数据集出现两次 | GSE48060 和 '48060' 是同一个 |
| 研究设计 | 不是 case-control 转录组 | GSE220865 = CD34+ 细胞治疗效力测试 |

> **实战经验**：首轮 AI 审核（8 个疾病、26 个 GSE）发现了 11 个问题 GSE，其中 3 个是严重错误（16S rRNA、PAH 混入高血压）。审核后 dual list 从 8 个疾病降为 7 个。

批量添加所有 15 个心血管疾病：

```bash
# 一次搜完（约 10-15 分钟）
python ops/internal/auto_discover_geo.py --batch ops/internal/disease_list_day1_origin.txt --write-yaml --out-dir ops/internal/geo_curation

# 查看批量路线报告（哪些能走 A、哪些只能走 B）
cat ops/internal/geo_curation/batch_summary.tsv

# 批量生成 config（仅 ≥2 GSE 且无 TODO 的疾病会进入 dual 列表）
python ops/internal/generate_dsmeta_configs.py --geo-dir ops/internal/geo_curation --config-dir dsmeta_signature_pipeline/configs --update-disease-list
```

### GSE 数量与路线决策

| GSE 数量 | 路线推荐 | 说明 |
|----------|---------|------|
| 0 | ❌ 只走 Direction B | 没有 GEO 表达数据，做不了疾病基因签名 |
| 1 | ⚠️ Direction A 低可信度 | meta-analysis 退化为单实验，无交叉验证。建议手动补搜或只走 B |
| 2-3 | ✅ Direction A 可用 | meta-analysis 有效，RRA 开始有交叉验证价值 |
| 3-5 | ✅ Direction A 理想 | 最佳性价比区间 |
| >5 | ✅ Direction A 充足 | 可以更严格筛选，只留高质量数据集 |

> **原则**：`generate_dsmeta_configs.py --update-disease-list` 只会把 ≥2 GSE 且无 TODO 的疾病写入 `ops/internal/disease_list_day1_dual.txt`。GSE 不足的疾病自动走 Direction B only。

### 当前疾病配置状态

| 列表 | 疾病数 | 可用模式 |
|------|--------|---------|
| `ops/internal/disease_list_day1_origin.txt` | 15 | Direction B (origin_only) |
| `ops/internal/disease_list_day1_dual.txt` | 7 | Direction A + B (dual) |
| `ops/disease_list.txt` | 全量列表 | 自定义 |
| `ops/disease_list_test.txt` | 2 | 测试用 |

`ops/internal/disease_list_day1_dual.txt` 当前包含（2026-02-16 AI 审核后）：

| 疾病 | 原始 GSE | 审核后 GSE | 移除原因 | 评级 |
|------|---------|-----------|---------|------|
| atherosclerosis | 2 | 2 | — | ✅ 人工审核 |
| coronary_artery_disease | 5 | 3 | GSE242047=16S rRNA, GSE152498=exosomal circRNA | ✅ GOOD |
| myocardial_infarction | 5 | 3 | 48060=重复GSE48060, GSE220865=CD34+方法学 | ✅ GOOD |
| cardiomyopathy | 5 | 2 | GSE133754/GSE125990=成纤维细胞, GSE152261=感染 | ⚠️ MODERATE |
| heart_failure | 3 | 2 | GSE157205=周细胞非心肌 | ⚠️ MODERATE |
| abdominal_aortic_aneurysm | 2 | 2 | — | ✅ GOOD |
| pulmonary_arterial_hypertension | 2 | 2 | — | ⚠️ MODERATE |

未进入 dual list 的疾病（只走 Direction B）：

| 疾病 | GSE 数 | 原因 |
|------|--------|------|
| hypertension | 1 | 审核后仅 1 GSE（3/4 是 PAH 非高血压） |
| atrial_fibrillation | 1 | 仅 1 GSE，低可信度 |
| stroke | 1 | 仅 1 GSE，低可信度 |
| deep_vein_thrombosis | 0 | 无 GEO 数据 |
| venous_thromboembolism | 0 | 无 GEO 数据 |
| angina_pectoris | 0 | 无 GEO 数据 |
| myocarditis | 0 | 无 GEO 数据 |
| pulmonary_embolism | 0 | 无 GEO 数据 |
| endocarditis | 0 | 无 GEO 数据 |

---

## 2.5 磁盘空间管理（M1 Mac 用户注意）

dsmeta pipeline 每个 GSE 的表达矩阵（`workdir/geo/GSE*/expr.tsv`）约 20-50MB，7 个疾病全跑约 300-500MB 中间文件。默认已启用自动清理。

### 自动清理（默认开启）

24x7 runner 和 start.sh 默认在每个疾病跑完后自动删除 dsmeta workdir：

```bash
# 环境变量控制（默认 1=清理）
export DSMETA_CLEANUP=1    # 每个疾病跑完后自动删除 workdir（推荐）
export DSMETA_CLEANUP=0    # 保留 workdir（调试用）
```

手动运行 dsmeta 时用 `--cleanup-workdir` 参数：

```bash
cd dsmeta_signature_pipeline
python run.py --config configs/atherosclerosis.yaml --cleanup-workdir
```

### 清理范围

| 目录 | 内容 | 大小/GSE | 清理时机 |
|------|------|---------|---------|
| `work/geo/{GSE}/` | GEO 表达矩阵 + 表型数据 | 20-50 MB | 每个疾病跑完后 |
| `work/de/{GSE}/` | 差异表达结果 | 5-10 MB | 每个疾病跑完后 |
| `work/genesets/` | 基因集数据库 | ~1 MB | 每个疾病跑完后 |
| `work/gsea/` | GSEA 结果 | ~1 MB | 每个疾病跑完后 |
| `data/cache/gpl_annotations/` | GPL 平台注释 | 1-5 MB | >100MB 时清理 |

> **注意**：`outputs/` 目录（最终结果）不受清理影响，始终保留。清理的只是 `work/` 目录（可重新生成的中间文件）。

### 手动清理

```bash
# 查看 workdir 占用
du -sh dsmeta_signature_pipeline/work/

# 手动清理所有 workdir
rm -rf dsmeta_signature_pipeline/work/

# 只清理某个疾病
rm -rf dsmeta_signature_pipeline/work/atherosclerosis/
```

---

## 3. 环境准备

建议每个模块单独虚拟环境。`start.sh setup` 可自动创建大部分 venv。

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
- `output/drug_disease_rank.csv`
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
  --bridge ../kg_explain/output/bridge_repurpose_cross.csv \
  --outdir output/step8_repurpose_cross \
  --target_disease atherosclerosis \
  --topk 5 \
  --docking_primary_n 1 \
  --docking_backup_n 2 \
  --docking_structure_policy pdb_first \
  --docking_block_on_no_pdb 0 \
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
- `output/step8_repurpose_cross/step8_shortlist_topK.csv` (Release Gate 已自动移除 NO-GO 药物；含 docking 就绪列)
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
  --bridge ../kg_explain/output/bridge_origin_reassess.csv \
  --outdir output/step8_origin_reassess \
  --target_disease atherosclerosis --topk 10 --include_explore 1 \
  --docking_primary_n 1 --docking_backup_n 2 --docking_structure_policy pdb_first

python scripts/step9_validation_plan.py \
  --step8_dir output/step8_origin_reassess \
  --step7_dir output/step7_origin_reassess \
  --outdir output/step9_origin_reassess \
  --target_disease atherosclerosis
```

核心结果 (Direction B: 原疾病重评估)：
- `output/step7_origin_reassess/step7_gating_decision.csv`
- `output/step8_origin_reassess/step8_shortlist_topK.csv`
  - 重点列: `docking_feasibility_tier`, `docking_primary_structure_id`, `docking_risk_flags`
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

### Q3. 我审核完是不是"直接保存"就行？
- 不是。保存后还要：
  1) 形成 release 决策单
  2) 将最终标注并入 `gold_standard_v1.csv`
  3) 运行 `eval_extraction.py` 形成量化评估闭环

### Q4. 管线跑着跑着出错了，怎么看是哪个疾病出问题？

使用诊断工具 `ops/check_status.sh`：

```bash
# 全局概览 — 一目了然看所有疾病状态
bash ops/check_status.sh

# 只看失败记录（显示失败时间、阶段、错误消息）
bash ops/check_status.sh --failures

# 查看单个疾病的详细运行历史 + 失败原因
bash ops/check_status.sh coronary_artery_disease

# 检查 Ollama 是否正常（模型是否存在 + 推理测试）
bash ops/check_status.sh --ollama

# 查看磁盘占用（防止 M1 空间不够）
bash ops/check_status.sh --disk

# 所有检查一次跑完
bash ops/check_status.sh --all
```

输出示例（概览表）：
```
疾病                             │ A │ B │ 最近运行   │ 状态
─────────────────────────────────┼───┼───┼────────────┼──────
coronary_artery_disease            │ ✅ │ ✅ │ 2026-02-16 │ 全部成功
hypertension                       │ — │ ✅ │ 2026-02-16 │ 失败(2次)
```

---

## 9. 你应该优先看的结果文件

### 按重要性排序

| 优先级 | 文件 | 说明 |
|--------|------|------|
| ★★★ | `ab_comparison.csv` | A+B 交叉验证: 两路线重叠药 = 最高可信 |
| ★★★ | `step8_shortlist_topK.csv` | 最终候选药 (含靶点/PDB/AlphaFold/docking) |
| ★★ | `step8_candidate_pack.xlsx` | Excel 候选报告 (每药独立 Sheet) |
| ★★ | `step9_validation_plan.csv` | 实验验证计划 (P1/P2/P3 优先级) |
| ★ | `bridge_*.csv` | KG 中间排名 + 靶点结构信息 |
| ★ | `step7_gating_decision.csv` | 全部药物 GO/MAYBE/NO-GO |
| | `step6/dossiers/*.json` | PubMed 证据原文 (溯源用) |

### Step8 最终候选表 (step8_shortlist_topK.csv) 各列详解

#### 基本信息
| 列名 | 含义 |
|------|------|
| `canonical_name` | 药物标准名 |
| `gate` | 决策: GO / MAYBE / NO-GO |
| `decision_channel` | exploit(已有证据) / explore(需探索) |
| `total_score_0_100` | 综合评分 (0-100) |
| `novelty_score` | 新颖性 (0-1), 越高重定位价值越大 |
| `uncertainty_score` | 不确定性 (0-1), 越高需更多验证 |

#### 安全性
| 列名 | 含义 |
|------|------|
| `safety_blacklist_hit` | 是否命中安全黑名单 |
| `neg_trials_n` | 阴性临床试验数 (越多风险越大) |

#### 文献证据
| 列名 | 含义 |
|------|------|
| `supporting_sentence_count` | PubMed 支持性文献句数 |
| `harm_or_neutral_sentence_count` | 不利/中性文献句数 |
| `unique_supporting_pmids_count` | 支持性独立论文数 |

#### 靶点信息 (核心!)
| 列名 | 含义 |
|------|------|
| `targets` | 人类可读靶点摘要: `TargetName (CHEMBL_ID) [UniProt:ACC] [PDB+AlphaFold] — MoA` |
| `target_details` | JSON 数组, 含 ChEMBL ID、靶点名、UniProt、PDB列表、AlphaFold状态 |

#### 分子对接就绪 (Docking)
| 列名 | 含义 |
|------|------|
| `docking_primary_target_chembl_id` | 对接主靶点 ChEMBL ID |
| `docking_primary_target_name` | 对接主靶点名称 |
| `docking_primary_uniprot` | 主靶点 UniProt 蛋白ID |
| `docking_primary_structure_source` | 结构来源: PDB+AlphaFold / PDB / AlphaFold_only / none |
| `docking_primary_structure_provider` | 实际对接用: PDB (优先) 或 AlphaFold |
| `docking_primary_structure_id` | PDB ID (如 4YAY) 或 AF ID (如 AF-P30556-F1) |
| `alphafold_structure_id` | **AlphaFold 结构 ID** (格式 AF-{UniProt}-F1, 即使有PDB也展示) |
| `docking_feasibility_tier` | 对接可行性: READY_PDB > READY_AF > LIMITED > BLOCKED |
| `docking_backup_targets_json` | 备选靶点列表 (JSON) |

#### AlphaFold ID 来源说明

AlphaFold 结构 ID **不是** 通过相似性匹配找到的，而是:
1. Pipeline 从 **ChEMBL API** 查询靶点交叉引用 (`target_component_xrefs`)
2. 如果发现 `xref_src_db == "AlphaFoldDB"` → 标记 `has_alphafold = True`
3. AlphaFold ID 按官方命名规则拼接: `AF-{UniProt}-F1`
4. 例如 UniProt=P30556 → AlphaFold ID = `AF-P30556-F1`
5. 可直接在 https://alphafold.ebi.ac.uk/entry/{UniProt} 查看预测结构

#### 结构来源分类
| 标签 | 含义 | 对接策略 |
|------|------|---------|
| `PDB+AlphaFold` | 有实验结构 + AlphaFold预测 | 优先用PDB |
| `PDB` | 仅有实验结构 | 用PDB |
| `AlphaFold_only` | 仅有预测结构 | 用AlphaFold (需谨慎) |
| `none` | 无结构数据 | 无法对接 |

---

## 10. 完整数据流图

```
┌─────────────────────────────────────────────────────────────────┐
│                    Direction A: 跨疾病迁移                       │
│                                                                 │
│  Step 1: dsmeta/archs4  →  疾病基因签名 (up/down genes)         │
│             ↓ (如dsmeta失败自动回退archs4, 再回退OT-only)        │
│  Step 2: sigreverse     →  LINCS反向匹配药物排名                 │
│  Step 3: kg_explain     →  Drug-Target-Pathway-Disease 机制链路  │
│             ↓               + 靶点PDB/AlphaFold/UniProt 标注     │
│          bridge_repurpose_cross.csv                              │
│             ↓                                                    │
│  Step 6: PubMed RAG     →  文献证据抽取 (per-drug dossier)       │
│  Step 7: 5维评分        →  GO / MAYBE / NO-GO 决策               │
│  Step 8: 候选包         →  step8_shortlist_topK.csv (★最终产物)  │
│  Step 9: 验证计划       →  P1/P2/P3 优先级实验设计                │
├─────────────────────────────────────────────────────────────────┤
│                    Direction B: 原疾病重评估                     │
│                                                                 │
│  Step 1: CT.gov         →  失败/终止临床试验药物                  │
│  Step 2: kg_explain     →  Drug-Target-Pathway-Disease 机制链路  │
│          bridge_origin_reassess.csv                              │
│  Step 6-9: 同上                                                  │
├─────────────────────────────────────────────────────────────────┤
│                    A+B Cross-Validation                          │
│                                                                 │
│  compare_ab_routes.py   →  ab_comparison.csv                    │
│  A-only | B-only | A+B overlap (两路线都推荐 = 最高可信)         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 11. 关联文档

- 全项目统一入口：`README.md`
- 环境安装指南：`HOW_TO_RUN.md`（Prerequisites、云服务器搭建、硬件推荐）
- 运维唯一入口：`ops/start.sh`
- 状态查看：`ops/check_status.sh`、`ops/show_results.sh`
- A+B 交叉验证：`ops/compare_ab_routes.py`
- 底层脚本（无需直接调用）：`ops/internal/`

### 子项目文档
- `kg_explain/README.md` — 知识图谱 + V5 排名
- `LLM+RAG证据工程/README.md` — PubMed RAG + 证据打分
- `dsmeta_signature_pipeline/README.md` — GEO microarray 签名
- `archs4_signature_pipeline/` — RNA-seq 备选签名
- `sigreverse/README.md` — LINCS 反向匹配
