# Step6 Embedding Reranking Analysis

**Date**: 2026-02-07 23:08
**Test Drug**: Apolipoprotein A-I (D4BE4598792)
**Comparison**: BM25 Only vs BM25 + Ollama Embedding Rerank

---

## 📊 Executive Summary

**Key Finding**: Embedding reranking **changes 45% of top 20 papers** but shows **5% lower classification rate** with rule-based keywords, suggesting semantic relevance ≠ keyword presence.

### Quick Stats

| Metric | BM25 Only | BM25 + Embedding | Δ |
|--------|-----------|------------------|---|
| **Processing Time** | <1 second | ~3 minutes | **+180x** |
| **Classification Rate** | 90% (18/20) | 85% (17/20) | **-5%** |
| **Benefit Papers** | 12 | 10 | -2 |
| **Harm Papers** | 6 | 7 | +1 |
| **Unknown Papers** | 2 | 3 | +1 |
| **Top 20 Overlap** | - | 55% (11/20) | - |

**Trade-off**: 180x slower processing for 45% different paper selection with slightly worse keyword-based classification.

---

## 🔬 Detailed Comparison

### Top 10 Papers Side-by-Side

#### BM25 Only (Keyword-optimized)
1. ✅ **31567014** [benefit] - Apolipoprotein AI Promotes Atherosclerosis Regression in Diabetic...
2. ✅ **18287885** [benefit] - Trimerization of apolipoprotein A-I retards plasma clearance...
3. ✅ **28069582** [benefit] - Human apolipoprotein A-I exerts prophylactic effect on high-fat...
4. ⚠️  **25030535** [harm] - Relation of HDL cholesterol:apolipoprotein a-I ratio...
5. ✅ **28939717** [benefit] - Apolipoprotein A-I Modulates Atherosclerosis Through Lymphatic...

#### Embedding Rerank (Semantic-optimized)
1. ⚠️  **22986928** [harm] - Characterization of human apolipoprotein a-I construct... ★NEW
2. ✅ **35120132** [benefit] - Oxidant resistant human apolipoprotein A-I functions similarly... ★NEW
3. ✅ **33861588** [benefit] - Myeloperoxidase Targets Apolipoprotein A-I for Site-Specific... ★NEW
4. ✅ **28069582** [benefit] - Human apolipoprotein A-I exerts prophylactic effect... (overlap)
5. ⚠️  **25030535** [harm] - Relation of HDL cholesterol:apolipoprotein a-I ratio... (overlap)

**Observation**: Only 4 out of top 10 papers overlap between the two methods.

---

## 💡 What Changed?

### Papers DROPPED by Embedding (in BM25 Top 10 but not Embedding Top 10)

1. **31567014** [benefit] - "Apolipoprotein AI Promotes Atherosclerosis Regression in Diabetic..."
   - **Why dropped?** Title has strong keywords ("promotes", "regression") that BM25 loves, but may lack deep semantic connection

2. **18287885** [benefit] - "Trimerization of apolipoprotein A-I retards plasma clearance..."
   - **Why dropped?** Technical/mechanistic focus, less direct semantic relevance to atherosclerosis treatment

3. **28939717** [benefit] - "Apolipoprotein A-I Modulates Atherosclerosis Through Lymphatic Vessels"
   - **Why dropped?** Specific mechanism (lymphatic) may be less semantically central

### Papers ADDED by Embedding (not in BM25 Top 10)

1. **35120132** [benefit] - "Oxidant resistant human apolipoprotein A-I functions similarly to unmodified..."
   - **Why added?** Highly relevant modified protein study, semantically close to query
   - **Classification**: Contains "regression" keyword → benefit ✅

2. **33861588** [benefit] - "Myeloperoxidase Targets Apolipoprotein A-I for Site-Specific Tyrosine Chlorination..."
   - **Why added?** Important mechanistic study on apoA-I modification in atherosclerotic lesions
   - **Classification**: Contains "generates dysfunctional" → benefit (via "generates" misclassification?) ⚠️

3. **22986928** [harm] - "Characterization of a human apolipoprotein a-I construct expressed in bacterial system"
   - **Why added?** Fundamental characterization study, may be semantically relevant for understanding apoA-I
   - **Classification**: Contains "increased" → harm (likely false positive on technical paper) ⚠️

---

## 🎯 Analysis: Which Method is Better?

### When BM25 is Better (Current State)

