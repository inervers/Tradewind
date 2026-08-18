"""视觉识别模块：商家照片 → 仪器/品牌/用途（GLM/Qwen/火山）。

- httpx 调用 OpenAI 兼容接口（零 SDK 依赖，exe 打包不受影响）
- 图片下载走代理，默认仅保留内存 bytes，可选同步落盘供人工抽查
- 同 URL + 服务商 + 模型内存缓存：多店共用照片不重复计费
- 主服务商失败时，仅切换到本机已配置 Key 的备用服务商
- 任何失败静默降级返回 []，不拖垮整轮采集
"""

import base64
import hashlib
import json
import math
import random
import re
import statistics
import threading
import time
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from app.config import VISION_PROVIDERS, get_vision_config, get_vision_failover_configs
from app.crawler.progress import report as print  # 终端输出或当前 API 任务日志

VISION_MODEL_DEFAULT = ""

PROMPT = (
    "你是医美设备专家。下面是同一家美容院/医美诊所的照片，识别其中明确可见的仪器设备。\n"
    "只输出 JSON，格式严格如下：\n"
    '{"items": [{"device": "仪器名称", "brand": "品牌或null", "purpose": "用途一句话", "confidence": 0-1}], "notes": "其他观察或null"}\n'
    "规则：\n"
    "- 只识别画面中明确可见的仪器，模糊、看不清、无法判断的不要硬编\n"
    "- 仪器名称用中文（如：激光脱毛仪、皮秒激光、强脉冲光IPL、水光针、皮肤检测仪、微针、射频仪）\n"
    "- 品牌如果能看到机身 logo/文字就写，看不到就 null\n"
    "- 多张照片出现同一台或同类设备时只输出一次\n"
    "- 照片可能是店内环境、操作场景、仪器特写、宣传图，只要没有仪器就输出空 items"
)

PROMPT_VERSION = "vision-v2"
MIN_CONFIDENCE = 0.55
VISION_MAX_ATTEMPTS = 3
_cache: dict[tuple[str, str, str], list[dict]] = {}
_cache_lock = threading.Lock()


def analyze_photo(url: str, api_key: str = "", proxy: str = "", model: str = VISION_MODEL_DEFAULT,
                  base_url: str = "", needs_proxy: bool | None = None, downloader=None,
                  verbose: bool = False, save_dir: str = "", provider: str = "",
                  enable_failover: bool = True) -> list[dict]:
    """下载图片 → 视觉识别 → 返回仪器列表 [{device, brand, purpose, confidence}]。失败返回 []。

    downloader: 可选 callable(url) → bytes，优先使用（爬虫传 Playwright page.request 走浏览器网络栈，
    下载 Google 图片 CDN 比独立 httpx 稳得多）；失败或缺失时回退 httpx。
    """
    return analyze_photos(
        [url], api_key, proxy, model, base_url, needs_proxy, downloader, verbose,
        save_dir=save_dir, provider=provider, enable_failover=enable_failover,
    )


def analyze_photos(urls: list[str], api_key: str = "", proxy: str = "",
                   model: str = VISION_MODEL_DEFAULT, base_url: str = "",
                   needs_proxy: bool | None = None, downloader=None,
                   verbose: bool = False, save_dir: str = "", provider: str = "",
                   enable_failover: bool = True) -> list[dict]:
    """同一家店的多张照片一次分析；成功结果按模型和图片集合缓存。"""
    # 标准模式硬上限：单店/单批最多 8 张，避免图库过多时费用失控。
    clean_urls = list(dict.fromkeys(u for u in urls if u))[:8]
    if not clean_urls:
        return []
    candidates = _provider_candidates(
        api_key, model, base_url, needs_proxy, provider, enable_failover,
    )
    if not candidates:
        return []
    url_digest = hashlib.sha256("\n".join(clean_urls).encode()).hexdigest()
    for candidate in candidates:
        cache_key = _cache_key(url_digest, candidate)
        with _cache_lock:
            cached = _cache.get(cache_key)
            if cached is not None:
                return [dict(item) for item in cached]

    images: list[bytes] = []
    content_hashes: set[bytes] = set()
    perceptual_hashes: list[int] = []
    for index, url in enumerate(clean_urls, start=1):
        img = None
        if downloader:
            try:
                img = downloader(url)
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    print(f"[vision] 浏览器下载失败，回退 httpx: {str(exc)[:80]}")
        if not img:
            img = _download(url, proxy, verbose=verbose)
        if img:
            usable, reason = _candidate_image_check(img)
            if not usable:
                if verbose:
                    print(f"[vision] 跳过无效照片: {reason} | {url[:90]}")
                continue
            content_hash = hashlib.sha256(img).digest()
            perceptual_hash = _perceptual_hash(img)
            if content_hash in content_hashes or (
                perceptual_hash is not None
                and any(_hash_distance(perceptual_hash, known) <= 10 for known in perceptual_hashes)
            ):
                if verbose:
                    print(f"[vision] 跳过近似重复照片 | {url[:90]}")
                continue
            content_hashes.add(content_hash)
            if perceptual_hash is not None:
                perceptual_hashes.append(perceptual_hash)
            if save_dir:
                try:
                    photo_dir = Path(save_dir)
                    photo_dir.mkdir(parents=True, exist_ok=True)
                    suffix = _image_suffix(img)
                    short_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:6]
                    photo_path = photo_dir / f"{index:02d}_{short_hash}{suffix}"
                    photo_path.write_bytes(img)
                    if verbose:
                        print(f"[vision] 已保存照片: {photo_path}")
                except Exception:  # noqa: BLE001 - 磁盘问题不能影响视觉识别
                    pass
            images.append(img)
        elif verbose:
            print(f"[vision] 图片下载失败: {url[:90]}")
    if not images:
        return []

    items, _error, used = _analyze_images(images, candidates, proxy, verbose)
    if items is not None and used is not None:
        with _cache_lock:
            _cache[_cache_key(url_digest, used)] = [dict(item) for item in items]
    return items or []


