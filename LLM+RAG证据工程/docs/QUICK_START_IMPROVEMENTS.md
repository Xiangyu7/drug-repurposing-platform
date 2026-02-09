# LLM+RAG证据工程 工业化改进：快速启动指南

**目标**：6周内从科研代码（3.0/5.0）升级至工业级（4.0/5.0）

---

## 🚀 第1周：基础设施建立

### Day 1-2：日志系统迁移

**创建 `src/logger.py`**：

```python
"""统一日志配置"""
import logging
import logging.handlers
from pathlib import Path

def setup_logger(name: str, log_file: str = "dr_pipeline.log", level: str = "INFO"):
    """配置标准logger"""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()))

    # 避免重复handler
    if logger.handlers:
        return logger

    # 文件handler（带轮换）
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=100*1024*1024,  # 100MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # 格式
    formatter = logging.Formatter(
        '%(asctime)s | %(name)s | %(levelname)s | %(funcName)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger
```

**迁移示例**（step6第1步）：

```python
# 旧代码
def log(msg: str) -> None:
    print(msg, flush=True)

log(f"[HTTP] attempt {attempt}/{MAX_RETRIES} failed: {e}")

# 新代码
from src.logger import setup_logger
logger = setup_logger(__name__)

logger.error(
    "HTTP request failed on attempt %d/%d: %s",
    attempt, MAX_RETRIES, e,
    exc_info=True  # 自动记录栈
)
```

**检查点**：
- [ ] `src/logger.py` 创建
- [ ] step6迁移完成
- [ ] 运行step6，检查`dr_pipeline.log`生成

---

### Day 3-5：创建共享utils库

**创建 `src/common.py`**：

```python
"""共享工具函数库

消除跨脚本的代码重复（canonicalize_name在5个脚本中重复）
"""
import re
from typing import Optional

STOP_WORDS = {
    "tablet", "tablets", "capsule", "capsules", "injection", "injectable",
    "infusion", "oral", "iv", "intravenous", "sc", "subcutaneous",
    "im", "intramuscular", "po", "qd", "bid", "tid", "qod", "qhs",
    "sustained", "extended", "release", "er", "sr", "xr",
    "solution", "suspension", "gel", "cream", "patch", "spray",
    "drops", "drop", "mg", "g", "mcg", "ug", "iu", "ml"
}

def normalize_basic(x: str) -> str:
    """基础标准化：小写、去标点、去多余空格"""
    s = str(x).lower().strip()
    s = re.sub(r"[\(\)\[\]\{\},;:/\\]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def canonicalize_name(x: str) -> str:
    """药物名称规范化：去剂量、去停用词、统一希腊字母

    Example:
        >>> canonicalize_name("Aspirin 100mg Tablet")
        'aspirin'
        >>> canonicalize_name("Interferon-α 2b Injection")
        'interferon alpha 2b'
    """
    s = normalize_basic(x)
    if not s:
        return ""

    # 去剂量
    s = re.sub(r"\b\d+(\.\d+)?\s*(mg|g|mcg|ug|iu|ml)\b", " ", s, flags=re.I)
    s = re.sub(r"\b\d+(\.\d+)?\b", " ", s)

    # 分词+去停用词
    toks = [t for t in re.split(r"\s+", s) if t]
    toks = [t for t in toks if t not in STOP_WORDS]

    # 统一希腊字母
    joined = " ".join(toks).replace("α", "alpha").replace("β", "beta")
    joined = re.sub(r"\s+", " ", joined).strip()

    return joined

def safe_filename(s: str, max_len: int = 80) -> str:
    """转换为安全的文件名

    Example:
        >>> safe_filename("Drug/Name (100mg)")
        'drug_name_100mg_'
    """
    s = re.sub(r"[^a-zA-Z0-9\-_]+", "_", str(s).strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len] or "drug"

def normalize_pmid(v: Optional[str]) -> str:
    """提取标准PMID（6-9位数字）

    Example:
        >>> normalize_pmid("PMID: 12345678")
        '12345678'
        >>> normalize_pmid("123")
        ''
    """
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    m = re.search(r"\b(\d{6,9})\b", s)
    return m.group(1) if m else ""
```

