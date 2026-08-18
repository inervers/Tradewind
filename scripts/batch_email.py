"""批量生成开发信：读客户名单 → 逐客户生成 → 输出到 outputs/。

客户名单 CSV 格式（爬虫产出或手动整理）：
    name,country,city,email,website,notes
    Glow Skin Clinic,Spain,Madrid,,glowskin.es,"激光脱毛为主"

用法：
    python scripts/batch_email.py --file data/customers.csv --product 激光脱毛仪
    python scripts/batch_email.py --file data/customers.csv --product 皮秒 --judge --limit 5
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

# 允许从 scripts/ 目录直接运行：把项目根加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ROOT_DIR
from app.email_agent import generate_email

OUTPUT_DIR = ROOT_DIR / "outputs"


def main() -> None:
    parser = argparse.ArgumentParser(description="Tradewind 批量开发信生成")
    parser.add_argument("--file", required=True, help="客户名单 CSV")
    parser.add_argument("--product", default="医美设备", help="产品类别")
    parser.add_argument("--judge", action="store_true", help="每封都 LLM 打分（更贵）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个客户")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    csv_path = Path(args.file)
    if not csv_path.exists():
        print(f"客户名单不存在: {csv_path}")
        return

    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    if args.limit:
        rows = rows[: args.limit]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    print(f"批量生成 {len(rows)} 封开发信（产品: {args.product}）...\n")

    summary: list[dict] = []
    for i, row in enumerate(rows, 1):
        name = (row.get("name") or "").strip()
        country = (row.get("country") or "").strip()
        notes = (row.get("notes") or "").strip()
        if not name:
            print(f"[{i}/{len(rows)}] 跳过空名称行")
            continue

        try:
            result = generate_email(
                name, country, args.product,
                judge=args.judge, verbose=args.verbose, extra=notes,
            )
        except Exception as exc:  # noqa: BLE001 - 单个客户失败不中断批量
            print(f"[{i}/{len(rows)}] ✗ {name}: {exc}")
            continue

        # 落盘（客户名做安全文件名）
        safe = name.replace("/", "_").replace("\\", "_").replace(":", "：")[:40]
        out = OUTPUT_DIR / f"{i:02d}_{safe}.txt"
        out.write_text(result["email"], encoding="utf-8")
        score = (result["scores"] or {}).get("overall", "-") if result["scores"] else "-"
        status = "✓" if not result["issues"] else "△"
        print(f"[{i}/{len(rows)}] {status} {name} ({country}) score={score} → {out.name}")
        summary.append(
            {"name": name, "country": country, "score": score,
             "issues": len(result["issues"]), "file": out.name}
        )

    # 汇总表
    total = time.time() - t0
    print(f"\n完成：{len(summary)}/{len(rows)} 封，耗时 {total:.0f}s")
    if summary:
        ok = sum(1 for s in summary if s["issues"] == 0)
        print(f"规则自检通过 {ok}/{len(summary)} 封（未通过的可在对应文件里手工微调）")


if __name__ == "__main__":
    main()