def analyze_image_bytes_with_meta(
    images: list[bytes], proxy: str = "", verbose: bool = False,
    api_key: str = "", model: str = "", base_url: str = "",
    needs_proxy: bool | None = None, provider: str = "",
    enable_failover: bool = True,
) -> tuple[list[dict], str | None, dict | None]:
    """识别已在内存中的图片；供照片筛选任务复用下载后的 bytes。"""
    clean_images = [img for img in images if img][:12]
    if not clean_images:
        return [], "图片内容为空", None
    candidates = _provider_candidates(
        api_key, model, base_url, needs_proxy, provider, enable_failover,
    )
    if not candidates:
        return [], "未配置视觉识别 API Key", None
    items, error, used = _analyze_images(clean_images, candidates, proxy, verbose)
    return items or [], error, used


def _infer_provider(base_url: str, fallback: str) -> str:
    normalized = base_url.rstrip("/")
    for pid, cfg in VISION_PROVIDERS.items():
        if normalized and normalized == str(cfg["base_url"]).rstrip("/"):
            return pid
    return fallback if fallback in VISION_PROVIDERS else "glm"


def _provider_candidates(api_key: str, model: str, base_url: str,
                         needs_proxy: bool | None, provider: str,
                         enable_failover: bool) -> list[dict]:
    """合并显式主配置与本机备用配置，且不重复尝试同一服务商。"""
    current = get_vision_config()
    primary_provider = provider if provider in VISION_PROVIDERS else _infer_provider(base_url, current["provider"])
    primary_defaults = next(
        (cfg for cfg in get_vision_failover_configs(primary_provider) if cfg["provider"] == primary_provider),
        current if current["provider"] == primary_provider else {
            "provider": primary_provider,
            "api_key": "", "model": next(iter(VISION_PROVIDERS[primary_provider]["models"])),
            "base_url": VISION_PROVIDERS[primary_provider]["base_url"],
            "needs_proxy": VISION_PROVIDERS[primary_provider]["needs_proxy"],
        },
    )
    primary = {
        "provider": primary_provider,
        "api_key": api_key or primary_defaults["api_key"],
        "model": model or primary_defaults["model"],
        "base_url": base_url or primary_defaults["base_url"],
        "needs_proxy": primary_defaults["needs_proxy"] if needs_proxy is None else needs_proxy,
    }
    out: list[dict] = [primary] if primary["api_key"] else []
    if enable_failover:
        for candidate in get_vision_failover_configs(primary_provider):
            if candidate["provider"] == primary_provider:
                continue
            out.append(candidate)
    return out


def _cache_key(url_digest: str, candidate: dict) -> tuple[str, str, str]:
    provider_model = f"{candidate['provider']}:{candidate['model']}"
    endpoint = str(candidate["base_url"]).rstrip("/")
    return (url_digest, f"{provider_model}@{endpoint}", PROMPT_VERSION)


