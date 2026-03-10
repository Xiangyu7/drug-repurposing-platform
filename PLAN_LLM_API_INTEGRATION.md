# LLM API Integration Plan — Config Generation + Semantic Classification

## 目标

解决两个问题：
1. **新增疾病时的人工配置瓶颈** — EXTRA_KEYWORDS、同义词、safety blacklist 等需要人工研究和编写
2. **classify_endpoint / topic_match_ratio 的硬编码瓶颈** — CV-specific 关键词无法泛化到新疾病

## 设计原则

- **Config-time LLM, not Runtime LLM** — 贵的 API 调用只在配置阶段（每疾病一次），运行时保持现有的快速关键词匹配
- **不侵入现有 pipeline** — 新增模块，不改动 runner.sh / step6 的运行流程
- **Human-in-the-loop** — LLM 生成草稿，人 review 后写入 config
- **Provider-agnostic** — 支持 Claude API / OpenAI / Ollama，env var 切换

---

## Phase 0: LLM Provider 抽象层

### 新文件: `LLM+RAG证据工程/src/dr/llm/provider.py`

```
LLM+RAG证据工程/src/dr/llm/
├── __init__.py
├── provider.py      # LLMProvider 基类 + 工厂函数
└── prompts.py       # 所有 prompt 模板集中管理
```

### 设计

```python
class LLMProvider:
    """统一 LLM 调用接口"""
    def generate_json(self, prompt: str, schema: dict) -> dict:
        """发送 prompt，返回结构化 JSON"""
        ...

class ClaudeProvider(LLMProvider):
    """Anthropic Claude API (推荐用于 config generation)"""
    # 使用 anthropic SDK
    # 环境变量: ANTHROPIC_API_KEY, CLAUDE_MODEL (default: claude-sonnet-4-20250514)

class OpenAIProvider(LLMProvider):
    """OpenAI API (备选)"""
    # 环境变量: OPENAI_API_KEY, OPENAI_MODEL

class OllamaProvider(LLMProvider):
    """复用现有 OllamaClient (免费但质量较低)"""
    # 包装现有 ollama.py

def get_provider(provider_name: str = None) -> LLMProvider:
    """工厂函数，按 LLM_PROVIDER env var 选择"""
    # LLM_PROVIDER=claude|openai|ollama (default: claude)
```

### 环境变量 (加到 .env)

```
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-sonnet-4-20250514
```

### 为什么不直接硬编码 Claude？

未来可能换模型；Ollama 用户可以零成本试用；测试时可以 mock。
但接口极简 — 只有一个 `generate_json()` 方法，不过度抽象。

---

## Phase 1: Disease Config Generator (解决问题 1)

### 新文件: `ops/internal/generate_disease_config_llm.py`

### 功能

输入一个疾病名 + EFO ID，一次 LLM 调用生成所有配置草稿：

```bash
python ops/internal/generate_disease_config_llm.py \
  --disease "nonalcoholic steatohepatitis" \
  --disease_key nash \
  --efo_id EFO_1001249 \
  --output_dir /tmp/nash_config_draft/
```

### 输出文件 (草稿，需人 review)

```
/tmp/nash_config_draft/
├── extra_keywords.yaml          # ARCHS4 GEO 搜索关键词
├── topic_synonyms.yaml          # PubMed topic matching 同义词
├── safety_blacklist.yaml        # 安全性黑名单药物 (该疾病的标准用药)
├── serious_ae_keywords.yaml     # 严重不良事件关键词
├── endpoint_keywords.yaml       # 疾病特异性终点关键词 (Phase 2 用)
└── review_summary.md            # 人类可读的摘要 + 审核要点
```

### LLM Prompt 设计 (单次调用)

一个结构化 prompt，要求 LLM 返回 JSON，包含 6 个 section：

