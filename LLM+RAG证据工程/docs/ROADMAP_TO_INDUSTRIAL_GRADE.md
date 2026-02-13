# 工业级改进路线图

**当前状态**: 研究级原型 → 目标: 工业级生产系统
**评估日期**: 2026-02-08
**预计周期**: 4-6周全面升级

> **⚠️ 2026-02-12 进度更新**:
> - ✅ **差距 1 (测试)**: 0% → 501 tests / 75%+ 覆盖率，全通过
> - ✅ **差距 2 (监控)**: 已完成 — Prometheus MetricsTracker + 可配置阈值告警 (alerts.py)
> - ✅ **差距 6 (数据管理)**: 已完成 — ContractEnforcer (schema 校验) + Audit Log (SHA256 哈希链) + Provenance (run manifest)
> - ✅ **差距 7 (安全)**: 部分完成 — Release Gate (NO-GO 拦截) + 人审质量门控 (IRR Kappa)
> - ✅ **差距 8 (文档)**: 已完成 — 5 个 README + USER_GUIDE + HUMAN_JUDGMENT_CHECKLIST 全部更新
> - ⬜ **差距 3 (部署)**: 待做 — Docker + K8s
> - ⬜ **差距 4 (UI)**: 待做
> - ⬜ **差距 5 (性能)**: 待做

---

## 📊 差距总览

### 当前优势 ✅
1. **代码质量优秀**: 类型提示、文档字符串、模块化设计
2. **功能完整**: 端到端pipeline可运行
3. **架构清晰**: 4层架构，职责分明
4. **已有6000+行生产级代码**

### 主要差距 🔴

| 编号 | 差距 | 严重性 | 工作量 | 优先级 | 状态 (2026-02-12) |
|------|------|--------|--------|--------|-------------------|
| 1 | 测试覆盖不足 | 🔴 High | 2周 | P0 | ✅ 501 tests / 75%+ |
| 2 | 监控告警缺失 | 🔴 High | 1周 | P0 | ✅ MetricsTracker + alerts.py |
| 3 | 部署方案不完善 | 🔴 High | 1周 | P1 | ⬜ 待做 |
| 4 | 用户界面缺失 | 🟡 Medium | 2周 | P1 | ⬜ 待做 |
| 5 | 性能未优化 | 🟡 Medium | 1周 | P2 | ⬜ 待做 |
| 6 | 数据管理不规范 | 🟡 Medium | 1周 | P2 | ✅ ContractEnforcer + AuditLog |
| 7 | 安全机制薄弱 | 🔴 High | 1周 | P1 | 🟡 Release Gate 完成 |
| 8 | 文档待完善 | 🟢 Low | 3天 | P2 | ✅ 全部 README 更新 |

---

## 🔴 差距 1: 测试覆盖不足 (P0)

### 现状分析
```
当前测试覆盖: ~5%
├── 手动测试: 7个药物端到端测试 ✅
├── 单元测试: 0个 ❌
├── 集成测试: 0个 ❌
└── 性能测试: 0个 ❌

工业级标准:
├── 单元测试覆盖: >80%
├── 集成测试: 覆盖所有关键路径
├── 回归测试: 每次提交自动运行
└── 性能基准: 有明确指标
```

### 改进方案

#### 1.1 单元测试 (1周)

**目录结构**:
```
tests/
├── unit/
│   ├── test_common/
│   │   ├── test_text.py          # 文本处理测试
│   │   ├── test_file_io.py       # 文件IO测试
│   │   └── test_hashing.py       # 哈希函数测试
│   ├── test_retrieval/
│   │   ├── test_cache.py         # 缓存测试
│   │   ├── test_pubmed.py        # PubMed客户端测试
│   │   └── test_ctgov.py         # CTGov客户端测试
│   ├── test_evidence/
│   │   ├── test_ranker.py        # BM25测试
│   │   ├── test_ollama.py        # Ollama测试
│   │   └── test_extractor.py     # 证据提取测试
│   └── test_scoring/
│       ├── test_scorer.py        # 评分测试
│       ├── test_gating.py        # 门控测试
│       ├── test_cards.py         # 卡片生成测试
│       └── test_validation.py    # 验证计划测试
├── integration/
│   ├── test_step6_pipeline.py    # Step6集成测试
│   ├── test_step7_pipeline.py    # Step7集成测试
│   └── test_end_to_end.py        # 端到端测试
├── performance/
│   ├── benchmark_bm25.py         # BM25性能基准
│   ├── benchmark_llm.py          # LLM性能基准
│   └── benchmark_pipeline.py     # Pipeline性能基准
└── fixtures/
    ├── sample_papers.json        # 测试用论文数据
    ├── sample_dossiers.json      # 测试用档案
    └── expected_outputs.json     # 预期输出
```

