"""Tradewind 浏览器版启动器。"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8101
BROWSER_URL = f"http://{HOST}:{PORT}/"
HEALTH_URL = f"http://{HOST}:{PORT}/api/health"
MUTEX_NAME = "Local\\TradewindBrowserPortable"
STARTUP_TIMEOUT = 120.0
_MUTEX_HANDLE: int | None = None
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def executable_dir() -> Path:
    """返回源码根目录或打包后的 exe 目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_data_root() -> Path:
    """默认使用 AppData；放置 portable.flag 后改为随程序目录存储。"""
    configured = os.getenv("TRADEWIND_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    program_dir = executable_dir()
    if (program_dir / "portable.flag").is_file():
        return program_dir

    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data).resolve() / "Tradewind"
    return program_dir


def is_tradewind_ready(timeout: float = 0.5) -> bool:
    """只接受 Tradewind 健康响应，并强制绕过系统/环境代理。"""
    try:
        with _LOCAL_OPENER.open(HEALTH_URL, timeout=timeout) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            return payload.get("status") == "ok" and payload.get("service") == "tradewind"
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def port_is_in_use() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.3):
            return True
    except OSError:
        return False


def wait_until_ready(timeout: float = STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_tradewind_ready():
            return True
        time.sleep(0.25)
    return False


def open_browser() -> None:
    """打开页面；自动验收可通过环境变量抑制浏览器窗口。"""
    if os.getenv("TRADEWIND_NO_BROWSER", "").strip() != "1":
        webbrowser.open(BROWSER_URL)


def open_browser_when_ready() -> None:
    if wait_until_ready():
        open_browser()
    elif port_is_in_use():
        print("[Tradewind] 本地端口已启动，但健康检查未返回；正在尝试直接打开页面。")
        print(f"[Tradewind] 如浏览器未打开，请手动访问 {BROWSER_URL}")
        open_browser()
    else:
        print(f"[Tradewind] 后端在 {int(STARTUP_TIMEOUT)} 秒内未监听端口 {PORT}。")
        print("[Tradewind] 请确认已完整解压 _internal 文件夹，并检查杀毒软件的拦截记录。")


def acquire_instance_mutex() -> bool:
    """Windows 打包版单实例锁；返回 True 表示已有启动器正在运行。"""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return False
    _MUTEX_HANDLE = int(handle)
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def configure_runtime_data() -> Path:
    data_root = resolve_data_root()
    (data_root / "data").mkdir(parents=True, exist_ok=True)
    (data_root / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["TRADEWIND_DATA_DIR"] = str(data_root)
    os.environ["TRADEWIND_BROWSER_PORTABLE"] = "1"
    return data_root


def main() -> int:
    if is_tradewind_ready():
        open_browser()
        return 0

    already_running = acquire_instance_mutex()
    if already_running:
        if wait_until_ready():
            open_browser()
            return 0
        print("[Tradewind] 已有实例正在启动，但等待服务就绪超时。")
        return 1

    if port_is_in_use():
        print(f"[Tradewind] 端口 {PORT} 已被其他程序占用，无法启动。")
        print("请关闭占用该端口的程序后再试。")
        input("按 Enter 键退出...")
        return 1

    data_root = configure_runtime_data()
    print("=" * 58)
    print(" Tradewind 浏览器版正在启动")
    print(f" 页面地址：{BROWSER_URL}")
    print(f" 数据目录：{data_root / 'data'}")
    print(" 本窗口关闭后服务会停止；再次双击可重新打开页面。")
    print("=" * 58)

    # 必须在 TRADEWIND_DATA_DIR 设置后导入，确保业务模块使用用户数据目录。
    import uvicorn
    from server import app

    threading.Thread(target=open_browser_when_ready, daemon=True).start()
    try:
        uvicorn.run(
            app,
            host=HOST,
            port=PORT,
            log_level="warning",
            server_header=False,
        )
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 - 双击启动时必须把错误留在窗口中
        print(f"[Tradewind] 启动失败：{exc}")
        input("按 Enter 键退出...")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
