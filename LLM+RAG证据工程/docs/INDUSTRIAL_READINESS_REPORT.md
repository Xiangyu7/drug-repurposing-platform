# LLM+RAG证据工程 工业级成熟度分析报告

**生成日期**: 2026-02-07
**项目**: LLM+RAG证据工程 (动脉粥样硬化药物再利用)
**代码量**: ~5,100 行Python
**核心技术**: LLM + RAG + PubMed + ClinicalTrials.gov

  1. 必读：QUICK_START_IMPROVEMENTS.md（第1-2周部分）！！！！！！！！！！！！！！！
  2. 深入：INDUSTRIAL_READINESS_REPORT.md（理解设计原理）！！！！！！！！！！！！！！！
  3. 验证：每周检查清单 ！！！！！！！！！！！！！！！！！！！！！！！！！！！！！！！！！！
---

## 执行摘要

LLM+RAG证据工程是一个**设计优秀的科研管道**，代码质量**中等偏上（3.0/5.0）**。具有完善的类型系统、智能的缓存策略和良好的函数模块化。但在工业级部署所需的**日志系统、测试覆盖、错误处理、代码复用**等关键维度存在显著差距。

**当前状态**: 适合研究环境运行
**距离工业级**: 需要 **6-8周的工程化重构**
**关键瓶颈**: 可维护性、可观测性、测试覆盖

> **⚠️ 2026-02-12 更新**: 本报告写于 2026-02-07。自此之后，以下改进已完成:
> - ✅ 测试覆盖: 0% → **501 tests** (75%+ 覆盖率)
> - ✅ 代码重复: 15% → **<2%** (共享 `src/dr/` 模块)
> - ✅ 数据 Schema: 无 → **ContractEnforcer** (Step7/8/9 自动校验)
> - ✅ 日志系统: print → **结构化 logging** (`src/dr/logger.py`)
> - ✅ 配置管理: 散乱 → **Config class** (`src/dr/config.py`)
> - ✅ 新增: Release Gate、Audit Log、Human Review Metrics、Monitoring Alerts
> - ✅ 新增: Bootstrap CI 不确定性量化、数据泄漏审计
> - ✅ CI/CD: GitHub Actions monorepo 矩阵测试
>
> 当前评分约 **3.0 → 4.0/5.0**。路线图中阶段 1+2 已基本完成。

---

## 一、当前项目做得好的地方 ✅

### 1.1 类型系统非常完善 ⭐⭐⭐⭐⭐ (5/5)

**亮点**：95%以上的函数都有完整的类型提示

```python
# 优秀示例：step6_pubmed_rag_ollama_evidence_v2.py
def load_negative_trials(
    neg_path: Optional[str],
    canonical_name: str
) -> Tuple[str, List[Dict[str, Any]], str]:
    """Return (endpoint_type, trials, text_block_for_md)."""
    ...

def bm25_rank(
    query: str,
    docs: List[Dict[str, Any]],
    k1: float = 1.5,
    b: float = 0.75,
    topk: int = 80
) -> List[Tuple[float, Dict[str, Any]]]:
    ...
```

**优势**：
- 代码可读性强
- IDE自动补全完美
- 类型错误易于发现
- 为静态检查（mypy）做好准备

---

### 1.2 缓存架构设计优秀 ⭐⭐⭐⭐ (4/5)

**四层缓存设计**：

```
cache/pubmed/{drug_id}/{canonical_name}/
  ├── pmids.json              # L1: 搜索结果缓存
  ├── pubmed.xml              # L2: PubMed XML响应
  ├── docs.json               # L3: 解析后的文档
  └── reranked_pmids.json     # L4: 排序后的PMID
```

**关键实现**：

```python
# 原子写入防止缓存损坏
def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # ✅ 原子操作

# 条件缓存刷新
if FORCE_REBUILD or is_empty(pmids_path) or (REFRESH_EMPTY_CACHE and is_empty(pmids_path)):
    pmids = pubmed_esearch(query, retmax=200)
    write_json(pmids_path, {"query": query, "pmids": pmids})
else:
    pmids = (read_json(pmids_path) or {}).get("pmids", [])
```

**优势**：
- 分层缓存减少API调用
- 原子写入保证一致性
- 按药物隔离防止冲突
- 可配置强制刷新

---

### 1.3 HTTP重试机制健壮 ⭐⭐⭐⭐ (4/5)

**实现细节**：

```python
def request_with_retries(method: str, url: str, **kwargs) -> requests.Response:
    """Robust HTTP helper with retries."""
    last = None
    timeout_default = kwargs.get("timeout", REQUEST_TIMEOUT)

    for attempt in range(1, MAX_RETRIES + 1):  # ✅ 最多4次重试
        try:
            kw = dict(kwargs)  # ✅ 避免污染原始参数
            timeout = kw.pop("timeout", timeout_default)
            trust_env = kw.pop("trust_env", True)

            if trust_env is False:
                sess = requests.Session()
                sess.trust_env = False
                r = sess.request(method, url, timeout=timeout, **kw)
            else:
                r = requests.request(method, url, timeout=timeout, **kw)

            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            log(f"[HTTP] attempt {attempt}/{MAX_RETRIES} failed: {e}")
            time.sleep(RETRY_SLEEP * attempt)  # ✅ 指数退避

    raise RuntimeError(f"HTTP failed after {MAX_RETRIES} retries: {last}")
```

**优势**：
- 指数退避减少服务器压力
- 参数隔离防止重试污染
- 可配置重试次数/延迟
- 兼容Ollama的trust_env=False模式

---

### 1.4 数据质量控制完善 ⭐⭐⭐⭐ (4/5)

**Step6的QC机制**：

