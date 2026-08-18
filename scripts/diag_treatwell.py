"""探测 Treatwell 商家页：有没有邮箱，值不值得做预约平台路线。

用法：
    python scripts/diag_treatwell.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from app.config import settings  # noqa: E402


def _safe_content(page) -> str:
    """页面稳定后再取 content（导航中取会报错）。"""
    for _ in range(3):
        try:
            return page.content()
        except Exception:  # noqa: BLE001
            page.wait_for_timeout(2000)
    return ""


def main() -> None:
    proxy = settings.crawler_proxy
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": proxy} if proxy else None,
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            locale="es-ES",
            viewport={"width": 1280, "height": 800},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()

        # 已知商家页（之前 Maps 诊断里发现的）
        shop_url = "https://www.treatwell.es/establecimiento/beauty-33/"
        try:
            page.goto(shop_url, timeout=20000, wait_until="commit")
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(6000)

        print("URL  :", page.url)
        print("TITLE:", page.title())
        body = _safe_content(page)
        if "one moment" in body.lower():
            print(">>> 被 Cloudflare 拦截")
            browser.close()
            return

        # 全页文本找邮箱
        text = page.inner_text("body") if page.locator("body").count() else ""
        emails = sorted(set(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text)))
        print("商家页邮箱:", emails if emails else "（无）")

        # 找电话（多数平台商家页有电话）
        phones = re.findall(r"(\+?\d[\d\s\-]{7,15})", text)
        print("电话样本:", phones[:3] if phones else "（无）")

        # 联系方式区块（常见文案）
        for kw in ("email", "correo", "tel", "teléfono", "contacto", "contact"):
            if kw in text.lower():
                idx = text.lower().find(kw)
                print(f"含'{kw}'片段: ...{text[max(0,idx-60):idx+80]}...".replace("\\n", " "))
                break
        else:
            print("页面未含 email/电话/contact 字样")

        page.screenshot(path="data/treatwell_diag.png")
        print("\n截图: data/treatwell_diag.png")
        browser.close()


if __name__ == "__main__":
    main()
