"""Google Maps 美容院挖掘（Playwright，真实浏览器行为）。

与 lead_hunter（DDG 搜索）的区别：
- lead_hunter：只有"有官网被搜索引擎收录"的店，覆盖窄，偏大店
- maps_hunter：Google Maps 全量商家（含没官网的小店），名单真实完整

香港模式（v0.3，市场转向）：
    1. Google Maps 搜索多关键词（如「醫學美容,醫美診所,美容中心 儀器」）→ 滚动加载全量
    2. 提取每家的 店名/评分/评论数/地址/place 链接/电话/WhatsApp 直链
    3. 逐个点开详情 → 官网（解析 google 跳转链接）→ 挖邮箱 + 官网文本仪器检测
    4. 无邮箱但有电话/WhatsApp → 入 WhatsApp 名单（wa.me 链接，业务用户手动发消息，零风控）
    5. 输出 CSV：name,country,city,website,email,phone,wa_link,instrument

用法：
    python -m app.crawler.maps_hunter --queries "醫學美容,醫美診所,美容中心 儀器" --country 香港 --max 30 -o data/maps_hk.csv -v
    python -m app.crawler.maps_hunter --query "beauty salon" --country Spain --max 20 --headful

注意：Google Maps 反爬严格，需代理（默认 7897），headless 可能触发验证码，
失败时加 --headful 打开真实浏览器窗口手动过验证。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from app.config import ROOT_DIR, get_vision_config, get_vision_failover_configs, settings
from app.crawler.equipment import build_catalog, gap_analysis
from app.crawler.browser_runtime import launch_chromium
from app.crawler.lead_hunter import (
    _crawl_emails, _is_skip_domain, _new_http_client, detect_instrument,
    detect_instruments, fetch_site_text,
)
from app.crawler.progress import report as print  # 终端输出或当前 API 任务日志
from app.crawler.result_utils import target_reached
from app.crawler.vision_analyzer import analyze_photos

RATING_RE = re.compile(r"(\d[.,]?\d?)\s*★")
REVIEWS_RE = re.compile(r"\((\d+)\)")

# 纯手法按摩店：即使有官网邮箱也跳过（用户要求：要有仪器项目）
SKIP_INSTRUMENT = ("massage",)
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}



def _name_from_place_url(href: str) -> str:
    """从 place URL 路径提取店名：/maps/place/Sasha-+Beauty+Salon/..."""
    m = re.search(r"/maps/place/([^/]+)/", href)
    if not m:
        return ""
    name = unquote(m.group(1)).replace("+", " ").strip()
    return name.split(" - ")[0][:60]  # 去掉 " - 城市/描述" 后缀


def _place_key(href: str) -> str:
    """生成可跨任务复用的 Maps 地点键；优先 Google 内部实体 ID，回退店名路径。"""
    entity = re.search(r"!1s([^!/?&]+)", href)
    if entity:
        return f"entity:{entity.group(1).casefold()}"
    name = _name_from_place_url(href)
    compact = re.sub(r"[^\w]+", "", name.casefold())
    return f"place:{compact}" if compact else ""


def _safe_photo_dir_name(name: str) -> str:
    """将店名清洗为可安全用于 Windows 目录的名称。"""
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .")
    if not safe_name:
        return "unknown"
    if safe_name.upper() in WINDOWS_RESERVED_NAMES:
        safe_name += "_"
    return safe_name


PHOTO_CDN_RE = re.compile(r"lh3\.googleusercontent\.com|googleusercontent\.com")


def _is_candidate_photo_url(url: str) -> bool:
    """排除 Maps 页面中的头像、图标、地图瓦片和街景缩略图。

    商家实拍通常位于 googleusercontent 的 ``/p/`` 路径；用户头像多为
    ``/a-/``，街景则来自 streetviewpixels。这里只做高确定性过滤，剩余
    图片仍交给下载后的本地画面检查，避免误删真实设备照。
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = parsed.netloc.casefold()
    path = parsed.path.casefold()
    lowered = url.casefold()
    if not parsed.scheme.startswith("http"):
        return False
    if any(token in host for token in ("streetview", "maps.gstatic", "maps.googleapis")):
        return False
    if any(token in lowered for token in ("streetview", "staticmap", "maptiles", "/a-/", "/a/")):
        return False
    # 明确的小尺寸参数通常是头像/图标；商家照片会在后面统一改写到 800px。
    size_match = re.search(r"=(?:s|w)(\d+)(?:-h(\d+))?", url, re.IGNORECASE)
    if size_match:
        width = int(size_match.group(1))
        height = int(size_match.group(2) or width)
        if max(width, height) < 160:
            return False
    return "googleusercontent.com" in host or "googleapis.com" in host


