import { useCallback, useEffect, useRef, useState, type ReactNode, type SyntheticEvent } from "react";
import { AuroraBackground, FadeIn, useToast } from "./components/effects";
import {
  BatchIcon, BoxIcon, ClockIcon, GearIcon, InboxIcon, MailIcon, PhotoIcon, RadarIcon, RouteIcon, SailboatIcon, UsersIcon,
} from "./components/Icons";
import { api } from "./api";
import ConfirmDialog from "./components/ConfirmDialog";
import ComposeTab from "./tabs/ComposeTab";
import BatchTab from "./tabs/BatchTab";
import CustomersTab from "./tabs/CustomersTab";
import CrawlerTab from "./tabs/CrawlerTab";
import PhotosTab from "./tabs/PhotosTab";
import ProductsTab from "./tabs/ProductsTab";
import HistoryTab from "./tabs/HistoryTab";
import TemplatesTab from "./tabs/TemplatesTab";
import SettingsTab from "./tabs/SettingsTab";
import OutreachTab from "./tabs/OutreachTab";
import type { ComposeSeed, DataSafetyState } from "./types";
import DataMaintenanceNotice from "./components/DataMaintenanceNotice";
import BackupSettingsCard from "./components/BackupSettingsCard";
import { getDataSafetyState, getPageEpoch, initializeDataEpoch, subscribeDataSafety } from "./dataEpoch";
import { resetTaskReferencesForEpoch } from "./tasks";

type TabKey = "compose" | "batch" | "customers" | "outreach" | "crawler" | "photos" | "products" | "templates" | "history" | "settings";

const TABS: { key: TabKey; label: string; icon: (p: { size?: number }) => ReactNode }[] = [
  { key: "compose", label: "开发信", icon: MailIcon },
  { key: "batch", label: "批量生成", icon: BatchIcon },
  { key: "customers", label: "客户名单", icon: UsersIcon },
  { key: "outreach", label: "外联队列", icon: RouteIcon },
  { key: "crawler", label: "爬虫", icon: RadarIcon },
  { key: "photos", label: "照片库", icon: PhotoIcon },
  { key: "products", label: "产品资料", icon: BoxIcon },
  { key: "templates", label: "话术模板", icon: InboxIcon },
  { key: "history", label: "历史记录", icon: ClockIcon },
  { key: "settings", label: "设置", icon: GearIcon },
];

function BrandMark() {
  const [isRocking, setIsRocking] = useState(false);

  return (
    <div className="brand">
      <button
        type="button"
        className={`brand-mark-button${isRocking ? " is-rocking" : ""}`}
        aria-label="轻轻摇晃小船"
        title="点一下，让小船乘风摇晃"
        onClick={() => setIsRocking(true)}
        onAnimationEnd={() => setIsRocking(false)}
      >
        <span className="brand-mark" aria-hidden="true">
          <SailboatIcon size={20} />
        </span>
      </button>
      <div>
        <div className="brand-name">Tradewind</div>
        <div className="brand-sub">信风 · 外贸开发信</div>
      </div>
    </div>
  );
}

function StartupScreen() {
  return (
    <div className="startup-screen" role="status" aria-live="polite">
      <div className="startup-wordmark">Tradewind</div>
      <div className="startup-route" aria-hidden="true">
        <svg className="startup-waves" viewBox="0 0 320 20" preserveAspectRatio="none">
          <path
            className="startup-wave-base"
            d="M0 10 Q10 2 20 10 T40 10 T60 10 T80 10 T100 10 T120 10 T140 10 T160 10 T180 10 T200 10 T220 10 T240 10 T260 10 T280 10 T300 10 T320 10"
          />
          <path
            className="startup-wave-progress"
            d="M0 10 Q10 2 20 10 T40 10 T60 10 T80 10 T100 10 T120 10 T140 10 T160 10 T180 10 T200 10 T220 10 T240 10 T260 10 T280 10 T300 10 T320 10"
          />
        </svg>
        <span className="startup-boat">
          <SailboatIcon size={30} />
        </span>
      </div>
      <div className="startup-copy">
        <span>正在启航</span>
        <span className="startup-dots" aria-hidden="true">…</span>
      </div>
    </div>
  );
}

/** 航线装饰：淡琥珀虚线航线 + 罗盘（呼应“信风”意象，纯背景装饰） */
function TradeRoutes() {
  return (
    <svg className="routes" viewBox="0 0 560 380" aria-hidden="true">
      <path
        className="route"
        d="M 20 320 Q 140 260 260 300 T 500 150 Q 520 130 540 96"
      />
      <path className="route" d="M 30 60 Q 120 30 240 44 T 470 22" opacity="0.6" />
      <circle className="dot" cx="540" cy="96" r="2.6" opacity="0.5" />
      <circle className="dot" cx="470" cy="22" r="2.2" opacity="0.4" />
      <g opacity="0.5" transform="translate(96, 268)">
        <circle className="ring" r="22" />
        <path className="route" d="M 0 -26 L 0 26 M -26 0 L 26 0" opacity="0.7" />
        <circle className="dot" r="2.4" />
      </g>
      <g opacity="0.35" transform="translate(430, 210)">
        <circle className="ring" r="13" />
        <path className="route" d="M 0 -16 L 0 16 M -16 0 L 16 0" opacity="0.7" />
      </g>
    </svg>
  );
}

