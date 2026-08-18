// ---------- 数据模型 ----------

export interface Product {
  id: string;
  title: string;
  content: string;
  source?: string;
  tags?: string[];
}

export interface Template {
  id: string;
  title: string;
  content: string;
  source?: string;
  tags?: string[];
}

export type CustomerStatus = "new" | "sent" | "replied";

export interface Customer {
  id: string;
  name: string;
  country: string;
  city: string;
  email: string;
  website: string;
  phone?: string;
  notes: string;
  source?: "manual" | "maps" | "webs";
  status: CustomerStatus;
  created_at?: number;
}

export type Lang = "zh-hant" | "en";
export type OutFormat = "email" | "whatsapp";

export interface Scores {
  personalization?: number;
  value_prop?: number;
  clarity?: number;
  cta?: number;
  overall?: number;
  suggestions?: string;
}

export interface EmailResult {
  customer: string;
  country: string;
  product: string;
  email: string;
  issues: string[];
  scores: Scores | null;
  tokens: number;
  time_s: number;
  language?: Lang;
  format?: OutFormat;
  templates_used?: string[];
}

export interface BatchItem {
  name: string;
  country: string;
  email: string;
  issues: string[];
  scores: Scores | null;
  ok: boolean;
  error?: string;
  templates_used?: string[];
}

export interface TaskState {
  status: "running" | "done" | "cancelled" | "error" | "not_found" | string;
  total: number;
  done: number;
  current: string;
  results: BatchItem[];
  result?: EmailResult | null;
  error?: string;
  stream?: string;
}

export interface HistoryItem {
  id: number;
  customer: string;
  country: string;
  product: string;
  email: string;
  score: number;
  language?: Lang;
  format?: OutFormat;
  created_at: number;
}

// ---------- 爬虫 ----------

export type CrawlerTarget = "all" | "email" | "phone" | "whatsapp";

export interface CrawlerResult {
  name: string;
  country: string;
  city: string;
  website: string;
  email: string;
  phone: string;
  wa_link: string;
  whatsapp?: string;
  facebook?: string;
  instagram?: string;
  instrument: string;
  instruments?: string;
  overview?: string;
  photo_analysis?: string;
  gap_recs?: string;
}

export interface CrawlerState {
  status: "running" | "done" | "cancelled" | "error" | "not_found" | string;
  log: string[];
  results: CrawlerResult[];
  error?: string;
}

// ---------- 照片库 / 照片筛选 ----------

export interface PhotoDevice {
  device: string;
  brand?: string;
  purpose?: string;
  confidence: number;
}

export interface PhotoScanResult {
  filename: string;
  saved_path: string;
  preview_url?: string;
  has_device: boolean;
  confidence: number;
  devices: PhotoDevice[];
  error?: string | null;
  provider?: string;
  model?: string;
}

export interface PhotoTaskState {
  status: "running" | "done" | "cancelled" | "error" | "not_found" | string;
  total: number;
  done: number;
  results: PhotoScanResult[];
  error?: string;
}

export interface PhotoLibraryPhoto {
  photo_id: string;
  filename: string;
  size: number;
  modified_at: number;
  url: string;
}

export interface PhotoLibraryStore {
  store_id: string;
  name: string;
  count: number;
  photos: PhotoLibraryPhoto[];
}

export interface ExtractItem {
  title: string;
  content: string;
  tags?: string[];
}

/** 文档识别任务状态（products/templates 共用，前端轮询 GET /api/extract/tasks/{id}） */
export interface ExtractTask {
  status: "running" | "done" | "cancelled" | "error" | "not_found" | string;
  error?: string;
  items?: ExtractItem[];
  mode?: "summary" | "raw";
  ocr?: boolean;
  filename?: string;
}

export interface ProviderInfo {
  id: string;
  name: string;
  base_url: string;
  model: string;
  has_key: boolean;
}

export interface ConfigState {
  has_key: boolean;
  model?: string;
  active_provider?: string;
  providers?: ProviderInfo[];
  company_profile?: {
    sender_name: string;
    company_name: string;
    email: string;
    whatsapp: string;
    website: string;
  };
  vision?: {
    configured?: boolean;
    provider?: string;
    model?: string;
    effective_provider?: string;
    effective_model?: string;
    providers?: Record<string, {
      name?: string;
      models?: Record<string, string>;
      has_key?: boolean;
      model?: string;
    }>;
  };
  data_dir?: string;
  data_files?: {
    customers?: string;
    products?: string;
    templates?: string;
    memory?: string;
    crawler_csv?: string;
  };
}