def _collect_photos(page, max_photos: int = 8, verbose: bool = False) -> list[str]:
    """收集详情页照片 URL（Google 图片 CDN 任意子域 + CSS 背景图），
    改写缩略图参数为高清，去重取前 N 张。verbose 时打印诊断（无照片时
    能看到页面实际有多少图、是什么 URL，区分'没照片'和'没匹配上'）。"""
    try:
        # 等页面主体渲染（Maps 详情页加载慢且随代理抖动：title 还是 'Loading' 时 body 为空，照片无从谈起）
        try:
            page.wait_for_function(
                "() => !(document.title || '').startsWith('Loading') && document.body && document.body.innerText.trim().length > 0",
                timeout=12000,
            )
        except Exception:  # noqa: BLE001 - 12s 还在 Loading：刷新重试一次（慢加载 vs 真风控，给第二次机会）
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                page.wait_for_function(
                    "() => !(document.title || '').startsWith('Loading') && document.body && document.body.innerText.trim().length > 0",
                    timeout=8000,
                )
            except Exception:  # noqa: BLE001 - 刷新后仍不渲染：代理疲劳/风控，按现状继续
                pass
        # 触发照片懒加载：分段滚到底再回顶（Maps 照片区不滚动不加载）
        for step in range(5):
            page.evaluate("window.scrollBy(0, 600)")
            page.wait_for_timeout(250)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(600)
        collect_script = """() => {
            const out = new Set();
            const add = (u) => { if (u && u.startsWith('http')) out.add(u); };
            for (const img of document.querySelectorAll('img')) {
                if (img.naturalWidth && img.naturalHeight && Math.max(img.naturalWidth, img.naturalHeight) < 120) continue;
                add(img.currentSrc || img.src || '');
                const ds = img.getAttribute('data-src'); if (ds) add(ds);
            }
            for (const el of document.querySelectorAll('[style]')) {
                const bg = getComputedStyle(el).backgroundImage;
                if (bg && bg.startsWith('url(')) add(bg.slice(4, -1).replace(/["']/g, ''));
            }
            return Array.from(out);
        }"""
        urls = page.evaluate(collect_script)

        # 详情页首屏通常只有门头/招牌。尝试进入完整图库并分段滚动，失败则沿用首屏结果。
        gallery_selectors = (
            'button[aria-label*="photo" i], [role="button"][aria-label*="photo" i], '
            'button:has-text("Photos"), button:has-text("相片"), button:has-text("照片"), '
            'button:has-text("圖片"), button:has-text("图片")'
        )
        try:
            gallery_buttons = page.locator(gallery_selectors)
            for button_index in range(min(gallery_buttons.count(), 8)):
                button = gallery_buttons.nth(button_index)
                if not button.is_visible():
                    continue
                button.click(timeout=2500)
                page.wait_for_timeout(1000)
                gallery_urls: list[str] = []
                for _ in range(6):
                    gallery_urls.extend(page.evaluate(collect_script) or [])
                    page.evaluate("""() => {
                        const nodes = Array.from(document.querySelectorAll('[role="dialog"] div, main div, [role="main"] div'));
                        const scrollable = nodes
                          .filter((el) => el.scrollHeight > el.clientHeight + 180 && el.clientHeight > 180)
                          .sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
                        if (scrollable) scrollable.scrollBy(0, Math.max(600, scrollable.clientHeight * 0.8));
                        else window.scrollBy(0, 800);
                    }""")
                    page.wait_for_timeout(350)
                urls.extend(gallery_urls)
                break
        except Exception:  # noqa: BLE001 - Maps 语言/布局变化时保留首屏兜底
            pass
    except Exception:  # noqa: BLE001
        return []
    urls = urls or []
    if verbose:
        print(f"[maps]   诊断: 页面共 {len(urls)} 个图片 URL")
        for u in urls[:6]:
            print(f"[maps]      img: {u[:110]}")
    # 只保留候选商家实拍；头像、图标、地图和街景资源在这里先被排除。
    cdn = [u for u in urls if _is_candidate_photo_url(u)]
    if verbose:
        print(f"[maps]   诊断: 其中 CDN {len(cdn)} 个")
    cleaned: list[str] = []
    photo_keys: set[str] = set()
    for u in cdn:
        # 缩略图参数（=w100-h100-k-no）改写为高清（=w800-h800）
        u2 = re.sub(r"=w\d+(-h\d+)?-k-no", "=w800-h800-k-no", u)
        u2 = re.sub(r"=w\d+-h\d+", "=w800-h800", u2)
        # 同一 Google 照片常以不同裁剪参数重复出现；参数前路径相同视为同图。
        photo_key = u2.split("=", 1)[0]
        if photo_key not in photo_keys:
            photo_keys.add(photo_key)
            cleaned.append(u2)
        if len(cleaned) >= 24:
            break
    if verbose:
        print(f"[maps]   诊断: URL/画面路径去重后 {len(cleaned)} 张候选照片")
        if len(cleaned) == 1:
            print("[maps]   该店当前仅提供 1 张非头像、非街景的商家照片")
    if len(cleaned) <= max_photos:
        return cleaned
    # 跨图库前中后段均匀取样，避免 8 张全是连续门头照；仍只发起一次视觉请求。
    indexes = [round(i * (len(cleaned) - 1) / (max_photos - 1)) for i in range(max_photos)]
    return [cleaned[index] for index in indexes]


