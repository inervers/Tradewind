// This module deliberately imports neither api nor tasks: bootstrap must not
// wait on its own request wrapper, and task cleanup belongs to fresh App boot.
import type { BackupState, DataSafetyState } from "./types";

const stateUrl = `${(import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "")}/api/backup/state`;
let pageEpoch: string | null = null;
let bootstrap: BackupState | null = null;
let initialization: Promise<void> | null = null;
let refresh: Promise<BackupState> | null = null;
let safety: DataSafetyState = { mode: "maintenance", message: "正在确认本机资料状态…" };
const listeners = new Set<(state: DataSafetyState) => void>();
let timer: number | null = null;

export function getPageEpoch(): string | null { return pageEpoch; }
export function getDataSafetyState(): DataSafetyState { return safety; }
export function getBootstrapBackupState(): BackupState | null { return bootstrap; }

function publish(next: DataSafetyState): void {
  if (safety.mode === next.mode && safety.message === next.message) return;
  safety = next;
  listeners.forEach(listener => listener(safety));
}

export function markDataStale(): void {
  publish({ mode: "stale", message: "本机资料已恢复，此页面不能再保存或发送。请先复制需要的草稿，再重新打开页面。" });
}

export function markDataMaintenance(): void {
  if (safety.mode !== "stale" && safety.mode !== "prepared" && safety.mode !== "blocked") {
    publish({ mode: "maintenance", message: "资料正在维护，暂不能操作。已有内容和草稿仍保留，请稍后重试。" });
  }
}

async function readState(): Promise<BackupState> {
  const response = await fetch(stateUrl, { cache: "no-store" });
  if (!response.ok) throw new Error("无法确认本机资料状态，请稍后重新打开页面。");
  const state = await response.json() as BackupState;
  if (!state || typeof state.epoch !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(state.epoch)
    || typeof state.require_header !== "boolean" || !["ready", "snapshot", "prepare", "prepared", "blocked"].includes(state.mode)) {
    throw new Error("无法确认本机资料状态，请稍后重新打开页面。");
  }
  return state;
}

function observe(state: BackupState): void {
  if (safety.mode === "stale") return; // Irreversible within this page lifetime.
  if (state.epoch !== pageEpoch) { markDataStale(); return; }
  if (state.mode === "prepared") {
    publish({ mode: "prepared", message: "恢复已准备完成，重启程序后才会生效。已有草稿仍可复制；重启前不能保存或发送。" });
  } else if (state.mode === "blocked") {
    publish({ mode: "blocked", message: "资料恢复暂未完成，已停止业务操作。请保留现有草稿，并检查备份与恢复状态。" });
  } else if (state.mode === "ready") {
    publish({ mode: "ready", message: "" });
  } else {
    markDataMaintenance();
    // A prepared hold can be cancelled and followed by another maintenance job.
    if (safety.mode === "prepared" || safety.mode === "blocked") {
      publish({ mode: "maintenance", message: "资料正在维护，暂不能操作。已有内容和草稿仍保留，请稍后重试。" });
    }
  }
}

/** Freeze once per page; GET backup state never assigns a later epoch. */
export function initializeDataEpoch(): Promise<void> {
  if (!initialization) {
    initialization = readState().then(state => {
      pageEpoch = state.epoch;
      bootstrap = state;
      observe(state);
    }).catch(error => {
      publish({ mode: "blocked", message: "无法确认本机资料状态，暂不能操作。请稍后重新打开页面。" });
      throw error;
    });
  }
  return initialization;
}

/** Cheap state reads coalesce; callers keep existing data if the read fails. */
export async function refreshDataSafety(afterResponse = false): Promise<BackupState> {
  await initializeDataEpoch();
  // A coalesced read started before a business response may describe the old
  // root. Drain it first, then use a read initiated after this response boundary.
  if (afterResponse && refresh) await refresh;
  if (!refresh) {
    refresh = readState().then(state => { observe(state); return state; }).catch(error => {
      markDataMaintenance();
      throw error;
    }).finally(() => { refresh = null; });
  }
  return refresh;
}

export function subscribeDataSafety(listener: (state: DataSafetyState) => void): () => void {
  listeners.add(listener);
  listener(safety);
  if (timer === null) {
    const poll = () => { void refreshDataSafety().catch(() => {}); };
    timer = window.setInterval(poll, 2000);
    window.addEventListener("focus", poll);
    const visibility = () => { if (!document.hidden) poll(); };
    document.addEventListener("visibilitychange", visibility);
    stopPolling = () => {
      if (timer !== null) window.clearInterval(timer);
      timer = null;
      window.removeEventListener("focus", poll);
      document.removeEventListener("visibilitychange", visibility);
    };
  }
  return () => { listeners.delete(listener); if (!listeners.size) stopPolling?.(); };
}
let stopPolling: (() => void) | null = null;
