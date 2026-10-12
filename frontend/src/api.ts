// Tradewind API 封装
import type {
  ConfigState, CrawlerState, CrawlerTarget, Customer, EmailOutreachSettings, EmailResult,
  ExtractTask, HistoryItem, Lang, OutFormat, OutreachChannel, OutreachJob,
  OutreachStatus, OutreachSuppression, PhotoLibraryStore, PhotoTaskState, Product,
  TaskState, Template, BackupState, OperationView,
  FollowupChannel, FollowupOutcome, FollowupRecord, FollowupPlan, FollowupSummary, FollowupTimelinePage,
} from "./types";
import { getDataSafetyState, getPageEpoch, initializeDataEpoch, markDataMaintenance, markDataStale, refreshDataSafety } from "./dataEpoch";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");

/** 浏览器版沿用同源 /api；桌面版指向本机 FastAPI 服务。 */
export function apiUrl(url: string): string {
  if (!url || /^(?:[a-z]+:)?\/\//i.test(url) || url.startsWith("data:") || url.startsWith("blob:")) {
    return url;
  }
  return `${API_BASE_URL}${url.startsWith("/") ? url : `/${url}`}`;
}

async function req<T>(url: string, opts?: RequestInit): Promise<T> {
  await initializeDataEpoch();
  const control = url.startsWith("/api/backup/");
  const mutation = !["GET", "HEAD"].includes((opts?.method || "GET").toUpperCase());
  if (!control) await refreshDataSafety();
  const safety = getDataSafetyState();
  if (safety.mode === "stale" || (!control && safety.mode !== "ready")) throw new Error(safety.message);
  const mergedHeaders = new Headers(opts?.headers);
  if (mutation) mergedHeaders.set("X-Tradewind-Data-Epoch", getPageEpoch()!);
  const resp = await fetch(apiUrl(url), { ...opts, headers: mergedHeaders });
  if (!resp.ok) {
    let detail = "";
    try {
      const j = await resp.json();
      detail = j.detail || j.error || "";
      if (resp.status === 409 && j.code === "stale_epoch") markDataStale();
      if (resp.status === 503 && j.code === "maintenance_busy") markDataMaintenance();
    } catch { /* ignore */ }
    throw new Error(control ? "备份操作未确认成功，请刷新状态或重试。" : detail || `请求失败（${resp.status}）`);
  }
  const result = await resp.json() as T;
  // In-flight task/list reads can finish after restart. Never deliver not_found
  // or new-root data to a still-mounted old form before reconciling safety.
  if (!control) {
    await refreshDataSafety(true);
    const after = getDataSafetyState();
    if (after.mode !== "ready") throw new Error(after.message);
  }
  return result;
}

async function downloadResponse(url: string): Promise<string> {
  const resp = await fetch(apiUrl(url));
  if (!resp.ok) {
    let detail = "";
    try {
      const payload = await resp.json();
      detail = payload.detail || payload.error || "";
    } catch { /* ignore */ }
    throw new Error(detail || `下载失败（${resp.status}）`);
  }
  const disposition = resp.headers.get("Content-Disposition") || "";
  const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || "Tradewind-diagnostics.zip";
  const blob = await resp.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  return filename;
}

const post = <T>(url: string, body: unknown, signal?: AbortSignal): Promise<T> =>
  req<T>(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });

async function postEmail<T>(url: string, body: unknown, signal?: AbortSignal): Promise<T> {
  // Read current local configuration only on an explicit generation action.
  // A settings save must work without remounting App, and no-key browsing must
  // not start a model-backed task merely to discover that configuration is absent.
  const config = await req<ConfigState>("/api/config");
  if (!config.has_key) throw new Error("请先在设置中配置 API Key，再生成开发信");
  return post<T>(url, body, signal);
}

/** Native navigation keeps large ZIPs out of renderer memory. */
export function backupDownloadUrl(id: string, rescue: boolean): string {
  return apiUrl(`/api/backup/${rescue ? "rescues" : "operations"}/${encodeURIComponent(id)}/download`);
}