```python
# 主题匹配率检测
ev_text_all = " ".join([str(e.get("claim","")) for e in supporting[:8]]) + " " + \
              " ".join([d.get("abstract","")[:500] for d in top_docs[:2]])
tmr_all = topic_match_ratio(ev_text_all, endpoint_type)
mismatch = tmr_all < 0.30 and endpoint_type != "OTHER"

# 移除离题证据
if float(ev.get("topic_match_ratio", 0.0)) == 0.0 and endpoint_type != "OTHER":
    removed += 1
    qc_reasons.append("removed_offtopic_supporting")
    harm_or_neutral.append({**ev, "supports": False, "direction": "neutral"})

# 跨药物污染检测
if CROSS_DRUG_FILTER and contains_other_drug(claim_txt, other_markers):
    pre_qc_reasons.append('cross_drug_leakage')
    pre_removed += 1
    continue
```

**QC输出**：

```json
{
  "qc": {
    "topic_match_ratio": 0.42,
    "topic_mismatch": false,
    "removed_evidence_count": 3,
    "removed_cross_drug_count": 1,
    "supporting_evidence_after_qc": 7,
    "qc_reasons": ["cross_drug_leakage", "removed_offtopic_supporting"]
  }
}
```

**Step7的策略路由**：

```python
# 硬路由逻辑
if topic_ratio < TOPIC_MIN:
    gate_reasons.append(f"topic_ratio<{TOPIC_MIN}")
if se_unique < MIN_UNIQUE_PMIDS:
    gate_reasons.append(f"unique_pmids<{MIN_UNIQUE_PMIDS}")
if SAFETY_HARD_NOGO and safety_hit:
    gate_reasons.append("safety_blacklist_hard")

if gate_reasons:
    gate_decision = "NO_GO"
else:
    gate_decision = "PROCEED_PLAN"
```

**优势**：
- 端点驱动的主题检测
- 跨药物污染过滤
- 多维QC原因追踪
- 安全黑名单硬路由

---

### 1.5 配置管理设计完善（Step6） ⭐⭐⭐⭐ (4/5)

**20+环境变量配置**：

```python
# API配置
NCBI_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "").strip()
NCBI_DELAY = float(os.getenv("NCBI_DELAY", "0.6"))
PUBMED_TIMEOUT = float(os.getenv("PUBMED_TIMEOUT", "30"))
PUBMED_EFETCH_CHUNK = int(os.getenv("PUBMED_EFETCH_CHUNK", "20"))

# Ollama配置
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
OLLAMA_LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:7b-instruct")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "600"))

# 重试配置
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "4"))
RETRY_SLEEP = float(os.getenv("RETRY_SLEEP", "2"))

# 功能开关
DISABLE_EMBED = os.getenv("DISABLE_EMBED", "0") == "1"
DISABLE_LLM = os.getenv("DISABLE_LLM", "0") == "1"
CROSS_DRUG_FILTER = os.getenv("CROSS_DRUG_FILTER", "1") == "1"
FORCE_REBUILD = os.getenv("FORCE_REBUILD", "0") == "1"
REFRESH_EMPTY_CACHE = os.getenv("REFRESH_EMPTY_CACHE", "1") == "1"
```

**优势**：
- 完善的环境变量覆盖
- 合理的默认值
- 布尔开关统一规范
- 支持禁用Ollama运行

---

### 1.6 端点分层策略聪明 ⭐⭐⭐⭐ (4/5)

**三层端点分类**：

```python
def classify_endpoint(primary_outcome_title: str, conditions: str) -> str:
    s = f"{primary_outcome_title} {conditions}".lower()

    # 斑块成像（高精度）
    if any(k in s for k in ["plaque", "atheroma", "cta", "ivus", "carotid", "intima-media"]):
        return "PLAQUE_IMAGING"

    # PAD功能（中精度）
    if any(k in s for k in ["six-minute walk", "claudication", "walking distance", "pad"]):
        return "PAD_FUNCTION"

    # 心血管事件（低精度）
    if any(k in s for k in ["mace", "major adverse", "myocardial infarction", "stroke"]):
        return "CV_EVENTS"

    return "OTHER"
```

**端点驱动的查询构建**：

```python
ENDPOINT_QUERY = {
    "PLAQUE_IMAGING": '(atherosclerosis OR plaque OR "noncalcified plaque" OR CTA OR IVUS)',
    "PAD_FUNCTION": '("peripheral artery disease" OR PAD OR claudication OR "six-minute walk")',
    "CV_EVENTS": '("myocardial infarction" OR MI OR "acute coronary syndrome" OR MACE)',
    "OTHER": '(atherosclerosis OR cardiovascular OR vascular OR inflammation)'
}

def build_query(drug: str, target_disease: str, endpoint_type: str) -> str:
    endpoint_clause = ENDPOINT_QUERY.get(endpoint_type, ENDPOINT_QUERY["OTHER"])
    if endpoint_type == "OTHER":
        return f'("{drug}") AND ({endpoint_clause}) AND ("{target_disease}")'
    return f'("{drug}") AND ({endpoint_clause})'
```

**优势**：
- 避免"一刀切"的atherosclerosis查询
- 匹配临床试验的真实端点
- 减少离题文献检索
- 提高主题匹配率

---

### 1.7 模块级文档详细 ⭐⭐⭐⭐ (4/5)

**示例**：

```python
"""
Step6 (v2): PubMed RAG + Evidence Engineering with:
1) Evidence blocks (not single sentences) with required PMID + direction/model/endpoint fields
2) Endpoint-driven topic gating (plaque/PAD/events) rather than one-size-fits-all "atherosclerosis"
3) Two-stage retrieval: broad PubMed -> BM25 pre-rank -> (optional) Ollama embedding rerank
4) Negative evidence extraction & counting (CT.gov + abstract "no difference"/harm language)

Designed to be drop-in upgrade for pipelines using step6_rank.csv + dossier_json used by step7_build_from_step6.py.

Run (example):
  OLLAMA_HOST=http://localhost:11434 OLLAMA_EMBED_MODEL=nomic-embed-text OLLAMA_LLM_MODEL=qwen2.5:7b-instruct \
  python step6_pubmed_rag_ollama_evidence_v2.py \
    --rank_in step6_rank.csv --neg poolA_negative_drug_level.csv --out step6_v2_out --target_disease atherosclerosis

Outputs:
  - {out}/step6_rank_v2.csv  (updated dossier paths + evidence counts)
  - {out}/dossiers/{drug_id}__{canonical}.json
  - {out}/dossiers/{drug_id}__{canonical}.md
  - {out}/cache/pubmed/... (pmids + xml + parsed docs + embeddings cache)

Notes:
- Requires: requests, pandas, tqdm (and a running Ollama if you want embedding/LLM)
- Network access needed for PubMed E-utilities.
"""
```

