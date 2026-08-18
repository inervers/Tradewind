"""图片型 PDF → 每页 PNG（供视觉读取/OCR）。

用法：
    pip install -i https://mirrors.aliyun.com/pypi/simple/ pymupdf
    python scripts/pdf_to_png.py <pdf路径> [输出目录] [dpi]
"""

from __future__ import annotations

import sys
from pathlib import Path

import fitz  # PyMuPDF


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python scripts/pdf_to_png.py <pdf> [输出目录] [dpi]")
        sys.exit(1)
    pdf = Path(sys.argv[1])
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else pdf.parent / "pages"
    dpi = int(sys.argv[3]) if len(sys.argv) > 3 else 150
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf))
    print(f"共 {doc.page_count} 页，渲染 dpi={dpi} → {out_dir}")
    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=dpi)
        out = out_dir / f"page_{i + 1:02d}.png"
        pix.save(str(out))
        print(f"  [{i + 1}/{doc.page_count}] {out} ({pix.width}x{pix.height})")
    doc.close()
    print("完成")


if __name__ == "__main__":
    main()