/** Raw File body, never FormData; progress represents actual uploaded bytes. */
async function uploadBackupFile(file: File, signal: AbortSignal, progress?: (loaded: number, total: number) => void): Promise<OperationView> {
  await initializeDataEpoch();
  if (signal.aborted) throw new DOMException("上传已取消", "AbortError");
  if (getDataSafetyState().mode === "stale") throw new Error("页面数据版本已变化，请先复制草稿再重新打开。");
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const cleanup = () => signal.removeEventListener("abort", abort);
    const fail = () => { cleanup(); reject(new Error("备份上传未确认成功，请刷新状态或重新选择文件。")); };
    const abort = () => xhr.abort();
    xhr.open("POST", apiUrl("/api/backup/preview"));
    xhr.setRequestHeader("Content-Type", "application/zip");
    xhr.setRequestHeader("X-Tradewind-Data-Epoch", getPageEpoch()!);
    xhr.upload.onprogress = event => {
      if (event.lengthComputable && event.total === file.size) progress?.(Math.max(0, Math.min(event.loaded, file.size)), file.size);
    };
    xhr.onerror = fail;
    xhr.onabort = () => { cleanup(); reject(new DOMException("上传已取消", "AbortError")); };
    xhr.onload = () => {
      cleanup();
      let payload;
      try { payload = JSON.parse(xhr.responseText); } catch { fail(); return; }
      if (xhr.status < 200 || xhr.status >= 300) {
        if (payload.code === "stale_epoch") markDataStale();
        if (payload.code === "maintenance_busy") markDataMaintenance();
        fail(); return;
      }
      resolve(payload as OperationView);
    };
    signal.addEventListener("abort", abort, { once: true });
    xhr.send(file);
  });
}