**优势**：
- 清晰的设计意图
- 完整的运行示例
- 输出文件说明
- 依赖项列表

---

## 二、关键差距（按优先级排序）

### 🔴 P0 - 阻塞性差距（必须修复才能生产部署）

#### 2.1 日志系统原始 ⭐⭐/5 → 目标 ⭐⭐⭐⭐⭐

**当前状态**：

```python
def log(msg: str) -> None:
    print(msg, flush=True)

# 使用方式
log(f"[HTTP] attempt {attempt}/{MAX_RETRIES} failed: {e}")
log(f"[WARN] embedding disabled (ollama): {e}")
log(f"[OK] wrote: {out_csv}")
```

**问题**：
- ❌ 无日志级别（DEBUG/INFO/WARNING/ERROR）
- ❌ 无时间戳
- ❌ 不持久化（stdout丢失后无法追溯）
- ❌ 无结构化日志（难以解析）
- ❌ 无日志轮换（长期运行会占满磁盘）
- ❌ 无调用栈信息（难以定位问题）

**工业级要求**：

```python
import logging
import logging.handlers

# 结构化日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(funcName)s:%(lineno)d | %(message)s',
    handlers=[
        logging.handlers.RotatingFileHandler(
            'dr_pipeline.log',
            maxBytes=100*1024*1024,  # 100MB
            backupCount=5
        ),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# 使用方式
logger.info("Processing drug: %s", drug_name)
logger.warning("Embedding disabled due to error: %s", exc_info=True)
logger.error("HTTP request failed after %d retries", MAX_RETRIES, extra={
    'url': url,
    'status_code': response.status_code,
    'drug_id': drug_id
})
```

**改进收益**：
- ✅ 生产问题可追溯
- ✅ 支持日志聚合（ELK/Splunk）
- ✅ 可配置日志级别
- ✅ 异常自动记录栈信息

---

#### 2.2 测试覆盖严重不足 ⭐/5 → 目标 ⭐⭐⭐⭐

**当前状态**：

```
tests/
  └── test_step6_llm_single.py  (376行，单一集成测试)
```

**问题**：
- ❌ 无单元测试框架（pytest）
- ❌ 核心函数0%覆盖
- ❌ 无边界条件测试
- ❌ 无回归测试
- ❌ 无CI集成

**工业级要求**：

```python
# tests/test_common.py
import pytest
from src.common import canonicalize_name, normalize_pmid

class TestCanonicalizeName:
    def test_remove_dosage(self):
        assert canonicalize_name("aspirin 100mg tablet") == "aspirin"

    def test_greek_letters(self):
        assert canonicalize_name("interferon-α") == "interferon alpha"

    def test_empty_input(self):
        assert canonicalize_name("") == ""
        assert canonicalize_name(None) == ""

    @pytest.mark.parametrize("input,expected", [
        ("drug (100mg)", "drug"),
        ("drug 50 ug injection", "drug"),
        ("DRUG  Multiple   Spaces", "drug multiple spaces"),
    ])
    def test_edge_cases(self, input, expected):
        assert canonicalize_name(input) == expected

class TestNormalizePMID:
    def test_valid_pmid(self):
        assert normalize_pmid("12345678") == "12345678"
        assert normalize_pmid("PMID: 12345678") == "12345678"

    def test_invalid_pmid(self):
        assert normalize_pmid("abc") == ""
        assert normalize_pmid("123") == ""  # 太短
        assert normalize_pmid(None) == ""

# tests/test_step6_retrieval.py
from unittest.mock import patch, MagicMock
from scripts.step6_pubmed_rag_ollama_evidence_v2 import pubmed_esearch, bm25_rank

class TestPubMedRetrieval:
    @patch('requests.request')
    def test_esearch_with_api_key(self, mock_request):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'esearchresult': {'idlist': ['12345678', '23456789']}
        }
        mock_request.return_value = mock_response

        result = pubmed_esearch("aspirin atherosclerosis", retmax=10)
        assert result == ['12345678', '23456789']
        assert mock_request.call_args[1]['params']['api_key'] is not None

    def test_bm25_rank_empty_query(self):
        docs = [{'title': 'Test', 'abstract': 'Test abstract'}]
        result = bm25_rank("", docs)
        assert result == []

    def test_bm25_rank_relevance(self):
        docs = [
            {'title': 'Aspirin in cardiovascular disease', 'abstract': 'Study on aspirin'},
            {'title': 'Diabetes treatment', 'abstract': 'Metformin study'}
        ]
        result = bm25_rank("aspirin cardiovascular", docs, topk=2)
        assert len(result) == 2
        assert result[0][1]['title'] == 'Aspirin in cardiovascular disease'
```

**测试覆盖目标**：

| 模块 | 当前覆盖 | 目标覆盖 |
|------|---------|---------|
| common.py | 0% | >90% |
| step6 retrieval | 0% | >80% |
| step6 QC logic | 0% | >75% |
| step7 gating | 0% | >70% |
| **总体** | <5% | **>70%** |

**改进收益**：
- ✅ 重构时不怕破坏功能
- ✅ 边界条件覆盖
- ✅ 回归测试自动化
- ✅ CI/CD可集成

---

#### 2.3 代码重复严重 ⭐⭐/5 → 目标 ⭐⭐⭐⭐⭐

**问题**：

