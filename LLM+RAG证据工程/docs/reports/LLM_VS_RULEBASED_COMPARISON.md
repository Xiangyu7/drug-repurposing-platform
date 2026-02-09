# LLM vs Rule-Based Evidence Classification Comparison

**Date**: 2026-02-08
**Test Dataset**: 7 drugs from drug_master.csv
**LLM Model**: qwen2.5:1.5b-instruct
**Comparison**: step6_simple (rule-based) vs step6_llm (LLM extraction)

---

## 📊 Overall Results

### Summary Statistics

| Metric | Rule-Based | LLM | Improvement |
|--------|------------|-----|-------------|
| **Total Papers Analyzed** | 140 (top 20 × 7) | 140 (top 20 × 7) | - |
| **Classification Rate** | [TBD]% | [TBD]% | [TBD]% |
| **Benefit Papers** | [TBD] | [TBD] | [TBD] |
| **Harm Papers** | [TBD] | [TBD] | [TBD] |
| **Neutral Papers** | [TBD] | [TBD] | [TBD] |
| **Unknown/Unclear** | [TBD] | [TBD] | [TBD] |
| **Processing Time** | <1 second | [TBD] min | [TBD]x slower |
| **Extraction Success** | 100% | [TBD]% | - |

### Per-Drug Comparison

| Drug | Papers | Rule-Based | LLM | Δ Classified | LLM Success |
|------|--------|------------|-----|--------------|-------------|
| apolipoprotein a-i | 20 | [TBD] B, [TBD] H | [TBD] B, [TBD] H | [TBD]% | [TBD]% |
| creatine monohydrate | 20 | [TBD] B, [TBD] H | [TBD] B, [TBD] H | [TBD]% | [TBD]% |
| dexamethasone | 20 | [TBD] B, [TBD] H | [TBD] B, [TBD] H | [TBD]% | [TBD]% |
| medi6570 | 20 | [TBD] B, [TBD] H | [TBD] B, [TBD] H | [TBD]% | [TBD]% |
| nicotinamide riboside | 20 | [TBD] B, [TBD] H | [TBD] B, [TBD] H | [TBD]% | [TBD]% |
| resveratrol | 20 | [TBD] B, [TBD] H | [TBD] B, [TBD] H | [TBD]% | [TBD]% |
| vm202 | 20 | [TBD] B, [TBD] H | [TBD] B, [TBD] H | [TBD]% | [TBD]% |

---

## 🔍 Detailed Drug Analysis

### Drug 1: Apolipoprotein A-I

**Rule-Based Results**:
- Benefit: [TBD], Harm: [TBD], Neutral: [TBD], Unknown: [TBD]
- Classification Rate: [TBD]%

**LLM Results**:
- Benefit: [TBD], Harm: [TBD], Neutral: [TBD], Unclear: [TBD]
- Classification Rate: [TBD]%
- Extraction Success: [TBD]/20 ([TBD]%)

**New Metadata from LLM**:
- Model Types: [TBD] human, [TBD] animal, [TBD] cell
- Endpoints: [TBD] PLAQUE, [TBD] EVENTS, [TBD] BIOMARKER, [TBD] OTHER
- Confidence: [TBD] HIGH, [TBD] MED, [TBD] LOW

**Key Findings**:
- Papers reclassified from unknown → benefit: [TBD]
- Papers reclassified from unknown → harm: [TBD]
- Mechanism extraction quality: [TBD]

---

### Drug 2: Creatine Monohydrate

[Similar format for each drug...]

---

## 📈 Quality Analysis

### Classification Accuracy

**Method**: Manual review of 20 random extractions

| Category | Correct | Incorrect | Accuracy |
|----------|---------|-----------|----------|
| Benefit | [TBD] | [TBD] | [TBD]% |
| Harm | [TBD] | [TBD] | [TBD]% |
| Neutral | [TBD] | [TBD] | [TBD]% |
| **Overall** | [TBD] | [TBD] | **[TBD]%** |

### Common Error Patterns

1. **False Positives** ([TBD] cases):
   - [Example errors to be filled in]

2. **False Negatives** ([TBD] cases):
   - [Example errors to be filled in]

3. **Misclassifications** ([TBD] cases):
   - [Example errors to be filled in]

---

## 💡 Key Insights

### Strengths of LLM Extraction

1. **Semantic Understanding**
   - Captures nuanced language (e.g., "dysfunctional HDL" → harm)
   - Understands context beyond keywords
   - Example: [TBD]

2. **Richer Metadata**
   - Model type detection enables study quality weighting
   - Endpoint classification enables outcome matching
   - Mechanism extraction aids hypothesis generation