def _parse_item_text(text: str) -> dict:
    """解析 Google Maps 列表项文本：
    店名 / 评分★ (评论数) / 类别 · 地址"""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    item = {"name": lines[0] if lines else "", "rating": "", "reviews": "", "address": ""}
    for ln in lines[1:]:
        m = RATING_RE.search(ln)
        if m and not item["rating"]:
            item["rating"] = m.group(1)
            continue
        m = REVIEWS_RE.search(ln)
        if m and not item["reviews"]:
            item["reviews"] = m.group(1)
            continue
        # 地址/类别行（含 · 分隔）
        if "·" in ln:
            parts = [p.strip() for p in ln.split("·")]
            item["address"] = parts[-1] if parts else ""
    return item


def _is_google_internal_host(host: str) -> bool:
    normalized = host.casefold().strip(".")
    return (
        bool(re.fullmatch(r"(?:[^.]+\.)*google\.[a-z.]+", normalized))
        or any(normalized == domain or normalized.endswith(f".{domain}") for domain in (
            "gstatic.com", "googleusercontent.com", "googleapis.com",
        ))
    )


def _extract_website(page) -> str:
    """在 place 详情页找官网。

    要点：Google Maps 的官网按钮 href 常是跳转链接
    https://www.google.com/url?q=http://real-domain → 需解析出真实地址。
    """
    for a in page.locator("a[href^='http']").all():
        try:
            href = a.get_attribute("href") or ""
        except Exception:  # noqa: BLE001
            continue
        if not href:
            continue
        # Google 跳转链接 → 解析真实 URL
        if "google.com/url?q=" in href:
            m = re.search(r"[?&]q=([^&]+)", href)
            if m:
                href = unquote(m.group(1))
        try:
            host = (urlsplit(href).hostname or "").casefold()
        except ValueError:
            continue
        if _is_google_internal_host(host):
            continue
        if _is_skip_domain(href):
            continue
        return href
    return ""


def _extract_phone(page) -> str:
    """place 详情页电话：<a href="tel:..."> 或 button[data-item-id^="phone:tel:"]。"""
    for sel in ('a[href^="tel:"]', 'button[data-item-id^="phone:tel:"]'):
        el = page.locator(sel).first
        try:
            if el.count() == 0:
                continue
            attr = el.get_attribute("href") or el.get_attribute("data-item-id") or ""
            m = re.search(r"tel:\s*([+\d][\d\s\-()]*)", attr, re.I)
            if m:
                return re.sub(r"[\s\-()]", "", m.group(1))
        except Exception:  # noqa: BLE001
            continue
    return ""


