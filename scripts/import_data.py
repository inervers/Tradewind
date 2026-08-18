"""数据灌入脚本：把原始数据（CSV / 文本）转为行业库 JSON 格式。

用法：
    # CSV（Excel 导出的产品/邮件表）
    python scripts/import_data.py csv --file data/raw/products.csv --tag 产品 \
        --col-title name --col-content desc --col-source url --out data/products.json

    # 文本（爬取的官网正文/产品介绍，一个文件一条）
    python scripts/import_data.py txt --dir data/raw/官网/ --tag 客户官网 \
        --out data/customers.json

清洗逻辑：空行丢弃、content 截断、source 归一、按 (title+source) 去重。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path


def _norm_source(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    return url


def _clean_text(text: str, max_len: int = 2000) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_len]


def _record(tag: str, title: str, content: str, source: str, seen: set) -> dict | None:
    title = (title or "").strip()
    content = _clean_text(content)
    source = _norm_source(source)
    if not title and not content:
        return None
    key = hashlib.md5(f"{title}|{source}".encode()).hexdigest()
    if key in seen:
        return None
    seen.add(key)
    return {
        "id": f"{tag[:2]}-{len(seen):04d}",
        "title": title,
        "content": content,
        "source": source,
        "tags": [tag],
    }


def import_csv(args) -> None:
    path = Path(args.file)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    records: list[dict] = []
    with open(path, encoding="utf-8-sig", newline="") as f:  # utf-8-sig 兼容 Excel BOM
        reader = csv.DictReader(f)
        for row in reader:
            rec = _record(
                args.tag,
                row.get(args.col_title, ""),
                row.get(args.col_content, ""),
                row.get(args.col_source, ""),
                seen,
            )
            if rec:
                records.append(rec)
    out_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[import] CSV → {out_path}（{len(records)} 条，源文件 {path.name}）")


def import_txt(args) -> None:
    in_dir = Path(args.dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    records: list[dict] = []
    for f in sorted(in_dir.glob("*")):
        if not f.is_file() or f.suffix.lower() in (".json", ".md"):
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        rec = _record(args.tag, f.stem, text, args.source or "", seen)
        if rec:
            records.append(rec)
    out_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[import] TXT → {out_path}（{len(records)} 条，来自 {in_dir}）")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tradewind 数据灌入")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_csv = sub.add_parser("csv", help="CSV/Excel 导出表导入")
    p_csv.add_argument("--file", required=True)
    p_csv.add_argument("--tag", required=True, help="数据类别标签，如 产品/邮件/客户")
    p_csv.add_argument("--col-title", default="title")
    p_csv.add_argument("--col-content", default="content")
    p_csv.add_argument("--col-source", default="source")
    p_csv.add_argument("--out", required=True)
    p_csv.set_defaults(func=import_csv)

    p_txt = sub.add_parser("txt", help="文本文件批量导入（一个文件一条）")
    p_txt.add_argument("--dir", required=True)
    p_txt.add_argument("--tag", required=True)
    p_txt.add_argument("--source", default="")
    p_txt.add_argument("--out", required=True)
    p_txt.set_defaults(func=import_txt)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