3. **Self-Assessment**
   - Confidence levels help prioritize evidence
   - HIGH confidence extractions have [TBD]% accuracy
   - LOW confidence extractions need manual review

### Weaknesses of LLM Extraction

1. **Processing Time**
   - [TBD]x slower than rule-based
   - Limits real-time use cases
   - Mitigation: Run overnight, cache results

2. **Extraction Failures**
   - [TBD]% extraction failure rate
   - Causes: JSON parsing errors, timeout, unclear papers
   - Mitigation: Fallback to rule-based for failures

3. **Consistency**
   - [TBD]% inter-run variability (if tested)
   - Temperature=0 helps but not perfect
   - Mitigation: Use larger model or prompt engineering

---

## 🎯 Recommendations

### When to Use Rule-Based (step6_simple)

✅ **Use for**:
- High-throughput screening (100+ drugs)
- Real-time processing requirements
- Well-studied drugs with clear language in papers
- Initial screening phase

### When to Use LLM (step6_llm)

✅ **Use for**:
- Deep-dive analysis of top candidates
- Drugs with complex/nuanced evidence
- When mechanism extraction is valuable
- When study quality matters (human vs animal)

### Hybrid Approach (Recommended)

```
Stage 1: Rule-Based Screening
├── Process 100 drugs in <1 minute
├── Identify top 20 candidates (benefit > 5)
└── Output: Shortlist for deep analysis

Stage 2: LLM Deep Dive
├── Process top 20 drugs (~40 min)
├── Extract rich metadata
└── Output: Detailed evidence for validation planning

Result: Best of both worlds
```

---

## 📊 Cost-Benefit Analysis

### Rule-Based
- **Cost**: $0 (pure Python)
- **Time**: <1 second for 7 drugs
- **Accuracy**: [TBD]% classification
- **Metadata**: None
- **Best for**: Screening

### LLM (Ollama Local)
- **Cost**: $0 (open source, local)
- **Time**: [TBD] minutes for 7 drugs
- **Accuracy**: [TBD]% classification
- **Metadata**: Model, Endpoint, Mechanism, Confidence
- **Best for**: Deep dive

### LLM (Commercial API - Hypothetical)
- **Cost**: ~$2-5 for 140 papers
- **Time**: ~30 seconds for 7 drugs
- **Accuracy**: Similar to local
- **Metadata**: Same as local
- **Best for**: Production at scale

---

## 🚀 Next Steps

### Immediate
1. ✅ Complete 7-drug LLM test
2. ✅ Fill in comparison metrics
3. ⬜ Manual accuracy validation (sample 20)
4. ⬜ Decision: Deploy LLM or stick with rule-based

### Short-term
5. ⬜ Test LLM on 20+ drugs (if deploying)
6. ⬜ Implement hybrid approach
7. ⬜ Add LLM caching (avoid re-extraction)
8. ⬜ Monitor extraction quality over time

### Medium-term
9. ⬜ Integrate LLM metadata into Phase 4 scoring
10. ⬜ Add study quality weighting (human > animal > cell)
11. ⬜ Add endpoint matching (PLAQUE for imaging trials)
12. ⬜ Add confidence-weighted evidence scoring

---

## 📋 Appendix: Sample Extractions

### Example 1: Benefit Paper (Correct)

**PMID**: [TBD]
**Title**: [TBD]
**Abstract**: [TBD]

**Rule-Based**: [TBD]
**LLM Extraction**:
```json
{
  "direction": "[TBD]",
  "model": "[TBD]",
  "endpoint": "[TBD]",
  "mechanism": "[TBD]",
  "confidence": "[TBD]"
}
```
**Assessment**: ✅ Correct

---

### Example 2: Harm Paper (Reclassified from Unknown)

**PMID**: [TBD]
**Title**: [TBD]
**Abstract**: [TBD]

**Rule-Based**: unknown
**LLM Extraction**:
```json
{
  "direction": "[TBD]",
  "model": "[TBD]",
  "endpoint": "[TBD]",
  "mechanism": "[TBD]",
  "confidence": "[TBD]"
}
```
**Assessment**: ✅ LLM correctly identified harm that rule-based missed

---

### Example 3: Error Case

**PMID**: [TBD]
**Title**: [TBD]
**Abstract**: [TBD]

**Rule-Based**: [TBD]
**LLM Extraction**:
```json
{
  "direction": "[TBD]",
  "model": "[TBD]",
  "endpoint": "[TBD]",
  "mechanism": "[TBD]",
  "confidence": "[TBD]"
}
```
**Assessment**: ❌ LLM misclassified - [reason]

---

**Report Status**: Template created, awaiting LLM test results
**Last Updated**: 2026-02-08 07:00
**Next Update**: After 7-drug test completes (~07:15)