**示例: test_scorer.py**
```python
import pytest
from src.dr.scoring import DrugScorer, ScoringConfig

class TestDrugScorer:
    @pytest.fixture
    def scorer(self):
        return DrugScorer(config=ScoringConfig())

    @pytest.fixture
    def sample_dossier(self):
        return {
            "drug_id": "TEST001",
            "canonical_name": "test_drug",
            "total_pmids": 20,
            "evidence_count": {
                "benefit": 10,
                "harm": 2,
                "neutral": 1,
                "unknown": 7
            }
        }

    def test_score_drug_high_benefit(self, scorer, sample_dossier):
        """测试高benefit药物评分"""
        scores = scorer.score_drug(sample_dossier)

        assert scores["total_score_0_100"] > 60
        assert scores["evidence_strength_0_30"] > 15
        assert 0 <= scores["total_score_0_100"] <= 100

    def test_score_drug_zero_evidence(self, scorer):
        """测试零证据药物评分"""
        dossier = {
            "drug_id": "TEST002",
            "canonical_name": "test_drug_2",
            "total_pmids": 0,
            "evidence_count": {
                "benefit": 0,
                "harm": 0,
                "neutral": 0,
                "unknown": 0
            }
        }
        scores = scorer.score_drug(dossier)

        assert scores["evidence_strength_0_30"] < 5
        assert scores["total_score_0_100"] < 30

    def test_score_drug_safety_penalty(self, scorer):
        """测试安全惩罚"""
        dossier = {
            "drug_id": "TEST003",
            "canonical_name": "dexamethasone",  # 安全黑名单
            "total_pmids": 20,
            "evidence_count": {
                "benefit": 10,
                "harm": 0,
                "neutral": 0,
                "unknown": 10
            }
        }
        scores = scorer.score_drug(dossier)

        assert scores["safety_fit_0_20"] < 20  # 应该有惩罚
```

**运行测试**:
```bash
# 安装pytest
pip install pytest pytest-cov

# 运行所有测试
pytest tests/ -v

# 查看覆盖率
pytest tests/ --cov=src/dr --cov-report=html

# 运行特定测试
pytest tests/unit/test_scoring/test_scorer.py -v
```

**预期结果**:
- 单元测试: 150+ 测试用例
- 覆盖率: 80%+
- 运行时间: <30秒

#### 1.2 集成测试 (3天)

**示例: test_end_to_end.py**
```python
import pytest
from pathlib import Path
import pandas as pd

class TestEndToEnd:
    def test_step6_to_step7_pipeline(self, tmp_path):
        """测试Step6到Step7完整流程"""
        # 1. 运行Step6
        from scripts.step6_pubmed_rag_simple import main as step6_main
        # ... setup args
        step6_main()

        # 2. 验证Step6输出
        rank_csv = tmp_path / "step6_simple" / "step6_rank_simple.csv"
        assert rank_csv.exists()
        df = pd.read_csv(rank_csv)
        assert len(df) > 0

        # 3. 运行Step7
        from scripts.step7_score_and_gate import main as step7_main
        # ... setup args
        step7_main()

        # 4. 验证Step7输出
        gating_csv = tmp_path / "step7" / "step7_gating_decision.csv"
        assert gating_csv.exists()
        df = pd.read_csv(gating_csv)
        assert "gate_decision" in df.columns
        assert set(df["gate_decision"]).issubset({"GO", "MAYBE", "NO-GO"})
```

#### 1.3 CI/CD集成 (1天)

**GitHub Actions配置: .github/workflows/tests.yml**
```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: [3.9, 3.10, 3.11]

    steps:
    - uses: actions/checkout@v3
    - name: Set up Python ${{ matrix.python-version }}
      uses: actions/setup-python@v4
      with:
        python-version: ${{ matrix.python-version }}

    - name: Install dependencies
      run: |
        pip install -r requirements.txt
        pip install pytest pytest-cov

    - name: Run tests
      run: |
        pytest tests/ --cov=src/dr --cov-report=xml

    - name: Upload coverage
      uses: codecov/codecov-action@v3
      with:
        file: ./coverage.xml
```

**预期效果**:
- 每次commit自动测试
- Pull Request必须通过测试才能合并
- 覆盖率报告自动生成

---

## 🔴 差距 2: 监控告警缺失 (P0)

### 现状分析
```
当前监控: 仅有日志
├── 日志: 结构化logging ✅
├── 指标监控: 无 ❌
├── 错误追踪: 无 ❌
├── 性能监控: 无 ❌
└── 告警系统: 无 ❌

工业级标准:
├── 实时指标监控
├── 错误自动上报
├── 性能瓶颈识别
└── 异常自动告警
```

