# Drug Repurposing Platform - 操作手册

更新：2026-03-03 | 37+ 种疾病 | 双签名源 (dsmeta / ARCHS4)

---

## 1. 每次跑的标准流程（只看这里就够了）

```bash
cd /path/to/Drug\ Repurposing

# ① 检查关键词（新疾病 或 上次 Cross 失败时必须做）
bash ops/start.sh check-keywords --list ops/disease_list_commercial.txt
#    → 全部 [OK] = 可以跑
#    → 有 [!!]  = 先去补 EXTRA_KEYWORDS（见第 3 节）

# ② 跑
bash ops/start.sh run atherosclerosis           # 单个疾病
bash ops/start.sh start --list your_list.txt    # 批量跑（后台）

# ③ 看状态
bash ops/start.sh status                        # 全局概览
bash ops/start.sh status --failures             # 只看失败
bash ops/start.sh status atherosclerosis        # 单个详情

# ④ 看结果
bash ops/start.sh results atherosclerosis
# 详细审核 → 见 RUN_REVIEW_CHECKLIST.md
```

> **第一次？** 先装环境：`bash ops/start.sh setup`（见第 4 节）
>
> **什么时候需要 check-keywords？**
> - 第一次跑新疾病 → **必须**
> - 上次 Cross 路线失败（签名不足）→ **必须**
> - 已经跑通过的疾病重跑 → 不需要

---

## 2. 所有命令速查

| 命令 | 用途 |
|------|------|
| `bash ops/start.sh setup` | 安装环境（只需一次） |
| `bash ops/start.sh check` | 检查环境 |
| `bash ops/start.sh check-keywords --list <file>` | 检查 ARCHS4 关键词配置 |
| `bash ops/start.sh run <disease>` | 跑单个疾病 |
| `bash ops/start.sh run <disease> --mode dual` | 跑 A+B 两条路线 |
| `bash ops/start.sh run <disease> --mode origin_only` | 只跑 Origin (B) |
| `bash ops/start.sh run <disease> --mode cross_only` | 只跑 Cross (A) |
| `bash ops/start.sh start` | 批量跑（后台） |
| `bash ops/start.sh start --list <file>` | 指定疾病列表 |
| `bash ops/start.sh status` | 全局状态 |
| `bash ops/start.sh status --latest` | 最近一轮结果 |
| `bash ops/start.sh status --failures` | 只看失败 |
| `bash ops/start.sh status <disease>` | 单个疾病详情 |
| `bash ops/start.sh status --all` | 全部检查（含 Ollama、磁盘） |
| `bash ops/start.sh results <disease>` | 查看结果 |
| `SIG_PRIORITY=dsmeta bash ops/start.sh run <disease>` | dsmeta 优先（默认 ARCHS4） |

---

## 3. 添加新疾病

### 步骤

1. 在疾病列表中加一行（格式：`disease_key|disease_name|EFO_ID|`）
2. 检查 ARCHS4 关键词：`bash ops/start.sh check-keywords --list your_list.txt`
3. 如果关键词不足（`[!!]`）：编辑 `archs4_signature_pipeline/scripts/auto_generate_config.py` 的 `EXTRA_KEYWORDS`
4. 跑：`bash ops/start.sh start --list your_list.txt`

### 疾病列表文件

| 文件 | 内容 |
|------|------|
| `ops/disease_list_commercial.txt` | 17 个商业疾病（代谢/自免/神经/纤维化/肿瘤） |
| `ops/internal/disease_list_day1_dual.txt` | 7 个心血管疾病（A+B 双路线） |
| `ops/internal/disease_list_day1_origin.txt` | 15 个心血管疾病（B 路线） |

### ARCHS4 关键词不够怎么办？

ARCHS4 从 H5 文件按关键词搜索样本。关键词来自：
- OpenTargets 同义词（自动，正式医学术语）
- `EXTRA_KEYWORDS`（手动，GEO 研究者常用的缩写/通俗术语）

如果 `check-keywords` 报 `[!!]`，说明缺少 GEO 友好术语。在 `auto_generate_config.py` 的 `EXTRA_KEYWORDS` 中加一行即可，例如：

```python
"nash": ["NASH", "MASH", "NAFLD", "fatty liver", "hepatic steatosis", "steatosis"],
```

然后删掉旧的自动生成 config，重新跑。

---

## 4. 环境安装

### 一键安装（推荐）

```bash
bash ops/start.sh setup    # 创建所有 venv + 安装依赖
bash ops/start.sh check    # 验证
```

### 系统依赖