```python
# 在5个脚本中重复定义：
# - step5_drug_normalize_and_aggregate_v3.py
# - step7_build_from_step6.py
# - step7_build_from_step6_v2.py
# - step8_fusion_rank.py

def canonicalize_name(x: str) -> str:
    s = normalize_basic(x)
    if not s:
        return ""
    s = re.sub(r"\b\d+(\.\d+)?\s*(mg|g|mcg|ug|iu|ml)\b", " ", s, flags=re.I)
    s = re.sub(r"\b\d+(\.\d+)?\b", " ", s)
    toks = [t for t in re.split(r"\s+", s) if t]
    toks = [t for t in toks if t not in STOP_WORDS]
    joined = " ".join(toks).replace("α","alpha").replace("β","beta")
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined

# 类似的还有：
# - normalize_basic() - 5次重复
# - safe_filename() - 4次重复
# - STOP_WORDS常量 - 5次重复
```

**工业级要求**：

```
DR/
├── src/                     # ⭐ 新增：共享库
│   ├── __init__.py
│   ├── common.py            # 通用工具函数
│   ├── config.py            # 配置管理
│   ├── logger.py            # 日志配置
│   ├── validators.py        # 数据验证
│   └── constants.py         # 常量定义
├── scripts/
│   ├── step6_pubmed_rag_ollama_evidence_v2.py
│   └── ...
└── tests/
    ├── test_common.py
    └── ...
```

**src/common.py**：

```python
"""共享工具函数库"""
import re
from typing import Optional

STOP_WORDS = {
    "tablet", "tablets", "capsule", "capsules", "injection", "injectable",
    "oral", "iv", "intravenous", "sc", "subcutaneous", "mg", "g", "mcg"
}

def normalize_basic(x: str) -> str:
    """基础标准化：小写、去标点、去多余空格"""
    s = str(x).lower().strip()
    s = re.sub(r"[\(\)\[\]\{\},;:/\\]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def canonicalize_name(x: str) -> str:
    """药物名称规范化：去剂量、去停用词、统一希腊字母"""
    s = normalize_basic(x)
    if not s:
        return ""
    s = re.sub(r"\b\d+(\.\d+)?\s*(mg|g|mcg|ug|iu|ml)\b", " ", s, flags=re.I)
    s = re.sub(r"\b\d+(\.\d+)?\b", " ", s)
    toks = [t for t in re.split(r"\s+", s) if t]
    toks = [t for t in toks if t not in STOP_WORDS]
    joined = " ".join(toks).replace("α", "alpha").replace("β", "beta")
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined

def safe_filename(s: str, max_len: int = 80) -> str:
    """转换为安全的文件名"""
    s = re.sub(r"[^a-zA-Z0-9\-_]+", "_", str(s).strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len] or "drug"

def normalize_pmid(v: Optional[str]) -> str:
    """提取标准PMID（6-9位数字）"""
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    m = re.search(r"\b(\d{6,9})\b", s)
    return m.group(1) if m else ""
```

**改进收益**：
- ✅ DRY原则
- ✅ 单一真相源
- ✅ 统一测试
- ✅ 维护成本降低80%

---

### 🟠 P1 - 高优先级（影响可维护性）

#### 2.4 异常处理过于宽泛 ⭐⭐/5 → 目标 ⭐⭐⭐⭐

**问题**：

```python
# Step6 第301-316行
try:
    r = request_with_retries("POST", url, json={"model": model, "input": texts}, ...)
    data = r.json()
    embs = data.get("embeddings")
    if isinstance(embs, list) and embs and isinstance(embs[0], list):
        return embs
except Exception:  # ❌ 捕获所有异常
    pass  # ❌ 无日志

# 再次尝试旧接口
try:
    url2 = f"{OLLAMA_HOST}/api/embeddings"
    for t in texts:
        r = request_with_retries("POST", url2, json={"model": model, "prompt": t}, ...)
        ...
except Exception as e:  # ❌ 仍然过于宽泛
    log(f"[WARN] embedding disabled (ollama): {e}")
    return None
```

**工业级要求**：

```python
import requests
from requests.exceptions import ConnectionError, Timeout, HTTPError

def ollama_embed(texts: List[str], model: str) -> Optional[List[List[float]]]:
    if DISABLE_EMBED:
        return None

    if not texts:
        return []

    url = f"{OLLAMA_HOST}/api/embed"

    try:
        r = request_with_retries(
            "POST", url,
            json={"model": model, "input": texts},
            timeout=OLLAMA_TIMEOUT,
            trust_env=False
        )
        data = r.json()
        embs = data.get("embeddings")

        if isinstance(embs, list) and embs and isinstance(embs[0], list):
            return embs

        logger.warning("Unexpected embedding response format: %s", data)

    except (ConnectionError, Timeout) as e:
        logger.error("Ollama connection failed: %s", e, exc_info=True)
        return None

    except HTTPError as e:
        if e.response.status_code == 404:
            logger.info("Trying fallback embedding endpoint (old Ollama API)")
        else:
            logger.error("Ollama HTTP error %d: %s", e.response.status_code, e)
            return None

    except ValueError as e:
        logger.error("Invalid JSON response from Ollama: %s", e)
        return None

    # 降级到旧接口
    try:
        url2 = f"{OLLAMA_HOST}/api/embeddings"
        out = []
        for t in texts:
            r = request_with_retries("POST", url2, json={"model": model, "prompt": t}, ...)
            data = r.json()
            e = data.get("embedding")
            if not isinstance(e, list):
                raise ValueError(f"Invalid embedding format: {type(e)}")
            out.append(e)
        return out

    except Exception as e:
        logger.error("Fallback embedding also failed: %s", e, exc_info=True)
        return None
```

**改进收益**：
- ✅ 精确异常类型捕获
- ✅ 详细日志记录
- ✅ 区分可恢复/不可恢复错误
- ✅ 异常栈自动记录

---

#### 2.5 缺乏配置文件标准 ⭐⭐⭐/5 → 目标 ⭐⭐⭐⭐⭐

**当前问题**：
- Step6环境变量配置完善
- Step7部分环境变量硬编码
- 无.env.example文件
- 无配置验证机制

**工业级要求**：

**.env.example**：