### 改进方案

#### 2.1 指标监控 (3天)

**使用Prometheus + Grafana**

**代码改造: src/dr/monitoring/metrics.py**
```python
from prometheus_client import Counter, Histogram, Gauge, start_http_server
import time

# 定义指标
drug_processed = Counter('drug_processed_total', 'Total drugs processed', ['status'])
evidence_extracted = Counter('evidence_extracted_total', 'Total evidence extracted', ['direction'])
llm_latency = Histogram('llm_extraction_seconds', 'LLM extraction latency')
pipeline_duration = Histogram('pipeline_duration_seconds', 'Pipeline duration', ['stage'])
active_extractions = Gauge('active_extractions', 'Number of active LLM extractions')

class MetricsCollector:
    """指标收集器"""

    @staticmethod
    def record_drug_processed(status: str):
        """记录处理的药物"""
        drug_processed.labels(status=status).inc()

    @staticmethod
    def record_evidence(direction: str):
        """记录证据提取"""
        evidence_extracted.labels(direction=direction).inc()

    @staticmethod
    def time_llm_extraction():
        """计时LLM提取"""
        return llm_latency.time()

    @staticmethod
    def time_pipeline_stage(stage: str):
        """计时pipeline阶段"""
        return pipeline_duration.labels(stage=stage).time()

# 启动metrics服务器
def start_metrics_server(port=8000):
    start_http_server(port)
```

**集成到代码**:
```python
# 在step7_score_and_gate.py中
from src.dr.monitoring.metrics import MetricsCollector

def main():
    metrics = MetricsCollector()

    for drug in drugs:
        with metrics.time_pipeline_stage("scoring"):
            try:
                scores = scorer.score_drug(dossier)
                metrics.record_drug_processed("success")
            except Exception as e:
                metrics.record_drug_processed("failed")
                raise
```

**Grafana仪表盘**:
- 药物处理速率 (drugs/hour)
- 证据提取分布 (benefit/harm/neutral)
- LLM延迟分布 (p50, p95, p99)
- 错误率趋势

#### 2.2 错误追踪 (2天)

**使用Sentry**

**安装**:
```bash
pip install sentry-sdk
```

**配置: src/dr/monitoring/errors.py**
```python
import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration

def init_sentry(dsn: str, environment: str = "production"):
    """初始化Sentry错误追踪"""
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        traces_sample_rate=0.1,  # 10%采样
        integrations=[
            LoggingIntegration(
                level=logging.INFO,
                event_level=logging.ERROR
            )
        ]
    )

def capture_exception(error: Exception, context: dict = None):
    """捕获异常并上报"""
    with sentry_sdk.push_scope() as scope:
        if context:
            for key, value in context.items():
                scope.set_context(key, value)
        sentry_sdk.capture_exception(error)
```

**使用**:
```python
try:
    scores = scorer.score_drug(dossier)
except Exception as e:
    capture_exception(e, context={
        "drug_id": drug_id,
        "canonical_name": canonical_name,
        "stage": "scoring"
    })
    raise
```

**预期效果**:
- 所有错误自动上报Sentry
- 错误按频率、影响面聚合
- 收到告警邮件/Slack通知
- 错误堆栈完整保存

#### 2.3 告警系统 (2天)

**AlertManager配置**:
```yaml
# alertmanager.yml
route:
  group_by: ['alertname', 'severity']
  group_wait: 10s
  group_interval: 10s
  repeat_interval: 1h
  receiver: 'team-emails'

receivers:
- name: 'team-emails'
  email_configs:
  - to: 'team@example.com'
    from: 'alertmanager@example.com'
    smarthost: smtp.gmail.com:587
    auth_username: 'alertmanager@example.com'
    auth_password: '<password>'

- name: 'slack'
  slack_configs:
  - api_url: '<slack_webhook_url>'
    channel: '#alerts'
```

**告警规则: alerts.yml**
```yaml
groups:
- name: drug_pipeline
  rules:
  - alert: HighErrorRate
    expr: rate(drug_processed_total{status="failed"}[5m]) > 0.1
    for: 5m
    labels:
      severity: critical
    annotations:
      summary: "High error rate in drug processing"
      description: "Error rate is {{ $value }} errors/sec"

  - alert: SlowLLMExtraction
    expr: histogram_quantile(0.95, llm_extraction_seconds) > 300
    for: 10m
    labels:
      severity: warning
    annotations:
      summary: "LLM extraction is slow"
      description: "95th percentile is {{ $value }} seconds"
```

---

## 🔴 差距 3: 部署方案不完善 (P1)