```
你是一位生物医学信息学专家。给定一个疾病，生成药物重定位平台所需的配置。

疾病: {disease_name}
EFO ID: {efo_id}
OpenTargets 已有同义词: {ot_synonyms}  ← 先调 OpenTargets API 获取

请生成以下 JSON:

1. extra_keywords: GEO/ARCHS4 数据库中研究者常用的实验室缩写和俗称
   - 要求: 3-12 个词，每个 3-60 字符
   - 示例: 对 NASH → ["NASH", "MASH", "NAFLD", "fatty liver", "steatosis"]
   - 注意: OpenTargets 已提供正式医学名称，这里只需补充非正式名称

2. topic_synonyms: PubMed 文献中该疾病的同义表达
   - 要求: 5-10 个词组
   - 用途: 判断一篇论文是否真的在讨论这个疾病

3. safety_blacklist_drugs: 该疾病的标准治疗药物 (不应作为重定位候选)
   - 要求: 正则表达式格式 (\\b药名\\b)
   - 示例: 对动脉粥样硬化 → ["\\batorvastatin\\b", "\\baspirin\\b"]

4. serious_ae_keywords: 该疾病语境下算"严重"的不良事件关键词
   - 要求: 5-15 个关键词
   - 示例: 对 NASH → ["hepatic failure", "cirrhosis", "variceal bleeding"]

5. endpoint_keywords: 该疾病特有的临床终点关键词 (按类型分组)
   - 格式: {endpoint_type: [keywords]}
   - 示例: 对 NASH →
     {
       "LIVER_IMAGING": ["MRI-PDFF", "elastography", "fibroscan", "CAP score"],
       "LIVER_BIOPSY": ["NAS score", "fibrosis stage", "steatosis grade", "ballooning"],
       "CLINICAL_OUTCOME": ["cirrhosis progression", "liver transplant", "hepatic decompensation"]
     }

6. mechanism_hints: 该疾病相关的机制关键词 (用于 PubMed Route 3 查询)
   - 格式: {endpoint_type: [mechanism_keywords]}
   - 示例: 对 NASH →
     {
       "LIVER_IMAGING": ["lipogenesis", "beta-oxidation", "insulin resistance"],
       "LIVER_BIOPSY": ["stellate cell", "TGF-beta", "collagen deposition"]
     }
```

### 与现有系统的集成方式

生成后，脚本自动:

1. **EXTRA_KEYWORDS** → 提示用户将内容追加到 `auto_generate_config.py` 的 EXTRA_KEYWORDS dict
   - 或者: 改为从外部 YAML 文件读取 (推荐，避免改代码)
2. **topic_synonyms** → 提示追加到 `step6` 的 `_DISEASE_TOPIC_SYNONYMS` dict
   - 或者: 改为从外部 YAML 文件读取 (推荐)
3. **safety_blacklist** → 写入 `kg_explain/configs/diseases/{disease}.yaml`
4. **serious_ae_keywords** → 写入 `kg_explain/configs/diseases/{disease}.yaml`
5. **endpoint_keywords** → Phase 2 使用

### 重构: 关键词从代码搬到数据文件

当前 EXTRA_KEYWORDS 和 _DISEASE_TOPIC_SYNONYMS 是 hardcode 在 .py 文件里的。
改为从 YAML 文件加载，LLM 直接写文件，不需要改 Python 代码：

```
ops/internal/disease_keywords/
├── extra_keywords.yaml          # 所有疾病的 ARCHS4 关键词 (替代 auto_generate_config.py 里的 dict)
├── topic_synonyms.yaml          # 所有疾病的 PubMed 同义词 (替代 step6 里的 dict)
└── endpoint_keywords.yaml       # 所有疾病的终点关键词 (新增)
```

格式示例 (`extra_keywords.yaml`):

```yaml
nash:
  - NASH
  - MASH
  - NAFLD
  - fatty liver
  - steatosis

lupus:
  - SLE
  - lupus nephritis
  - autoimmune
```

`auto_generate_config.py` 和 `step6` 改为优先从 YAML 读取，fallback 到代码内 dict (向后兼容)。

---

## Phase 2: 语义 Endpoint 分类 (解决问题 2)

### 问题分析

