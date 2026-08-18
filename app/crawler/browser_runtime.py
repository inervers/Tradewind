"""Playwright 浏览器启动策略：源码用已安装 Chromium，桌面包优先系统 Edge。"""

from __future__ import annotations

import sys


def launch_chromium(browser_type, **kwargs):
    """启动 Chromium；打包后不携带大型浏览器，复用 Windows 自带 Edge。"""
    if not getattr(sys, "frozen", False):
        return browser_type.launch(**kwargs)

    errors: list[str] = []
    for channel in ("msedge", "chrome"):
        try:
            return browser_type.launch(channel=channel, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 继续尝试下一种系统浏览器
            errors.append(f"{channel}: {exc}")
    raise RuntimeError("未找到可用的 Microsoft Edge 或 Google Chrome：" + "；".join(errors))