### 现状分析
```
当前部署: 手动运行脚本
├── 环境管理: requirements.txt ✅
├── 配置管理: 环境变量 ✅
├── 容器化: 无 ❌
├── 编排: 无 ❌
└── 版本管理: Git ✅

工业级标准:
├── Docker容器化
├── Kubernetes编排
├── 自动化部署
└── 滚动更新
```

### 改进方案

#### 3.1 Docker容器化 (2天)

**Dockerfile**:
```dockerfile
FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY src/ ./src/
COPY scripts/ ./scripts/

# 复制配置
COPY config/ ./config/

# 暴露端口 (metrics, API)
EXPOSE 8000 5000

# 健康检查
HEALTHCHECK --interval=30s --timeout=3s \
  CMD curl -f http://localhost:8000/metrics || exit 1

# 默认命令
CMD ["python", "scripts/step7_score_and_gate.py"]
```

**docker-compose.yml**:
```yaml
version: '3.8'

services:
  pipeline:
    build: .
    image: drug-repurposing:latest
    environment:
      - NCBI_API_KEY=${NCBI_API_KEY}
      - USE_CHAT_SCHEMA=0
    volumes:
      - ./data:/app/data
      - ./output:/app/output
    ports:
      - "8000:8000"  # Metrics
    depends_on:
      - ollama
      - prometheus

  ollama:
    image: ollama/ollama:latest
    volumes:
      - ollama_data:/root/.ollama
    ports:
      - "11434:11434"

  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    ports:
      - "9090:9090"

  grafana:
    image: grafana/grafana:latest
    volumes:
      - grafana_data:/var/lib/grafana
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin

volumes:
  ollama_data:
  prometheus_data:
  grafana_data:
```

**运行**:
```bash
# 构建
docker-compose build

# 启动全部服务
docker-compose up -d

# 查看日志
docker-compose logs -f pipeline

# 停止
docker-compose down
```

#### 3.2 Kubernetes编排 (3天)

**k8s/deployment.yaml**:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: drug-pipeline
spec:
  replicas: 3
  selector:
    matchLabels:
      app: drug-pipeline
  template:
    metadata:
      labels:
        app: drug-pipeline
    spec:
      containers:
      - name: pipeline
        image: drug-repurposing:latest
        resources:
          requests:
            memory: "2Gi"
            cpu: "1000m"
          limits:
            memory: "4Gi"
            cpu: "2000m"
        env:
        - name: NCBI_API_KEY
          valueFrom:
            secretKeyRef:
              name: api-keys
              key: ncbi
        ports:
        - containerPort: 8000
        livenessProbe:
          httpGet:
            path: /metrics
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /metrics
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 5
```

**k8s/service.yaml**:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: drug-pipeline
spec:
  selector:
    app: drug-pipeline
  ports:
  - port: 80
    targetPort: 5000
    name: api
  - port: 8000
    targetPort: 8000
    name: metrics
  type: LoadBalancer
```

**部署**:
```bash
# 创建namespace
kubectl create namespace drug-pipeline

# 部署
kubectl apply -f k8s/ -n drug-pipeline

# 查看状态
kubectl get pods -n drug-pipeline
kubectl logs -f <pod-name> -n drug-pipeline

# 扩容
kubectl scale deployment drug-pipeline --replicas=5
```

---

## 🟡 差距 4: 用户界面缺失 (P1)

### 现状分析
```
当前交互: 命令行
├── CLI脚本: ✅
├── Web界面: 无 ❌
├── API接口: 无 ❌
└── 可视化: Markdown报告 ✅

工业级标准:
├── Web Dashboard
├── RESTful API
├── 交互式可视化
└── 用户权限管理
```

### 改进方案

#### 4.1 FastAPI后端 (3天)

**src/dr/api/main.py**:
```python
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import List, Optional
import uvicorn

app = FastAPI(title="LLM+RAG证据工程 API", version="1.0.0")

class DrugRequest(BaseModel):
    canonical_name: str
    drug_id: Optional[str] = None

class ScoreResponse(BaseModel):
    drug_id: str
    canonical_name: str
    scores: dict
    gate_decision: str
    gate_reasons: List[str]

@app.post("/api/v1/score", response_model=ScoreResponse)
async def score_drug(request: DrugRequest):
    """对单个药物进行评分"""
    try:
        # 调用pipeline
        from src.dr.scoring import DrugScorer, GatingEngine
        # ... 处理逻辑
        return ScoreResponse(...)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/batch-score")
async def batch_score(drugs: List[DrugRequest], background_tasks: BackgroundTasks):
    """批量评分（后台任务）"""
    task_id = str(uuid.uuid4())
    background_tasks.add_task(process_batch, drugs, task_id)
    return {"task_id": task_id, "status": "processing"}

@app.get("/api/v1/task/{task_id}")
async def get_task_status(task_id: str):
    """查询任务状态"""
    # 从Redis/数据库查询
    return {"task_id": task_id, "status": "completed", "results": [...]}

@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
```

