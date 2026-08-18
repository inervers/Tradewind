"""Tradewind Tauri sidecar：只启动本地 API，不打开浏览器。"""

from __future__ import annotations

import multiprocessing

import uvicorn


def main() -> None:
    from server import app

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8101,
        log_level="warning",
        server_header=False,
    )


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
