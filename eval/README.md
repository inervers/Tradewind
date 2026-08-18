# 生成质量评测

`generate_quality_report.py` 默认使用 `fixtures/quality_cases.json` 做离线回放，验证两类不会消耗 token 的契约：

- `_rule_check` 的规则通过率与失败类型
- `_parse_judge_scores` 对 `valid`、`malformed`、`unavailable`、`disabled` 的状态归类

运行：

```powershell
python eval/generate_quality_report.py
```

产物写入 `outputs/eval/quality-report.json` 和 `outputs/eval/quality-report.md`。fixture 是脱敏合成数据，报告中的 `dataset.sha256` 与 `code_commit` 用于追溯。线上 LLM 评测不在默认路径，必须另行提供 provider、预算和脱敏数据后再启用。