/** 右下装饰：信封 + 虚线航线（信在途），外圈虚线圆保留印章感 */
function Stamp() {
  return (
    <div className="stamp" aria-hidden="true">
      <svg viewBox="0 0 120 120">
        <path className="ring-dash" d="M60 6a54 54 0 1 1 0 108a54 54 0 1 1 0-108z" />
        <rect className="env" x="26" y="42" width="68" height="46" rx="3" />
        <path className="env" d="m26 46 34 25 34-25" />
        <path className="route2" d="M94 60q24-4 18-36" />
        <circle className="dot" cx="112" cy="22" r="3" opacity="0.55" />
      </svg>
    </div>
  );
}

export default function App() {
  const [view, setView] = useState<"loading" | "setup" | "main">("loading");
  const [dataSafety, setDataSafety] = useState<DataSafetyState>(getDataSafetyState);
  const [bootReady, setBootReady] = useState(false);
  // Fresh maintenance boot has no business components. Once mounted, changing
  // safety mode must never replace them and discard local form state.
  const [bootRestricted, setBootRestricted] = useState(true);
  const [tab, setTab] = useState<TabKey>("compose");
  const [hasKey, setHasKey] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [composeSeed, setComposeSeed] = useState<ComposeSeed | null>(null);
  const [followupDirty, setFollowupDirty] = useState(false);
  const [followupBusy, setFollowupBusy] = useState(false);
  const [followupOpen, setFollowupOpen] = useState(false);
  const [pendingNavigation, setPendingNavigation] = useState<{ tab: TabKey; seed?: ComposeSeed } | null>(null);
  const checkingGeneration = useRef(new WeakSet<HTMLButtonElement>());
  const checkedGeneration = useRef(new WeakSet<HTMLButtonElement>());
  const toast = useToast();

  const navigate = useCallback((destination: TabKey, seed?: ComposeSeed) => {
    if (destination === tab && !seed) return;
    if (!bootRestricted && getDataSafetyState().mode !== "ready") {
      toast.push("请先复制需要的草稿，资料可操作后再切换页面", "info"); return;
    }
    if (followupBusy) { toast.push("跟进正在保存，请稍候再切换页面", "info"); return; }
    if (followupDirty) { setPendingNavigation({ tab: destination, seed }); return; }
    if (seed) setComposeSeed(seed);
    setTab(destination);
  }, [tab, bootRestricted, followupDirty, followupBusy, toast.push]);
  const startOutreach = useCallback((seed: ComposeSeed) => {
    navigate("compose", seed);
  }, [navigate]);

  useEffect(() => {
    let cancelled = false;
    const unsubscribe = subscribeDataSafety(setDataSafety);
    void initializeDataEpoch().then(() => {
      if (cancelled) return;
      resetTaskReferencesForEpoch(getPageEpoch()!);
      setBootReady(true);
      if (getDataSafetyState().mode !== "ready") setView("main");
    }).catch(() => {
      if (!cancelled) { setBootReady(true); setView("main"); }
    });
    return () => { cancelled = true; unsubscribe(); };
  }, []);

  useEffect(() => {
    if (!bootReady || !bootRestricted || dataSafety.mode !== "ready" || !getPageEpoch()) return;
    let cancelled = false;
    const deadline = Date.now() + 60_000;
    const loadConfig = async () => {
      while (!cancelled) {
        try {
          const config = await api.getConfig();
          if (cancelled) return;
          setHasKey(config.has_key);
          setBootRestricted(false);
          setView(config.has_key ? "main" : "setup");
          return;
        } catch {
          if (getDataSafetyState().mode !== "ready") {
            if (!cancelled) setView("main");
            return;
          }
          if (Date.now() >= deadline) {
            if (!cancelled) { setError("设置暂时无法读取，请稍后重新打开页面。"); setView("main"); }
            return;
          }
          await new Promise((resolve) => window.setTimeout(resolve, 400));
        }
      }
    };
    void loadConfig();
    return () => {
      cancelled = true;
    };
  }, [bootReady, bootRestricted, dataSafety.mode]);

  // Capture before existing handlers clear results or start saving. React also
  // carries portal form events here, so follow-up drafts remain editable and
  // copyable without adding safety code to each business component.
  const guardBusinessAction = (event: SyntheticEvent) => {
    const state = getDataSafetyState();
    const target = event.target as HTMLElement;
    // Controls have their own frozen-epoch API and server-side maintenance gates.
    if (target.closest("[data-backup-controls]")) return;
    const button = target.closest("button");
    if (event.type === "click" && !button) return;
    if (state.mode !== "ready") {
      if (button && /复制|下载|关闭|取消/.test(button.getAttribute("aria-label") || button.title || button.textContent || "")) return;
      event.preventDefault(); event.stopPropagation();
      toast.push(state.message, "info");
    } else if (event.type === "click" && button && /生成开发信|开始批量生成/.test(button.textContent || "")) {
      if (checkedGeneration.current.delete(button)) return;
      // These handlers clear the previous generated draft before awaiting api.
      // Reconcile first, so a restore just before the click cannot erase it.
      event.preventDefault(); event.stopPropagation();
      if (checkingGeneration.current.has(button)) return;
      checkingGeneration.current.add(button);
      void api.getConfig().then(config => {
        if (!config.has_key) { toast.push("请先在设置中配置 API Key，再生成开发信", "info"); return; }
        if (button.isConnected && !button.disabled) {
          checkedGeneration.current.add(button);
          button.click();
        }
      }).catch(error => toast.push(error instanceof Error ? error.message : "暂不能生成，请稍后重试", "info"))
        .finally(() => checkingGeneration.current.delete(button));
    }
  };

  const saveKey = useCallback(async () => {
    setSaving(true);
    setError("");
    try {
      const data = await api.saveKey("", apiKey);
      if (data.ok) {
        setHasKey(true);
        setView("main");
        toast.push(data.verified === false ? "Key 已保存（验证超时，稍后可重试）" : "配置成功", "ok");
      } else {
        setError(data.error || "保存失败");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "网络错误");
    } finally {
      setSaving(false);
    }
  }, [apiKey, toast.push]);

  if (view === "loading") {
    return <StartupScreen />;
  }

  if (view === "setup") {
    return (
      <div className="setup-wrap">
        <AuroraBackground />
        <div className="card setup-card">
          <BrandMark />
          <DataMaintenanceNotice state={dataSafety} controls={<BackupSettingsCard />} />
          <h1 className="page-title" style={{ fontSize: 20, marginBottom: 6 }}>
            首次使用
          </h1>
          <p className="setup-tip">
            需要配置 DeepSeek API Key 才能生成开发信。
            <br />
            获取：platform.deepseek.com → API Keys（费用约几厘一封）。
          </p>
          <input
            className="input"
            type="password"
            placeholder="sk-..."
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && saveKey()}
          />
          {error && <div className="setup-error">{error}</div>}
          <div className="setup-actions">
            <button className="btn btn-primary" onClick={saveKey} disabled={saving || !apiKey.trim()}>
              {saving ? "验证中…" : "保存并进入"}
            </button>
            <button type="button" className="btn btn-ghost" style={{ minHeight: 44 }} onClick={() => setView("main")}>暂不配置，查看本机资料</button>
          </div>
        </div>
        {toast.el}
      </div>
    );
  }

  return (
    <div className="shell">
      <AuroraBackground />
      <aside className="sidebar">
        <BrandMark />
        <nav style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {TABS.map((t) => (
            <button
              key={t.key}
              aria-label={t.label}
              className={`nav-item ${tab === t.key ? "active" : ""}`}
              onClick={() => navigate(t.key)}
            >
              <t.icon size={17} />
              <span>{t.label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <span className={`status-dot ${hasKey ? "ok" : "warn"}`} />
          <span>API Key {hasKey ? "已配置" : "未配置"}</span>
        </div>
      </aside>
      <main className="main">
        <TradeRoutes />
        {!followupOpen && <DataMaintenanceNotice state={dataSafety} controls={!bootRestricted && tab === "settings" ? <p>请使用下方业务备份卡中的恢复控制。已有表单仍可复制。</p> : <BackupSettingsCard />} />}
        {bootRestricted && error && <p role="alert">{error}</p>}
        {!bootRestricted && <div onClickCapture={guardBusinessAction} onSubmitCapture={guardBusinessAction}>
        <FadeIn key={tab}>
          {tab === "compose" && (
            <ComposeTab
              seed={composeSeed}
              onSeedConsumed={() => setComposeSeed(null)}
              onOpenOutreach={() => navigate("outreach")}
            />
          )}
          {tab === "batch" && <BatchTab />}
          {tab === "customers" && <CustomersTab onStartOutreach={startOutreach} onFollowupDirtyChange={setFollowupDirty} onFollowupBusyChange={setFollowupBusy} onFollowupOpenChange={setFollowupOpen} maintenanceControls={<DataMaintenanceNotice state={dataSafety} controls={<BackupSettingsCard />} />} />}
          {tab === "outreach" && <OutreachTab />}
          {tab === "crawler" && <CrawlerTab />}
          {tab === "photos" && <PhotosTab />}
          {tab === "products" && <ProductsTab />}
          {tab === "templates" && <TemplatesTab />}
          {tab === "history" && <HistoryTab />}
          {tab === "settings" && <SettingsTab />}
        </FadeIn>
        </div>}
      </main>
      <Stamp />
      {toast.el}
      {pendingNavigation && <ConfirmDialog title="离开客户名单并放弃未保存的跟进内容？" desc="已保存的沟通记录和跟进安排会保留。" confirmText="放弃并切换" onCancel={() => setPendingNavigation(null)} onConfirm={() => {
        if (pendingNavigation.seed) setComposeSeed(pendingNavigation.seed);
        setFollowupDirty(false); setTab(pendingNavigation.tab); setPendingNavigation(null);
      }} />}
    </div>
  );
}
