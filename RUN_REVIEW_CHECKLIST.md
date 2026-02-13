# Drug Repurposing 人工审核清单

本清单是整个仓库的最小人工审核流程。  
每次运行使用一份（按 `run_id`），并将审核产物与当次输出一起归档。

## 1) 每次 Run 必做

- [ ] 冻结本次 `run_id` 和 manifest（配置、模型版本、输出目录）。
- [ ] 审核 trial screening 产物 `manual_review_queue.csv`。
- [ ] 审核 `manual_alias_review_queue.csv`；必要时更新 `manual_alias_overrides.csv`。
- [ ] 审核 Step7 的边缘候选（`MAYBE` / `explore`）及其关键文献。
- [ ] 生成并审核 reject 回抽队列（`build_reject_audit_queue.py` 输出 CSV）。
- [ ] Reviewer A/B 独立填写 `review_log_<run_id>.csv`。
- [ ] 在 `adjudication_<run_id>.md` 记录分歧仲裁结果。
- [ ] 在 `release_decision_<run_id>.md` 写最终放行结论。
- [ ] 将确认标注增量合并到 `data/gold_standard/gold_standard_v1.csv`。
- [ ] 重跑抽取评估（`eval_extraction.py`，建议含 holdout）并归档报告 JSON。

## 2) 触发告警时必做

- [ ] 泄漏审计失败（`pair_overlap` 不 clean）：立即停止放行，先修复 split/数据泄漏。
- [ ] `kg_explain` 出现 `Uncertainty quantification skipped`：修复前禁止放行。
- [ ] `sigreverse` 出现 `possible_toxicity_confounder=True`：补做文献核查并记录保留/剔除理由。
- [ ] release gate 被阻断（kill/miss/IRR 超阈）：做人工根因复核并形成书面决策。

## 3) 新疾病或大改配置时必做

- [ ] 重新审核 `dsmeta` 的数据集选择（`gse_list`）和 case/control 规则。
- [ ] 重新审核 dsmeta QC 报告（`outputs/reports/qc_summary.html`）。
- [ ] 若启用 KEGG，手工提供 `kegg.gmt` 并记录来源与许可。
- [ ] 重新校准疾病相关安全黑名单、门控阈值和验证标准。

## 4) 建议放行阈值

- [ ] `kill_rate <= 0.15`
- [ ] `miss_rate <= 0.10`
- [ ] `IRR (kappa) >= 0.60`
- [ ] 泄漏审计 `pair_overlap == 0`
- [ ] 最终日志无 uncertainty-skip 告警

## 5) 每次 Run 至少保留的产物

- [ ] `review_log_<run_id>.csv`
- [ ] `adjudication_<run_id>.md`
- [ ] `release_decision_<run_id>.md`
- [ ] Step6-9 manifest 与关键输出
- [ ] 评估报告 JSON（`eval_extraction`，若使用 holdout 也一起保留）
- [ ] reject audit 队列 CSV（含审核后版本）