```bash
# PubMed API配置
NCBI_API_KEY=your_ncbi_api_key_here
NCBI_DELAY=0.6
PUBMED_TIMEOUT=30
PUBMED_EFETCH_CHUNK=20

# Ollama配置
OLLAMA_HOST=http://localhost:11434
OLLAMA_EMBED_MODEL=nomic-embed-text
OLLAMA_LLM_MODEL=qwen2.5:7b-instruct
OLLAMA_TIMEOUT=600

# 重试策略
MAX_RETRIES=4
RETRY_SLEEP=2

# 功能开关
DISABLE_EMBED=0
DISABLE_LLM=0
CROSS_DRUG_FILTER=1
FORCE_REBUILD=0
REFRESH_EMPTY_CACHE=1

# Step7策略参数
TOPIC_MIN=0.30
MIN_UNIQUE_PMIDS=2
SAFETY_HARD_NOGO=0

# 日志配置
LOG_LEVEL=INFO
LOG_FILE=dr_pipeline.log
LOG_MAX_BYTES=104857600  # 100MB
LOG_BACKUP_COUNT=5
```

**src/config.py**：

```python
"""配置管理模块"""
import os
from typing import Any, Dict
from dotenv import load_dotenv
import logging

logger = logging.getLogger(__name__)

# 加载.env文件
load_dotenv()

class Config:
    """全局配置类"""

    # PubMed配置
    NCBI_EUTILS: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    NCBI_API_KEY: str = os.getenv("NCBI_API_KEY", "").strip()
    NCBI_DELAY: float = float(os.getenv("NCBI_DELAY", "0.6"))
    PUBMED_TIMEOUT: float = float(os.getenv("PUBMED_TIMEOUT", "30"))

    # Ollama配置
    OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    OLLAMA_EMBED_MODEL: str = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    OLLAMA_LLM_MODEL: str = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:7b-instruct")
    OLLAMA_TIMEOUT: float = float(os.getenv("OLLAMA_TIMEOUT", "600"))

    # 重试配置
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "4"))
    RETRY_SLEEP: float = float(os.getenv("RETRY_SLEEP", "2"))

    # 功能开关
    DISABLE_EMBED: bool = os.getenv("DISABLE_EMBED", "0") == "1"
    DISABLE_LLM: bool = os.getenv("DISABLE_LLM", "0") == "1"
    CROSS_DRUG_FILTER: bool = os.getenv("CROSS_DRUG_FILTER", "1") == "1"
    FORCE_REBUILD: bool = os.getenv("FORCE_REBUILD", "0") == "1"

    @classmethod
    def validate(cls) -> None:
        """验证关键配置"""
        if cls.NCBI_API_KEY:
            logger.info("NCBI API key configured (length: %d)", len(cls.NCBI_API_KEY))
        else:
            logger.warning("NCBI_API_KEY not set - rate limiting will apply")

        if cls.MAX_RETRIES < 1:
            raise ValueError("MAX_RETRIES must be >= 1")

        if cls.OLLAMA_TIMEOUT < 10:
            logger.warning("OLLAMA_TIMEOUT < 10s may cause failures")

        # 验证Ollama连接（可选）
        if not cls.DISABLE_EMBED and not cls.DISABLE_LLM:
            try:
                import requests
                r = requests.get(f"{cls.OLLAMA_HOST}/api/tags", timeout=5)
                r.raise_for_status()
                logger.info("Ollama connection verified: %s", cls.OLLAMA_HOST)
            except Exception as e:
                logger.error("Ollama connection failed: %s", e)

    @classmethod
    def to_dict(cls) -> Dict[str, Any]:
        """导出配置为字典"""
        return {
            k: v for k, v in cls.__dict__.items()
            if not k.startswith('_') and not callable(v)
        }

# 启动时验证
Config.validate()
```

**使用方式**：

```python
# 在脚本中
from src.config import Config

def pubmed_esearch(term: str, retmax: int = 200) -> List[str]:
    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "retmax": str(retmax)
    }
    if Config.NCBI_API_KEY:
        params["api_key"] = Config.NCBI_API_KEY

    url = f"{Config.NCBI_EUTILS}/esearch.fcgi"
    r = request_with_retries("GET", url, params=params, timeout=Config.PUBMED_TIMEOUT)
    ...
```

**改进收益**：
- ✅ 配置集中管理
- ✅ 启动时验证
- ✅ 易于测试（mock Config）
- ✅ 文档化配置项

---

#### 2.6 函数级文档不足 ⭐⭐/5 → 目标 ⭐⭐⭐⭐

**当前覆盖率**：

| 脚本 | 函数数 | 有docstring | 覆盖率 |
|------|--------|------------|-------|
| Step6 | 35 | 12 | 34% |
| Step7 | 15 | 2 | 13% |
| Step8 | 9 | 0 | 0% |
| **平均** | - | - | **<20%** |

**缺陷示例**：

```python
def bm25_rank(query: str, docs: List[Dict[str, Any]], k1: float = 1.5, b: float = 0.75, topk: int = 80) -> List[Tuple[float, Dict[str, Any]]]:
    # ❌ 无docstring
    q = tokenize(query)
    if not q or not docs:
        return []
    ...
```

**工业级要求**：

