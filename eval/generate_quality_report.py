"""Generate a privacy-safe, reproducible quality report from offline fixtures.

The default mode never calls an LLM and never reads runtime data/ files. It
replays rule checks and judge-schema parsing against committed, synthetic cases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import types
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = Path(__file__).resolve().parent / "fixtures" / "quality_cases.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keep the offline evaluator runnable in a bare standard-library environment.
# The imported production module only needs these packages when an actual LLM
# generation is requested; this evaluator never makes that call.
if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *_args, **_kwargs: None
    sys.modules["dotenv"] = dotenv
if "langchain_openai" not in sys.modules:
    langchain_openai = types.ModuleType("langchain_openai")
    langchain_openai.ChatOpenAI = object
    sys.modules["langchain_openai"] = langchain_openai

from app.email_agent import _parse_judge_scores, _rule_check  # noqa: E402


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def evaluate(cases: list[dict]) -> dict:
    issue_counts: Counter[str] = Counter()
    judge_counts: Counter[str] = Counter()
    details: list[dict] = []
    elapsed = []

    for case in cases:
        issues = _rule_check(
            str(case.get("email", "")),
            [str(x) for x in case.get("product_kws", [])],
            [str(x) for x in case.get("identifiers", [])],
            language=str(case.get("language", "zh-hant")),
            format_=str(case.get("format", "email")),
        )
        for issue in issues:
            issue_counts[issue] += 1

        requested = bool(case.get("judge_requested"))
        raw = case.get("judge_raw")
        if not requested:
            scores, judge_status = None, "disabled"
        elif raw is None:
            scores, judge_status = None, "unavailable"
        else:
            scores, judge_status = _parse_judge_scores(str(raw))
        judge_counts[judge_status] += 1
        if isinstance(case.get("elapsed_ms"), (int, float)):
            elapsed.append(float(case["elapsed_ms"]))
        details.append({
            "id": str(case.get("id", "")),
            "rules_pass": not issues,
            "issues": issues,
            "judge_status": judge_status,
            "judge_overall": scores.get("overall") if scores else None,
        })

    total = len(cases)
    passed = sum(1 for item in details if item["rules_pass"])
    return {
        "schema_version": "twd-quality-v1",
        "mode": "offline_fixture",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "dataset": {"n": total, "sha256": hashlib.sha256(_canonical_bytes(cases)).hexdigest()},
        "code_commit": _git_commit(),
        "model": os.getenv("TRADEWIND_EVAL_MODEL", "offline-fixture"),
        "rules": {"passed": passed, "total": total, "pass_rate": round(passed / total, 4) if total else None, "issue_counts": dict(issue_counts)},
        "judge": {"counts": dict(judge_counts), "valid_rate_requested": round(judge_counts["valid"] / max(1, sum(v for k, v in judge_counts.items() if k != "disabled")), 4)},
        "latency": {"n": len(elapsed), "avg_ms": round(sum(elapsed) / len(elapsed), 1) if elapsed else None},
        "cases": details,
    }


def _markdown(report: dict) -> str:
    rules = report["rules"]
    judge = report["judge"]
    lat = report["latency"]
    lines = [
        "# Tradewind 生成质量评测报告",
        "",
        "> 口径：脱敏 synthetic fixture 离线回放；不调用 LLM，不读取 `data/` 运行时数据。",
        "",
        f"- Dataset：{report['dataset']['n']} cases；SHA-256 `{report['dataset']['sha256']}`",
        f"- Code commit：`{report.get('code_commit') or 'unknown'}`",
        f"- Model：`{report['model']}`",
        f"- 规则通过率：{rules['passed']}/{rules['total']}（{rules['pass_rate']}）",
        f"- Judge 状态：`{judge['counts']}`；请求 judge 的 valid rate：{judge['valid_rate_requested']}",
        f"- 平均耗时：{lat['avg_ms']} ms（{lat['n']} cases 有耗时字段）",
        "",
        "## Case 明细",
        "",
        "| ID | 规则 | Judge | Overall | Issues |",
        "|---|---|---|---:|---|",
    ]
    for item in report["cases"]:
        lines.append(f"| {item['id']} | {'PASS' if item['rules_pass'] else 'FAIL'} | {item['judge_status']} | {item['judge_overall'] if item['judge_overall'] is not None else '-'} | {'；'.join(item['issues']) or '-'} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--out-dir", type=Path, default=Path("outputs") / "eval")
    args = parser.parse_args()
    cases = json.loads(args.dataset.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not all(isinstance(x, dict) for x in cases):
        raise SystemExit("dataset must be a JSON array of objects")
    report = evaluate(cases)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "quality-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out_dir / "quality-report.md").write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({"out_dir": str(args.out_dir), "dataset_sha256": report["dataset"]["sha256"], "rules": report["rules"], "judge": report["judge"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
