"""诊断 Google Maps 详情面板：点击第一家 → dump 面板内所有链接 + 截图。

用法：
    python scripts/diag_maps.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from app.config import settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args()

    proxy = settings.crawler_proxy
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headful,
            proxy={"server": proxy} if proxy else None,
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        context.add_cookies([{
            "name": "SOCS", "value": "CAI",
            "domain": ".google.com", "path": "/",
        }])
        page = context.new_page()
        page.goto("https://www.google.com/maps/search/beauty+salon+Madrid",
                  timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)

        print("URL  :", page.url)
        anchors = page.locator('a[href*="/maps/place/"]')
        count = anchors.count()
        print("place 链接数:", count)
        if count == 0:
            page.screenshot(path="data/maps_diag.png")
            print("无列表，截图 data/maps_diag.png")
            browser.close()
            return

        # 点击第一家
        try:
            anchors.first.click(timeout=8000)
            page.wait_for_timeout(3000)
            print("点击后 URL:", page.url)
        except Exception as exc:  # noqa: BLE001
            print("点击失败:", type(exc).__name__, exc)
            browser.close()
            return

        # dump 详情面板（main 区域）所有链接
        panel = page.locator('div[role="main"]')
        print("\n=== main 区域内所有 a[href] ===")
        links = panel.locator("a[href]")
        for i in range(min(links.count(), 30)):
            try:
                href = links.nth(i).get_attribute("href")
                text = (links.nth(i).inner_text() or "")[:40].replace("\n", " ")
            except Exception:  # noqa: BLE001
                continue
            print(f"  [{i}] {text}  ->  {href}")

        # 整个页面所有 a[href] 里的外链
        print("\n=== 全页外链（非 google 域）===")
        for a in page.locator("a[href^='http']").all():
            href = a.get_attribute("href") or ""
            if "google.com" in href:
                continue
            print(" ", href[:120])

        page.screenshot(path="data/maps_diag.png")
        print("\n截图已存: data/maps_diag.png")
        browser.close()


if __name__ == "__main__":
    main()