export const api = {
  backup: {
    state: (): Promise<BackupState> => refreshDataSafety(),
    getState: (): Promise<BackupState> => refreshDataSafety(),
    startExport: (includesPhotos: boolean) => post<OperationView>("/api/backup/export", { includes_photos: includesPhotos }),
    getOperation: (id: string) => req<OperationView>(`/api/backup/operations/${encodeURIComponent(id)}`, { cache: "no-store" }),
    cancelOperation: (id: string) => post<{ ok: true }>(`/api/backup/operations/${encodeURIComponent(id)}/cancel`, {}),
    startPreview: uploadBackupFile,
    prepareRestore: (previewId: string, epoch: string, requestId: string) => post<OperationView>("/api/backup/restore/prepare", { preview_id: previewId, epoch, request_id: requestId }),
    cancelRestore: (operationId: string) => post<{ ok: true }>("/api/backup/restore/cancel", { operation_id: operationId }),
    deleteRescue: (id: string) => req<{ ok: true }>(`/api/backup/rescues/${encodeURIComponent(id)}`, { method: "DELETE" }),
  },
  getConfig: () => req<ConfigState>("/api/config"),
  saveKey: (provider: string, apiKey: string) =>
    post<{ ok: boolean; error?: string; verified?: boolean | null }>("/api/config", { provider, api_key: apiKey }),
  activateProvider: (provider: string) =>
    post<{ ok: boolean; active_provider?: string; error?: string }>("/api/config/activate", { provider }),
  saveProviderParams: (provider: string, baseUrl: string, model: string) =>
    post<{ ok: boolean; error?: string }>("/api/config/params", { provider, base_url: baseUrl, model }),
  saveCompanyProfile: (profile: { sender_name: string; company_name: string; email: string; whatsapp: string; website: string }) =>
    post<{ ok: boolean; error?: string; company_profile?: ConfigState["company_profile"] }>("/api/config/company", profile),
  providerModels: (provider: string) =>
    req<{ ok: boolean; models?: string[]; error?: string }>(`/api/config/${provider}/models`),
  saveVision: (apiKey: string, model: string, provider: string) =>
    post<{ ok: boolean; configured?: boolean; provider?: string; model?: string; error?: string }>("/api/config/vision", {
      api_key: apiKey,
      model,
      provider,
    }),
  exportDiagnostics: () => downloadResponse("/api/diagnostics/export"),

  email: (p: { customer: string; country?: string; product?: string; extra?: string; judge?: boolean; language?: Lang; format?: OutFormat }, signal?: AbortSignal) =>
    postEmail<EmailResult>("/api/email", p, signal),

  startEmail: (p: { customer_id?: string; customer: string; country?: string; product?: string; extra?: string; judge?: boolean; language?: Lang; format?: OutFormat; template_id?: string }) =>
    postEmail<{ task_id: string; error?: string }>("/api/email/start", p),
  cancelTask: (taskId: string) =>
    post<{ ok: boolean; error?: string }>(`/api/email/tasks/${taskId}/cancel`, {}),

  startBatch: (rows: { name: string; country?: string; notes?: string }[], product: string, judge: boolean, language?: Lang, format?: OutFormat, templateId = "") =>
    postEmail<{ task_id: string; error?: string }>("/api/email/batch", { rows, product, judge, language, format, template_id: templateId }),
  task: (taskId: string) => req<TaskState>(`/api/email/tasks/${taskId}`),

  products: {
    list: () => req<Product[]>("/api/products"),
    add: (p: { title: string; content: string; tags?: string[] }) => post<{ ok: boolean; item: Product }>("/api/products", p),
    update: (id: string, p: { title: string; content: string; tags?: string[] }) =>
      req<{ ok: boolean; item?: Product }>(`/api/products/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(p) }),
    remove: (id: string) => req<{ ok: boolean }>(`/api/products/${id}`, { method: "DELETE" }),
  },

  templates: {
    list: () => req<Template[]>("/api/templates"),
    add: (p: { title: string; content: string; tags?: string[] }) => post<{ ok: boolean; item?: Template; duplicate?: boolean; error?: string }>("/api/templates", p),
    update: (id: string, p: { title: string; content: string; tags?: string[] }) =>
      req<{ ok: boolean; item?: Template; error?: string }>(`/api/templates/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(p),
      }),
    remove: (id: string) => req<{ ok: boolean }>(`/api/templates/${id}`, { method: "DELETE" }),
  },

  customers: {
    list: () => req<Customer[]>("/api/customers", { cache: "no-store" }),
    add: (c: Partial<Customer>) => post<{ ok: boolean; item?: Customer; duplicate?: boolean; error?: string }>("/api/customers", c),
    update: (id: string, c: Partial<Customer>) =>
      req<{ ok: boolean; item?: Customer }>(`/api/customers/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(c) }),
    remove: (id: string) => req<{ ok: boolean; customer_deleted?: boolean; error?: string }>(`/api/customers/${id}`, { method: "DELETE" }),
    removeBatch: (ids: string[]) => post<{ ok: boolean; customer_deleted?: boolean; removed?: number; total?: number; error?: string }>("/api/customers/batch-delete", { ids }),
    importCsv: (text: string, source?: "manual" | "maps" | "webs") =>
      post<{ ok: boolean; added?: number; duplicates?: number; invalid?: number; total?: number; items?: Customer[]; error?: string }>("/api/customers/import", { text, source: source || "manual" }),
  },

  followups: {
    summary: () => req<{ today: string; items: FollowupSummary[] }>("/api/customers/followup-summary", { cache: "no-store" }),
    get: (id: string) => req<{ plan: FollowupPlan | null }>(`/api/customers/${encodeURIComponent(id)}/followup`, { cache: "no-store" }),
    timeline: (id: string, offset = 0) => req<FollowupTimelinePage>(`/api/customers/${encodeURIComponent(id)}/followup/timeline?limit=30&offset=${offset}`, { cache: "no-store" }),
    addRecord: (id: string, payload: { request_id: string; contacted_at: string; channel: FollowupChannel; outcome: FollowupOutcome; content: string }) =>
      post<{ ok: true; item: FollowupRecord }>(`/api/customers/${encodeURIComponent(id)}/followup/records`, payload),
    deleteRecord: (id: string, recordId: number) => req<{ ok: true }>(`/api/customers/${encodeURIComponent(id)}/followup/records/${recordId}`, { method: "DELETE" }),
    setPlan: (id: string, payload: { request_id: string; due_date: string; action: string; expected_plan_id: number | null }) =>
      post<{ ok: true; item: FollowupPlan }>(`/api/customers/${encodeURIComponent(id)}/followup/plans`, payload),
    completePlan: (id: string, planId: number) => post<{ ok: true; item: FollowupPlan }>(`/api/customers/${encodeURIComponent(id)}/followup/plans/${planId}/complete`, {}),
    cancelPlan: (id: string, planId: number) => post<{ ok: true; item: FollowupPlan }>(`/api/customers/${encodeURIComponent(id)}/followup/plans/${planId}/cancel`, {}),
  },

  history: {
    list: (limit = 50) => req<HistoryItem[]>(`/api/history?limit=${limit}`),
    remove: (id: number) => req<{ ok: boolean }>(`/api/history/${id}`, { method: "DELETE" }),
    removeBatch: (ids: number[]) => post<{ ok: boolean; removed?: number; error?: string }>("/api/history/batch-delete", { ids }),
  },

  outreach: {
    list: (channel?: OutreachChannel, statuses?: OutreachStatus[]) => {
      const params = new URLSearchParams();
      if (channel) params.set("channel", channel);
      if (statuses?.length) params.set("status", statuses.join(","));
      const query = params.toString();
      return req<OutreachJob[]>(`/api/outreach/jobs${query ? `?${query}` : ""}`, { cache: "no-store" });
    },
    create: (payload: { customer_id: string; channel: OutreachChannel; content: string; language: Lang; workflow_id?: string }) =>
      post<{ ok: boolean; item?: OutreachJob; duplicate?: boolean; error?: string }>("/api/outreach/jobs", payload),
    update: (id: number, payload: { subject: string; body: string }) =>
      req<{ ok: boolean; item?: OutreachJob; error?: string }>(`/api/outreach/jobs/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    openWhatsApp: (id: number) =>
      post<{ ok: boolean; item?: OutreachJob; url?: string; error?: string }>(`/api/outreach/jobs/${id}/open-whatsapp`, {}),
    sendEmail: (id: number) =>
      post<{ ok: boolean; item?: OutreachJob; error?: string; issues?: string[] }>(`/api/outreach/jobs/${id}/send-email`, {}),
    rebindSender: (id: number) =>
      post<{ ok: boolean; item?: OutreachJob; error?: string }>(`/api/outreach/jobs/${id}/rebind-sender`, {}),
    transition: (id: number, status: OutreachStatus, reason = "") =>
      post<{ ok: boolean; item?: OutreachJob; error?: string }>(`/api/outreach/jobs/${id}/transition`, { status, reason }),
    listSuppressions: () => req<OutreachSuppression[]>("/api/outreach/suppressions", { cache: "no-store" }),
    suppress: (payload: { channel: OutreachChannel; recipient: string; reason: string }) =>
      post<{ ok: boolean; item?: OutreachSuppression; error?: string }>("/api/outreach/suppressions", payload),
    unsuppress: (id: number) => req<{ ok: boolean; error?: string }>(`/api/outreach/suppressions/${id}`, { method: "DELETE" }),
    getEmailSettings: () => req<EmailOutreachSettings>("/api/outreach/email-settings", { cache: "no-store" }),
    saveEmailSettings: (payload: { provider: EmailOutreachSettings["provider"]; address: string; transport: EmailOutreachSettings["transport"]; credential?: string }) =>
      req<{ ok: boolean; error?: string } & EmailOutreachSettings>("/api/outreach/email-settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    deleteEmailCredential: () => req<{ ok: boolean; error?: string } & EmailOutreachSettings>("/api/outreach/email-settings/credential", { method: "DELETE" }),
    testEmailSettings: () => post<{ ok: boolean; status?: string; code?: string; summary?: string; error?: string }>("/api/outreach/email-settings/test", {}),
  },

  crawler: {
    start: (p: { queries: string; country: string; targets: CrawlerTarget[]; max: number; source?: string; engine?: string; region?: string; allowRecrawl?: boolean; savePhotos?: boolean }) =>
      post<{ task_id: string; error?: string }>("/api/crawler/start", {
        queries: p.queries, country: p.country, targets: p.targets, max_customers: p.max,
        source: p.source || "maps", engine: p.engine || "auto", region: p.region || "hk",
        allow_recrawl: p.allowRecrawl || false,
        save_photos: p.savePhotos ?? true,
      }),
    task: (taskId: string) => req<CrawlerState>(`/api/crawler/tasks/${taskId}`),
    cancel: (taskId: string) => post<{ ok: boolean; error?: string }>(`/api/crawler/tasks/${taskId}/cancel`, {}),
  },

  photos: {
    library: () => req<{ stores: PhotoLibraryStore[]; total: number }>("/api/photos/library"),
    remove: (items: { store_id: string; photo_id: string }[]) =>
      post<{ ok: boolean; removed: number }>("/api/photos/library/delete", { items }),
    renameStore: (storeId: string, name: string) =>
      post<{ ok: boolean; store_id?: string; name?: string; error?: string }>("/api/photos/library/store/rename", { store_id: storeId, name }),
    removeStore: (storeId: string) =>
      post<{ ok: boolean; removed: number; error?: string; warning?: string }>("/api/photos/library/store/delete", { store_id: storeId }),
    start: (images: { filename: string; data_base64: string }[]) =>
      post<{ task_id: string; error?: string }>("/api/photos/start", { images }),
    task: (taskId: string) => req<PhotoTaskState>(`/api/photos/tasks/${taskId}`),
    cancel: (taskId: string) => post<{ ok: boolean; error?: string }>(`/api/photos/tasks/${taskId}/cancel`, {}),
  },

  // ---------- 文档识别导入（任务制：start → 轮询 task → cancel） ----------
  extractStart: (file: File, kind: "products" | "templates") => {
    const fd = new FormData();
    fd.append("file", file);
    return req<{ ok: boolean; task_id?: string; error?: string }>(`/api/${kind}/extract`, {
      method: "POST",
      body: fd,
    });
  },
  extractTask: (taskId: string) => req<ExtractTask>(`/api/extract/tasks/${taskId}`),
  extractCancel: (taskId: string) => post<{ ok: boolean; error?: string }>(`/api/extract/tasks/${taskId}/cancel`, {}),
};

// ---------- 工具 ----------

export function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  }
}

export function downloadText(filename: string, text: string): void {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