def _extract_wa(page) -> str:
    """place 详情页的 WhatsApp 按钮（香港商家常直接挂 wa.me 链接）：
    <a href="https://wa.me/852xxx"> 或 button[data-item-id="phone:wa.me:..."]。"""
    for sel in ('a[href*="wa.me"]', 'button[data-item-id*="wa.me"]'):
        el = page.locator(sel).first
        try:
            if el.count() == 0:
                continue
            attr = el.get_attribute("href") or el.get_attribute("data-item-id") or ""
            m = re.search(r"wa\.me/(\d+)", attr)
            if m:
                return f"https://wa.me/{m.group(1)}"
        except Exception:  # noqa: BLE001
            continue
    return ""


def _wa_link(phone: str) -> str:
    """电话 → WhatsApp 链接。香港手机/固话 8 位本地号 → wa.me/852xxxxxxxx。"""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("852") and len(digits) == 11:
        return f"https://wa.me/{digits}"
    if len(digits) == 8:  # 香港本地 8 位
        return f"https://wa.me/852{digits}"
    if digits.isdigit() and len(digits) >= 10:
        return f"https://wa.me/{digits}"  # 其他国家号码
    return ""


def hunt_maps_customers(queries: list[str], country: str = "", max_customers: int = 10,
                        headless: bool = True, verbose: bool = False,
                        cancel_check=None, result_filter=None,
                        max_candidates: int | None = None,
                        excluded_place_keys: set[str] | None = None,
                        exclude_filter=None, save_photos: bool = True) -> list[dict]:
    """核心流程：多词搜索 → 详情 → 官网/电话/WhatsApp → 邮箱/WhatsApp。

    max_customers 是目标有效结果数；为避免低命中率时提前结束，默认最多检查目标数 3 倍的候选。
    result_filter 可指定什么结果算作目标命中（例如只要邮箱）。
    cancel_check: Callable[[], bool]，返回 True 表示用户取消（每家之间检查）。
    """
    from playwright.sync_api import sync_playwright

    proxy = settings.crawler_proxy
    found: list[dict] = []
    target_results = max(1, max_customers)
    candidate_limit = max(target_results, max_candidates or target_results * 3)
    excluded_places = {str(key) for key in (excluded_place_keys or set()) if key}
    discovery_limit = max(candidate_limit, target_results * 8) if (excluded_places or exclude_filter) else candidate_limit
    try:
        products_file = ROOT_DIR / "data" / "products.json"
        products = json.loads(products_file.read_text(encoding="utf-8")) if products_file.exists() else []
        product_catalog = build_catalog(products)
    except Exception:  # noqa: BLE001 - 产品库异常不影响客户采集
        product_catalog = []

    def enough_results() -> bool:
        return target_reached(found, target_results, result_filter)

    with sync_playwright() as p:
        # 启动最多 15s（代理握手卡死时不再无限等，直接报错暴露问题）
        browser = launch_chromium(
            p.chromium,
            headless=headless,
            proxy={"server": proxy} if proxy else None,
            timeout=15000,
        )
        if cancel_check and cancel_check():
            if verbose:
                print("[maps] 启动期间用户已取消")
            browser.close()
            return found
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            locale="zh-HK",
            viewport={"width": 1280, "height": 800},
        )
        # 隐藏自动化痕迹，降低验证码概率
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        # 预置 Google consent cookie（跳过 GDPR 同意页，SOCS=CAI 表示接受全部）
        context.add_cookies([{
            "name": "SOCS",
            "value": "CAI",
            "domain": ".google.com",
            "path": "/",
        }])
        page = context.new_page()

        seen_places: set[str] = set()  # 跨词全局去重
        processed = 0  # 已处理详情数（含跳过的），达到 max 即停——避免 92 家全爬
        for qi, q in enumerate(queries):
            if enough_results() or processed >= candidate_limit:
                break
            query = f"{q} {country}".strip()
            url = f"https://www.google.com/maps/search/{quote(query)}"
            if verbose:
                print(f"\n[maps] 词 [{qi+1}/{len(queries)}] 搜索: {query}")
            # 导航失败重试一次（代理抖动/慢网常见），commit 模式避免 60s 干等
            nav_ok = False
            for attempt in (1, 2):
                try:
                    page.goto(url, timeout=30000, wait_until="commit")
                    nav_ok = True
                    break
                except Exception:  # noqa: BLE001
                    if verbose and attempt == 1:
                        print(f"[maps]   首次导航超时（{query}），重试…")
            if not nav_ok:
                if verbose:
                    print(f"[maps]   导航失败，跳过该词：{query}")
                continue

            # 兜底：若仍停在 consent 页，多语言匹配同意按钮
            if "consent.google.com" in page.url:
                clicked = False
                for text in ("Accept all", "Alle akzeptieren", "Tout accepter",
                             "Aceptar todo", "Accetta tutto", "同意全部", "全部接受"):
                    btn = page.locator(f'button:has-text("{text}")')
                    if btn.count():
                        try:
                            btn.first.click(timeout=5000)
                            page.wait_for_timeout(3000)
                            clicked = True
                            break
                        except Exception:  # noqa: BLE001
                            continue
                if not clicked:
                    print("[maps] 卡在 consent 同意页且未找到按钮，--headful 手动点一次")
                    browser.close()
                    return []
                if verbose:
                    print(f"[maps] 已处理 consent 页（按钮: {text}）")

            # 等列表出现（首次空白会刷新重试一次：慢加载 vs 真风控，两种状态区分开）
            list_ok = False
            for _attempt in (1, 2):
                try:
                    page.wait_for_selector('a[href*="/maps/place/"]', timeout=30000)
                    list_ok = True
                    break
                except Exception:  # noqa: BLE001
                    try:
                        body_txt = page.inner_text("body")[:160].replace("\n", " ")
                    except Exception:  # noqa: BLE001
                        body_txt = ""
                    if _attempt == 2 or len(body_txt.strip()) < 20:
                        # 第二次失败，或页面有内容（无结果/验证码/单店卡）——不再重试
                        try:
                            u = page.url[:120]
                            print(f"[maps] 「{query}」列表未加载（第 {_attempt} 次）\n       URL: {u}\n       页面文本: {body_txt or '(空)'}")
                        except Exception:  # noqa: BLE001
                            print(f"[maps] 「{query}」列表未加载（代理/验证码/反爬？），跳过此词")
                        break
                    print(f"[maps] 「{query}」页面空白，刷新重试…")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(4000)
                    except Exception:  # noqa: BLE001
                        break
            if not list_ok:
                continue

            # 滚动加载更多（Maps 列表是独立滚动容器 role=feed，必须滚它而不是主视口）
            stall = 0  # 连续几轮没有新店才判定到底（防加载慢误判）
            batch_new: list[str] = []  # 本词新增的店（只处理这些，避免跨词重复）
            for _ in range(60):
                if cancel_check and cancel_check():
                    if verbose:
                        print("[maps] 用户取消，提前收工")
                    browser.close()
                    return found
                page.evaluate("""() => {
                    const feed = document.querySelector('[role="feed"]');
                    if (feed) { feed.scrollTop = feed.scrollHeight; }
                    window.scrollTo(0, document.body.scrollHeight);
                }""")
                # 2s 等待拆成 4×500ms，取消后 ~0.5s 内响应
                for _ in range(4):
                    page.wait_for_timeout(500)
                    if cancel_check and cancel_check():
                        if verbose:
                            print("[maps] 用户取消，提前收工")
                        browser.close()
                        return found
                anchors = page.locator('a[href*="/maps/place/"]')
                count = anchors.count()
                new = 0
                for i in range(count):
                    try:
                        href = anchors.nth(i).get_attribute("href")
                    except Exception:  # noqa: BLE001
                        continue
                    if href and href not in seen_places:
                        seen_places.add(href)
                        key = _place_key(href)
                        if key and key in excluded_places:
                            if verbose:
                                print(f"[maps]   跳过已爬客户 {_name_from_place_url(href) or key}，继续寻找新客户")
                            continue
                        batch_new.append(href)
                        new += 1
                if verbose:
                    print(f"[maps]   滚动中… 累计 {len(seen_places)} 家")
                if new == 0:
                    stall += 1
                    if stall >= 3:  # 连续 3 轮无新增才认为到底
                        break
                else:
                    stall = 0
                if len(batch_new) >= discovery_limit:
                    break

            if verbose:
                print(f"[maps] 「{query}」新增 {len(batch_new)} 家，累计 {len(seen_places)} 家（去重后）")

            # 逐个访问本词新增的 place 详情页（列表点击被 JS 拦截，goto 详情页更可靠）
            for i, href in enumerate(batch_new):
                if cancel_check and cancel_check():
                    if verbose:
                        print("[maps] 用户取消，提前收工")
                    browser.close()
                    return found
                if enough_results() or processed >= candidate_limit:
                    break
                place_name = _name_from_place_url(href)
                place_key = _place_key(href)
                try:
                    # commit：导航开始即返回（place 页加载慢不会导致 goto 超时），
                    # 等电话/WhatsApp 按钮出现再提取（慢网下按钮可能晚渲染）
                    page.goto(href, timeout=12000, wait_until="commit")
                    try:
                        page.wait_for_selector(
                            'a[href^="tel:"], button[data-item-id^="phone:"], a[href*="wa.me"]',
                            timeout=3500,
                        )
                    except Exception:  # noqa: BLE001 - 没按钮也继续提取
                        pass
                    # 1.5s 缓冲拆成 3×500ms，取消后 ~0.5-1.5s 内响应
                    for _ in range(3):
                        page.wait_for_timeout(500)
                        if cancel_check and cancel_check():
                            if verbose:
                                print("[maps] 用户取消，提前收工")
                            browser.close()
                            return found
                except Exception:  # noqa: BLE001 - 导航失败也继续尝试提取
                    pass
                if verbose:
                    # 详情页状态诊断：区分“页面正常”“页面空白”“验证码拦截”
                    try:
                        _t = page.title()[:70]
                        _b = page.inner_text("body")[:150].replace("\n", " ")
                        print(f"[maps]   详情页状态: {_t!r} | body: {_b!r}")
                    except Exception:  # noqa: BLE001
                        print("[maps]   详情页状态: 页面未加载（DOM 读取失败）")
                try:
                    website = _extract_website(page)
                except Exception:  # noqa: BLE001
                    website = ""
                try:
                    phone = _extract_phone(page)
                except Exception:  # noqa: BLE001
                    phone = ""
                try:
                    wa_direct = _extract_wa(page)  # 优先用 Maps 上的 WhatsApp 按钮（直连，最准）
                except Exception:  # noqa: BLE001
                    wa_direct = ""
                wa_link = wa_direct or _wa_link(phone)

                # 旧数据可能没有 place key：详情基础字段到手后，再按官网/电话/名称兜底。
                candidate_stub = {
                    "name": place_name, "country": country, "website": website,
                    "phone": phone, "_place_key": place_key,
                }
                if exclude_filter and exclude_filter(candidate_stub):
                    if verbose:
                        print(f"[maps] [{i+1}] 跳过已爬客户 {place_name or website or phone or place_key}，继续寻找新客户")
                    continue
                processed += 1

                instrument = "unknown"
                instruments: list[str] = []
                emails: list[str] = []
                name = place_name
                text = ""  # 官网/兜底文本（概览与仪器检测共用）
                mtext = ""
                if website:
                    with _new_http_client(proxy) as website_client:
                        text, page_title = fetch_site_text(website, proxy, client=website_client)
                        if not text:
                            # 官网没抓到文本 → 用 Maps 详情页文本兜底检测仪器
                            # （商家简介/服务词常直接写“激光脫毛”“皮秒”等）
                            try:
                                text = page.inner_text("body")[:3000]
                            except Exception:  # noqa: BLE001
                                text = ""
                        if text:
                            instrument = detect_instrument(text)
                            instruments = detect_instruments(text)  # 具体仪器（激光/皮秒/IPL…）
                        if not text and not page_title:
                            # 官网首页都拿不到（超时/不可达）→ 不浪费 4 页挖邮箱，直接跳过
                            if verbose:
                                print(f"[maps]   官网不可达，跳过挖邮箱: {website}")
                        else:
                            crawled, _ = _crawl_emails(website, proxy, client=website_client)
                            emails = crawled
                    if not name and page_title:
                        # 官网 title 常是「首頁 - 店名」「Home | 店名」：取最后一个有效段，过滤噪音词
                        parts = [p.strip() for p in re.split(r"\s*[|\-–—]\s*", page_title) if p.strip()]
                        junk = {"首頁", "首頁", "首页", "主頁", "主页", "home", "welcome", "index"}
                        parts = [p for p in parts if p.lower() not in junk]
                        name = (parts[-1] if parts else page_title).strip()[:60]
                    elif not name:
                        name = website.split("//")[-1].split("/")[0]
                        name = re.sub(r"\s*[|\-–—]\s*.*$", "", name).strip()[:60]
                else:
                    # 没官网：Maps 详情页文本同样可做仪器检测（描述/服务词）
                    try:
                        mtext = page.inner_text("body")[:3000]
                    except Exception:  # noqa: BLE001
                        mtext = ""
                    if mtext:
                        instrument = detect_instrument(mtext)
                        instruments = detect_instruments(mtext)

                # 纯手法按摩店：跳过（用户要求：要有仪器项目）
                if instrument in SKIP_INSTRUMENT:
                    if verbose:
                        print(f"[maps] [{i+1}] {name or place_name or '?'} 纯手法按摩，跳过")
                    continue

                # ---------- 新：概览 + 照片识别 + 缺品分析 ----------
                overview = (text or mtext or "")[:400].strip()
                photo_analysis: list[str] = []
                gap_recs: list[str] = []
                equipment_labels = list(instruments)
                try:
                    vcfg = get_vision_config()
                except Exception:  # noqa: BLE001
                    vcfg = {"api_key": "", "model": "luna"}

                if get_vision_failover_configs(vcfg.get("provider", "")):
                    # 标准模式：每店最多 8 张、一次视觉请求；单图失败不影响整家。
                    photos = _collect_photos(page, max_photos=8, verbose=verbose)
                    if photos:
                        if verbose:
                            print(f"[maps]   照片 {len(photos)} 张 → 视觉识别…")
                        def _browser_dl(u: str) -> bytes:
                            resp = page.request.get(u, timeout=15000)
                            if not resp.ok:
                                raise RuntimeError(f"browser download {resp.status}")
                            return resp.body()
                        try:
                            items = analyze_photos(
                                photos, vcfg["api_key"], proxy,
                                vcfg.get("model") or "luna",
                                vcfg.get("base_url") or "",
                                needs_proxy=bool(vcfg.get("needs_proxy", True)),
                                downloader=_browser_dl,
                                verbose=verbose,
                                save_dir=(
                                    str(
                                        ROOT_DIR / "data" / "crawler_photos" /
                                        _safe_photo_dir_name(name or place_name or "unknown")
                                    )
                                    if save_photos else ""
                                ),
                            )
                        except Exception:  # noqa: BLE001
                            items = []
                        for it in items:
                            dev = (it.get("device") or "").strip()
                            if not dev:
                                continue
                            brand = (it.get("brand") or "").strip()
                            label = dev + (f"（{brand}）" if brand else "")
                            if label not in photo_analysis:
                                photo_analysis.append(label)
                            if dev not in equipment_labels:
                                equipment_labels.append(dev)  # 设备名并入品类对比
                        if verbose and photo_analysis:
                            print(f"[maps]   照片识别: {'; '.join(photo_analysis[:6])}")
                    elif verbose:
                        print("[maps]   该店无照片（跳过视觉识别）")

                # 缺品分析：店方仪器标签 → 对比产品库品类 → 推荐缺的
                if product_catalog:
                    gap_recs = gap_analysis(equipment_labels, product_catalog)
                if verbose and gap_recs:
                    print(f"[maps]   缺品推荐: {'; '.join(gap_recs)}")

                if emails:
                    found.append({
                        "name": name or place_name or "?",
                        "country": country, "city": "",
                        "website": website, "email": "|".join(emails),
                        "phone": phone, "wa_link": wa_link, "instrument": instrument,
                        "instruments": ",".join(instruments),
                        "overview": overview,
                        "photo_analysis": "; ".join(photo_analysis),
                        "gap_recs": "; ".join(gap_recs),
                        "_place_key": place_key,
                    })
                    if verbose:
                        ins_str = f"（{','.join(instruments)}）" if instruments else ""
                        print(f"[maps] [{i+1}] {name} | 邮箱 [OK] | 仪器:{instrument}{ins_str}")
                elif phone or wa_link:
                    # 无邮箱但有电话/WhatsApp → WhatsApp 名单（wa.me 链接手动发消息零风控）
                    found.append({
                        "name": name or place_name or "?",
                        "country": country, "city": "",
                        "website": website, "email": "（WhatsApp 跟进）",
                        "phone": phone, "wa_link": wa_link, "instrument": instrument,
                        "instruments": ",".join(instruments),
                        "overview": overview,
                        "photo_analysis": "; ".join(photo_analysis),
                        "gap_recs": "; ".join(gap_recs),
                        "_place_key": place_key,
                    })
                    if verbose:
                        ins_str = f"（{','.join(instruments)}）" if instruments else ""
                        print(f"[maps] [{i+1}] {name} | 电话/WhatsApp → 跟进{ins_str}")
                else:
                    if verbose:
                        print(f"[maps] [{i+1}] {name or place_name or '?'} 无邮箱无电话，跳过")

        browser.close()

    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="Tradewind Google Maps 客户挖掘")
    parser.add_argument("--query", default="", help="单个搜索词（兼容旧用法）")
    parser.add_argument("--queries", default="", help="多个搜索词，逗号分隔（如：醫學美容,醫美診所,美容中心 儀器）")
    parser.add_argument("--country", default="", help="国家/地区（如 香港 / Spain）")
    parser.add_argument("--max", type=int, default=10, help="最多挖几个客户（跨词总计）")
    parser.add_argument("-o", "--out", default="", help="输出 CSV 路径")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（反爬验证时用）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # 词源：--queries（逗号分隔）优先，其次 --query，最后默认
    if args.queries.strip():
        queries = [q.strip() for q in args.queries.split(",") if q.strip()]
    elif args.query.strip():
        queries = [args.query.strip()]
    else:
        queries = ["beauty salon"]

    print(f"[maps] 搜索 {len(queries)} 个词: {queries}（目标 {args.max} 条有效客户）...")
    t0 = time.time()
    customers = hunt_maps_customers(queries, args.country, args.max,
                                    headless=not args.headful, verbose=args.verbose)

    if not customers:
        print("[maps] 未挖到客户（代理/验证码/反爬？），--headful 重试或换代理")
        return

    out = args.out or str(ROOT_DIR / "data" / "maps_hk.csv")
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["name", "country", "city", "website", "email", "phone", "wa_link", "instrument", "instruments", "overview", "photo_analysis", "gap_recs"]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(customers)

    with_email = sum(1 for c in customers if c["email"] and "WhatsApp" not in c["email"])
    with_wa = sum(1 for c in customers if c["wa_link"])
    with_instrument = sum(1 for c in customers if c["instrument"] == "yes")
    print(f"[maps] 完成：{len(customers)} 家（{time.time()-t0:.0f}s）→ {out_path}")
    print(f"  邮箱 {with_email} 家 | WhatsApp 候选 {with_wa} 家 | 仪器项目标记 {with_instrument} 家")
    for c in customers:
        tag = {"yes": "仪器[OK]", "unknown": "未知", "massage": "按摩[NO]"}.get(c["instrument"], c["instrument"])
        ins = f"（{c['instruments']}）" if c.get("instruments") else ""
        photo = f" [PHOTO]{'; '.join(c['photo_analysis'].split('; ')[:3])}" if c.get("photo_analysis") else ""
        gap = f" → 推:{c['gap_recs']}" if c.get("gap_recs") else ""
        print(f"  - [{tag}] {c['name'][:32]}{ins}{photo}{gap} | {c['email'][:40]}")


if __name__ == "__main__":
    main()