**重构脚本**（step5示例）：

```python
# 旧代码（step5内部定义）
def canonicalize_name(x: str) -> str:
    s = normalize_basic(x)
    ...

# 新代码
from src.common import canonicalize_name, normalize_basic, safe_filename

# 直接使用，删除本地定义
```

**检查点**：
- [ ] `src/common.py` 创建
- [ ] step5/6/7/8迁移完成
- [ ] 删除本地重复定义
- [ ] 运行step5-8，确认无错误

---

### Day 6-7：单元测试框架

**创建 `tests/test_common.py`**：

```python
"""共享工具函数单元测试"""
import pytest
from src.common import canonicalize_name, normalize_pmid, safe_filename

class TestCanonicalizeName:
    """测试药物名称规范化"""

    def test_remove_dosage(self):
        assert canonicalize_name("aspirin 100mg tablet") == "aspirin"
        assert canonicalize_name("Drug 50 ug injection") == "drug"

    def test_greek_letters(self):
        assert canonicalize_name("interferon-α") == "interferon alpha"
        assert canonicalize_name("TNF-β inhibitor") == "tnf beta inhibitor"

    def test_empty_input(self):
        assert canonicalize_name("") == ""
        assert canonicalize_name("   ") == ""

    @pytest.mark.parametrize("input,expected", [
        ("DRUG (Parenteral)", "drug parenteral"),
        ("Drug  Multiple   Spaces", "drug multiple spaces"),
        ("123 mg only dosage", ""),  # 只有剂量
    ])
    def test_edge_cases(self, input, expected):
        assert canonicalize_name(input) == expected

class TestNormalizePMID:
    """测试PMID标准化"""

    def test_valid_pmid(self):
        assert normalize_pmid("12345678") == "12345678"
        assert normalize_pmid("PMID: 12345678") == "12345678"
        assert normalize_pmid("Found in PMID 23456789 study") == "23456789"

    def test_invalid_pmid(self):
        assert normalize_pmid("abc") == ""
        assert normalize_pmid("123") == ""  # 太短
        assert normalize_pmid("1234567890") == ""  # 太长
        assert normalize_pmid(None) == ""

    def test_multiple_pmids(self):
        # 返回第一个
        assert normalize_pmid("12345678 and 23456789") == "12345678"

class TestSafeFilename:
    """测试文件名安全化"""

    def test_special_chars(self):
        assert safe_filename("drug/name") == "drug_name"
        assert safe_filename("drug (100mg)") == "drug_100mg_"

    def test_long_name(self):
        long = "a" * 100
        result = safe_filename(long, max_len=80)
        assert len(result) == 80

    def test_empty(self):
        assert safe_filename("") == "drug"
        assert safe_filename("!!!") == "drug"
```

**运行测试**：

```bash
# 安装pytest
pip install pytest pytest-cov

# 运行测试
pytest tests/test_common.py -v

# 运行带覆盖率
pytest tests/test_common.py --cov=src.common --cov-report=term-missing
```

**检查点**：
- [ ] pytest安装
- [ ] `tests/test_common.py` 创建
- [ ] 测试通过（绿色）
- [ ] 覆盖率 >90%

---

## 🔧 第2周：配置与异常处理

### Day 8-9：配置管理

**创建 `.env.example`**：

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
```

**创建 `src/config.py`**：

```python
"""配置管理模块"""
import os
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
    PUBMED_EFETCH_CHUNK: int = int(os.getenv("PUBMED_EFETCH_CHUNK", "20"))

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
    REFRESH_EMPTY_CACHE: bool = os.getenv("REFRESH_EMPTY_CACHE", "1") == "1"

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

# 启动时验证
Config.validate()
```

**迁移示例**（step6）：

```python
# 旧代码
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "").strip()
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "4"))

# 新代码
from src.config import Config

def pubmed_esearch(term: str, retmax: int = 200) -> List[str]:
    params = {"db": "pubmed", "term": term, ...}
    if Config.NCBI_API_KEY:
        params["api_key"] = Config.NCBI_API_KEY
    ...
