"""Tradewind 本地检索层：扫描产品/邮件 JSON，以关键词 bigram 匹配。

数据格式（JSON 数组）：
    [{"id", "title", "content", "source", "tags": [...]}]
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.config import settings


class SearchResult:
    """单条检索结果。"""

    def __init__(self, title: str, url: str, snippet: str):
        self.title = title
        self.url = url
        self.snippet = snippet

    def __repr__(self) -> str:
        return f"SearchResult({self.title!r})"


class SearchTool:
    """检索工具接口。"""

    name: str = "search"

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise NotImplementedError


class LocalSearchTool(SearchTool):
    """本地行业库检索：目录扫描所有 *.json 合并，关键词 bigram 打分。

    data/ 目录结构：
        products.json  医美设备资料（tags: ["产品", "激光脱毛", ...]）
        emails.json    历史开发信（tags: ["邮件", "模板", ...]）
    """

    name = "local_search"

    def __init__(self, kb_path: str = "", include_files: tuple[str, ...] | None = None):
        self.kb_path = kb_path or settings.local_kb_path
        self.include_files = {name.casefold() for name in include_files or ()}
        self._kb = self._load(self.kb_path)

    def _load(self, path: str) -> list[dict]:
        p = Path(path)
        if not p.exists():
            print(f"[local_search] 知识库不存在: {p}")
            return []
        files: list[Path] = [p] if p.is_file() else sorted(p.glob("*.json"))
        # 排除运行时配置文件（非知识库）
        files = [f for f in files if f.name != "config.json"]
        if self.include_files:
            files = [f for f in files if f.name.casefold() in self.include_files]
        records: list[dict] = []
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data = [data]  # 单对象文件兜底
                # 只收 dict 条目（防 config.json 之类的键字符串混入）
                records.extend(item for item in data if isinstance(item, dict))
            except Exception as exc:  # noqa: BLE001 - 单个文件损坏不阻塞
                print(f"[local_search] 跳过损坏文件 {f.name}: {exc}")
        return records

    @staticmethod
    def _split_keywords(query: str) -> list[str]:
        kws: list[str] = []
        for part in re.split(r"[\s,，。、;；:：\-/()（）]+", query):
            part = part.strip()
            if not part:
                continue
            for zh in re.findall(r"[\u4e00-\u9fff]+", part):
                if len(zh) <= 2:
                    kws.append(zh)
                else:
                    kws.extend(zh[i : i + 2] for i in range(len(zh) - 1))
            kws.extend(e.lower() for e in re.findall(r"[a-z0-9]+", part, re.IGNORECASE))
        return [k for k in kws if k]

    @staticmethod
    def _score(item: dict, keywords: list[str]) -> int:
        title = item.get("title", "")
        content = item.get("content", "")
        tags = " ".join(item.get("tags", []))
        score = 0
        for kw in keywords:
            if kw in title:
                score += 3
            if kw in tags:
                score += 2
            if kw in content:
                score += 1
        return score

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        if not self._kb:
            return []
        keywords = self._split_keywords(query)
        scored = [
            (self._score(item, keywords), item)
            for item in self._kb
            if self._score(item, keywords) > 0
        ]
        scored.sort(key=lambda x: -x[0])
        return [
            SearchResult(
                item.get("title", ""),
                item.get("source", ""),
                item.get("content", ""),
            )
            for _, item in scored[:max_results]
        ]
