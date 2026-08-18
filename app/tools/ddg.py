"""DuckDuckGo 网络检索（客户背景补充，走代理）。"""

from __future__ import annotations

import urllib.parse

import httpx
from bs4 import BeautifulSoup

from app.tools.search import SearchResult


def _clean_ddg_url(url: str) -> str:
    """把 DuckDuckGo 重定向链接还原为真实 URL。"""
    if "duckduckgo.com/l/" in url:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        target = qs.get("uddg", [""])[0]
        if target:
            return urllib.parse.unquote(target)
    return url


class DuckDuckGoSearch:
    """DuckDuckGo HTML 端点，免费无需 key。proxy 走 Verge 代理。"""

    name = "duckduckgo_search"
    _BASE = "https://html.duckduckgo.com/html/"

    def __init__(self, proxy: str = ""):
        self.proxy = proxy

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        params = {"q": query}
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        last_exc: Exception | None = None
        for attempt in range(2):  # 轻量重试
            try:
                resp = httpx.get(
                    self._BASE,
                    params=params,
                    headers=headers,
                    timeout=15,
                    follow_redirects=True,
                    proxy=self.proxy or None,
                )
                resp.raise_for_status()
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        else:
            return [SearchResult(f"搜索失败: {last_exc}", "", "")]

        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[SearchResult] = []
        for item in soup.select(".result")[:max_results]:
            a = item.select_one(".result__a")
            snip = item.select_one(".result__snippet")
            if a is None:
                continue
            title = a.get_text(strip=True)
            url = a.get("href", "")
            if url.startswith("//"):
                url = "https:" + url
            snippet = snip.get_text(strip=True) if snip else ""
            results.append(SearchResult(title, _clean_ddg_url(url), snippet))
        return results
