"""缺品分析：店方仪器清单 → 对比产品库品类 → 推荐缺失品类产品。

不修改运行时数据文件：品类画像从 products.json 的 title/content/tags 现读现算，
店方品类集合来自爬虫的 detect_instruments 标签 + 照片视觉识别。

关键词归一化：检测标签（"IPL/光子嫩肤"）与产品关键词（"强脉冲光"）先映射到统一
品类 ID 再对比，避免"店已有 IPL 还被推 IPL 设备"的同物异名误判。
"""

# 统一品类别名表：关键词（检测标签 / 产品文本）→ 品类 ID
CATEGORY_ALIASES = {
    "皮肤检测": "皮肤检测",
    "皮肤分析": "皮肤检测",
    "皮肤管理": "皮肤检测",
    "激光脱毛": "激光脱毛",
    "激光": "激光脱毛",
    "强脉冲光": "IPL",
    "光子嫩肤": "IPL",
    "嫩肤": "IPL",
    "嫩膚": "IPL",
    "ipl": "IPL",
    "光治疗": "IPL",  # LumiView K8 治疗端（11 种波长滤镜，即光子治疗）
    "皮肤年轻化": "皮肤年轻化",
    "年轻化": "皮肤年轻化",
    "抗衰": "皮肤年轻化",
}

# 产品库侧用于匹配品类的最小关键词集（title/content/tags 命中即认为覆盖该品类）
PRODUCT_HINTS = ("皮肤检测", "激光脱毛", "强脉冲光", "光子嫩肤", "皮肤年轻化")


def _norm_categories(labels: list[str] | str) -> set[str]:
    """标签/文本 → 统一品类 ID 集合（子串匹配 + 别名归一）。"""
    hay = " ".join(labels) if isinstance(labels, list) else labels
    hay = (hay or "").lower()
    out: set[str] = set()
    for kw, cat in CATEGORY_ALIASES.items():
        if kw.lower() in hay:
            out.add(cat)
    return out


def build_catalog(products: list[dict]) -> list[dict]:
    """从产品数据提取品类画像：每台产品 → 覆盖品类 ID 列表。

    products: data/products.json 的原始条目列表
    返回: [{"id", "title", "categories": ["皮肤检测", ...]}, ...]
    """
    out: list[dict] = []
    for p in products or []:
        title = p.get("title", "") or ""
        content = p.get("content", "") or ""
        tags = " ".join(p.get("tags", []) or [])
        # 门槛：全文本命中任一品类关键词才纳入（排除无关产品）
        hay_all = f"{title} {content} {tags}"
        if not any(h.lower() in hay_all.lower() for h in PRODUCT_HINTS):
            continue
        # 品类画像：只用 title + tags（人工标注的产品类型）。content 的适应症列表
        # （脱毛/嫩肤/年轻化等）是使用场景，不是设备类型，混入会污染品类对比。
        cats = sorted(_norm_categories(f"{title} {tags}"))
        if cats:
            out.append({"id": p.get("id"), "title": title, "categories": cats})
    return out


def gap_analysis(store_labels: list[str], catalog: list[dict]) -> list[str]:
    """店方已有仪器标签（如 ['IPL/光子嫩肤', '激光脱毛']）→ 推荐缺失品类产品。

    返回推荐文案列表（无缺口返回空）。
    """
    store = _norm_categories(store_labels)
    recs: list[str] = []
    for prod in catalog:
        missing = [c for c in prod["categories"] if c not in store]
        if missing:
            recs.append(f"{prod['title']}（缺：{'/'.join(missing)}）")
    return recs
