// Tradewind API 封装
import type {
  ConfigState, CrawlerState, CrawlerTarget, Customer, EmailResult, ExtractTask, HistoryItem, Lang, OutFormat,
  PhotoLibraryStore, PhotoTaskState, Product, TaskState, Template,
} from "./types";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");

/** 浏览器版沿用同源 /api；桌面版指向本机 FastAPI 服务。 */
export function apiUrl(url: string): string {
  if (!url || /^(?:[a-z]+:)?\/\//i.test(url) || url.startsWith("data:") || url.startsWith("blob:")) {
    return url;
  }
  return `${API_BASE_URL}${url.startsWith("/") ? url : `/${url}`}`;
}

async function req<T>(url: string, opts?: RequestInit): Promise<T> {
  const resp = await fetch(apiUrl(url), opts);
  if (!resp.ok) {
    let detail = "";
    try {
      const j = await resp.json();
      detail = j.detail || j.error || "";
    } catch { /* ignore */ }
    throw new Error(detail || `请求失败（${resp.status}）`);
  }
  return resp.json() as Promise<T>;
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

export const api = {
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
    post<EmailResult>("/api/email", p, signal),

  startEmail: (p: { customer: string; country?: string; product?: string; extra?: string; judge?: boolean; language?: Lang; format?: OutFormat; template_id?: string }) =>
    post<{ task_id: string; error?: string }>("/api/email/start", p),
  cancelTask: (taskId: string) =>
    post<{ ok: boolean; error?: string }>(`/api/email/tasks/${taskId}/cancel`, {}),

  startBatch: (rows: { name: string; country?: string; notes?: string }[], product: string, judge: boolean, language?: Lang, format?: OutFormat, templateId = "") =>
    post<{ task_id: string; error?: string }>("/api/email/batch", { rows, product, judge, language, format, template_id: templateId }),
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
    remove: (id: string) => req<{ ok: boolean }>(`/api/customers/${id}`, { method: "DELETE" }),
    removeBatch: (ids: string[]) => post<{ ok: boolean; removed?: number; total?: number; error?: string }>("/api/customers/batch-delete", { ids }),
    importCsv: (text: string, source?: "manual" | "maps" | "webs") =>
      post<{ ok: boolean; added?: number; duplicates?: number; invalid?: number; total?: number; items?: Customer[]; error?: string }>("/api/customers/import", { text, source: source || "manual" }),
  },

  history: {
    list: (limit = 50) => req<HistoryItem[]>(`/api/history?limit=${limit}`),
    remove: (id: number) => req<{ ok: boolean }>(`/api/history/${id}`, { method: "DELETE" }),
    removeBatch: (ids: number[]) => post<{ ok: boolean; removed?: number; error?: string }>("/api/history/batch-delete", { ids }),
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
