"""Webs：搜医美机构 → 深挖官网 邮箱/电话/WhatsApp/社媒/仪器品牌 → 缺品分析。

与 maps_hunter 互补：有官网的美容院/诊所更正规、更有采购力；官网"仪器介绍"页
直接列出品牌型号（Candela / Lumenis / Fotona…），比照片识别更精确。
输出另一组客户数据（webs CSV，含 WhatsApp/Instagram/Facebook 触达渠道）。

搜索三引擎（--engine 切换，auto 合并 google + bing + ddg 并按域名去重）：
    - google：Playwright 打开 google.com/search（需代理，反爬最严）
    - bing：RSS 优先，Playwright 兜底
    - ddg：httpx DuckDuckGo（lead_hunter 同款，auto 优先）

用法：
    python -m app.crawler.webs_hunter --queries "medical aesthetic clinic Hong Kong laser treatment" --max 10 -v
    python -m app.crawler.webs_hunter --queries "medspa hong kong" --engine google --max 15
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import ROOT_DIR, settings
from app.crawler.equipment import build_catalog, gap_analysis
from app.crawler.browser_runtime import launch_chromium
from app.crawler.lead_hunter import (
    EMAIL_RE, _fetch, _is_skip_domain, _new_http_client, _valid_email, detect_instruments,
)
from app.crawler.progress import report as print  # 终端输出或当前 API 任务日志
from app.crawler.result_utils import target_reached
from app.tools.ddg import DuckDuckGoSearch

# 官网要探测的页面（挖邮箱/电话/仪器介绍）
SITE_PATHS = [
    "", "/contact", "/contact-us", "/about", "/about-us",
    "/services", "/treatments", "/treatment", "/technology", "/equipment",
]

# 品牌型号 → 品类标签（官网"仪器介绍"页命中即认为该店在用这类仪器）
# 注意：短型号（m22/4d）和常见词（alma/venus/cryo）易误报，一律用品牌+型号全名
BRAND_ITEMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("candela", "gentlelase", "gentlemax", "gentleyag"), "激光脱毛"),
    (("soprano ice", "sopranoice", "alma harmony", "harmony xl"), "激光脱毛"),
    (("lumenis", "stella m22", "m22 "), "IPL/光子嫩肤"),
    (("lumecca",), "IPL/光子嫩肤"),
    (("fotona", "starwalker"), "激光"),
    (("cutera", "excel v", "xeo "), "激光"),
    (("cynosure", "apogee"), "激光"),
    (("thermage", "thermacool"), "射频/热玛吉"),
    (("venus legacy", "venus freeze", "venus versa"), "射频"),
    (("ulthera", "ultraformer", "doublo"), "HIFU"),
    (("picosure", "picoway", "discover pico"), "皮秒"),
    (("hydrafacial",), "水光针"),
    (("coolsculpting", "cryolipolysis"), "冷冻溶脂"),
    (("emsculpt",), "Emsculpt"),
)

HK_PHONE_RE = re.compile(r"\+?852[\s\-]?\d{4}[\s\-]?\d{4}")

# 搜索结果里常见、但不是潜在采购客户的网站。只做确定性较高的域名过滤，
# 其余候选再交给官网正文相关性评分，避免误伤使用 .com 的香港机构。
NON_CUSTOMER_DOMAINS = (
    "wikipedia.org", "facebook.com", "instagram.com", "linkedin.com", "youtube.com",
    "hk01.com", "scmp.com", "hket.com", "etnet.com.hk", "thestandard.com.hk",
    "openrice.com", "yelp.com", "tripadvisor.com", "yellowpages.com.hk",
    "whatclinic.com", "clinicbooking.com", "baike.baidu.com", "discoverhongkong.com",
    "hongkongairport.com", "hongkongpost.hk", "gov.hk", "ha.org.hk",
)

INDUSTRY_TERMS = (
    "醫學美容", "医学美容", "醫美", "医美", "美容院", "美容中心", "美容診所",
    "皮膚診所", "皮肤诊所", "激光脫毛", "激光脱毛", "皮秒", "光子嫩膚",
    "medical spa", "medical aesthetics", "medspa", "med spa", "aesthetic clinic",
    "aesthetics clinic", "aesthetic clinics", "aesthetic centre",
    "aesthetic center", "beauty salon", "beauty centre", "beauty center",
    "skin clinic", "laser clinic", "dermatology clinic",
)

EDITORIAL_TERMS = (
    "新聞", "新闻", "即時新聞", "報道", "记者", "作者", "article", "news",
    "privacy policy", "terms of use",
)

# WhatsApp 链接 → 号码
WA_RE = re.compile(
    r"(?:wa\.me/(\d+)|api\.whatsapp\.com/send(?:\?[^\"'\s]*phone=)(\d+))", re.I)

# 社交媒体账号（排除功能页/分享路径）
SOCIAL_EXCLUDE = re.compile(
    r"/(login|sharer|share|posts|photos|reels|stories|events|about|p/|settings|groups?|watch|profile\.php)/?$|facebook\.com/$|instagram\.com/$",
    re.I)


def _load_products() -> list[dict]:
    """读产品库（缺品分析用）。"""
    try:
        return json.loads((ROOT_DIR / "data" / "products.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def _extract_phone(html: str) -> str:
    """优先 tel: 链接，其次文本 +852 8 位模式。8 位本地号补 +852 区号。"""
    for t in re.findall(r'href="tel:([^"]+)"', html, re.I):
        digits = re.sub(r"\D", "", t)
        if len(digits) == 8:
            return "+852" + digits  # 本地 8 位号，补香港区号
        if len(digits) >= 11 and digits.startswith("852"):
            return "+" + digits if not digits.startswith("+") else t
    m = HK_PHONE_RE.search(html)
    if m:
        return re.sub(r"\s", "", m.group(0))
    return ""


def _extract_whatsapp(html: str) -> str:
    """官网 WhatsApp 链接 → 号码（wa.me / api.whatsapp.com）。"""
    for a, b in WA_RE.findall(html):
        digits = (a or b)
        digits = re.sub(r"\D", "", digits)
        if len(digits) >= 8:
            return "+" + digits if not digits.startswith("+") else digits
    return ""


def _extract_socials(html: str) -> dict:
    """官网社交链接 → {facebook: 账号, instagram: 账号}。"""
    out: dict[str, str] = {}
    for platform, host in (("facebook", "facebook.com"), ("instagram", "instagram.com")):
        for m in re.findall(rf"{host}/([A-Za-z0-9._\-]+)", html, re.I):
            acc = m.rstrip("/")
            if not acc or acc.lower() in ("share", "sharer", "login", "watch"):
                continue
            if SOCIAL_EXCLUDE.search("/" + acc):
                continue
            out[platform] = acc
            break  # 每平台取第一个
    return out


def _brand_instruments(text: str) -> list[str]:
    """官网文本 → 品牌型号命中的品类标签（如 ["激光脱毛", "IPL/光子嫩肤"]）。"""
    low = " " + (text or "").lower() + " "
    found: list[str] = []
    for kws, label in BRAND_ITEMS:
        if any(k.lower() in low for k in kws):
            if label not in found:
                found.append(label)
    return found


def _is_hong_kong_site(domain: str, html: str) -> bool:
    """用域名、区号和页面地址判断香港站，允许香港公司使用 .com 等域名。"""
    host = domain.lower().split(":", 1)[0]
    if host.endswith(".hk"):
        return True
    low = (html or "").lower()
    return bool(
        re.search(r"(?:\+|00)?852[\s\-]?\d{4}[\s\-]?\d{4}", low)
        or "hong kong" in low
        or "香港" in low
    )


def _is_non_customer_domain(domain: str) -> bool:
    """过滤新闻、百科、目录及社媒等明显不是机构官网的域名。"""
    host = domain.lower().split(":", 1)[0].strip(".")
    return any(host == blocked or host.endswith("." + blocked) for blocked in NON_CUSTOMER_DOMAINS)


def _industry_relevance(domain: str, title: str, text: str) -> int:
    """按官网标题/域名/正文判断是否为医美或美容机构；3 分以上可接纳。"""
    host = domain.lower()
    title_low = (title or "").lower()
    text_low = (text or "").lower()
    score = 0
    for term in INDUSTRY_TERMS:
        term_low = term.lower()
        if term_low in title_low:
            score += 3
        elif term_low in host:
            score += 2
        elif term_low in text_low:
            score += 1
    # 多个关键词可能在导航栏重复出现，封顶可使阈值的含义保持稳定。
    score = min(score, 8)
    if any(term in title_low for term in EDITORIAL_TERMS):
        score -= 3
    return score


def _render_site_pages(domain: str, proxy: str, verbose: bool = False) -> dict[str, str]:
    """静态抓取无联系方式时，用一个浏览器受控渲染主页和联系页。"""
    pages: dict[str, str] = {}
    p = browser = None
    try:
        p, browser = _launch_browser(proxy)
        page = browser.new_page()
        for path in ("", "/contact"):
            try:
                page.goto(f"https://{domain}{path}", wait_until="domcontentloaded", timeout=10000)
                page.wait_for_timeout(500)
                html = page.content()
                if html:
                    pages[f"rendered:{path or '/'}"] = html
                joined = "\n".join(pages.values())
                if any(_valid_email(m) for m in EMAIL_RE.findall(joined)) or _extract_phone(joined) or _extract_whatsapp(joined):
                    break
            except Exception as exc:  # noqa: BLE001 - 单页失败继续下一路径
                if verbose:
                    print(f"[webs]    动态页失败 {path or '/'}: {str(exc)[:80]}")
    except Exception as exc:  # noqa: BLE001 - 浏览器兜底失败不影响静态结果
        if verbose:
            print(f"[webs]    动态渲染不可用: {str(exc)[:100]}")
    finally:
        if browser is not None:
            browser.close()
        if p is not None:
            p.stop()
    return pages


def _crawl_site(domain: str, proxy: str, verbose: bool = False) -> dict:
    """深挖一个官网：邮箱 / 电话 / WhatsApp / 社媒 / 仪器品类。"""
    pages: dict[str, str] = {}
    with _new_http_client(proxy) as client:
        for path in SITE_PATHS:
            url = f"https://{domain}{path}"
            html = _fetch(url, proxy, timeout=6, client=client)  # 同一官网复用连接
            if not html:
                continue
            pages[path or "/"] = html
            if len(pages) >= 8:  # 最多 8 页，够挖了
                break
            if len(pages) >= 3:
                # 已抓 3 页仍无任何联系方式 → 垃圾站/无联系页，快速跳过（省后面 5 页耗时）
                _probe = "\n".join(pages.values())
                has_contact = bool(
                    any(_valid_email(m) for m in EMAIL_RE.findall(_probe))
                    or _extract_phone(_probe) or _extract_whatsapp(_probe))
                if not has_contact:
                    break
    all_html = "\n".join(pages.values())
    has_contact = bool(
        any(_valid_email(m) for m in EMAIL_RE.findall(all_html))
        or _extract_phone(all_html) or _extract_whatsapp(all_html)
    )
    if not has_contact:
        static_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", all_html))
        first_static_html = pages.get("") or next(iter(pages.values()), "")
        static_soup = BeautifulSoup(first_static_html, "html.parser")
        static_title = (static_soup.title.string.strip()
                        if static_soup.title and static_soup.title.string else "")
        # 内容充足却没有任何行业信号时，不再花 20 秒动态渲染明显无关的网站。
        should_render = (not pages or len(static_text) < 500
                         or _industry_relevance(domain, static_title, static_text) > 0)
        if should_render:
            if verbose:
                print("[webs]    静态页面无联系方式，尝试动态渲染…")
            rendered = _render_site_pages(domain, proxy, verbose)
            pages.update(rendered)
            all_html = "\n".join(pages.values())
        elif verbose:
            print("[webs]    静态页面无行业信号，跳过动态渲染")
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", all_html))

    emails: set[str] = set()
    for m in EMAIL_RE.findall(all_html):
        if _valid_email(m):
            emails.add(m.lower())

    instruments = detect_instruments(text)
    for b in _brand_instruments(text):
        if b not in instruments:
            instruments.append(b)

    title = ""
    first_html = pages.get("") or next(iter(pages.values()), "")
    soup = BeautifulSoup(first_html, "html.parser")
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    return {
        "domain": domain,
        "title": title,
        "emails": sorted(emails),
        "phone": _extract_phone(all_html),
        "whatsapp": _extract_whatsapp(all_html),
        "socials": _extract_socials(all_html),
        "instruments": instruments,
        "is_hong_kong": _is_hong_kong_site(domain, all_html),
        "relevance_score": _industry_relevance(domain, title, text),
    }


# ---------- 搜索引擎 ----------

def _launch_browser(proxy: str):
    from playwright.sync_api import sync_playwright
    pxy = proxy
    if pxy and not pxy.lower().startswith(("http://", "https://", "socks4://", "socks5://")):
        pxy = "http://" + pxy  # config 默认已带 http://，兼容裸 host:port
    p = sync_playwright().start()
    try:
        browser = launch_chromium(
            p.chromium,
            headless=True,
            proxy={"server": pxy} if pxy else None,
        )
        return p, browser
    except Exception:
        p.stop()  # 启动失败也要释放驱动，否则下次 start 报 Sync API 错误
        raise


def _search_google(query: str, proxy: str, verbose: bool = False) -> list[str]:
    """Playwright Google 网页搜索（需代理；结果链接是 /url?q= 重定向，要解析）。"""
    urls: list[str] = []
    try:
        p, browser = _launch_browser(proxy)
        try:
            page = browser.new_page()
            page.goto(
                f"https://www.google.com/search?q={quote(query)}&num=20&hl=zh-HK&gl=hk",
                timeout=30000, wait_until="domcontentloaded",
            )
            try:
                page.wait_for_selector('a[href*="/url?q="]', timeout=10000)  # 等结果链接渲染
            except Exception:  # noqa: BLE001 - 被拦/超时按现状
                pass
            page.wait_for_timeout(1500)
            hrefs = page.eval_on_selector_all(
                "a", "els => els.map(e => e.href).filter(h => h.startsWith('http'))")
            if verbose:
                t = page.title()[:70]
                print(f"[webs]   Google 诊断: title={t!r} 链接 {len(hrefs)} 个")
        finally:
            browser.close()
            p.stop()
        for u in hrefs:
            if "/url?q=" in u:
                m = re.search(r"/url\?q=([^&]+)", u)
                u = m.group(1) if m else u
            try:
                host = urlparse(u).netloc
            except Exception:  # noqa: BLE001
                continue
            if host and "google." not in host:
                urls.append(u)
    except Exception as e:  # noqa: BLE001 - 反爬/超时降级
        if verbose:
            print(f"[webs] Google 搜索失败（{str(e)[:60]}），降级下一引擎")
    return urls


def _resolve_bing_url(href: str) -> str:
    """Bing 结果链接多为 /ck/a 加密跳转，真实 URL 在 u 参数的 base64url 里（a1 前缀）。"""
    if "/ck/a" not in href:
        return href
    m = re.search(r"[?&]u=([^&]+)", href)
    if not m:
        return ""
    try:
        import base64
        raw = base64.urlsafe_b64decode(m.group(1)[2:] + "==")
        real = raw.decode("utf-8", "ignore")
        return real if real.startswith("http") else ""
    except Exception:  # noqa: BLE001
        return ""


def _search_bing(query: str, proxy: str, verbose: bool = False) -> list[str]:
    """优先使用 Bing RSS；无结果时回退 Playwright 网页搜索。"""
    rss_urls = _search_bing_rss(query, proxy, verbose)
    if rss_urls:
        return rss_urls

    return _search_bing_browser(query, proxy, verbose)


def _search_bing_rss(query: str, proxy: str, verbose: bool = False) -> list[str]:
    """Bing RSS 搜索结果，不依赖页面 JS，规避浏览器上下文被销毁。"""
    try:
        response = httpx.get(
            "https://www.bing.com/search",
            params={"q": query, "format": "rss", "mkt": "zh-HK", "setlang": "zh-hk"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=15,
            follow_redirects=True,
            proxy=proxy or None,
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
        urls = [node.text.strip() for node in root.findall(".//item/link") if node.text]
        if verbose:
            print(f"[webs]   Bing RSS: 链接 {len(urls)} 个")
        return urls
    except Exception as exc:  # noqa: BLE001 - RSS 不可用时回退浏览器
        if verbose:
            print(f"[webs]   Bing RSS 不可用（{str(exc)[:60]}），回退网页搜索")
        return []


def _search_bing_browser(query: str, proxy: str, verbose: bool = False) -> list[str]:
    """Playwright Bing 搜索。结果 /ck/a 跳转需解析。"""
    urls: list[str] = []
    try:
        p, browser = _launch_browser(proxy)
        try:
            page = browser.new_page()
            page.goto(
                f"https://www.bing.com/search?q={quote(query)}&count=20&mkt=zh-HK&setlang=zh-hk",
                timeout=30000, wait_until="domcontentloaded",
            )
            page.wait_for_timeout(2000)
            raw = page.eval_on_selector_all(
                "li.b_algo h2 a",
                "els => els.map(e => e.href).filter(h => h && h.startsWith('http'))")
            if verbose:
                t = page.title()[:70]
                print(f"[webs]   Bing 诊断: title={t!r} 原始链接 {len(raw)} 个")
        finally:
            browser.close()
            p.stop()
        for h in raw:
            real = _resolve_bing_url(h)
            if real:
                urls.append(real)
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"[webs] Bing 搜索失败（{str(e)[:60]}）")
    return urls


SEARCH_DOMAINS = ("bing.com", "google.com", "google.com.hk", "duckduckgo.com", "yandex.com")


def _clean_results(urls: list[str]) -> list[str]:
    """过滤搜索引擎自身域、明显非客户域，并按域名去重。"""
    out: list[str] = []
    seen_hosts: set[str] = set()
    for u in urls:
        if not u:
            continue
        try:
            host = urlparse(u).netloc.replace("www.", "").lower()
        except Exception:  # noqa: BLE001
            continue
        if (not host or host in SEARCH_DOMAINS or "google." in host
                or _is_non_customer_domain(host) or host in seen_hosts):
            continue
        seen_hosts.add(host)
        out.append(u)
    return out


def _search(query: str, engine: str, proxy: str, verbose: bool = False) -> list[str]:
    """按引擎搜索；auto 优先 DDG，有效候选不足时再合并浏览器引擎。"""
    if engine == "google":
        return _clean_results(_search_google(query, proxy, verbose))
    if engine == "bing":
        return _clean_results(_search_bing(query, proxy, verbose))
    if engine == "ddg":
        return _clean_results([r.url for r in DuckDuckGoSearch(proxy=proxy).search(query, max_results=10)
                               if r.url])
    ddg_urls = _clean_results([
        r.url for r in DuckDuckGoSearch(proxy=proxy).search(query, max_results=10) if r.url
    ])
    if len(ddg_urls) >= 3:
        if verbose:
            print(f"[webs]   DuckDuckGo: {len(ddg_urls)} 个候选官网")
        return ddg_urls
    urls: list[str] = list(ddg_urls)
    urls.extend(_search_google(query, proxy, verbose))
    urls.extend(_search_bing(query, proxy, verbose))
    cleaned = _clean_results(urls)
    if verbose:
        print(f"[webs]   多引擎合并: {len(cleaned)} 个候选官网")
    return cleaned


# ---------- 主流程 ----------

FIELDS = ["name", "website", "email", "phone", "whatsapp", "facebook", "instagram",
          "instruments", "gap_recs"]


def hunt_websites(queries: list[str], max_customers: int = 10, engine: str = "auto",
                  proxy: str = "", region: str = "hk", verbose: bool = False,
                  cancel_check=None, result_filter=None,
                  max_candidates: int | None = None,
                  excluded_domains: set[str] | None = None) -> list[dict]:
    """搜索 → 官网深挖 → 缺品分析。目标有效结果与候选检查上限分开计数。"""
    proxy = proxy or settings.crawler_proxy
    catalog = build_catalog(_load_products())
    found: list[dict] = []
    seen_domains: set[str] = set()
    excluded = {d.casefold().removeprefix("www.") for d in (excluded_domains or set())}
    target_results = max(1, max_customers)
    candidate_limit = max(target_results, max_candidates or max(8, target_results * 5))

    def enough_results() -> bool:
        return target_reached(found, target_results, result_filter)

    for q in queries:
        processed = 0
        if cancel_check and cancel_check():
            break
        if enough_results() or processed >= candidate_limit:
            break
        if verbose:
            print(f"[webs] 搜索: {q}（引擎: {engine}）")
        for url in _search(q, engine, proxy, verbose):
            if cancel_check and cancel_check():
                break
            if enough_results() or processed >= candidate_limit:
                break
            if not url or _is_skip_domain(url):
                continue
            try:
                domain = urlparse(url).netloc.replace("www.", "").lower()
            except Exception:  # noqa: BLE001
                continue
            if not domain or domain in seen_domains:
                continue
            seen_domains.add(domain)
            if domain in excluded:
                if verbose:
                    print(f"[webs]  跳过已爬客户 {domain}，继续寻找新客户")
                continue
            if _is_non_customer_domain(domain):
                if verbose:
                    print(f"[webs]  非客户域名，跳过: {domain}")
                continue
            processed += 1

            if verbose:
                print(f"[webs]  官网: {domain}")
            info = _crawl_site(domain, proxy, verbose)
            if region == "hk" and not info["is_hong_kong"]:
                if verbose:
                    print("[webs]    页面无香港域名/地址/+852 信号，跳过")
                continue
            if info["relevance_score"] < 3:
                if verbose:
                    print(f"[webs]    行业相关性不足（{info['relevance_score']} 分），跳过")
                continue
            if not (info["emails"] or info["phone"] or info["whatsapp"]):
                if verbose:
                    print("[webs]    无邮箱/电话/WhatsApp，跳过")
                continue
            recs = gap_analysis(info["instruments"], catalog) if info["instruments"] else []
            found.append({
                "name": re.sub(r"\s*[|\-–—]\s*.*$", "", info["title"] or domain)[:60],
                "website": f"https://{domain}",
                "email": "|".join(info["emails"]),
                "phone": info["phone"],
                "whatsapp": info["whatsapp"],
                "facebook": info["socials"].get("facebook", ""),
                "instagram": info["socials"].get("instagram", ""),
                "instruments": "|".join(info["instruments"]),
                "gap_recs": "; ".join(recs),
            })
            if verbose:
                print(f"[webs]    [OK] 邮箱 {len(info['emails'])} | 电话 {info['phone'] or '无'} "
                      f"| WA {info['whatsapp'] or '无'} | IG {info['socials'].get('instagram', '-')} "
                      f"| 仪器 {info['instruments'] or '未知'}")
                if recs:
                    print(f"          缺品: {recs[0]}")
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="Tradewind Webs（机构官网深挖 + 社媒触达）")
    parser.add_argument("--queries", default="medical aesthetic clinic Hong Kong laser treatment,香港 醫學美容 診所,香港 美容中心 激光",
                        help="搜索词，逗号分隔（默认覆盖中文/英文及使用 .com 的香港机构）")
    parser.add_argument("--engine", default="auto", choices=["auto", "google", "bing", "ddg"])
    parser.add_argument("--region", default="hk", choices=["hk", "any"],
                        help="目标地区：hk=只收有香港域名/地址/+852 信号的网站，any=不过滤")
    parser.add_argument("--max", type=int, default=10, help="最多挖几个机构")
    parser.add_argument("-o", "--out", default="", help="输出 CSV 路径（默认 data/webs_hk.csv）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    queries = [q.strip() for q in args.queries.split(",") if q.strip()]
    print(f"[webs] Webs：{len(queries)} 个词（目标 {args.max} 家，region={args.region}）…")
    t0 = time.time()
    found = hunt_websites(queries, args.max, args.engine,
                          region=args.region, verbose=args.verbose)

    if not found:
        print("[webs] 未挖到（反爬/无官网/代理问题），换引擎或词重试")
        return

    out = args.out or str(ROOT_DIR / "data" / "webs_hk.csv")
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(found)

    print(f"[webs] 完成：{len(found)} 家（{time.time()-t0:.0f}s）→ {out_path}")
    for c in found[:8]:
        print(f"  - {c['name'][:36]} | {c['email'] or c['phone'] or c['whatsapp']} "
              f"| {c['instruments'] or '仪器未知'}")


if __name__ == "__main__":
    main()