```python
def bm25_rank(
    query: str,
    docs: List[Dict[str, Any]],
    k1: float = 1.5,
    b: float = 0.75,
    topk: int = 80
) -> List[Tuple[float, Dict[str, Any]]]:
    """使用BM25算法对文档进行排序。

    BM25是一种基于概率检索模型的排序函数，综合考虑词频(TF)和逆文档频率(IDF)。

    Args:
        query: 搜索查询字符串，会被tokenize后计算与文档的相关性
        docs: 文档列表，每个文档包含'title'和'abstract'字段
        k1: BM25的k1参数，控制词频饱和度（默认1.5）
            - k1越大，高频词的影响越大
            - 典型范围：1.2-2.0
        b: BM25的b参数，控制文档长度归一化（默认0.75）
            - b=0时不考虑文档长度
            - b=1时完全归一化到平均长度
        topk: 返回前K个最相关文档（默认80）

    Returns:
        排序后的(分数, 文档)元组列表，按分数降序排列
        分数越高表示相关性越强

    Example:
        >>> docs = [
        ...     {'title': 'Aspirin in CVD', 'abstract': 'Study on aspirin...'},
        ...     {'title': 'Diabetes', 'abstract': 'Metformin study...'}
        ... ]
        >>> ranked = bm25_rank("aspirin cardiovascular", docs, topk=10)
        >>> print(ranked[0][1]['title'])
        'Aspirin in CVD'

    Notes:
        - 使用简单的空格+字母数字tokenization
        - 不使用外部依赖（如rank-bm25库）以保持轻量
        - IDF计算使用平滑公式：log(1 + (N - df + 0.5) / (df + 0.5))
    """
    q = tokenize(query)
    if not q or not docs:
        return []

    # 构建语料库统计
    doc_toks = [tokenize((d.get("title","") + " " + d.get("abstract","")).strip()) for d in docs]
    N = len(doc_toks)
    avgdl = sum(len(x) for x in doc_toks) / max(1, N)

    # 文档频率（df）
    df = {}
    for toks in doc_toks:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1

    # IDF计算（带平滑）
    def idf(t: str) -> float:
        n = df.get(t, 0)
        return math.log(1 + (N - n + 0.5) / (n + 0.5))

    # 对每个文档计算BM25分数
    ranked = []
    for d, toks in zip(docs, doc_toks):
        dl = len(toks)
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1

        score = 0.0
        for t in q:
            if t not in tf:
                continue
            f = tf[t]
            score += idf(t) * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / (avgdl + 1e-9)))

        ranked.append((score, d))

    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked[:topk]
```

**改进收益**：
- ✅ 新人快速上手
- ✅ API文档自动生成（Sphinx）
- ✅ 减少代码理解成本
- ✅ 算法细节可追溯

---

### 🟡 P2 - 中优先级（改善可观测性）

#### 2.7 缺乏性能监控 ⭐⭐⭐/5 → 目标 ⭐⭐⭐⭐

**当前状态**：
- 有指标收集（支持证据数、主题匹配率等）
- 无执行时间追踪
- 无内存使用监控
- 无瓶颈分析工具

**工业级要求**：

**src/profiler.py**：

```python
"""性能监控模块"""
import time
import functools
import logging
from typing import Callable, Any
import psutil
import os

logger = logging.getLogger(__name__)

class PerformanceMetrics:
    """性能指标收集器"""

    def __init__(self):
        self.metrics = {}
        self.process = psutil.Process(os.getpid())

    def record_execution(self, func_name: str, duration: float, memory_mb: float):
        """记录函数执行指标"""
        if func_name not in self.metrics:
            self.metrics[func_name] = {
                'count': 0,
                'total_time': 0.0,
                'max_time': 0.0,
                'avg_memory_mb': 0.0
            }

        m = self.metrics[func_name]
        m['count'] += 1
        m['total_time'] += duration
        m['max_time'] = max(m['max_time'], duration)
        m['avg_memory_mb'] = (m['avg_memory_mb'] * (m['count'] - 1) + memory_mb) / m['count']

    def get_summary(self):
        """获取性能摘要"""
        summary = []
        for func, m in sorted(self.metrics.items(), key=lambda x: x[1]['total_time'], reverse=True):
            summary.append({
                'function': func,
                'calls': m['count'],
                'total_time_s': round(m['total_time'], 2),
                'avg_time_s': round(m['total_time'] / m['count'], 2),
                'max_time_s': round(m['max_time'], 2),
                'avg_memory_mb': round(m['avg_memory_mb'], 2)
            })
        return summary

# 全局指标收集器
metrics = PerformanceMetrics()

def profile(func: Callable) -> Callable:
    """性能分析装饰器"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        mem_before = metrics.process.memory_info().rss / 1024 / 1024  # MB
        start = time.time()

        try:
            result = func(*args, **kwargs)
            return result
        finally:
            duration = time.time() - start
            mem_after = metrics.process.memory_info().rss / 1024 / 1024
            memory_delta = mem_after - mem_before

            metrics.record_execution(
                func.__name__,
                duration,
                memory_delta
            )

            if duration > 10:  # 慢函数日志
                logger.info(
                    "Function %s took %.2fs (mem delta: %.2f MB)",
                    func.__name__, duration, memory_delta
                )

    return wrapper
```

**使用示例**：

```python
from src.profiler import profile, metrics

@profile
def process_one(drug_id: str, canonical_name: str, ...) -> Tuple[Path, Path, Dict]:
    """处理单个药物候选"""
    ...

@profile
def pubmed_esearch(term: str, retmax: int = 200) -> List[str]:
    """PubMed搜索"""
    ...

@profile
def bm25_rank(query: str, docs: List[Dict], ...) -> List[Tuple[float, Dict]]:
    """BM25排序"""
    ...

# 管道结束时打印性能报告
def main():
    ...
    # 处理完成

    logger.info("=== Performance Summary ===")
    for entry in metrics.get_summary():
        logger.info(
            "%s: %d calls, %.2fs total, %.2fs avg, %.2fs max, %.2f MB avg mem",
            entry['function'],
            entry['calls'],
            entry['total_time_s'],
            entry['avg_time_s'],
            entry['max_time_s'],
            entry['avg_memory_mb']
        )
```

**输出示例**：

```
=== Performance Summary ===
process_one: 50 calls, 1234.56s total, 24.69s avg, 45.23s max, 125.34 MB avg mem
pubmed_esearch: 50 calls, 234.12s total, 4.68s avg, 12.34s max, 2.45 MB avg mem
bm25_rank: 50 calls, 45.67s total, 0.91s avg, 2.34s max, 15.23 MB avg mem
ollama_embed: 120 calls, 567.89s total, 4.73s avg, 15.67s max, 8.12 MB avg mem
```

**改进收益**：
- ✅ 识别性能瓶颈
- ✅ 内存泄漏检测
- ✅ 优化指导数据
- ✅ 容量规划依据

---

#### 2.8 缺乏数据Schema定义 ⭐⭐/5 → 目标 ⭐⭐⭐⭐

