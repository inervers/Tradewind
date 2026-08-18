// 模块级任务轮询单例：任务在后台持续跑，切换 Tab 不中断、不丢状态。
// 组件卸载只解除自己的监听，轮询保留到任务结束；切回时 resumeTask 立即恢复。
// 泛型：邮件任务用 TaskState，爬虫任务用 CrawlerState（各自 fetcher 决定）。

import { api } from "./api";
import type { TaskState } from "./types";

type Listener<T> = (t: T) => void;
type TaskLike = { status: string };
type Fetcher<T extends TaskLike> = (id: string) => Promise<T>;

const subs = new Map<string, Set<(t: unknown) => void>>();
const timers = new Map<string, number>();
const lastByKind = new Map<string, string>();

/** 监听一个任务。返回解除监听函数（不停止轮询）。 */
export function watchTask<T extends TaskLike = TaskState>(
  kind: string,
  taskId: string,
  listener: Listener<T>,
  fetcher: Fetcher<T> = api.task as unknown as Fetcher<T>,
): () => void {
  lastByKind.set(kind, taskId);
  if (!subs.has(taskId)) subs.set(taskId, new Set());
  subs.get(taskId)!.add(listener as (t: unknown) => void);

  if (!timers.has(taskId)) {
    // 立即拉一次最新状态（切回页面时马上恢复显示）
    fetcher(taskId)
      .then((t) => {
        if (!subs.has(taskId)) return;
        subs.get(taskId)!.forEach((l) => l(t as unknown));
        const finished = t.status === "done" || t.status === "cancelled" || t.status === "error";
        if (finished) return; // 已结束不再建轮询
        if (timers.has(taskId)) return;
        timers.set(
          taskId,
          window.setInterval(async () => {
            let t2: T;
            try {
              t2 = await fetcher(taskId);
            } catch {
              return; // 轮询失败继续，下次再试
            }
            if (!subs.has(taskId)) return;
            subs.get(taskId)!.forEach((l) => l(t2 as unknown));
            if (t2.status === "done" || t2.status === "cancelled" || t2.status === "error") {
              window.clearInterval(timers.get(taskId)!);
              timers.delete(taskId);
            }
          }, 1000),
        );
      })
      .catch(() => {
        /* 立即拉取失败（网络抖动），等轮询兜底 */
      });
  }
  return () => {
    subs.get(taskId)?.delete(listener as (t: unknown) => void);
    // 监听者清空也不停轮询：任务仍在后台生成，切回时恢复订阅即可
  };
}

/** 组件挂载时恢复上次任务（生成中切走再切回，不丢状态）。 */
export function resumeTask<T extends TaskLike = TaskState>(
  kind: string,
  listener: Listener<T>,
  fetcher: Fetcher<T> = api.task as unknown as Fetcher<T>,
): (() => void) | null {
  const id = lastByKind.get(kind);
  if (!id) return null;
  return watchTask(kind, id, listener, fetcher);
}

/** 读取当前任务 ID，供页面重挂载后恢复停止/清空等操作。 */
export function currentTaskId(kind: string): string {
  return lastByKind.get(kind) || "";
}

/** 主动清空某类任务的当前工作区引用；后端历史记录不受影响。 */
export function forgetTask(kind: string, taskId?: string): void {
  const current = lastByKind.get(kind);
  if (!current || (taskId && current !== taskId)) return;
  lastByKind.delete(kind);
}
