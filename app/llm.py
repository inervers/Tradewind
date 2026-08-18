"""LLM 封装（OpenAI 兼容接口）。按 active provider 动态读 key/base_url/model。"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from app.config import get_provider_config


def build_llm(temperature: float = 0.3):
    """构建 ChatOpenAI 客户端（当前激活的 provider）。配置页切换后即时生效。"""
    cfg = get_provider_config()
    if not cfg["api_key"]:
        raise ValueError("未配置 API Key，请先在设置页选择服务商并填写")
    return ChatOpenAI(
        model=cfg["model"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        temperature=temperature,
    )
