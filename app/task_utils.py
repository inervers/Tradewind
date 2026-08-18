from __future__ import annotations

import time

TASK_TTL_SECONDS = 24 * 60 * 60


def prune_finished_tasks(store: dict[str, dict]) -> None:
    """删除已结束超过 24 小时的内存任务；运行中的任务永不清理。"""
    cutoff = time.time() - TASK_TTL_SECONDS
    expired = [
        task_id for task_id, task in list(store.items())
        if task.get("status") != "running"
        and task.get("finished_at", task.get("created_at", 0)) < cutoff
    ]
    for task_id in expired:
        store.pop(task_id, None)
