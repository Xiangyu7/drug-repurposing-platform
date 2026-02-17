# data/ 中间表（全自动重跑会生成）

## CT.gov 模式 + 共享文件

- failed_trials_drug_rows.csv         (CT.gov 拉取：失败/终止试验 + 干预名)
- failed_drugs_summary.csv            (按 drug 汇总)
- drug_rxnorm_map.csv                 (RxNorm 近似匹配)
- drug_canonical.csv                  (标准化药物名列表)
- drug_chembl_map.csv                 (药名 -> ChEMBL molecule)
- edge_drug_target.csv                (ChEMBL mechanism: Drug -> Target)
- target_xref.csv                     (ChEMBL target xref：含 UniProt / Ensembl)
- target_chembl_to_ensembl_all.csv    (Target -> ENSG)
- edge_target_pathway_all.csv         (Reactome: UniProt -> Pathway)
- edge_target_disease_ot.csv          (OpenTargets: ENSG -> Disease)
- edge_gene_pathway.csv               (join: ENSG -> Pathway)
- edge_pathway_disease.csv            (aggregate: Pathway -> Disease, 含 support_genes)
- edge_trial_ae.csv                   (试验停止原因 -> 安全/疗效分类)
- edge_drug_ae_faers.csv              (FAERS: Drug -> AE, 含 PRR)
- edge_disease_phenotype.csv          (OpenTargets: Disease -> Phenotype)

## Signature 模式新增

- drug_from_signature.csv             (基因签名反查结果：药物+靶点+签名基因+方向+权重)
- drug_known_indications.csv          (ChEMBL drug_indication: 各药物已知适应症)

## output/ 输出文件

- bridge_repurpose_cross.csv          Direction A: 跨疾病迁移 bridge (每药最高分疾病)
  列: drug_id, canonical_name, chembl_pref_name, max_mechanism_score, top_disease, final_score, n_trials, trial_statuses, trial_source, example_condition, why_stopped, **targets**, **target_details**

- bridge_origin_reassess.csv          Direction B: 原疾病重评估 bridge (目标疾病 + 文献注入)
  列: drug_id, canonical_name, chembl_pref_name, max_mechanism_score, max_mechanism_score_global, top_disease, final_score, endpoint_type, n_trials, trial_statuses, trial_source, example_condition, why_stopped, ci_lower, ci_upper, ci_width, confidence_tier, n_evidence_paths, source, **targets**, **target_details**
  source 列: "kg" (KG 发现), "literature" (文献注入), "kg+literature" (两者重叠)

### 靶点列说明 (2026-02-16 新增)

- **targets**: 人类可读靶点摘要 (分号分隔)
  示例: `PCSK9 (CHEMBL2929) [UniProt:Q8NBP7] [PDB+AlphaFold] — Subtilisin/kexin type 9 inhibitor`
- **target_details**: JSON 数组，每个靶点包含:
  - target_chembl_id, target_name, mechanism_of_action, uniprot
  - pdb_ids (前 5 个实验 PDB ID), pdb_count (PDB 条目总数)
  - has_alphafold (是否有 AlphaFold 预测)
  - structure_source: PDB+AlphaFold | PDB | AlphaFold_only | none

数据来源: edge_drug_target.csv + node_target.csv + target_xref.csv 自动 join

## 数据流

```
CT.gov 模式:
  Step 1  CT.gov ──→ failed_trials_drug_rows.csv, failed_drugs_summary.csv
  Step 2  RxNorm ──→ drug_rxnorm_map.csv
  Step 3  合并    ──→ drug_canonical.csv
  Step 4  ChEMBL ──→ drug_chembl_map.csv
  Step 5  靶点    ──→ edge_drug_target.csv

Signature 模式:
  Step 1  基因签名反查 ──→ drug_from_signature.csv
                          + drug_chembl_map.csv (兼容)
                          + edge_drug_target.csv (兼容)
          (Step 2-5 跳过)

共享步骤 (两种模式):
  Step 6  Xref    ──→ target_xref.csv, target_chembl_to_ensembl_all.csv
  Step 7  Reactome──→ edge_target_pathway_all.csv
  Step 8  OT      ──→ edge_target_disease_ot.csv
  Step 9  聚合    ──→ edge_gene_pathway.csv, edge_pathway_disease.csv, edge_trial_ae.csv
  Step 10 FAERS   ──→ edge_drug_ae_faers.csv
          表型    ──→ edge_disease_phenotype.csv
          适应症  ──→ drug_known_indications.csv (Signature 模式)
```