**运行**:
```bash
# 启动API
python src/dr/api/main.py

# 测试
curl -X POST http://localhost:5000/api/v1/score \
  -H "Content-Type: application/json" \
  -d '{"canonical_name": "resveratrol"}'
```

#### 4.2 Streamlit Dashboard (2天)

**src/dr/ui/dashboard.py**:
```python
import streamlit as st
import pandas as pd
import plotly.express as px
from pathlib import Path

st.set_page_config(page_title="LLM+RAG证据工程 Dashboard", layout="wide")

st.title("🧬 LLM+RAG证据工程 Dashboard")

# 侧边栏
with st.sidebar:
    st.header("Settings")
    use_llm = st.checkbox("Use LLM Extraction", value=False)
    top_n = st.slider("Top N Drugs", 5, 50, 20)

# 主面板
tab1, tab2, tab3 = st.tabs(["Overview", "Drug Details", "Comparison"])

with tab1:
    st.header("Pipeline Overview")

    # 加载结果
    gating_df = pd.read_csv("output/step7/step7_gating_decision.csv")

    # 统计卡片
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Drugs", len(gating_df))
    with col2:
        go_count = len(gating_df[gating_df["gate_decision"] == "GO"])
        st.metric("GO Drugs", go_count)
    with col3:
        maybe_count = len(gating_df[gating_df["gate_decision"] == "MAYBE"])
        st.metric("MAYBE Drugs", maybe_count)
    with col4:
        no_go_count = len(gating_df[gating_df["gate_decision"] == "NO-GO"])
        st.metric("NO-GO Drugs", no_go_count)

    # 评分分布图
    fig = px.histogram(gating_df, x="total_score",
                      color="gate_decision",
                      title="Score Distribution by Decision")
    st.plotly_chart(fig, use_container_width=True)

    # 结果表格
    st.dataframe(gating_df, use_container_width=True)

with tab2:
    st.header("Drug Details")

    drug = st.selectbox("Select Drug", gating_df["canonical_name"].tolist())

    if drug:
        drug_data = gating_df[gating_df["canonical_name"] == drug].iloc[0]

        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Scores")
            st.metric("Total Score", f"{drug_data['total_score']:.1f}/100")
            st.write(f"Decision: **{drug_data['gate_decision']}**")
            if drug_data['gate_reasons']:
                st.warning(f"Reasons: {drug_data['gate_reasons']}")

        with col2:
            st.subheader("Evidence")
            st.write(f"Benefit: {drug_data['benefit']}")
            st.write(f"Harm: {drug_data['harm']}")
            st.write(f"Neutral: {drug_data['neutral']}")
            st.write(f"Total PMIDs: {drug_data['total_pmids']}")

        # 显示hypothesis card
        st.subheader("Hypothesis Card")
        with open(f"output/step7/dossiers/{drug_data['drug_id']}.json") as f:
            dossier = json.load(f)
        st.json(dossier)

# 运行命令
if __name__ == "__main__":
    st.sidebar.success("Dashboard is running!")
```

**运行**:
```bash
streamlit run src/dr/ui/dashboard.py
```

---

## 🟡 差距 5: 性能未优化 (P2)

### 现状分析
```
当前性能:
├── Step6 (rule-based): <1秒/7药物 ✅
├── Step6 (LLM): ~2-4小时/7药物 ❌
├── Step7: <1秒/7药物 ✅
├── 并行处理: 无 ❌
└── 缓存优化: 基础缓存 ✅

优化目标:
├── LLM并行处理
├── 批量推理优化
├── 结果缓存
└── 增量更新
```

### 改进方案

#### 5.1 并行处理 (2天)

**多进程处理**:
```python
from multiprocessing import Pool
from functools import partial

def process_drug_parallel(drugs: List[str], extractor: LLMEvidenceExtractor):
    """并行处理多个药物"""
    with Pool(processes=4) as pool:
        results = pool.map(
            partial(process_single_drug, extractor=extractor),
            drugs
        )
    return results

def process_single_drug(drug: str, extractor: LLMEvidenceExtractor):
    """处理单个药物"""
    # ... 处理逻辑
    return dossier
```

**异步处理**:
```python
import asyncio
import aiohttp

async def extract_evidence_async(papers: List[dict], extractor):
    """异步提取证据"""
    tasks = [
        extract_single_paper_async(paper, extractor)
        for paper in papers
    ]
    results = await asyncio.gather(*tasks)
    return results

# 预期加速: 4-8x (取决于CPU核心数)
```

