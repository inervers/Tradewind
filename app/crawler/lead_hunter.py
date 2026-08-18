"""客户挖掘爬虫：搜索美容院 → 官网 → 挖邮箱 → 客户名单 CSV。

路线（轻量，不依赖 Playwright）：
    1. DuckDuckGo 搜索 "beauty salon {city}" / "beauty clinic {city}"
       → 提取官网 URL（过滤社交/目录站）
    2. 爬官网首页 + contact/about 页 → 正则提取邮箱
    3. 域名去重 → 客户名单 CSV（name, country, city, website, email）

用法：
    python -m app.crawler.lead_hunter --city Madrid --country Spain --max 10 -v
    python -m app.crawler.lead_hunter --city "Barcelona" --country Spain --max 20 -o data/customers_bcn.csv

说明：搜索引擎反爬/网站结构差异是常态，单站点失败自动跳过不中断。
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from app.config import ROOT_DIR, settings
from app.crawler.progress import report as print  # 终端输出或当前 API 任务日志
from app.tools.ddg import DuckDuckGoSearch

# 社交/目录站/平台：不是官网，排除
SKIP_DOMAINS = (
    "facebook.com", "instagram.com", "twitter.com", "linkedin.com",
    "youtube.com", "maps.google", "google.com/maps", "yelp.com",
    "tripadvisor", "booking.com", "foursquare", "yellowpages",
    "pinterest.com", "tiktok.com", "whatsapp.com",
    "fresha.com",          # 预约平台
    "mejorvalorados",      # 评分目录站
    "trustpilot", "citysearch",
    "whatclinic", "zocdoc", "healthgrades", "doctify", "treatwell",  # 医疗/美容目录站
)

# 噪音邮箱：自动化/示例/占位符/隐私法律类/图片域名
SKIP_EMAIL_PATTERNS = (
    r"noreply", r"no-reply", r"donotreply", r"example\.", r"sentry",
    r"wixpress", r"godaddy", r"@2x|\.png$|\.jpg$|\.jpeg$|\.gif$|\.webp$",
    r"admin@", r"webmaster@", r"hostmaster@", r"postmaster@",
    r"^tu@correo\.com$", r"^your@email\.com$", r"^your@domain\.com$",
    r"^name@", r"^email@", r"^mail@",
    r"privacy@", r"privacidad@", r"legal@", r"datos@", r"gdpr@",
)

# 仪器项目检测（香港市场筛选：不要纯手法按摩，要有仪器项目）
INSTRUMENT_TERMS = (
    "激光", "laser", "皮秒", "picosecond", "射頻", "射频", "rf ",
    "hifu", "超聲", "超声波", "ultherapy", "ulthera", "水光", "hydrafacial",
    "脫毛", "脱毛", "嫩膚", "嫩肤", "微針", "微针", "fractional", "ipl",
    "醫學美容", "医学美容", "medical aesthetics", "medical beauty", "medical spa",
    "冷凍", "冷冻", "cryo", "emsculpt", "venus", "dermal", "美容儀器", "美容仪器",
    "電波", "电波", "thermage",
)
MASSAGE_TERMS = (
    "按摩", "推拿", "massage", "spa ", "水療", "水疗", "腳底", "脚底", "採耳", "采耳",
)

# 具体仪器识别（供“这家店用什么仪器”的分析，命中即进 instruments 列表）
INSTRUMENT_ITEMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("激光", "laser"), "激光"),
    (("脫毛", "脱毛", "hair removal", "soprano", "diode"), "激光脱毛"),
    (("皮秒", "picosecond", "pico"), "皮秒"),
    (("射頻", "射频", " rf", "thermage", "venus legacy"), "射频/热玛吉"),
    (("hifu", "ultherapy", "ulthera", "超聲", "超声波"), "HIFU"),
    (("水光", "hydrafacial", "dermafacial"), "水光针"),
    (("微針", "微针", "microneedling", "fractional"), "微针"),
    (("ipl", "強脈衝", "强脉冲", "光子嫩肤", "嫩膚", "嫩肤", "bb light", "lumenis"), "IPL/光子嫩肤"),
    (("冷凍", "冷冻", "cryo", "cryolipolysis"), "冷冻溶脂"),
    (("emsculpt", "emsculpt", "emsculpt neo"), "Emsculpt"),
    (("皮膚管理", "皮肤管理", "dermal", "skin analysis", "皮肤检测"), "皮肤管理/检测"),
    (("ultraformer", "ultraformer", "doublo", "doublo"), "超声刀"),
    (("脫毛儀", "脱毛仪", "美容儀器", "美容仪器", "aesthetic device"), "美容仪器"),
)


def detect_instruments(text: str) -> list[str]:
    """官网文本 → 这家店用的具体仪器列表（如 ["激光", "皮秒", "IPL/光子嫩肤"]）。
    无匹配返回空列表（调用方决定如何归类）。"""
    low = " " + (text or "").lower() + " "
    found: list[str] = []
    for kws, label in INSTRUMENT_ITEMS:
        if any(k.lower() in low for k in kws):
            if label not in found:
                found.append(label)
    return found


def detect_instrument(text: str) -> str:
    """官网文本仪器检测（三态）：
    yes（有仪器项目）/ massage（疑似纯手法按摩）/ unknown（官网未写明，保留观察）。"""
    if detect_instruments(text):
        return "yes"
    low = " " + text.lower() + " "
    if any(t.lower() in low for t in MASSAGE_TERMS):
        return "massage"
    return "unknown"


def fetch_site_text(base_url: str, proxy: str, max_chars: int = 3000,
                    client: httpx.Client | None = None) -> tuple[str, str]:
    """抓官网首页文本（供仪器检测）。返回 (text, title)，失败返回空。"""
    if client is None:
        with _new_http_client(proxy) as owned_client:
            return fetch_site_text(base_url, proxy, max_chars, owned_client)
    html = _fetch(base_url.rstrip("/") + "/", proxy, client=client)
    if not html:
        return "", ""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:max_chars]
    title = (soup.title.string.strip() if soup.title and soup.title.string else "")
    return text, title

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

CONTACT_PATHS = ["", "/contact", "/contact-us", "/about"]
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MAX_HTML_BYTES = 2 * 1024 * 1024


def _new_http_client(proxy: str = "") -> httpx.Client:
    """官网抓取共享连接池；调用方负责关闭。"""
    return httpx.Client(
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
        },
        follow_redirects=True,
        proxy=proxy or None,
    )


def _is_skip_domain(url: str) -> bool:
    return any(d in url.lower() for d in SKIP_DOMAINS)


def _valid_email(email: str) -> bool:
    email = email.lower()
    if any(re.search(p, email) for p in SKIP_EMAIL_PATTERNS):
        return False
    return True


def _fetch(url: str, proxy: str, timeout: int = 8,
           client: httpx.Client | None = None) -> str | None:
    """抓取 HTML；仅重试临时状态和网络异常，拒绝非网页或超大响应。"""
    for attempt in range(2):
        try:
            if client is None:
                with _new_http_client(proxy) as owned_client:
                    resp = owned_client.get(url, timeout=timeout)
            else:
                resp = client.get(url, timeout=timeout)
            if resp.status_code in RETRYABLE_STATUS:
                if attempt == 0:
                    delay = min(float(resp.headers.get("Retry-After", "0") or 0), 2.0)
                    time.sleep(delay or 0.5)
                continue
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").lower()
            if content_type and not any(kind in content_type for kind in ("html", "xml", "text/plain")):
                return None
            if len(resp.content) > MAX_HTML_BYTES:
                return None
            return resp.text
        except Exception:  # noqa: BLE001 - 单站失败跳过
            if attempt == 0:
                time.sleep(0.5)
    return None


def _crawl_emails(base_url: str, proxy: str,
                  client: httpx.Client | None = None) -> tuple[list[str], str]:
    """爬官网首页 + contact 页，收集邮箱；顺便提取页面标题当店名。

    遇到反爬拦截页（Cloudflare 等）直接返回空，不浪费后续请求。"""
    if client is None:
        with _new_http_client(proxy) as owned_client:
            return _crawl_emails(base_url, proxy, owned_client)
    emails: set[str] = set()
    page_title = ""
    for path in CONTACT_PATHS:
        url = base_url.rstrip("/") + path
        html = _fetch(url, proxy, timeout=5, client=client)  # 同一官网复用连接
        if not html:
            continue
        # Cloudflare / 验证码拦截页检测
        if ("one moment" in html.lower()
                or "cf-challenge" in html.lower()
                or "captcha" in html.lower()
                or "attention required" in html.lower()):
            return [], ""
        if not page_title:
            soup = BeautifulSoup(html, "html.parser")
            if soup.title and soup.title.string:
                page_title = soup.title.string.strip()
        for m in EMAIL_RE.findall(html):
            if _valid_email(m):
                emails.add(m.lower())
        if emails:  # 首页或首个路径挖到就够
            break
    return sorted(emails), page_title


def hunt_customers(city: str, country: str, max_customers: int = 10,
                   proxy: str = "", verbose: bool = False) -> list[dict]:
    """核心流程：搜索 → 官网 → 邮箱。"""
    proxy = proxy or settings.crawler_proxy
    search = DuckDuckGoSearch(proxy=proxy)
    found: list[dict] = []
    seen_domains: set[str] = set()

    queries = [
        f'beauty salon {city} {country}',
        f'beauty clinic {city} {country}',
        f'aesthetic clinic {city} {country}',
    ]

    for q in queries:
        if len(found) >= max_customers:
            break
        if verbose:
            print(f"[hunt] 搜索: {q}")
        results = search.search(q, max_results=10)
        for r in results:
            if len(found) >= max_customers:
                break
            url = r.url
            if not url or _is_skip_domain(url):
                continue
            # 域名去重
            from urllib.parse import urlparse
            domain = urlparse(url).netloc.replace("www.", "")
            if domain in seen_domains:
                continue
            seen_domains.add(domain)

            if verbose:
                print(f"[hunt]  官网: {url}")
            emails, page_title = _crawl_emails(f"https://{domain}", proxy)
            if not emails:
                continue
            # 店名优先用官网 <title>，否则搜索标题/域名
            name = (page_title or r.title or domain).strip()
            name = re.sub(r"\s*[|\-–—]\s*.*$", "", name).strip()[:60]  # 去掉 SEO 后缀
            found.append({
                "name": name or domain,
                "country": country,
                "city": city,
                "website": f"https://{domain}",
                "email": "|".join(emails),
            })
            if verbose:
                print(f"[hunt]    [OK] 邮箱: {emails}")

    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="Tradewind 客户挖掘")
    parser.add_argument("--city", required=True, help="城市（如 Madrid）")
    parser.add_argument("--country", default="", help="国家（如 Spain）")
    parser.add_argument("--max", type=int, default=10, help="最多挖几个客户")
    parser.add_argument("-o", "--out", default="", help="输出 CSV 路径（默认 data/customers_城市.csv）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    print(f"[hunt] 挖掘 {args.city} {args.country} 的美容院邮箱（目标 {args.max} 个）...")
    t0 = time.time()
    customers = hunt_customers(args.city, args.country, args.max, verbose=args.verbose)

    if not customers:
        print("[hunt] 未挖到邮箱（反爬/无公开邮箱/代理问题），可换城市或人工补充")
        return

    out = args.out or str(ROOT_DIR / "data" / f"customers_{args.city.lower()}.csv")
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "country", "city", "website", "email"])
        writer.writeheader()
        writer.writerows(customers)

    print(f"[hunt] 完成：{len(customers)} 个客户（{time.time()-t0:.0f}s）→ {out_path}")
    for c in customers[:5]:
        print(f"  - {c['name'][:40]} | {c['email']}")


if __name__ == "__main__":
    main()