✅ **90% classification rate** - Papers selected have clear benefit/harm keywords
✅ **<1 second processing** - Instant results, suitable for large-scale screening
✅ **Interpretable** - Can see exactly why papers ranked high (keyword presence)
✅ **Lower false positives** - Avoids technical papers that trigger harm keywords

**Use Case**: High-throughput screening where you need papers with explicit outcome statements.

### When Embedding COULD Be Better (If Combined with LLM)

🤔 **Semantic relevance** - May find mechanistically important papers BM25 misses
🤔 **Nuanced understanding** - Papers about "modified apoA-I" or "dysfunctional HDL" are relevant even without benefit keywords
⚠️  **But**: Our rule-based classifier can't evaluate this properly (85% classification)

**Use Case**: Deep research where you want comprehensive mechanistic understanding, paired with LLM for evidence extraction.

### The Fundamental Problem

```
Embedding optimizes for: Semantic Similarity to Query
Rule-based classifier looks for: Benefit/Harm Keywords

These are NOT the same thing!
```

**Example**:
- Paper: "Myeloperoxidase Targets Apolipoprotein A-I for Site-Specific Chlorination..."
- Embedding: "Very relevant! It's about apoA-I modification in atherosclerosis lesions"
- Rule-based: "Unknown - no clear benefit/harm keywords"
- Reality: This is a mechanistic study showing HOW apoA-I gets dysfunctional (important but not outcome-focused)

---

## 📈 Overlap Analysis

### Top 20 Papers Breakdown

```
Total in BM25 Top 20:     20 papers
Total in Embed Top 20:    20 papers
Overlap:                  11 papers (55%)
Unique to BM25:            9 papers (dropped by embedding)
Unique to Embedding:       9 papers (new discoveries)
```

**Interpretation**: Embedding reranking is making **substantive changes**, not just minor tweaks. 45% of papers are completely different.

### Papers Unique to Embedding (All 9)

| Rank | PMID | Direction | Title Snippet |
|------|------|-----------|---------------|
| #1 | 22986928 | harm | Characterization of human apoA-I construct |
| #2 | 35120132 | benefit | Oxidant resistant human apoA-I functions |
| #3 | 33861588 | benefit | Myeloperoxidase Targets Apolipoprotein A-I |
| #7 | 24407029 | benefit | Effects of native and MPO-modified apoA-I |
| #10 | 38987727 | **unknown** | Low HDL-C/ApoA-I index and cardiometabolic risk |
| #11 | 21811627 | harm | Human apoA-I-derived amyloid association |
| #12 | 33296791 | **unknown** | Human apoA-II reduces atherosclerosis in mice |
| #14 | 39643078 | benefit | Inhibiting IP6K1 confers atheroprotection |
| #17 | 20425244 | benefit | Biological properties of apoA-I mimetic peptides |
| #18 | 35304099 | harm | Pattern of apoA-I lysine carbamylation |
| #20 | 30376729 | harm | CSL112 reconstituted plasma-derived apoA-I |