#### 5.2 LLM批量推理 (3天)

**批处理优化**:
```python
class BatchLLMExtractor:
    """批量LLM提取器"""

    def extract_batch(self, papers: List[dict], batch_size: int = 8):
        """批量提取（减少API调用次数）"""
        results = []

        for i in range(0, len(papers), batch_size):
            batch = papers[i:i+batch_size]

            # 构建批量prompt
            batch_prompt = self._build_batch_prompt(batch)

            # 一次LLM调用处理多篇论文
            response = self.client.generate(batch_prompt, format="json")

            # 解析批量结果
            batch_results = json.loads(response)
            results.extend(batch_results)

        return results

# 预期加速: 3-5x (减少LLM调用次数)
```

#### 5.3 智能缓存 (1天)

**多级缓存**:
```python
import redis
from functools import lru_cache

class SmartCache:
    """智能缓存管理器"""

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_client = redis.from_url(redis_url)

    def get_llm_extraction(self, pmid: str, drug_id: str) -> Optional[dict]:
        """获取LLM提取结果（避免重复提取）"""
        key = f"llm_extraction:{drug_id}:{pmid}"
        cached = self.redis_client.get(key)
        if cached:
            return json.loads(cached)
        return None

    def set_llm_extraction(self, pmid: str, drug_id: str, result: dict, ttl: int = 86400*30):
        """缓存LLM提取结果（30天）"""
        key = f"llm_extraction:{drug_id}:{pmid}"
        self.redis_client.setex(key, ttl, json.dumps(result))

    @lru_cache(maxsize=1000)
    def get_bm25_ranking(self, drug_id: str, query_hash: str):
        """内存缓存BM25排名结果"""
        # ...
```

**预期效果**:
- 第二次运行同一药物: >99%加速
- 相似药物: 部分复用缓存

---

## 🟡 差距 6: 数据管理不规范 (P2)

### 现状分析
```
当前数据管理:
├── 存储: CSV + JSON文件 ✅
├── 备份: 手动Git ✅
├── 版本控制: 无 ❌
├── 数据验证: 基础检查 ✅
└── 数据血缘: 无 ❌

工业级标准:
├── 结构化数据库
├── 自动备份
├── 版本追踪
└── 数据血缘图
```

### 改进方案

#### 6.1 数据库设计 (3天)

**PostgreSQL Schema**:
```sql
-- 药物表
CREATE TABLE drugs (
    drug_id VARCHAR(20) PRIMARY KEY,
    canonical_name VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 证据表
CREATE TABLE evidence (
    id SERIAL PRIMARY KEY,
    drug_id VARCHAR(20) REFERENCES drugs(drug_id),
    pmid VARCHAR(20) NOT NULL,
    title TEXT,
    abstract TEXT,
    direction VARCHAR(20),  -- benefit/harm/neutral/unclear
    model VARCHAR(20),      -- human/animal/cell
    endpoint VARCHAR(50),   -- PLAQUE_IMAGING/CV_EVENTS etc
    mechanism TEXT,
    confidence VARCHAR(10), -- HIGH/MED/LOW
    extraction_method VARCHAR(20),  -- rule-based/llm
    created_at TIMESTAMP DEFAULT NOW()
);

-- 评分表
CREATE TABLE scores (
    id SERIAL PRIMARY KEY,
    drug_id VARCHAR(20) REFERENCES drugs(drug_id),
    evidence_strength FLOAT,
    mechanism_plausibility FLOAT,
    translatability FLOAT,
    safety_fit FLOAT,
    practicality FLOAT,
    total_score FLOAT,
    version INT,  -- 评分算法版本
    created_at TIMESTAMP DEFAULT NOW()
);

-- 门控决策表
CREATE TABLE gating_decisions (
    id SERIAL PRIMARY KEY,
    drug_id VARCHAR(20) REFERENCES drugs(drug_id),
    decision VARCHAR(10),  -- GO/MAYBE/NO-GO
    reasons TEXT[],
    created_at TIMESTAMP DEFAULT NOW()
);

-- 创建索引
CREATE INDEX idx_evidence_drug_id ON evidence(drug_id);
CREATE INDEX idx_evidence_pmid ON evidence(pmid);
CREATE INDEX idx_scores_drug_id ON scores(drug_id);
```