```

**检查点**：
- [ ] `.env.example` 创建
- [ ] `src/config.py` 创建
- [ ] step6迁移完成
- [ ] 启动时打印配置验证日志

---

### Day 10-11：规范化异常处理

**重构示例**（step6的ollama_embed函数）：

```python
# 旧代码
def ollama_embed(texts: List[str], model: str) -> Optional[List[List[float]]]:
    try:
        r = request_with_retries("POST", url, ...)
        data = r.json()
        embs = data.get("embeddings")
        if isinstance(embs, list) and embs:
            return embs
    except Exception:  # ❌ 过于宽泛
        pass

    try:
        # 降级到旧接口
        ...
    except Exception as e:  # ❌ 仍然过于宽泛
        log(f"[WARN] embedding disabled: {e}")
        return None

# 新代码
import requests
from requests.exceptions import ConnectionError, Timeout, HTTPError

def ollama_embed(texts: List[str], model: str) -> Optional[List[List[float]]]:
    """调用Ollama embedding API

    Returns:
        嵌入向量列表，失败返回None

    Raises:
        不抛出异常，所有错误都被捕获并记录
    """
    if Config.DISABLE_EMBED:
        return None

    if not texts:
        return []

    url = f"{Config.OLLAMA_HOST}/api/embed"

    # 尝试新接口
    try:
        r = request_with_retries(
            "POST", url,
            json={"model": model, "input": texts},
            timeout=Config.OLLAMA_TIMEOUT,
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
            logger.info("New API not found, trying fallback endpoint")
        else:
            logger.error("Ollama HTTP error %d: %s", e.response.status_code, e)
            return None

    except ValueError as e:
        logger.error("Invalid JSON response: %s", e)
        return None

    # 降级到旧接口
    try:
        url_old = f"{Config.OLLAMA_HOST}/api/embeddings"
        out = []
        for t in texts:
            r = request_with_retries(
                "POST", url_old,
                json={"model": model, "prompt": t},
                timeout=Config.OLLAMA_TIMEOUT,
                trust_env=False
            )
            data = r.json()
            e = data.get("embedding")
            if not isinstance(e, list):
                raise ValueError(f"Invalid embedding type: {type(e)}")
            out.append(e)
        return out

    except Exception as e:
        logger.error("Fallback embedding also failed: %s", e, exc_info=True)
        return None
```

**检查点**：
- [ ] step6的ollama_embed重构完成
- [ ] 异常类型精确化
- [ ] 日志记录详细化
- [ ] 运行step6，检查日志质量

---

## 📊 第3周：测试覆盖扩展

### Day 12-14：核心函数单元测试

**创建 `tests/test_step6_retrieval.py`**：

```python
"""Step6检索功能单元测试"""
import pytest
from unittest.mock import patch, MagicMock
import sys
sys.path.insert(0, 'scripts')

from step6_pubmed_rag_ollama_evidence_v2 import (
    pubmed_esearch,
    bm25_rank,
    classify_endpoint,
    topic_match_ratio
)

class TestPubMedSearch:
    """测试PubMed搜索"""

    @patch('step6_pubmed_rag_ollama_evidence_v2.request_with_retries')
    def test_esearch_success(self, mock_request):
        """测试成功搜索"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'esearchresult': {'idlist': ['12345678', '23456789']}
        }
        mock_request.return_value = mock_response

        result = pubmed_esearch("aspirin atherosclerosis", retmax=10)
        assert result == ['12345678', '23456789']

    @patch('step6_pubmed_rag_ollama_evidence_v2.request_with_retries')
    def test_esearch_empty(self, mock_request):
        """测试空结果"""
        mock_response = MagicMock()
        mock_response.json.return_value = {'esearchresult': {}}
        mock_request.return_value = mock_response

        result = pubmed_esearch("nonexistent_drug_12345", retmax=10)
        assert result == []

class TestBM25Rank:
    """测试BM25排序"""

    def test_empty_query(self):
        """空查询应返回空列表"""
        docs = [{'title': 'Test', 'abstract': 'Abstract'}]
        result = bm25_rank("", docs)
        assert result == []

    def test_empty_docs(self):
        """空文档应返回空列表"""
        result = bm25_rank("query", [])
        assert result == []

    def test_relevance_ranking(self):
        """测试相关性排序"""
        docs = [
            {'title': 'Aspirin in cardiovascular disease', 'abstract': 'Study on aspirin effects'},
            {'title': 'Diabetes treatment', 'abstract': 'Metformin for diabetes'},
            {'title': 'Aspirin mechanism', 'abstract': 'Aspirin inhibits platelets'}
        ]

        result = bm25_rank("aspirin cardiovascular", docs, topk=3)

        assert len(result) == 3
        # 第一个应该是最相关的
        assert 'aspirin' in result[0][1]['title'].lower()
        # 分数应该递减
        assert result[0][0] >= result[1][0] >= result[2][0]

class TestEndpointClassification:
    """测试端点分类"""

    def test_plaque_imaging(self):
        assert classify_endpoint("Coronary plaque volume by CTA", "") == "PLAQUE_IMAGING"
        assert classify_endpoint("Carotid intima-media thickness", "") == "PLAQUE_IMAGING"

    def test_pad_function(self):
        assert classify_endpoint("Six-minute walking distance", "") == "PAD_FUNCTION"
        assert classify_endpoint("Treadmill test for claudication", "") == "PAD_FUNCTION"

    def test_cv_events(self):
        assert classify_endpoint("MACE composite endpoint", "") == "CV_EVENTS"
        assert classify_endpoint("Myocardial infarction rate", "") == "CV_EVENTS"

    def test_other(self):
        assert classify_endpoint("Quality of life score", "") == "OTHER"

class TestTopicMatch:
    """测试主题匹配"""

    def test_plaque_match(self):
        text = "Atherosclerotic plaque progression measured by IVUS showed significant reduction"
        ratio = topic_match_ratio(text, "PLAQUE_IMAGING")
        assert ratio > 0.2  # 应有多个关键词命中

    def test_no_match(self):
        text = "This study investigated diabetes in pediatric population"
        ratio = topic_match_ratio(text, "PLAQUE_IMAGING")
        assert ratio < 0.1  # 几乎无命中
```

**运行测试**：

```bash
pytest tests/test_step6_retrieval.py -v --cov=scripts.step6_pubmed_rag_ollama_evidence_v2
```

**检查点**：
- [ ] `tests/test_step6_retrieval.py` 创建
- [ ] 测试通过
- [ ] step6关键函数覆盖率 >70%

---

### Day 15-17：集成测试+回归测试

**创建 `tests/test_step6_integration.py`**：

```python
"""Step6集成测试"""
import pytest
import pandas as pd
from pathlib import Path
import tempfile
import sys
sys.path.insert(0, 'scripts')

from step6_pubmed_rag_ollama_evidence_v2 import process_one

class TestStep6Integration:
    """Step6端到端集成测试"""

    @pytest.fixture
    def temp_output_dir(self):
        """临时输出目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.mark.slow
    @pytest.mark.integration
    def test_process_one_drug(self, temp_output_dir):
        """测试处理单个药物（真实API调用）"""
        # 使用已知药物（如aspirin）
        json_path, md_path, dossier = process_one(
            drug_id="test_001",
            canonical_name="aspirin",
            target_disease="atherosclerosis",
            endpoint_type_hint="CV_EVENTS",
            neg_path=None,
            out_dir=temp_output_dir,
            cache_dir=temp_output_dir / "cache",
            all_drug_names=["aspirin", "metformin"]
        )

        # 验证输出
        assert json_path.exists()
        assert md_path.exists()

        # 验证dossier结构
        assert dossier['drug_id'] == "test_001"
        assert dossier['canonical_name'] == "aspirin"
        assert 'qc' in dossier
        assert 'llm_structured' in dossier
        assert 'pubmed_rag' in dossier

        # 验证QC指标
        qc = dossier['qc']
        assert 'topic_match_ratio' in qc
        assert 0.0 <= qc['topic_match_ratio'] <= 1.0

        # 验证证据提取
        llm_struct = dossier['llm_structured']
        assert 'confidence' in llm_struct
        assert llm_struct['confidence'] in ['HIGH', 'MED', 'LOW']
```

**运行集成测试**：

```bash
# 跳过慢速测试（日常开发）
pytest tests/ -v -m "not slow"

# 运行所有测试（提交前）
pytest tests/ -v
```

**检查点**：
- [ ] 集成测试通过
- [ ] 回归测试建立
- [ ] 总体覆盖率 >70%

---

## 📈 第4周：可观测性增强

### Day 18-20：性能监控

**创建 `src/profiler.py`**：

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

    def print_report(self):
        """打印性能报告"""
        logger.info("=" * 80)
        logger.info("Performance Summary")
        logger.info("=" * 80)
        for entry in self.get_summary():
            logger.info(
                "%-30s | %4d calls | %8.2fs total | %6.2fs avg | %6.2fs max | %6.2f MB avg",
                entry['function'],
                entry['calls'],
                entry['total_time_s'],
                entry['avg_time_s'],
                entry['max_time_s'],
                entry['avg_memory_mb']
            )

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

            # 慢函数日志
            if duration > 10:
                logger.info(
                    "Function %s took %.2fs (mem delta: %.2f MB)",
                    func.__name__, duration, memory_delta
                )

    return wrapper
```

**使用示例**（step6）：

```python
from src.profiler import profile, metrics

@profile
def process_one(drug_id: str, canonical_name: str, ...) -> Tuple[Path, Path, Dict]:
    """处理单个药物"""
    ...

@profile
def pubmed_esearch(term: str, retmax: int = 200) -> List[str]:
    """PubMed搜索"""
    ...

@profile
def bm25_rank(query: str, docs: List[Dict], ...) -> List[Tuple[float, Dict]]:
    """BM25排序"""
    ...

def main():
    ...
    # 管道结束时打印报告
    metrics.print_report()
```

**检查点**：
- [ ] `src/profiler.py` 创建
- [ ] 关键函数添加@profile装饰器
- [ ] 运行step6，查看性能报告
- [ ] 识别性能瓶颈

---

### Day 21-22：文档字符串补充

**优先级顺序**：
1. 公共API函数（被其他脚本调用）
2. 复杂算法函数（bm25_rank, evidence_strength_score）
3. 关键业务逻辑（process_one, load_negative_trials）

**示例**：

```python
def load_negative_trials(
    neg_path: Optional[str],
    canonical_name: str
) -> Tuple[str, List[Dict[str, Any]], str]:
    """从CT.gov negative CSV加载失败临床试验数据

    使用token-based匹配策略，避免因剂型/剂量差异导致的匹配失败。

    Args:
        neg_path: CT.gov negative CSV路径（可选）
            CSV应包含列：drug_raw/drug_name, nctId, conditions, phase,
            primary_outcome_title, primary_outcome_pvalues
        canonical_name: 规范化药物名称（用于匹配）

    Returns:
        三元组：
        - endpoint_type: 端点类型（PLAQUE_IMAGING/PAD_FUNCTION/CV_EVENTS/OTHER）
        - trials: 匹配到的试验列表（最多10条）
        - text_block: Markdown格式的试验摘要文本

    Example:
        >>> endpoint, trials, md = load_negative_trials(
        ...     "data/poolA_negative_drug_level.csv",
        ...     "aspirin"
        ... )
        >>> print(endpoint)
        'CV_EVENTS'
        >>> print(len(trials))
        5

    Notes:
        - 匹配策略：提取5+字符的token，避免短词误匹配
        - 去除剂型括号内容（如 [100mg tablet]）
        - 优先匹配完整药物名，然后匹配主要token
        - 返回的trials按CSV原始顺序（通常是相关性递减）
    """
    ...
```

**检查点**：
- [ ] step6核心函数docstring覆盖 >80%
- [ ] step7核心函数docstring覆盖 >60%
- [ ] 使用pydoc生成HTML文档验证

---

## ✅ 第5-6周：生产化准备（可选）

### Day 23-25：CI/CD管道

**创建 `.github/workflows/test.yml`**：

```yaml
name: Tests

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]

jobs:
  test:
    runs-on: ubuntu-latest

    steps:
    - uses: actions/checkout@v3

    - name: Set up Python 3.10
      uses: actions/setup-python@v4
      with:
        python-version: '3.10'

    - name: Install dependencies
      run: |
        pip install -r requirements.txt
        pip install pytest pytest-cov

    - name: Run tests
      run: |
        pytest tests/ -v --cov=src --cov=scripts --cov-report=xml

    - name: Upload coverage
      uses: codecov/codecov-action@v3
      with:
        files: ./coverage.xml
        fail_ci_if_error: true
```

**检查点**：
- [ ] GitHub Actions配置
- [ ] CI测试通过
- [ ] 覆盖率报告上传

---

### Day 26-28：Docker化

**创建 `Dockerfile`**：

```dockerfile
FROM python:3.10-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY data/ ./data/

# 环境变量
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO

# 默认命令
CMD ["python", "scripts/step6_pubmed_rag_ollama_evidence_v2.py", "--help"]
```

**创建 `docker-compose.yml`**：

```yaml
version: '3.8'

services:
  dr-pipeline:
    build: .
    volumes:
      - ./data:/app/data
      - ./output:/app/output
      - ./cache:/app/cache
    environment:
      - NCBI_API_KEY=${NCBI_API_KEY}
      - OLLAMA_HOST=http://ollama:11434
    depends_on:
      - ollama

  ollama:
    image: ollama/ollama:latest
    ports:
      - "11434:11434"
    volumes:
      - ollama_data:/root/.ollama

volumes:
  ollama_data:
```

**检查点**：
- [ ] Docker镜像构建成功
- [ ] docker-compose启动成功
- [ ] 容器内运行step6成功

---

## 📋 总结检查清单

### 阶段1完成标准（第1-2周）

- [ ] ✅ 日志系统迁移（step6完成）
- [ ] ✅ 共享utils库（src/common.py，消除重复）
- [ ] ✅ 单元测试框架（pytest，覆盖率>70%）
- [ ] ✅ 配置管理（src/config.py，.env.example）
- [ ] ✅ 异常处理规范化（精确异常类型）

**验证方式**：
```bash
# 运行测试
pytest tests/ -v --cov=src --cov-report=term-missing

# 预期：
# - 测试通过 >20个
# - 覆盖率 >70%
# - 日志文件dr_pipeline.log生成

# 运行step6
python scripts/step6_pubmed_rag_ollama_evidence_v2.py \
  --rank_in data/step6_rank.csv \
  --out output/step6_test

# 预期：
# - 日志清晰分级
# - 异常详细记录
# - 性能报告打印
```

---

### 阶段2完成标准（第3-4周）

- [ ] ✅ 测试覆盖扩展（step6 >80%，step7 >60%）
- [ ] ✅ 性能监控集成（@profile装饰器）
- [ ] ✅ 函数docstring（核心函数 >80%）
- [ ] ✅ 集成测试建立

**验证方式**：
```bash
# 运行所有测试
pytest tests/ -v --cov=src --cov=scripts --cov-report=html

# 查看覆盖率报告
open htmlcov/index.html

# 预期：
# - src/common.py覆盖率 >90%
# - step6关键函数覆盖率 >80%
# - 总体覆盖率 >70%
```

---

## 🎯 成功指标

| 指标 | 当前 | 目标 | 验证方式 |
|------|------|------|---------|
| **代码重复率** | ~15% | <2% | grep -r "def canonicalize_name" |
| **测试覆盖率** | <5% | >70% | pytest --cov |
| **日志质量** | print() | logging | 检查dr_pipeline.log |
| **配置管理** | 散乱 | 统一 | 检查src/config.py |
| **异常处理** | 宽泛 | 精确 | 代码审查 |
| **文档覆盖** | ~20% | >80% | pydoc生成 |

---

## 💡 常见问题

**Q: 改进会破坏现有功能吗？**
A: 不会。所有改进都是增量式的，每步都有验证。单元测试作为安全网。

**Q: 需要多少时间？**
A: 阶段1（基础设施）：2-3周
   阶段2（可观测性）：1-2周
   总计：**6周**（1人全职）

**Q: 如何处理科研deadline冲突？**
A: 优先完成P0任务（日志+共享库+测试），P1/P2可延后。

**Q: 测试覆盖70%是否过高？**
A: 不高。覆盖核心函数（common.py, retrieval, QC logic），跳过简单工具函数。

---

**开始行动**：从Day 1开始，每天进步一点点！🚀