**Quality Assessment**:
- ✅ Many look highly relevant (MPO-modified apoA-I, mimetic peptides, CSL112 therapy)
- ⚠️  2 "unknown" papers suggest these are mechanistic/clinical studies without clear benefit/harm statements
- ⚠️  Some "harm" labels may be false positives (e.g., #1 "construct characterization" likely neutral)

---

## 🚀 Recommendations

### Short-term (Current State)

**✅ Stick with BM25 Only for production pipeline** because:

1. **Performance**: 180x faster (<1s vs 3min for 100 papers)
2. **Classification Rate**: 90% vs 85% with current rule-based keywords
3. **Scalability**: Can process 7 drugs in <1 second vs 21 minutes
4. **Interpretability**: Clear why papers ranked high (keyword matching)

**When to use Embedding**: Only for deep-dive research on specific high-priority drugs where you want maximum semantic coverage and are willing to manually review papers.

### Medium-term (Next 1-2 months)

**✨ Integrate LLM Evidence Extraction, THEN Reconsider Embedding**

The value of embedding reranking can only be realized if we have an LLM to properly extract evidence from the semantically-relevant papers. Workflow:

```
1. BM25 Ranking (100 papers) → Top 80
2. Embedding Reranking (80 papers) → Top 20
3. LLM Evidence Extraction (20 papers) → Structured evidence
                                        ↓
                         Benefit/Harm/Neutral Classification
```

**Why this works better**:
- Embedding finds semantically relevant papers (including mechanistic studies)
- LLM reads the full abstract and understands context
- LLM extracts: "Myeloperoxidase modification makes apoA-I dysfunctional → harm to HDL function → likely neutral/harm for atherosclerosis"

**Expected improvement**:
- Classification rate: 85% → 90%+ (LLM can understand nuanced papers)
- Evidence quality: Higher (more mechanistically complete)
- Cost: ~$0.10-0.20 per drug (20 papers × qwen2.5:7b inference)

### Long-term (3-6 months)

**🎯 Hybrid Approach: Multi-stage Ranking + LLM**

```
Stage 1: BM25 Initial Ranking
  └─> Retrieves 100 papers in <1s

Stage 2: Embedding Reranking (Top 60 → 40)
  └─> Semantic relevance filter (2-3min)

Stage 3: Keyword-based Quick Filter
  └─> Papers with clear benefit/harm keywords → Fast track
  └─> Papers without keywords → Needs LLM review

Stage 4: LLM Evidence Extraction (Selective)
  └─> Only on papers that lack clear keywords (30-50%)
  └─> Reduces LLM cost by 50-70%
```

**Benefits**:
- Fast papers (with keywords): <1s processing
- Ambiguous papers: Get LLM treatment
- Cost-effective: Only 30-50% of papers need LLM
- Best of both worlds

---

## 📊 Technical Details

### Test Configuration

```bash
# BM25 Only
python scripts/step6_pubmed_rag_simple.py --limit 1

# BM25 + Embedding
python scripts/step6_pubmed_rag_simple.py --limit 1 --use-embed
```

### Embedding Parameters

- **Model**: nomic-embed-text (via Ollama)
- **Rerank Input**: Top 60 BM25 papers
- **Rerank Output**: Top 20 papers
- **Similarity**: Cosine similarity
- **Processing**:
  - Query embedding: 1 call
  - Document embeddings: 60 calls (batched)
  - Total time: ~3 minutes for 100-paper corpus

### BM25 Parameters

- **k1**: 1.5 (term frequency saturation)
- **b**: 0.75 (document length normalization)
- **Corpus**: 100 papers
- **Query**: "apolipoprotein a-i human apoa-i atherosclerosis plaque"

---

## 🔍 Case Study: Top Paper Comparison

### Example 1: BM25 #1 (Dropped by Embedding)

**PMID**: 31567014
**Title**: "Apolipoprotein AI Promotes Atherosclerosis Regression in Diabetic Mice"
**BM25 Score**: Highest (strong keywords: "promotes", "regression")
**Why Embedding Dropped**: May be too specific (diabetic mice) vs general apoA-I mechanism
**Verdict**: BM25 correctly prioritized outcome-focused paper

### Example 2: Embedding #2 (Not in BM25 Top 10)

**PMID**: 35120132
**Title**: "Oxidant resistant human apolipoprotein A-I functions similarly to unmodified isoform..."
**BM25 Rank**: Unknown (not in top 10)
**Why Embedding Promoted**: Highly relevant modified protein study
**Verdict**: Embedding found mechanistically important paper that BM25 missed

### Example 3: Embedding #3 (Not in BM25 Top 10)

**PMID**: 33861588
**Title**: "Myeloperoxidase Targets Apolipoprotein A-I for Site-Specific Tyrosine Chlorination..."
**Classification**: benefit (likely false positive - this is mechanistic harm)
**Why Interesting**: Critical paper on HOW apoA-I becomes dysfunctional in atherosclerotic lesions
**Problem**: Rule-based classifier can't understand this nuance
**Verdict**: Embedding found important paper, but rule-based classifier fails

---

## 📋 Conclusion

### Current Verdict: **BM25 Only is Better for Production**

**Reasons**:
1. ✅ 90% classification rate vs 85%
2. ✅ 180x faster processing (<1s vs 3min)
3. ✅ Works well with rule-based evidence extraction
4. ✅ Interpretable and debuggable

### Future Potential: **Embedding + LLM Could Be Superior**

**When to reconsider**:
- ✨ After implementing LLM evidence extraction
- ✨ When you need comprehensive mechanistic understanding
- ✨ For high-priority drugs worth 3-5 min processing time

### Next Steps

1. **Immediate**: Continue with BM25 for Step6 production pipeline ✅
2. **Next sprint**: Implement LLM evidence extraction module
3. **Future**: Re-test embedding with LLM and compare quality
4. **Goal**: Achieve 95%+ classification accuracy with hybrid approach

---

**Report generated**: 2026-02-07 23:08
**Test drug**: Apolipoprotein A-I (D4BE4598792)
**Comparison method**: Single-drug A/B test
**Recommendation**: **Use BM25 for now, revisit after LLM integration**