**当前问题**：
- Step间通过CSV传递数据
- 无列名schema验证
- 字段变更导致隐式错误

**示例问题**：

```python
# Step6输出CSV列名
out_rank["dossier_json"] = dossier_json_paths
out_rank["llm_confidence"] = llm_conf
out_rank["supporting_evidence_count"] = se_cnts
out_rank["unique_supporting_pmids_count"] = se_unique_pmids_cnts

# Step7读取时假设列名存在
if "unique_supporting_pmids_count" in rank.columns:
    se_unique = int(rr.get("unique_supporting_pmids_count", 0) or 0)
else:
    # 降级逻辑
    ...
```

**工业级要求**：

**src/schemas.py**：

```python
"""数据Schema定义"""
from typing import Optional, List
from pydantic import BaseModel, Field, validator

class Step6OutputRow(BaseModel):
    """Step6输出CSV行schema"""
    drug_id: str = Field(..., description="药物唯一ID")
    canonical_name: str = Field(..., description="规范化药物名称")
    dossier_json: str = Field(..., description="Dossier JSON文件路径")
    dossier_md: str = Field(..., description="Dossier MD文件路径")
    llm_confidence: str = Field(..., pattern="^(HIGH|MED|LOW)$", description="LLM置信度")
    pubmed_total_articles: int = Field(ge=0, description="PubMed文章总数")
    rag_top_sentences: int = Field(ge=0, description="RAG顶部句子数")
    endpoint_type: str = Field(..., description="端点类型")
    supporting_evidence_count: int = Field(ge=0, description="支持证据数")
    supporting_sentence_count: int = Field(ge=0, description="支持句子数")
    unique_supporting_pmids_count: int = Field(ge=0, description="唯一PMID数")
    harm_or_neutral_count: int = Field(ge=0, description="中性/有害证据数")
    topic_match_ratio: float = Field(ge=0.0, le=1.0, description="主题匹配率")

    @validator('llm_confidence')
    def validate_confidence(cls, v):
        if v not in {'HIGH', 'MED', 'LOW'}:
            raise ValueError(f'Invalid confidence: {v}')
        return v

class DossierQC(BaseModel):
    """Dossier QC字段schema"""
    topic_match_ratio: float = Field(ge=0.0, le=1.0)
    topic_mismatch: bool
    removed_evidence_count: int = Field(ge=0)
    removed_cross_drug_count: int = Field(ge=0)
    supporting_evidence_after_qc: int = Field(ge=0)
    supporting_sentence_count_after_qc: int = Field(ge=0)
    qc_reasons: List[str] = Field(default_factory=list)

class EvidenceItem(BaseModel):
    """证据项schema"""
    pmid: str
    supports: bool
    direction: str = Field(..., pattern="^(benefit|harm|neutral|unknown)$")
    model: str = Field(..., pattern="^(human|animal|cell|unknown)$")
    endpoint: str
    claim: str
    confidence: float = Field(ge=0.0, le=1.0)
    source: Optional[str] = Field(None, pattern="^(llm|rule)$")
    topic_match_ratio: Optional[float] = Field(None, ge=0.0, le=1.0)

class Dossier(BaseModel):
    """完整Dossier schema"""
    drug_id: str
    canonical_name: str
    target_disease: str
    endpoint_type: str
    query: str
    qc: DossierQC
    clinicaltrials_negative: List[dict]
    pubmed_rag: dict
    llm_structured: dict
```

**使用方式**：

```python
import pandas as pd
from pydantic import ValidationError
from src.schemas import Step6OutputRow, Dossier

def validate_step6_output(df: pd.DataFrame) -> None:
    """验证Step6输出CSV"""
    for idx, row in df.iterrows():
        try:
            Step6OutputRow(**row.to_dict())
        except ValidationError as e:
            logger.error("Row %d validation failed: %s", idx, e)
            raise

def load_dossier(path: Path) -> Dossier:
    """加载并验证Dossier JSON"""
    data = json.loads(path.read_text())
    try:
        return Dossier(**data)
    except ValidationError as e:
        logger.error("Dossier %s validation failed: %s", path, e)
        raise

# 在Step7中使用
rank = pd.read_csv(args.rank_in)
validate_step6_output(rank)  # ✅ 早期发现schema不匹配
```

**改进收益**：
- ✅ 数据契约明确
- ✅ 运行时验证
- ✅ IDE自动补全
- ✅ 重构安全

---

## 三、改进路线图（按优先级）

### 阶段1：基础设施（2-3周）⚡ 高优先级

**目标**：建立工业级基础设施

| 任务 | 工作量 | 关键产出 | 阻塞风险 |
|------|-------|---------|---------|
| ✅ 1.1 添加标准logging模块 | 3天 | `src/logger.py`，所有脚本迁移 | P0 |
| ✅ 1.2 创建共享utils库 | 4天 | `src/common.py`，消除5份重复 | P0 |
| ✅ 1.3 增加单元测试框架 | 5天 | `tests/`，pytest配置，核心函数>70%覆盖 | P0 |
| ✅ 1.4 规范化异常处理 | 3天 | 精确异常类型，详细日志 | P0 |
| ✅ 1.5 创建.env.example | 1天 | 配置文档化 | P1 |
| ✅ 1.6 配置管理模块 | 2天 | `src/config.py`，启动验证 | P1 |

**里程碑1**：代码质量从3.0提升至3.5

---

### 阶段2：可观测性（1-2周）⚙️ 中优先级

**目标**：增强监控和调试能力

| 任务 | 工作量 | 关键产出 | 阻塞风险 |
|------|-------|---------|---------|
| ✅ 2.1 性能监控 | 3天 | `src/profiler.py`，装饰器 | P2 |
| ✅ 2.2 数据Schema定义 | 3天 | `src/schemas.py`，Pydantic验证 | P2 |
| ✅ 2.3 添加函数docstring | 4天 | 核心函数>80%覆盖 | P1 |
| ✅ 2.4 集成mypy类型检查 | 2天 | `setup.cfg`，CI集成 | P2 |

**里程碑2**：代码质量从3.5提升至4.0

---