当前 `classify_endpoint()` 有两层:
- **CV-specific** (line 213-219): PLAQUE_IMAGING, PAD_FUNCTION, CV_EVENTS — 只对心血管有意义
- **Generic** (line 220-244): IMAGING, FUNCTIONAL, CLINICAL_OUTCOME, BIOMARKER, SURROGATE — 通用

新疾病缺少 disease-specific endpoint 类型。例如 NASH 应该有 LIVER_IMAGING, LIVER_BIOPSY。

### 方案: Config-time 生成 + Runtime 关键词匹配

**不在 runtime 调 API**，而是:

1. Phase 1 的 LLM 已生成 `endpoint_keywords.yaml`（每疾病的特异性终点关键词）
2. 修改 `classify_endpoint()` 加载该疾病的 endpoint keywords
3. 匹配顺序: disease-specific keywords → generic keywords → "OTHER"

### 修改: `step6_evidence_extraction.py`

```python
def classify_endpoint(primary_outcome_title: str, conditions: str,
                      disease_endpoint_keywords: dict = None) -> str:
    """
    分类临床终点类型。

    disease_endpoint_keywords: 从 endpoint_keywords.yaml 加载的
                               疾病特异性关键词 dict
                               e.g. {"LIVER_IMAGING": ["MRI-PDFF", ...]}
    """
    s = f"{primary_outcome_title} {conditions}".lower()

    # 1) Disease-specific endpoints (从 YAML 加载，LLM 生成)
    if disease_endpoint_keywords:
        for etype, keywords in disease_endpoint_keywords.items():
            if any(k.lower() in s for k in keywords):
                return etype

    # 2) Generic endpoints (保持现有逻辑)
    if any(k in s for k in ["ct scan", "mri", "ultrasound", ...]):
        return "IMAGING"
    if any(k in s for k in ["fev1", "spirometry", ...]):
        return "FUNCTIONAL"
    # ... 现有 generic 逻辑不变 ...

    return "OTHER"
```

### 修改: `VALID_ENDPOINTS` 动态化

```python
# 基础 endpoint 类型 (永远有效)
BASE_ENDPOINTS = {"CLINICAL_OUTCOME", "IMAGING", "FUNCTIONAL",
                  "BIOMARKER", "SURROGATE", "OTHER"}

# 从疾病 config 加载的额外 endpoint 类型
# e.g. CV: {"PLAQUE_IMAGING", "CV_EVENTS", "PAD_FUNCTION"}
# e.g. NASH: {"LIVER_IMAGING", "LIVER_BIOPSY"}
disease_endpoints = load_disease_endpoints(target_disease)

VALID_ENDPOINTS = BASE_ENDPOINTS | disease_endpoints
```

### 同步修改: ENDPOINT_QUERY + MECHANISM_HINTS_BY_ENDPOINT

这两个 dict 也需要从 YAML 加载疾病特异性条目 (Phase 1 LLM 已生成 `mechanism_hints`)：

```python
# 加载疾病特异性 endpoint query 和 mechanism hints
disease_endpoint_query = load_yaml("endpoint_keywords.yaml")[disease_key]
disease_mechanism_hints = load_yaml("mechanism_hints.yaml")[disease_key]

# 合并到全局 dict
ENDPOINT_QUERY.update({k: build_pubmed_or(v) for k, v in disease_endpoint_query.items()})
MECHANISM_HINTS_BY_ENDPOINT.update(disease_mechanism_hints)
```

### 对 topic_match_ratio() 的改进

不改算法（关键词命中比例），只改关键词来源：

```
当前: _DISEASE_TOPIC_SYNONYMS (hardcoded dict) + OpenTargets API
改后: topic_synonyms.yaml (LLM 生成) + OpenTargets API
```

`_build_disease_keywords()` 改为优先读 YAML，fallback 到 hardcoded dict。

---

## Phase 3: 集成到 start.sh workflow

### 新命令

