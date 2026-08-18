"""Tradewind 全局配置：.env（开发）+ data/config.json（运行时，用户填写）。

多 Provider 支持（v0.2）：
- config.json 里 providers.<id> 存各家的 api_key/base_url/model
- active_provider 决定生成邮件用哪家
- 兼容旧字段 deepseek_api_key（v0.1 自动迁移）

Key 优先级：
1. data/config.json 的 providers.<active>.api_key（运行时填写）
2. 旧字段 deepseek_api_key（v0.1 兼容）
3. .env 的 DEEPSEEK_API_KEY（开发环境）
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

# 预设 Provider（OpenAI 兼容接口，ChatOpenAI 通用）
PROVIDER_PRESETS: dict[str, dict] = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
    },
    "openai": {
        "name": "OpenAI (GPT)",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "kimi": {
        "name": "Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
    },
    "custom": {
        "name": "自定义（OpenAI 兼容）",
        "base_url": "",
        "model": "",
    },
}

DEFAULT_PROVIDER = "deepseek"

DEFAULT_COMPANY_PROFILE: dict[str, str] = {
    "sender_name": "Demo User",
    "company_name": "DemoMed – Medical Aesthetic Equipment",
    "email": "sales@example.com",
    "whatsapp": "+00 000 000 0000",
    "website": "www.example.com",
}


def base_dir() -> Path:
    """运行数据根目录：桌面端由环境变量指定；源码模式仍使用项目根。"""
    desktop_data_dir = os.getenv("TRADEWIND_DATA_DIR", "").strip()
    if desktop_data_dir:
        return Path(desktop_data_dir).expanduser().resolve()
    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# 项目根目录（Tradewind/）
ROOT_DIR = base_dir()
load_dotenv(ROOT_DIR / ".env")

# 运行时配置文件（源码模式在项目 data；桌面端由 TRADEWIND_DATA_DIR 指向用户目录）
CONFIG_FILE = ROOT_DIR / "data" / "config.json"


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def load_config_file() -> dict:
    """读 data/config.json（不存在返回空 dict）。"""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 配置损坏按空处理
            return {}
    return {}


def save_config_file(data: dict) -> None:
    """写 data/config.json（合并已有字段，不覆盖其他配置）。"""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    merged = {**load_config_file(), **data}
    tmp = CONFIG_FILE.with_name(f".{CONFIG_FILE.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
    finally:
        tmp.unlink(missing_ok=True)


def get_company_profile() -> dict[str, str]:
    """读取本机公司资料；首次使用时返回默认 DemoMed 资料。"""
    raw = load_config_file().get("company_profile", {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        key: str(raw[key]).strip() if key in raw else default
        for key, default in DEFAULT_COMPANY_PROFILE.items()
    }


def set_company_profile(profile: dict[str, str]) -> None:
    """保存公司资料，允许用户清空不需要显示的可选字段。"""
    clean = {
        key: str(profile.get(key, "")).strip()
        for key in DEFAULT_COMPANY_PROFILE
    }
    save_config_file({"company_profile": clean})


# ---------- Provider ----------

def load_providers() -> dict:
    """providers 配置（合并预设默认值，旧 deepseek_api_key 自动迁移）。"""
    raw = load_config_file().get("providers", {})
    providers: dict[str, dict] = {}
    for pid, preset in PROVIDER_PRESETS.items():
        p = dict(preset)
        if pid in raw and isinstance(raw[pid], dict):
            for k, v in raw[pid].items():
                if v:
                    p[k] = v
        providers[pid] = p
    legacy = load_config_file().get("deepseek_api_key", "")
    if legacy and not providers["deepseek"].get("api_key"):
        providers["deepseek"]["api_key"] = legacy
    return providers


def get_active_provider() -> str:
    active = load_config_file().get("active_provider", "")
    return active if active in PROVIDER_PRESETS else DEFAULT_PROVIDER


def set_active_provider(provider: str) -> None:
    if provider in PROVIDER_PRESETS:
        save_config_file({"active_provider": provider})


def get_provider_config(provider: str | None = None) -> dict:
    """某 provider 的生效配置（含 api_key）。"""
    pid = provider or get_active_provider()
    p = load_providers().get(pid, {})
    return {
        "id": pid,
        "name": p.get("name", pid),
        "api_key": p.get("api_key", ""),
        "base_url": p.get("base_url", ""),
        "model": p.get("model", ""),
    }


def set_api_key(provider: str, key: str) -> None:
    providers = load_providers()
    if provider not in providers:
        provider = DEFAULT_PROVIDER
    providers[provider]["api_key"] = key.strip()
    save_config_file({"providers": providers})


def set_provider_params(provider: str, base_url: str = "", model: str = "") -> None:
    """自定义 provider 的 base_url / model（custom 用）。"""
    providers = load_providers()
    if provider not in providers:
        return
    if base_url.strip():
        providers[provider]["base_url"] = base_url.strip()
    if model.strip():
        providers[provider]["model"] = model.strip()
    save_config_file({"providers": providers})


# ---------- 视觉识别（爬虫照片 → 多模态模型；写开发信仍走 provider） ----------

VISION_PROVIDERS = {
    "glm": {
        "name": "智谱 GLM-4.6V",
        "models": {
            "glm-4.6v-flash": "GLM-4.6V-Flash（免费）",
            "glm-4.6v-flashx": "GLM-4.6V-FlashX（0.15元/M，稳定）",
            "glm-4.6v": "GLM-4.6V（付费增强）",
        },
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "needs_proxy": False,  # 国内直连
    },
    "qwen": {
        "name": "阿里云 Qwen",
        "models": {
            "qwen3-vl-plus": "Qwen3-VL-Plus（推荐，有免费额度）",
            "qwen-vl-max": "Qwen-VL-Max（旗舰）",
        },
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "needs_proxy": False,
    },
    "volc": {
        "name": "火山豆包",
        "models": {
            "doubao-seed-2-0-lite-260428": "豆包 Seed 2.0 Lite（全模态，替代 1.6 vision）",
            "doubao-seed-2-0-mini-260428": "豆包 Seed 2.0 Mini（轻量全模态）",
        },
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "needs_proxy": False,
    },
}

_LEGACY_VOLC_VISION_MODELS = {
    "doubao-1.5-vision-pro",
    "doubao-seed-1-6-vision",
    "doubao-seed-1-6-vision-250815",
    "doubao-1-5-vision-pro-32k-250115",
}


def vision_key_format_error(provider: str, api_key: str) -> str:
    """只拦截确定无效的凭证形态，不限制服务商未来可能变化的合法 Key。"""
    key = api_key.strip()
    if not key or provider != "volc":
        return ""
    if key.lower().startswith("bearer "):
        return "请只填写火山方舟 API Key，不要包含 Bearer 前缀"
    if key.lower().startswith("ep-"):
        return "这里需要火山方舟 API Key，不是推理接入点 ep-xxx"
    if key.upper().startswith("AKLT"):
        return "这里需要火山方舟专用 API Key，不是火山 Access Key ID（AK/SK）"
    if any(char.isspace() for char in key) or '"' in key or "'" in key:
        return "火山方舟 API Key 中不能包含空格、换行或引号，请从方舟 API Key 页面重新复制"
    return ""


def get_vision_config() -> dict:
    """返回当前视觉识别配置；旧版 OpenAI 配置会平滑回退到 GLM。"""
    raw = load_config_file()
    provider = raw.get("vision_provider", "glm")
    if provider not in VISION_PROVIDERS:
        provider = "glm"
    return _get_vision_provider_config(provider, raw)


def _get_vision_provider_config(provider: str, raw: dict | None = None) -> dict:
    """读取指定视觉服务商配置，供主服务商失败后的安全切换使用。"""
    raw = raw or load_config_file()
    if provider not in VISION_PROVIDERS:
        provider = "glm"
    models = VISION_PROVIDERS[provider]["models"]
    model = raw.get(f"{provider}_vision_model", "")
    if not model and raw.get("vision_provider") == provider:
        model = raw.get("vision_model", "")
    if provider == "volc" and model in _LEGACY_VOLC_VISION_MODELS:
        model = ""
    env_model = _get(f"{provider.upper()}_VISION_MODEL", "") or _get("VISION_MODEL", "")
    if provider == "volc":
        model = model or env_model or next(iter(models))
    elif model not in models:
        model = env_model if env_model in models else next(iter(models))
    key = raw.get(f"{provider}_api_key", "") or _get(f"{provider.upper()}_API_KEY", "")
    if vision_key_format_error(provider, key):
        key = ""
    return {
        "provider": provider,
        "api_key": key,
        "model": model,
        "base_url": VISION_PROVIDERS[provider]["base_url"],
        "needs_proxy": VISION_PROVIDERS[provider]["needs_proxy"],
    }


def get_vision_provider_config(provider: str) -> dict:
    """返回指定视觉服务商已保存配置，供设置页展示与无须重填切换。"""
    return _get_vision_provider_config(provider)


def get_vision_failover_configs(primary_provider: str = "") -> list[dict]:
    """按“当前主服务商优先，其余按 GLM→Qwen→火山”返回已配置项。"""
    raw = load_config_file()
    current = primary_provider if primary_provider in VISION_PROVIDERS else get_vision_config()["provider"]
    order = [current, *(pid for pid in VISION_PROVIDERS if pid != current)]
    return [
        cfg for cfg in (_get_vision_provider_config(pid, raw) for pid in order)
        if cfg["api_key"]
    ]


def set_vision_config(provider: str = "", api_key: str = "", model: str = "") -> None:
    """保存视觉识别配置。provider 缺省保持当前；api_key 空串不覆盖已有值。"""
    if provider not in VISION_PROVIDERS:
        provider = get_vision_config()["provider"]
    d: dict = {"vision_provider": provider}
    if api_key.strip():
        d[f"{provider}_api_key"] = api_key.strip()
    if model and (provider == "volc" or model in VISION_PROVIDERS[provider]["models"]):
        d["vision_model"] = model.strip()
        d[f"{provider}_vision_model"] = model.strip()
    save_config_file(d)


class Settings:
    # 本地行业库（产品/邮件资料）
    local_kb_path: str = _get("LOCAL_KB_PATH", str(ROOT_DIR / "data"))

    # 生成历史与质量评测日志（沿用旧环境变量名，保持兼容）
    memory_db_path: str = _get("MEMORY_DB_PATH", str(ROOT_DIR / "data" / "tradewind_memory.db"))

    # 爬虫（Google 搜索 + 官网挖邮箱）
    crawler_proxy: str = _get("CRAWLER_PROXY", "http://127.0.0.1:7897")


settings = Settings()