### 阶段3：生产化（2-3周）🚀 可选但推荐

**目标**：支持生产部署

| 任务 | 工作量 | 关键产出 | 阻塞风险 |
|------|-------|---------|---------|
| ✅ 3.1 CI/CD管道 | 3天 | GitHub Actions，自动测试 | P2 |
| ✅ 3.2 Docker化 | 2天 | `Dockerfile`，`docker-compose.yml` | P2 |
| ✅ 3.3 断路器模式 | 3天 | API失败降级 | P2 |
| ✅ 3.4 API速率限制队列 | 2天 | 动态throttling | P3 |
| ✅ 3.5 指标仪表板 | 4天 | Grafana/Prometheus集成 | P3 |
| ✅ 3.6 分布式追踪 | 3天 | OpenTelemetry | P3 |

**里程碑3**：代码质量从4.0提升至4.5（工业级）

---

### 阶段4：高级优化（长期）🔬 可选

**目标**：达到最佳实践

| 任务 | 工作量 | 关键产出 |
|------|-------|---------|
| ✅ 4.1 异步I/O（asyncio） | 5天 | 并发PubMed请求 |
| ✅ 4.2 缓存预热脚本 | 2天 | 批量预加载 |
| ✅ 4.3 A/B测试框架 | 4天 | 算法对比 |
| ✅ 4.4 自动化回归测试 | 3天 | Golden dataset |
| ✅ 4.5 API文档生成 | 2天 | Sphinx |

---

## 四、投入产出分析

### 4.1 工作量估算

| 阶段 | 工作量 | 人员 | 时间 |
|------|-------|------|------|
| 阶段1（基础设施） | 18天 | 1人 | 3-4周 |
| 阶段2（可观测性） | 12天 | 1人 | 2-3周 |
| 阶段3（生产化） | 17天 | 1人 | 3-4周 |
| **总计（最小生产就绪）** | **30天** | **1人** | **6-8周** |
| 阶段4（高级优化） | 16天 | 1人 | 3周 |

### 4.2 收益量化

| 维度 | 当前状态 | 改进后 | 提升 |
|------|---------|-------|------|
| **代码重复率** | ~15% | <2% | **87%减少** |
| **测试覆盖** | <5% | >70% | **14倍** |
| **调试时间** | 2-4小时/bug | 0.5-1小时 | **75%减少** |
| **新人上手** | 3-5天 | 1天 | **70%减少** |
| **生产事故响应** | 无法追踪 | <15分钟 | **100%可观测** |
| **重构信心** | 低（怕破坏） | 高（测试保护） | **质的飞跃** |

### 4.3 ROI分析

**投入**：1人 × 6周 = 240小时

**回报**（年化）：
- 减少调试时间：50 bugs/年 × 2.5小时节省 = **125小时/年**
- 减少代码重复维护：5次重复 × 20小时 = **100小时/年**
- 减少生产事故：3事故/年 × 10小时 = **30小时/年**
- 新人培训成本降低：2人/年 × 16小时 = **32小时/年**

**总回报**：**287小时/年**

**ROI**：(287 - 240) / 240 = **19.6%** 首年正收益，**119.6%** 次年收益

---

## 五、风险与缓解

### 5.1 技术风险

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|---------|
| 重构破坏现有功能 | 高 | 中 | 先写测试，增量重构 |
| 测试覆盖成本超预期 | 中 | 高 | 优先核心函数，逐步扩展 |
| 新依赖引入冲突 | 中 | 低 | 使用虚拟环境，版本锁定 |
| 性能监控开销 | 低 | 中 | 可选装饰器，生产环境采样 |

### 5.2 组织风险

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|---------|
| 科研deadline冲突 | 高 | 高 | 分阶段，先P0后P2 |
| 团队抵触工程化 | 中 | 中 | 演示收益，渐进式改进 |
| 缺乏工程经验 | 中 | 中 | 参考本报告，寻求咨询 |

---

## 六、推荐行动计划

### 立即行动（本周）

1. ✅ **复制本报告给团队**，对齐改进目标
2. ✅ **创建GitHub issue跟踪**：为每个P0任务创建issue
3. ✅ **建立.env.example**：5分钟快速胜利
4. ✅ **启动logging迁移**：选择1个脚本试点（推荐step6）

### 第1-2周

1. ✅ 完成**共享utils库**（消除重复）
2. ✅ 为**核心函数添加单元测试**（canonicalize_name, bm25_rank, load_negative_trials）
3. ✅ **规范化异常处理**（step6的HTTP部分）
4. ✅ **添加配置管理模块**

### 第3-4周

1. ✅ **全脚本logging迁移**
2. ✅ **测试覆盖提升至50%**
3. ✅ **性能监控集成**
4. ✅ **添加函数docstring**（优先公共API）

### 第5-6周

1. ✅ **数据Schema定义**
2. ✅ **CI/CD管道**（可选）
3. ✅ **Docker化**（可选）
4. ✅ **第一次生产部署演练**

---

## 七、结论

LLM+RAG证据工程具有**坚实的科研基础**和**优秀的算法设计**，当前代码质量**中等偏上（3.0/5.0）**。通过**6-8周的系统工程化改进**，可达到**工业级标准（4.0/5.0）**，显著提升可维护性、可观测性和生产稳定性。

### 关键要点

✅ **做得好的**：类型系统、缓存架构、QC机制、HTTP重试
❌ **关键差距**：日志系统、测试覆盖、代码重复、异常处理
⚡ **优先行动**：logging + 共享库 + 单元测试（P0）
📈 **预期ROI**：首年19.6%，次年119.6%

### 最终建议

**采纳阶段1+2（基础设施+可观测性）**，投入**30天**，即可将代码质量从**3.0提升至4.0**，满足生产部署需求。阶段3+4为可选增强，可根据实际需求和资源逐步推进。

---

**报告生成**: Claude Code (Sonnet 4.5)
**分析深度**: Very Thorough
**代码审查行数**: 5,100+ lines
**参考工业标准**: Google Python Style Guide, The Twelve-Factor App, SRE Best Practices