def _analyze_images(images: list[bytes], candidates: list[dict], proxy: str,
                    verbose: bool) -> tuple[list[dict] | None, str | None, dict | None]:
    last_error = "视觉服务暂不可用"
    for index, candidate in enumerate(candidates):
        pid = candidate["provider"]
        model = candidate["model"]
        if verbose:
            action = "调用" if index == 0 else "切换备用"
            print(f"[vision] {action} {pid}/{model}")
        items, error = _ask_vision(
            images, candidate["api_key"], model,
            proxy if candidate.get("needs_proxy") else "",
            candidate["base_url"], verbose=verbose, provider=pid,
        )
        if items is not None:
            return items, None, candidate
        last_error = error or last_error
    return None, last_error, None


def _download(url: str, proxy: str, verbose: bool = False) -> bytes | None:
    """下载图片（Google 图片 CDN，走代理）。"""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = httpx.get(url, headers=headers, timeout=10, follow_redirects=True, proxy=proxy or None)
        if r.status_code != 200 or not r.content:
            if verbose:
                print(f"[vision] 下载异常状态: {r.status_code} 字节:{len(r.content or b'')} | {url[:90]}")
            return None
        return r.content
    except Exception as exc:  # noqa: BLE001 - 单图失败跳过
        if verbose:
            print(f"[vision] 下载异常: {exc} | {url[:90]}")
        return None


def _image_media_type(img: bytes) -> str:
    """根据文件签名确定 data URL MIME，避免把 PNG/WebP 误报为 JPEG。"""
    if img.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if img.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if img.startswith(b"RIFF") and img[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _image_suffix(img: bytes) -> str:
    """根据 magic bytes 选择落盘扩展名；未知格式按 JPEG 保存。"""
    if img.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if img.startswith(b"\x89PNG"):
        return ".png"
    if img.startswith(b"RIFF") and img[8:12] == b"WEBP":
        return ".webp"
    if img.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return ".jpg"


def _candidate_image_check(img: bytes) -> tuple[bool, str]:
    """用本地、低成本规则过滤头像/占位图和过小图片。

    解码失败时仍保留，由视觉接口按原有逻辑处理；这既兼容测试桩，也避免
    因少见图片编码误删真实照片。规则只拦截高确定性的无效图，不尝试在本地
    判断“室内还是仪器”，因此不会新增模型调用或下载。
    """
    try:
        with Image.open(BytesIO(img)) as image:
            width, height = image.size
            if width < 240 or height < 180:
                return False, f"尺寸过小 {width}x{height}"
            ratio = width / max(height, 1)
            if ratio > 1.85 or ratio < 0.42:
                return False, f"疑似街景/长条缩略图 {width}x{height}"

            sample = image.convert("RGB")
            sample.thumbnail((48, 48))
            quantized = sample.quantize(colors=32)
            color_count = len(quantized.getcolors(maxcolors=33) or [])
            entropy = sample.entropy()
            # 字母头像、纯色圆形等占位图通常颜色极少且接近方形。
            near_square = 0.82 <= ratio <= 1.22
            if color_count <= 8 or (near_square and entropy < 2.35):
                return False, f"疑似头像/占位图（颜色 {color_count}，信息量 {entropy:.2f}）"
    except Exception:  # noqa: BLE001 - 少见编码继续交给视觉服务兼容处理
        return True, ""
    return True, ""


def _perceptual_hash(img: bytes) -> int | None:
    """生成 64 位 pHash，识别同一画面的缩放、轻微裁剪和压缩版本。"""
    try:
        with Image.open(BytesIO(img)) as image:
            gray = image.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
            pixels = list(gray.tobytes())
    except Exception:  # noqa: BLE001 - 解码失败不参与近似去重
        return None

    coefficients: list[float] = []
    cosines = [
        [math.cos(math.pi * (2 * position + 1) * frequency / 64) for position in range(32)]
        for frequency in range(8)
    ]
    for vertical in range(8):
        for horizontal in range(8):
            coefficients.append(sum(
                pixels[y * 32 + x] * cosines[horizontal][x] * cosines[vertical][y]
                for y in range(32) for x in range(32)
            ))
    median = statistics.median(coefficients[1:])
    return sum(1 << index for index, value in enumerate(coefficients) if value > median)


def _hash_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _normalize_items(items: object, min_confidence: float = MIN_CONFIDENCE) -> list[dict]:
    """过滤低置信度/畸形结果，并按设备+品牌归一化去重。"""
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        device = str(raw.get("device") or "").strip()
        brand = str(raw.get("brand") or "").strip()
        if brand.lower() in {"null", "none", "未知", "不明"}:
            brand = ""
        try:
            confidence = float(raw.get("confidence"))
        except (TypeError, ValueError):
            continue
        if not device or confidence < min_confidence:
            continue
        confidence = max(0.0, min(1.0, confidence))
        key = (re.sub(r"\s+", "", device).casefold(), re.sub(r"\s+", "", brand).casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "device": device,
            "brand": brand,
            "purpose": str(raw.get("purpose") or "").strip(),
            "confidence": confidence,
        })
    return out