**ORM模型 (SQLAlchemy)**:
```python
from sqlalchemy import create_engine, Column, String, Float, Integer, ARRAY, Text, TIMESTAMP
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

Base = declarative_base()

class Drug(Base):
    __tablename__ = 'drugs'

    drug_id = Column(String(20), primary_key=True)
    canonical_name = Column(String(255), nullable=False)
    created_at = Column(TIMESTAMP, server_default='NOW()')
    updated_at = Column(TIMESTAMP, server_default='NOW()', onupdate='NOW()')

class Evidence(Base):
    __tablename__ = 'evidence'

    id = Column(Integer, primary_key=True)
    drug_id = Column(String(20), ForeignKey('drugs.drug_id'))
    pmid = Column(String(20), nullable=False)
    title = Column(Text)
    direction = Column(String(20))
    model = Column(String(20))
    endpoint = Column(String(50))
    mechanism = Column(Text)
    confidence = Column(String(10))
    extraction_method = Column(String(20))
    created_at = Column(TIMESTAMP, server_default='NOW()')
```

#### 6.2 数据版本控制 (2天)

**使用DVC (Data Version Control)**:
```bash
# 初始化DVC
dvc init

# 追踪数据文件
dvc add data/drug_master.csv
dvc add output/step6_simple/
dvc add output/step7/

# 配置远程存储 (S3/GCS/Azure)
dvc remote add -d myremote s3://my-bucket/dvc-storage

# 推送数据
dvc push

# 拉取数据
dvc pull
```

**数据流水线 (dvc.yaml)**:
```yaml
stages:
  step6:
    cmd: python scripts/step6_pubmed_rag_simple.py --limit 7
    deps:
      - data/drug_master.csv
      - scripts/step6_pubmed_rag_simple.py
    outs:
      - output/step6_simple/
    metrics:
      - output/step6_simple/metrics.json

  step7:
    cmd: python scripts/step7_score_and_gate.py
    deps:
      - output/step6_simple/
      - scripts/step7_score_and_gate.py
    outs:
      - output/step7/
    metrics:
      - output/step7/metrics.json
```

**运行**:
```bash
# 执行流水线
dvc repro

# 查看指标对比
dvc metrics diff
```

---

## 🔴 差距 7: 安全机制薄弱 (P1)

### 现状分析
```
当前安全:
├── 输入验证: 基础检查 ✅
├── API认证: 无 ❌
├── 数据加密: 无 ❌
├── 审计日志: 无 ❌
└── 密钥管理: 环境变量 ✅

工业级标准:
├── OAuth2认证
├── 数据加密
├── 完整审计日志
└── Secrets管理
```

### 改进方案

#### 7.1 API认证 (1天)

**JWT Token认证**:
```python
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
ALGORITHM = "HS256"

def verify_token(token: str = Depends(oauth2_scheme)):
    """验证JWT token"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

@app.post("/api/v1/score")
async def score_drug(request: DrugRequest, username: str = Depends(verify_token)):
    """需要认证的API"""
    # ... 处理逻辑
```

#### 7.2 审计日志 (1天)

```python
class AuditLogger:
    """审计日志记录器"""

    def log_api_access(self, user: str, endpoint: str, params: dict, response_code: int):
        """记录API访问"""
        audit_log = {
            "timestamp": datetime.now().isoformat(),
            "user": user,
            "endpoint": endpoint,
            "params": params,
            "response_code": response_code,
            "ip_address": request.client.host
        }
        logger.info("AUDIT", extra=audit_log)
```

#### 7.3 Secrets管理 (1天)

**使用HashiCorp Vault**:
```python
import hvac

class SecretsManager:
    """密钥管理器"""

    def __init__(self, vault_url: str, token: str):
        self.client = hvac.Client(url=vault_url, token=token)

    def get_secret(self, path: str) -> dict:
        """从Vault获取密钥"""
        return self.client.secrets.kv.v2.read_secret_version(path=path)

    def get_ncbi_api_key(self) -> str:
        """获取NCBI API密钥"""
        secret = self.get_secret("dr/ncbi")
        return secret["data"]["data"]["api_key"]
```

---

## 🟢 差距 8: 文档待完善 (P2)

### 改进方案

#### 8.1 API文档 (1天)

**Swagger/OpenAPI自动生成**:
```python
from fastapi import FastAPI

app = FastAPI(
    title="LLM+RAG证据工程 API",
    description="Industrial-grade LLM+RAG evidence engineering pipeline API",
    version="1.0.0",
    docs_url="/docs",      # Swagger UI
    redoc_url="/redoc"     # ReDoc
)

# 访问 http://localhost:5000/docs 查看API文档
```

#### 8.2 用户手册 (2天)

**docs/USER_MANUAL.md**:
- 安装指南
- 快速开始
- API使用示例
- 故障排除
- FAQ

#### 8.3 开发者文档 (2天)

**docs/DEVELOPER_GUIDE.md**:
- 架构设计
- 模块说明
- 贡献指南
- 测试指南
- 发布流程

---

## 📅 实施路线图

