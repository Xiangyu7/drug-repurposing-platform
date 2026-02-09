#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step6 LLM 单药诊断测试
======================
目的: 用 2 篇已知文献 (resveratrol + atherosclerosis) 调用 Ollama LLM,
     对比输出与人工标注的 ground truth, 定位问题.

运行: python tests/test_step6_llm_single.py

需要: Ollama 已启动, qwen2.5:7b-instruct 已拉取
"""

import json, sys, os, time, requests

# ===================================================================
# 配置 (和 Step6 一致)
# ===================================================================
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:7b-instruct")
OLLAMA_TIMEOUT = 120
OLLAMA_SCHEMA_TIMEOUT = 30  # schema mode often hangs, short timeout for diagnosis

# Step6 使用的 JSON schema
EVIDENCE_JSON_SCHEMA = {
    "type": "array",
    "maxItems": 2,
    "items": {
        "type": "object",
        "required": ["pmid", "supports", "direction", "model", "endpoint", "claim", "confidence"],
        "properties": {
            "pmid": {"type": "string"},
            "supports": {"type": "boolean"},
            "direction": {"type": "string", "enum": ["benefit", "harm", "neutral", "unknown"]},
            "model": {"type": "string", "enum": ["human", "animal", "cell", "unknown"]},
            "endpoint": {"type": "string"},
            "claim": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1}
        },
        "additionalProperties": True
    }
}

# ===================================================================
# 测试数据: 2 篇文献
# ===================================================================
TEST_CASES = [
    {
        "pmid": "24145604",
        "title": "Resveratrol attenuates vascular endothelial inflammation by inducing autophagy through the cAMP signaling pathway.",
        "abstract": (
            "Inflammation participates centrally in all stages of atherosclerosis (AS), "
            "which begins with inflammatory changes in the endothelium, characterized by "
            "expression of the adhesion molecules. Resveratrol (RSV) is a naturally occurring "
            "phytoalexin that can attenuate endothelial inflammation; however, the exact "
            "mechanisms have not been thoroughly elucidated. Autophagy refers to the normal "
            "process of cell degradation of proteins and organelles, and is protective against "
            "certain inflammatory injuries. Thus, we intended to determine the role of autophagy "
            "in the antiinflammatory effects of RSV in human umbilical vein endothelial cells "
            "(HUVECs). We found that RSV pretreatment reduced tumor necrosis factor (TNF)-induced "
            "inflammation and increased MAP1LC3B2 expression and SQSTM1/p62 degradation in a "
            "concentration-dependent manner. In conclusion, RSV attenuates endothelial inflammation "
            "by inducing autophagy, and the autophagy in part was mediated through the activation "
            "of the cAMP-PRKA-AMPK-SIRT1 signaling pathway."
        ),
        "ground_truth": {
            "pmid": "24145604",
            "supports": True,
            "direction": "benefit",
            "model": "cell",            # HUVECs = cell line
            "endpoint": "endothelial inflammation",
            "claim_keywords": ["resveratrol", "attenuate", "inflammation", "endothelial", "autophagy"],
            "confidence_range": [0.7, 1.0],
        }
    },
    {
        "pmid": "40043912",
        "title": "Pitavastatin and resveratrol bio-nanocomplexes against hyperhomocysteinemia-induced atherosclerosis via blocking ferroptosis-related lipid deposition.",
        "abstract": (
            "Atherosclerosis (AS) therapy has been commonly based on lipid-lowering agents "
            "(e.g., statins), supplemented by other therapies, such as anti-inflammatory agents "
            "and antioxidants. In the study, we constructed a macrophage targeted hybridization "
            "nanodrug of HMLRPP, which used Pit-loaded PLGA nanoparticles and Res-loaded liposomes "
            "as nano-core. In vivo studies demonstrated that HMLRPP NPs significantly attenuated "
            "plaque progression, characterized by decreased plaque area, less lipid deposition, "
            "and increased collagen. Meanwhile, HMLRPP NPs inhibited macrophage ferroptosis by "
            "decreasing the expression of BDH1, ORM1 and enhancing the expression of RPS27L, "
            "which resulted in the alleviation of lipid accumulation and inflammation. Our data "
            "suggest that the HMLRPP nanodrug delivery system with ferroptosis-regulating capability "
            "provides a feasible therapeutic strategy for atherosclerosis."
        ),
        "ground_truth": {
            "pmid": "40043912",
            "supports": True,
            "direction": "benefit",
            "model": "animal",           # in vivo studies
            "endpoint": "plaque progression / atherosclerosis",
            "claim_keywords": ["plaque", "atherosclerosis", "lipid", "ferroptosis"],
            "confidence_range": [0.7, 1.0],
        }
    },
]


# ===================================================================
# LLM 调用 (完全复制 Step6 的逻辑)
# ===================================================================
def call_llm(system_prompt: str, user_prompt: str, temperature: float = 0.2, use_schema: bool = True):
    """调用 Ollama chat API, 返回 raw content string."""
    url = f"{OLLAMA_HOST}/api/chat"
    payload = {
        "model": OLLAMA_LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": temperature},
        "stream": False,
    }
    if use_schema:
        payload["format"] = EVIDENCE_JSON_SCHEMA
    else:
        payload["format"] = "json"

    t0 = time.time()
    timeout = OLLAMA_SCHEMA_TIMEOUT if use_schema else OLLAMA_TIMEOUT
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        content = ((data.get("message") or {}).get("content") or "").strip()
        elapsed = time.time() - t0
        return content, elapsed, None
    except Exception as e:
        return None, time.time() - t0, str(e)


def extract_evidence_llm(drug, target_disease, endpoint_type, pmid, fragment):
    """复制自 Step6 的 extract_evidence_with_llm"""
    system = (
        "You extract citable evidence items from biomedical abstracts. "
        "Return STRICT JSON ONLY (no markdown). Output must be a JSON array of 0-2 objects. "
        "Each object must have keys: pmid, supports (true/false), direction (benefit|harm|neutral|unknown), "
        "model (human|animal|cell|unknown), endpoint (short phrase), claim (<=25 words), confidence (0-1)."
    )
    user = (
        f"DRUG={drug}\nTARGET={target_disease}\nENDPOINT_TYPE={endpoint_type}\nPMID={pmid}\n\n"
        f"TEXT:\n{fragment}\n\n"
        "Decide if the text supports repurposing for the TARGET (or its clinical spectrum if ENDPOINT_TYPE indicates it). "
        "Prefer extracting Results/Conclusions. If no actionable evidence, return []."
    )
    return system, user


# ===================================================================
# 评估函数
# ===================================================================
def evaluate(output, ground_truth):
    """对比 LLM 输出与 ground truth, 返回评分详情."""
    results = {"pass": True, "details": []}

    if not output or not isinstance(output, list) or len(output) == 0:
        results["pass"] = False
        results["details"].append("❌ LLM 返回空或非数组")
        return results

    # 取第一条 evidence
    ev = output[0] if isinstance(output[0], dict) else {}

    # 1. PMID 是否正确
    if str(ev.get("pmid", "")).strip() == str(ground_truth["pmid"]):
        results["details"].append(f"✅ PMID 正确: {ev['pmid']}")
    else:
        results["details"].append(f"❌ PMID 错误: 期望 {ground_truth['pmid']}, 得到 {ev.get('pmid','')}")
        results["pass"] = False

    # 2. supports
    if ev.get("supports") == ground_truth["supports"]:
        results["details"].append(f"✅ supports 正确: {ev['supports']}")
    else:
        results["details"].append(f"❌ supports 错误: 期望 {ground_truth['supports']}, 得到 {ev.get('supports','')}")
        results["pass"] = False

    # 3. direction
    if ev.get("direction") == ground_truth["direction"]:
        results["details"].append(f"✅ direction 正确: {ev['direction']}")
    else:
        results["details"].append(f"⚠️ direction 偏差: 期望 {ground_truth['direction']}, 得到 {ev.get('direction','')}")

    # 4. model
    if ev.get("model") == ground_truth["model"]:
        results["details"].append(f"✅ model 正确: {ev['model']}")
    else:
        results["details"].append(f"⚠️ model 偏差: 期望 {ground_truth['model']}, 得到 {ev.get('model','')}")

    # 5. endpoint (关键词匹配)
    ep = str(ev.get("endpoint", "")).lower()
    ep_truth = ground_truth["endpoint"].lower()
    if any(w in ep for w in ep_truth.split("/")):
        results["details"].append(f"✅ endpoint 相关: '{ev.get('endpoint','')}'")
    else:
        results["details"].append(f"⚠️ endpoint 可能偏离: 期望含 '{ground_truth['endpoint']}', 得到 '{ev.get('endpoint','')}'")

    # 6. claim 关键词覆盖
    claim = str(ev.get("claim", "")).lower()
    gt_kws = ground_truth["claim_keywords"]
    hits = [kw for kw in gt_kws if kw.lower() in claim]
    miss = [kw for kw in gt_kws if kw.lower() not in claim]
    if len(hits) >= len(gt_kws) * 0.6:
        results["details"].append(f"✅ claim 关键词覆盖 {len(hits)}/{len(gt_kws)}: {hits}")
    else:
        results["details"].append(f"⚠️ claim 关键词覆盖不足 {len(hits)}/{len(gt_kws)}: 命中={hits}, 缺失={miss}")

    # 7. confidence 范围
    conf = ev.get("confidence", 0)
    lo, hi = ground_truth["confidence_range"]
    if lo <= conf <= hi:
        results["details"].append(f"✅ confidence 在范围内: {conf} ∈ [{lo},{hi}]")
    else:
        results["details"].append(f"⚠️ confidence 偏离: {conf} ∉ [{lo},{hi}]")

    # 8. claim 长度 (<= 25 words as instructed)
    words = len(str(ev.get("claim", "")).split())
    if words <= 25:
        results["details"].append(f"✅ claim 长度合规: {words} 词 (≤25)")
    else:
        results["details"].append(f"⚠️ claim 过长: {words} 词 (要求 ≤25)")

    return results


# ===================================================================
# 主流程
# ===================================================================
def main():
    print("=" * 70)
    print("Step6 LLM 单药诊断测试")
    print(f"模型: {OLLAMA_LLM_MODEL}")
    print(f"Ollama: {OLLAMA_HOST}")
    print("=" * 70)

    # 0. 检查 Ollama 连通性
    print("\n[0] 检查 Ollama 连通性...")
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
        print(f"    ✅ Ollama 可达, 模型列表: {models}")
        if OLLAMA_LLM_MODEL not in models:
            print(f"    ❌ 所需模型 {OLLAMA_LLM_MODEL} 不在列表中!")
            sys.exit(1)
    except Exception as e:
        print(f"    ❌ 无法连接 Ollama: {e}")
        sys.exit(1)

    all_pass = True

    for i, tc in enumerate(TEST_CASES, 1):
        print(f"\n{'='*70}")
        print(f"[测试 {i}] PMID={tc['pmid']}")
        print(f"  标题: {tc['title'][:80]}...")
        print(f"  Ground Truth: supports={tc['ground_truth']['supports']}, "
              f"direction={tc['ground_truth']['direction']}, "
              f"model={tc['ground_truth']['model']}")
        print("-" * 70)

        # 构建 prompt (和 Step6 完全一致)
        system, user = extract_evidence_llm(
            drug="resveratrol",
            target_disease="atherosclerosis",
            endpoint_type="PLAQUE",
            pmid=tc["pmid"],
            fragment=f"{tc['title']}\n{tc['abstract']}"
        )

        # ---- Test A: 使用 JSON schema 约束 (Step6 默认) ----
        print("\n  [A] 使用 format=EVIDENCE_JSON_SCHEMA (Step6 默认):")
        raw_a, elapsed_a, err_a = call_llm(system, user, temperature=0.2, use_schema=True)

        if err_a:
            print(f"    ❌ LLM 调用失败: {err_a}")
            print(f"    → 这就是 fallback 的根因! schema 模式超时/报错, LLM 调用返回 None")
            print(f"    → Step6 代码中 extract_evidence_with_llm 返回 [] → 走 rule fallback")
            all_pass = False
        else:
            print(f"    耗时: {elapsed_a:.1f}s")
            print(f"    原始返回 (前 500 字符):\n    {(raw_a or '')[:500]}")

            # 解析
            parsed_a = None
            try:
                parsed_a = json.loads(raw_a)
            except Exception as e:
                print(f"    ❌ JSON 解析失败: {e}")
                import re
                m = re.search(r"\[.*\]", raw_a or "", re.S)
                if m:
                    try:
                        parsed_a = json.loads(m.group())
                        print(f"    ⚠️ 通过 regex 修复后解析成功")
                    except:
                        pass

            if parsed_a is None:
                print(f"    ❌ 无法解析为有效 JSON")
                all_pass = False
            else:
                # 处理 Ollama 包装: {"array": [...]}
                if isinstance(parsed_a, dict):
                    for key in ["array", "items", "evidence"]:
                        if isinstance(parsed_a.get(key), list):
                            parsed_a = parsed_a[key]
                            print(f"    ⚠️ Ollama 包装了 dict, 从 key='{key}' 取出数组")
                            break

                print(f"    解析结果 (类型={type(parsed_a).__name__}, 长度={len(parsed_a) if isinstance(parsed_a, list) else 'N/A'}):")
                print(f"    {json.dumps(parsed_a, indent=2, ensure_ascii=False)[:600]}")

                eval_result = evaluate(parsed_a, tc["ground_truth"])
                print(f"\n    评估结果: {'✅ PASS' if eval_result['pass'] else '❌ FAIL'}")
                for detail in eval_result["details"]:
                    print(f"    {detail}")
                if not eval_result["pass"]:
                    all_pass = False

        # ---- Test B: 使用 format="json" (降级模式) ----
        print(f"\n  [B] 使用 format='json' (降级模式, 对比用):")
        raw_b, elapsed_b, err_b = call_llm(system, user, temperature=0.2, use_schema=False)

        if err_b:
            print(f"    ❌ LLM 调用失败: {err_b}")
        else:
            print(f"    耗时: {elapsed_b:.1f}s")
            print(f"    原始返回 (前 500 字符):\n    {(raw_b or '')[:500]}")

            parsed_b = None
            try:
                parsed_b = json.loads(raw_b)
            except:
                import re
                m = re.search(r"\[.*\]", raw_b or "", re.S)
                if m:
                    try:
                        parsed_b = json.loads(m.group())
                    except:
                        pass
            if isinstance(parsed_b, dict):
                for key in ["array", "items", "evidence"]:
                    if isinstance(parsed_b.get(key), list):
                        parsed_b = parsed_b[key]
                        break

            if parsed_b:
                eval_b = evaluate(parsed_b, tc["ground_truth"])
                print(f"    评估结果: {'✅ PASS' if eval_b['pass'] else '❌ FAIL'}")
                for detail in eval_b["details"]:
                    print(f"    {detail}")

    # ---- 总结 ----
    print(f"\n{'='*70}")
    print(f"总结: {'✅ 全部通过' if all_pass else '❌ 有测试失败'}")
    print("=" * 70)

    if not all_pass:
        print("\n诊断建议:")
        print("  1. 如果 JSON schema 模式失败但 json 模式成功 → Ollama 版本不支持 schema format, 设置 USE_CHAT_SCHEMA=0")
        print("  2. 如果 LLM 返回空 → 检查模型是否正确加载, 试试 temperature=0.0")
        print("  3. 如果 PMID 不对 → LLM 没正确使用 prompt 中的 PMID, 需要在 post-process 中强制覆盖")
        print("  4. 如果 direction/model 偏差 → 正常, 7B 模型的分类能力有限")
        print("  5. 如果 claim 过长 → 在 prompt 中加强 '<=25 words' 约束")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