| 软件 | 用途 | 安装 |
|------|------|------|
| Python 3.10+ | 全部模块 | 系统自带或 `apt install python3` |
| R 4.1+ | dsmeta (Direction A) | `apt install r-base r-base-dev` |
| Ollama | LLM+RAG | `curl -fsSL https://ollama.com/install.sh \| sh` |

```bash
# Ubuntu 一键装 R + Bioconductor
sudo apt install -y r-base r-base-dev libcurl4-openssl-dev libxml2-dev libssl-dev
sudo Rscript -e 'install.packages("BiocManager", repos="https://cloud.r-project.org"); BiocManager::install(c("limma","GEOquery","Biobase","affy","fgsea"))'

# Ollama 模型
ollama pull qwen2.5:7b-instruct
ollama pull nomic-embed-text
```

> 如果只跑 Direction B（origin_only），不需要 R。

### ARCHS4 H5 数据文件

Direction A 需要 `archs4_signature_pipeline/data/archs4/human_gene_v2.4.h5`（43GB）。
从 https://archs4.org 下载，或用测试文件代替：

```bash
cd archs4_signature_pipeline
python scripts/generate_test_h5.py --out data/archs4/human_gene_v2.4.h5
```

### Docker 部署

```bash
docker compose up --build              # 构建 + 启动
docker compose run app bash ops/start.sh run atherosclerosis
docker compose up -d app               # 后台运行
docker compose logs -f app             # 看日志
```

---

## 5. 监控与调试

```bash
# 看日志
tail -f logs/continuous_runner/runner_dual_*.log

# 跟踪进度
grep "PROGRESS" logs/continuous_runner/runner_*.log

# 重试失败疾病
bash ops/internal/retry_disease.sh atherosclerosis --mode dual

# 停止所有 runner
bash ops/internal/restart_runner.sh --stop

# 清理磁盘（先 dry-run）
bash ops/internal/cleanup.sh --dry-run --all 7
bash ops/internal/cleanup.sh --all 7
```

---

## 6. 输出文件（按重要性）

| 优先级 | 文件 | 说明 |
|--------|------|------|
| ★★★ | `ab_comparison.csv` | A+B 交叉验证：两路线都推荐 = 最高可信 |
| ★★★ | `step8_shortlist_topK.csv` | 最终候选药（含靶点/PDB/AlphaFold/docking） |
| ★★ | `step8_fusion_rank_report.xlsx` | Excel 报告（每药独立 Sheet） |
| ★★ | `step9_validation_plan.csv` | 验证方案（P1/P2/P3 优先级） |
| ★ | `bridge_*.csv` | KG 排名 + 靶点结构信息 |
| ★ | `step7_gating_decision.csv` | GO/MAYBE/NO-GO 决策 |

输出目录：
```
runtime/results/<disease>/<date>/<run_id>/
  ├── direction_a/    # Cross 路线
  ├── direction_b/    # Origin 路线
  └── ab_comparison.csv
```

---

## 7. 常见问题

| 问题 | 解决 |
|------|------|
| ARCHS4 找不到样本 | `check-keywords` 检查关键词 → 补 EXTRA_KEYWORDS |
| Cross 路线被跳过 | 签名基因 < 30 或无 config → 正常，Origin 照跑 |
| Ollama 连接失败 | `ollama serve &` 启动服务 |
| 某步骤超时 | 设模块级超时：`TIMEOUT_LLM_STEP6=14400 bash ops/start.sh run ...` |
| Cross 药物太少/靶点不相关 | `SIG_PRIORITY=archs4` 切换签名源 |
| 磁盘满 | `bash ops/internal/cleanup.sh --all 7` |
| venv 问题 | `bash ops/start.sh setup` 重装 |

### 超时变量

所有步骤默认 `STEP_TIMEOUT=3600`（1小时），可单独调整：

```bash
TIMEOUT_LLM_STEP6=14400 bash ops/start.sh run atherosclerosis
```

可用：`TIMEOUT_CROSS_DSMETA`, `TIMEOUT_CROSS_ARCHS4`, `TIMEOUT_CROSS_SIGREVERSE`, `TIMEOUT_CROSS_KG_SIGNATURE`, `TIMEOUT_ORIGIN_KG_CTGOV`, `TIMEOUT_LLM_STEP6/7/8/9`

---

## 8. 关联文档

| 文档 | 用途 |
|------|------|
| `项目全览.html` | 架构图、数据流、打分公式（浏览器打开） |
| `README.md` | 技术细节、代码结构、API 列表 |
| `RUN_REVIEW_CHECKLIST.md` | 跑完后的审核清单 |
| `HUMAN_JUDGMENT_CHECKLIST.md` | 需要人工判断的参数清单 |