### 第1周: 测试 + 监控 (P0)
- Day 1-2: 单元测试框架搭建，核心模块测试
- Day 3: 集成测试
- Day 4: CI/CD集成
- Day 5: Prometheus + Grafana监控
- Day 6-7: Sentry错误追踪 + 告警系统

### 第2周: 部署 + API (P1)
- Day 1-2: Docker容器化
- Day 3-4: Kubernetes编排
- Day 5-7: FastAPI后端开发

### 第3周: UI + 性能 (P1-P2)
- Day 1-2: Streamlit Dashboard
- Day 3-4: 并行处理优化
- Day 5: LLM批量推理
- Day 6-7: 智能缓存

### 第4周: 数据 + 安全 (P2)
- Day 1-3: 数据库设计 + 迁移
- Day 4-5: 数据版本控制 (DVC)
- Day 6: API认证 + 审计日志
- Day 7: Secrets管理

### 第5-6周: 文档 + 优化 (P2)
- Week 5: 完善文档 (API/用户/开发者)
- Week 6: 性能优化、Bug修复、上线准备

---

## 🎯 优先级建议

### 立即执行 (本周内)
1. ✅ **测试**: 至少补充核心模块单元测试 (覆盖率 >50%)
2. ✅ **监控**: Prometheus + Grafana基础监控
3. ✅ **Docker化**: 便于部署和分发

### 短期执行 (2周内)
4. ✅ **API**: FastAPI后端，提供RESTful接口
5. ✅ **Dashboard**: Streamlit可视化界面
6. ✅ **CI/CD**: 自动化测试和部署

### 中期执行 (1月内)
7. ✅ **数据库**: 迁移到PostgreSQL
8. ✅ **安全**: API认证、审计日志
9. ✅ **性能优化**: 并行处理、批量推理

### 长期执行 (3月内)
10. ✅ **K8s编排**: 生产级编排和扩展
11. ✅ **完整监控**: Sentry + AlertManager
12. ✅ **Secrets管理**: Vault集成

---

## 📊 预期效果

### 升级后系统能力

| 维度 | 升级前 | 升级后 | 提升 |
|------|--------|--------|------|
| **测试覆盖** | 5% | 80%+ | +1500% |
| **部署时间** | 30分钟 | <5分钟 | -83% |
| **错误发现** | 被动发现 | 主动告警 | 实时 |
| **处理能力** | 7药/次 | 100+药/次 | +1400% |
| **用户体验** | CLI | Web UI + API | 质的飞跃 |
| **可维护性** | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | +67% |
| **安全性** | ⭐⭐ | ⭐⭐⭐⭐⭐ | +150% |

### ROI分析

**投入**:
- 开发时间: 4-6周
- 人力成本: ~$10,000-15,000 (按$50/小时计算)
- 基础设施: ~$200/月 (K8s集群 + 监控)

**回报**:
- 研发效率提升: 3-5x (自动化测试、CI/CD)
- Bug修复时间: -70% (主动监控)
- 用户满意度: +80% (Web UI)
- 可扩展性: 10x+ (从7药到100+药)
- 安全事故风险: -90%

**结论**: **6个月内回本，长期ROI > 500%**

---

## ✅ 验收标准

### 工业级系统核查表

```
[✅] 测试
  [✅] 单元测试覆盖 >80%
  [✅] 集成测试覆盖所有关键路径
  [✅] CI/CD每次提交自动运行
  [✅] 性能基准测试存在

[✅] 监控
  [✅] Prometheus指标收集
  [✅] Grafana仪表盘可视化
  [✅] Sentry错误追踪
  [✅] AlertManager告警通知

[✅] 部署
  [✅] Docker容器化
  [✅] docker-compose本地部署
  [✅] Kubernetes生产部署
  [✅] 自动化部署脚本

[✅] 接口
  [✅] RESTful API (FastAPI)
  [✅] API文档 (Swagger)
  [✅] Web Dashboard (Streamlit)
  [✅] CLI保持可用

[✅] 性能
  [✅] 并行处理支持
  [✅] 批量推理优化
  [✅] 多级缓存
  [✅] 可水平扩展

[✅] 数据
  [✅] PostgreSQL存储
  [✅] DVC版本控制
  [✅] 自动备份
  [✅] 数据血缘追踪

[✅] 安全
  [✅] API认证 (JWT)
  [✅] 审计日志
  [✅] Secrets管理 (Vault)
  [✅] HTTPS加密

[✅] 文档
  [✅] API文档 (自动生成)
  [✅] 用户手册
  [✅] 开发者指南
  [✅] 运维手册
```

---

**路线图版本**: 1.0
**最后更新**: 2026-02-08
**状态**: 待执行
**预计完成时间**: 2026-03-20 (6周)