def _ask_vision(images: list[bytes], api_key: str, model_id: str, proxy: str = "",
                base_url: str = "", verbose: bool = False,
                provider: str = "") -> tuple[list[dict] | None, str | None]:
    """调视觉 chat completions；成功返回列表（可为空），调用或解析失败返回 None。"""
    image_parts = [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:{_image_media_type(img)};base64,{base64.b64encode(img).decode()}"
            },
        }
        for img in images
    ]
    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    *image_parts,
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": 700,
        "response_format": {"type": "json_object"},
    }
    if provider == "glm":
        payload["thinking"] = {"type": "disabled"}
    elif provider == "qwen":
        # Qwen3 默认思考会明显增加图片响应时间；当前任务只需结构化识别。
        payload["enable_thinking"] = False
    r = None
    try:
        timeout = httpx.Timeout(90.0, connect=12.0, write=30.0, pool=10.0)
        with httpx.Client(headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, timeout=timeout, proxy=proxy or None) as client:
            for attempt in range(VISION_MAX_ATTEMPTS):
                try:
                    r = client.post(f"{base_url.rstrip('/')}/chat/completions", json=payload)
                except httpx.ReadTimeout:
                    # 服务端可能已收到并计费，不能自动重复提交同一批图片。
                    return None, f"{provider}/{model_id} 处理超过 90 秒，请稍后重试或切换轻量模型"
                except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
                    if attempt >= 1:
                        return None, f"连接 {provider}/{model_id} 超时，请检查网络后重试"
                    delay = 2.0 + random.uniform(0, 0.5)
                    if verbose:
                        print(f"[vision] 连接 {provider}/{model_id} 失败，{delay:.1f}s 后重试一次…")
                    time.sleep(delay)
                    continue
                except httpx.RequestError as exc:
                    if attempt == VISION_MAX_ATTEMPTS - 1:
                        return None, f"{provider}/{model_id} 网络错误：{type(exc).__name__}"
                    delay = min(8.0, 2 ** (attempt + 1) + random.uniform(0, 0.5))
                    if verbose:
                        print(f"[vision] 网络异常 {type(exc).__name__}，{delay:.1f}s 后重试（{attempt + 2}/{VISION_MAX_ATTEMPTS}）…")
                    time.sleep(delay)
                    continue
                if r.status_code not in (429, 500, 502, 503, 504) or attempt == VISION_MAX_ATTEMPTS - 1:
                    break
                retry_after = r.headers.get("Retry-After", "")
                try:
                    delay = min(8.0, max(0.0, float(retry_after)))
                except (TypeError, ValueError):
                    delay = 0.0
                if delay <= 0:
                    delay = min(8.0, 2 ** (attempt + 1) + random.uniform(0, 0.5))
                if verbose:
                    print(f"[vision] 服务繁忙 {r.status_code}，{delay:.1f}s 后重试（{attempt + 2}/{VISION_MAX_ATTEMPTS}）…")
                time.sleep(delay)
        assert r is not None
        if r.status_code != 200:
            if verbose:
                print(f"[vision] 视觉接口非 200: {r.status_code} {r.text[:200]}")
            detail = ""
            try:
                error_data = r.json().get("error", {})
                detail = str(error_data.get("message") or error_data.get("code") or "").strip()
            except Exception:  # noqa: BLE001 - 非 JSON 错误响应只显示状态码
                pass
            suffix = f"：{detail[:160]}" if detail else ""
            return None, f"{provider}/{model_id} 接口返回 {r.status_code}{suffix}"
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        out = _normalize_items(parsed.get("items"))
        if verbose:
            response_model = str(data.get("model") or model_id)
            print(f"[vision] 识别完成: {len(out)} 项 | {provider or 'vision'}/{response_model} | 原文: {content[:150]}")
        return out, None
    except Exception as exc:  # noqa: BLE001 - 单次识别失败跳过
        if verbose:
            print(f"[vision] 调用异常: {type(exc).__name__}: {exc}")
        return None, f"{type(exc).__name__}: {exc}"
