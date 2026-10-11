// 模块级任务轮询单例：任务在后台持续跑，切换 Tab 不中断、不丢状态。
// 组件卸载只解除自己的监听，轮询保留到任务结束；切回时 resumeTask 立即恢复。
// 泛型：邮件任务用 TaskState，爬虫任务用 CrawlerState（各自 fetcher 决定）。

import { api } from "./api";
import type { TaskState } from "./types";
import { getBootstrapBackupState, getDataSafetyState } from "./dataEpoch";

type Listener<T> = (t: T) => void;
type TaskLike = { status: string };
type Fetcher<T extends TaskLike> = (id: string) => Promise<T>;

const subs = new Map<string, Set<(t: unknown) => void>>();
const timers = new Map<string, number>();
const lastByKind = new Map<string, string>();
const STORAGE_PREFIX = "tradewind.task.";
let referencesInitialized = false;

/** Fresh App boot only. Never clear mounted drafts or backend checkpoints. */
export function resetTaskReferencesForEpoch(epoch: string): void {
  if (referencesInitialized) return;
  referencesInitialized = true;
  timers.forEach(timer => window.clearInterval(timer));
  timers.clear(); subs.clear(); lastByKind.clear();
  try {
    const prior = window.localStorage.getItem(`${STORAGE_PREFIX}epoch`);
    const restored = getBootstrapBackupState()?.require_header ?? true;
    if ((prior !== null && prior !== epoch) || (prior === null && restored)) {
      window.localStorage.removeItem(`${STORAGE_PREFIX}compose`);
    }
    window.localStorage.setItem(`${STORAGE_PREFIX}epoch`, epoch);
  } catch {
    // Storage restrictions must not prevent viewing local data.
  }
}

function rememberTask(kind: string, taskId: string): void {
  lastByKind.set(kind, taskId);
  if (kind !== "compose") return;
  try {
    window.localStorage.setItem(`${STORAGE_PREFIX}${kind}`, taskId);
  } catch {
    // 隐私模式或受限浏览器可能禁用 storage；内存恢复仍可用。
  }
}

function storedTask(kind: string): string {
  if (kind !== "compose") return "";
  try {
    return window.localStorage.getItem(`${STORAGE_PREFIX}${kind}`) || "";
  } catch {
    return "";
  }
}

function finished(status: string): boolean {
  return status === "done" || status === "cancelled" || status === "error" || status === "not_found";
}

/** 监听一个任务。返回解除监听函数（不停止轮询）。 */
export function watchTask<T extends TaskLike = TaskState>(
  kind: string,
  taskId: string,
  listener: Listener<T>,
  fetcher: Fetcher<T> = api.task as unknown as Fetcher<T>,
): () => void {
  rememberTask(kind, taskId);
  if (!subs.has(taskId)) subs.set(taskId, new Set());
  subs.get(taskId)!.add(listener as (t: unknown) => void);

  if (!timers.has(taskId)) {
    let pending = false;
    const poll = async () => {
      if (pending || getDataSafetyState().mode === "stale") return;
      pending = true;
      try {
        const task = await fetcher(taskId);
        if (!subs.has(taskId) || getDataSafetyState().mode !== "ready") return;
        subs.get(taskId)!.forEach(callback => callback(task));
        if (finished(task.status)) {
          window.clearInterval(timers.get(taskId)!);
          timers.delete(taskId);
          if (task.status === "not_found") forgetTask(kind, taskId);
        }
      } catch {
        // Initial failure and maintenance failure both keep the retry timer.
      } finally {
        pending = false;
      }
    };
    timers.set(taskId, window.setInterval(() => { void poll(); }, 1000));
    void poll(); // Immediate recovery on remount, without overlapping requests.
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
  const id = lastByKind.get(kind) || storedTask(kind);
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
  if (current && (!taskId || current === taskId)) lastByKind.delete(kind);
  if (kind === "compose") {
    try {
      const stored = window.localStorage.getItem(`${STORAGE_PREFIX}${kind}`);
      if (!taskId || !stored || stored === taskId) {
        window.localStorage.removeItem(`${STORAGE_PREFIX}${kind}`);
      }
    } catch {
      // storage 不可用时无需阻断页面操作。
    }
  }
}
