"""图片型 PDF 页 PNG → OCR 文本（RapidOCR，中文优先）。

用法：
    pip install -i https://mirrors.aliyun.com/pypi/simple/ rapidocr-onnxruntime
    python scripts/ocr_pages.py <pages目录> [输出txt目录]
"""

from __future__ import annotations

import sys
from pathlib import Path

from rapidocr_onnxruntime import RapidOCR


def ocr_page(engine, img_path: Path) -> str:
    result, _ = engine(str(img_path))
    if not result:
        return ""
    # result: list of [box(4点), text, score]，按 y 中心 + x 左排序还原阅读顺序
    items = []
    for box, text, score in result:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append((min(ys), min(xs), text))
    items.sort(key=lambda t: (round(t[0] / 24), t[1]))  # 行高约 24px，按行聚合
    lines = []
    for _, _, text in items:
        lines.append(text.strip())
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python scripts/ocr_pages.py <pages目录> [输出目录]")
        sys.exit(1)
    pages_dir = Path(sys.argv[1])
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else pages_dir / "ocr"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("加载 OCR 模型（首次稍慢）…")
    engine = RapidOCR()

    pngs = sorted(pages_dir.glob("*.png"))
    for png in pngs:
        print(f"识别 {png.name} …")
        text = ocr_page(engine, png)
        out = out_dir / f"{png.stem}.txt"
        out.write_text(text, encoding="utf-8")
        print(f"  → {out}（{len(text)} 字）")
    print("完成")


if __name__ == "__main__":
    main()