```bash
# 为新疾病生成全套配置草稿
bash ops/start.sh gen-config --disease "nonalcoholic steatohepatitis" \
                             --key nash \
                             --efo EFO_1001249

# 输出:
# [LLM] Generating config for nash (EFO_1001249)...
# [LLM] Fetching OpenTargets synonyms...
# [LLM] Calling Claude API...
# [OK]  Draft configs written to ops/internal/disease_keywords/
#
# === REVIEW REQUIRED ===
# 1. extra_keywords.yaml   — 8 ARCHS4 search terms generated
# 2. topic_synonyms.yaml   — 7 PubMed synonyms generated
# 3. endpoint_keywords.yaml — 3 disease-specific endpoint types
# 4. safety_blacklist       — 5 standard-of-care drugs excluded
# 5. serious_ae_keywords    — 8 serious AE terms
#
# Please review and edit, then run:
#   bash ops/start.sh check-keywords --list disease_list_commercial.txt
#   bash ops/start.sh run nash
```

### 新增疾病的完整 workflow (改进后)

```
Before (人工 ~2h):
  1. 研究疾病文献 → 手写 EXTRA_KEYWORDS        (30 min)
  2. 研究 PubMed 术语 → 手写 TOPIC_SYNONYMS     (20 min)
  3. 查标准用药 → 手写 safety_blacklist          (15 min)
  4. 查不良事件 → 手写 serious_ae_keywords       (15 min)
  5. 想临床终点 → 无法添加 (hardcoded)            (N/A)
  6. 跑 check-keywords 验证                      (5 min)
  7. 添加到 disease list                         (5 min)

After (LLM + 人 review ~15min):
  1. bash ops/start.sh gen-config --disease ... --key ... --efo ...    (2 min, LLM)
  2. 打开草稿文件 review + 微调                                         (10 min, 人)
  3. bash ops/start.sh check-keywords ...                               (2 min)
  4. 添加到 disease list                                                (1 min)
```

---

## 实施顺序

```
Phase 0: LLM Provider 抽象层
├── provider.py (ClaudeProvider + OllamaProvider)
├── prompts.py (prompt 模板)
├── .env 新增 ANTHROPIC_API_KEY
└── 测试: unit test with mock

Phase 1: Disease Config Generator
├── generate_disease_config_llm.py (主脚本)
├── ops/internal/disease_keywords/*.yaml (数据文件)
├── 重构 auto_generate_config.py: EXTRA_KEYWORDS → 从 YAML 读取
├── 重构 step6: _DISEASE_TOPIC_SYNONYMS → 从 YAML 读取
└── 测试: 对已有疾病 (atherosclerosis, nash) 生成 → 与手工配置对比

Phase 2: 语义 Endpoint 分类
├── classify_endpoint() 支持 disease_endpoint_keywords 参数
├── VALID_ENDPOINTS 动态化
├── ENDPOINT_QUERY / MECHANISM_HINTS 从 YAML 加载
├── CV-specific 关键词迁移到 YAML (atherosclerosis.yaml 等)
└── 测试: 现有 CV 测试不回归 + 新疾病 endpoint 分类正确

Phase 3: start.sh 集成
├── gen-config 子命令
├── review_summary.md 生成
└── 文档更新
```

---

## 成本估算

| 操作 | 调用次数 | Token 估算 | Claude Sonnet 成本 |
|------|---------|-----------|-------------------|
| gen-config (新增疾病) | 1 次/疾病 | ~3K input + ~2K output | ~$0.02 |
| gen-config (全部 37 疾病重跑) | 37 次 | ~185K total | ~$0.74 |

**Runtime 零额外 API 成本** — 所有 LLM 调用只在 config-time 发生。

---

## 风险与缓解

| 风险 | 缓解措施 |
|------|---------|
| LLM 生成错误关键词 | human review + check-keywords 验证 |
| LLM 遗漏重要同义词 | OpenTargets API 仍在用，LLM 是补充不是替代 |
| safety blacklist 漏掉药物 | 标注为 "proposal"，HUMAN_JUDGMENT_CHECKLIST 强调必须人工确认 |
| API 不可用 | fallback 到 Ollama；最坏情况 = 手工填写 (现状) |
| endpoint_keywords 与现有 CV 逻辑冲突 | CV 的 PLAQUE_IMAGING 等迁移到 YAML，单一来源 |
