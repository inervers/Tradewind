import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { FadeIn, useToast } from "../components/effects";
import { DownloadIcon, GearIcon, KeyIcon } from "../components/Icons";
import { VisionModelCombo } from "../components/Combo";
import type { ConfigState, ProviderInfo } from "../types";

const VISION_PROVIDER_NAMES: Record<string, string> = {
  glm: "智谱 GLM",
  qwen: "阿里云 Qwen",
  volc: "火山豆包",
};

const VISION_KEY_LABELS: Record<string, string> = {
  glm: "智谱 API Key",
  qwen: "阿里云 DashScope Key",
  volc: "火山方舟 API Key",
};

const VISION_KEY_HINTS: Record<string, string> = {
  glm: "智谱开放平台创建",
  qwen: "阿里云百炼控制台创建",
  volc: "火山方舟控制台创建",
};

const VISION_MODEL_FALLBACKS: Record<string, Record<string, string>> = {
  glm: {
    "glm-4.6v-flash": "GLM-4.6V-Flash（免费）",
    "glm-4.6v-flashx": "GLM-4.6V-FlashX（0.15元/M，稳定）",
    "glm-4.6v": "GLM-4.6V（付费增强）",
  },
  qwen: {
    "qwen3-vl-plus": "Qwen3-VL-Plus（推荐，有免费额度）",
    "qwen-vl-max": "Qwen-VL-Max（旗舰）",
  },
  volc: {
    "doubao-seed-2-0-lite-260428": "豆包 Seed 2.0 Lite（全模态，替代 1.6 vision）",
    "doubao-seed-2-0-mini-260428": "豆包 Seed 2.0 Mini（轻量全模态）",
  },
};

const EMPTY_COMPANY_PROFILE = {
  sender_name: "",
  company_name: "",
  email: "",
  whatsapp: "",
  website: "",
};

export default function SettingsTab() {
  const [config, setConfig] = useState<ConfigState>({ has_key: false });
  const [editing, setEditing] = useState<string | null>(null); // 正在配置的 provider id
  const [keyInput, setKeyInput] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [modelLoading, setModelLoading] = useState(false);
  const [modelError, setModelError] = useState("");
  const [busy, setBusy] = useState(false);
  const [visionKey, setVisionKey] = useState("");
  const [visionModel, setVisionModel] = useState("");
  const [visionProvider, setVisionProvider] = useState("glm");
  const [visionBusy, setVisionBusy] = useState(false);
  const [companyProfile, setCompanyProfile] = useState(EMPTY_COMPANY_PROFILE);
  const [companyBusy, setCompanyBusy] = useState(false);
  const [diagnosticBusy, setDiagnosticBusy] = useState(false);
  const toast = useToast();

  const load = useCallback(() => {
    api.getConfig()
      .then((c) => {
        setConfig(c);
        const provider = c.vision?.provider || "glm";
        setVisionProvider(provider);
        setVisionModel(c.vision?.providers?.[provider]?.model || c.vision?.model || "");
        setCompanyProfile(c.company_profile || EMPTY_COMPANY_PROFILE);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const openEdit = useCallback((p: ProviderInfo) => {
    setEditing(p.id);
    setKeyInput("");
    setBaseUrl(p.base_url);
    setModel(p.model);
    setModelOptions([]);
    setModelError("");
    // 有 Key 就自动拉取可用模型列表
    if (p.has_key) {
      setModelLoading(true);
      api
        .providerModels(p.id)
        .then((r) => {
          if (r.ok && r.models && r.models.length) {
            setModelOptions(r.models);
            setModel((prev) => prev || r.models![0]);
          } else {
            setModelError("未能自动获取模型列表，可手动填写");
          }
        })
        .catch(() => setModelError("未能自动获取模型列表，可手动填写"))
        .finally(() => setModelLoading(false));
    }
  }, []);

  const saveVision = useCallback(async () => {
    setVisionBusy(true);
    try {
      const r = await api.saveVision(visionKey, visionModel, visionProvider);
      if (!r.ok) {
        toast.push(r.error || "保存失败", "error");
        return;
      }
      setVisionKey("");
      toast.push(
        r.configured
          ? `视觉识别已启用（${VISION_PROVIDER_NAMES[visionProvider] || visionProvider}）`
          : `已切换 ${VISION_PROVIDER_NAMES[visionProvider] || visionProvider}（Key 未填，照片分析暂跳过）`,
        "ok"
      );
      load();
    } catch {
      toast.push("保存失败", "error");
    } finally {
      setVisionBusy(false);
    }
  }, [visionKey, visionModel, visionProvider, toast.push, load]);

  const selectVisionProvider = useCallback((provider: string) => {
    const saved = config.vision?.providers?.[provider];
    const fallback = VISION_MODEL_FALLBACKS[provider] || {};
    setVisionProvider(provider);
    setVisionModel(saved?.model || Object.keys(fallback)[0] || "");
    setVisionKey("");
  }, [config.vision?.providers]);

  const save = useCallback(async () => {
    if (!editing) return;
    setBusy(true);
    try {
      // 模型 / base_url 有改动 → 先存参数（同 Key 换模型场景）
      if (baseUrl.trim() || model.trim()) {
        await api.saveProviderParams(editing, baseUrl, model);
      }
      if (keyInput.trim()) {
        // 填了 Key → 存 Key（自动验证 + 激活）
        const r = await api.saveKey(editing, keyInput);
        if (!r.ok) {
          toast.push(r.error || "保存失败", "error");
          return;
        }
        toast.push(r.verified === false ? "Key 已保存（验证超时）" : "已保存并激活", "ok");
      } else {
        // 没填 Key → 只换模型 / 激活已有配置
        const r = await api.activateProvider(editing);
        if (!r.ok) {
          toast.push(r.error || "激活失败", "error");
          return;
        }
        toast.push("已更新模型并激活", "ok");
      }
      setEditing(null);
      load();
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "保存失败", "error");
    } finally {
      setBusy(false);
    }
  }, [editing, baseUrl, model, keyInput, load, toast.push]);

  const activate = useCallback(async (pid: string) => {
    if (pid === config.active_provider) return;
    try {
      const r = await api.activateProvider(pid);
      if (r.ok) {
        toast.push("已切换到 " + pid, "ok");
        load();
      } else {
        toast.push(r.error || "切换失败", "error");
      }
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "切换失败", "error");
    }
  }, [config.active_provider, load, toast.push]);

  const saveCompany = useCallback(async () => {
    if (!companyProfile.company_name.trim()) {
      toast.push("公司名称不能为空", "error");
      return;
    }
    setCompanyBusy(true);
    try {
      const r = await api.saveCompanyProfile(companyProfile);
      if (!r.ok) {
        toast.push(r.error || "保存失败", "error");
        return;
      }
      toast.push("公司资料与签名格式已保存", "ok");
      load();
    } catch (error) {
      toast.push(error instanceof Error ? error.message : "保存失败", "error");
    } finally {
      setCompanyBusy(false);
    }
  }, [companyProfile, load, toast.push]);

  const exportDiagnostics = useCallback(async () => {
    setDiagnosticBusy(true);
    try {
      await api.exportDiagnostics();
      toast.push("诊断包已下载，可在需要售后排查时主动发送", "ok");
    } catch (error) {
      toast.push(error instanceof Error ? error.message : "诊断包导出失败", "error");
    } finally {
      setDiagnosticBusy(false);
    }
  }, [toast.push]);

  const providers = config.providers || [];
  const active = config.active_provider || "deepseek";
  const selectedVision = config.vision?.providers?.[visionProvider];
  const visionActive = config.vision?.provider === visionProvider;

  return (
    <>
      <div className="page-head">
        <div className="page-title">
          设置 <span className="seal">配置</span>
        </div>
        <div className="page-desc">选择生成开发信的服务商，各家 Key 独立保存，本机 data/config.json</div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-title">
          <span>当前生效模型</span>
          <span className="badge badge-ok">{config.model || "—"}</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <GearIcon size={16} />
          <span style={{ fontFamily: "var(--font-serif)", fontSize: 15, fontWeight: 600 }}>
            {providers.find((p) => p.id === active)?.name || active}
          </span>
          {!config.has_key && (
            <span className="badge badge-warn">未配置 Key，先在下方选择服务商填写</span>
          )}
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {providers.map((p, i) => {
          const isActive = p.id === active;
          const isEditing = editing === p.id;
          return (
            <FadeIn key={p.id} delay={i * 40}>
              <div className={`card ${isActive ? "" : ""}`} style={{ padding: "16px 20px", borderColor: isActive ? "var(--amber)" : undefined }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                  <span style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 160 }}>
                    <KeyIcon size={15} style={{ color: isActive ? "var(--amber)" : "var(--ink-3)" }} />
                    <span style={{ fontFamily: "var(--font-serif)", fontWeight: 600, fontSize: 14.5 }}>{p.name}</span>
                  </span>
                  <span className="cell-muted" style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{p.model || "未设置模型"}</span>
                  <span className="spacer" />
                  {isActive && <span className="badge badge-ok">激活中</span>}
                  {!isActive && p.has_key && <span className="badge badge-neutral">已配置</span>}
                  {!p.has_key && <span className="badge badge-warn">未配置</span>}
                  {!isActive && p.has_key && (
                    <button className="btn btn-ghost btn-sm" onClick={() => activate(p.id)}>切换</button>
                  )}
                  <button className="btn btn-ghost btn-sm" onClick={() => (isEditing ? setEditing(null) : openEdit(p))}>
                    {isEditing ? "取消" : p.has_key ? "换 Key / 参数" : "配置"}
                  </button>
                </div>

                {isEditing && (
                  <div
                    style={{
                      marginTop: 14,
                      background: "var(--surface-2)",
                      border: "1px solid var(--line)",
                      borderRadius: "var(--r-sm)",
                      padding: "16px 18px",
                    }}
                  >
                    <div className="row">
                      <div className="field" style={{ flex: 2 }}>
                        <label className="field-label">API Key（留空 = 不换 Key）</label>
                        <input className="input" type="password" placeholder={p.has_key ? "已配置，留空则保持" : "sk-..."} value={keyInput} onChange={(e) => setKeyInput(e.target.value)} />
                      </div>
                      <div className="field">
                        <label className="field-label">模型（同 Key 可切换）</label>
                        <input
                          className="input"
                          placeholder={modelLoading ? "获取中…" : "模型名"}
                          value={model}
                          onChange={(e) => setModel(e.target.value)}
                        />
                        {modelOptions.length > 0 && (
                          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 4 }}>
                            {modelOptions.map((m) => (
                              <button
                                key={m}
                                type="button"
                                className={`badge ${model === m ? "badge-ok" : "badge-neutral"}`}
                                style={{ cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 11.5 }}
                                onClick={() => setModel(m)}
                              >
                                {m}
                              </button>
                            ))}
                          </div>
                        )}
                        <div className="field-hint">
                          {modelError
                            ? modelError
                            : modelOptions.length
                              ? `自动获取到 ${modelOptions.length} 个可用模型，可输入或选择`
                              : modelLoading
                                ? "正在拉取可用模型…"
                                : "填完 Key 保存后，可在此自动获取模型列表"}
                        </div>
                      </div>
                      {p.id === "custom" && (
                        <div className="field">
                          <label className="field-label">Base URL</label>
                          <input className="input" placeholder="https://api.example.com/v1" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
                        </div>
                      )}
                      <div className="field" style={{ flexShrink: 0 }}>
                        <label className="field-label">&nbsp;</label>
                        <div style={{ display: "flex", gap: 8 }}>
                          <button className="btn btn-primary btn-sm" onClick={save} disabled={busy}>
                            {busy ? "保存中…" : "保存并激活"}
                          </button>
                        </div>
                      </div>
                    </div>
                    <div className="field-hint" style={{ marginTop: 8 }}>
                      {p.id === "openai"
                        ? "OpenAI 需要网络代理才能访问；国内网络建议用 DeepSeek 或 Kimi"
                        : p.id === "kimi"
                          ? "国内直连，platform.moonshot.cn 获取 Key"
                          : p.id === "custom"
                            ? "任意 OpenAI 兼容接口：填 Base URL + 模型名 + Key"
                            : "platform.deepseek.com 获取 Key，国内直连"}
                    </div>
                  </div>
                )}
              </div>
            </FadeIn>
          );
        })}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-title">
          <span>公司资料与发件签名</span>
          <span className="badge badge-neutral">仅保存在本机</span>
        </div>
        <p className="setup-tip" style={{ marginTop: 0 }}>
          正式开发信会追加完整公司资料；WhatsApp 只显示“联系人｜公司简称”，避免短消息过于正式。
        </p>
        <div className="company-profile-grid">
          <div className="field">
            <label className="field-label">联系人</label>
            <input className="input" value={companyProfile.sender_name} onChange={(event) => setCompanyProfile({ ...companyProfile, sender_name: event.target.value })} placeholder="Demo User" />
          </div>
          <div className="field company-profile-name">
            <label className="field-label">公司名称 *</label>
            <input className="input" value={companyProfile.company_name} onChange={(event) => setCompanyProfile({ ...companyProfile, company_name: event.target.value })} placeholder="DemoMed – Medical Aesthetic Equipment" />
          </div>
          <div className="field">
            <label className="field-label">邮箱</label>
            <input className="input" type="email" value={companyProfile.email} onChange={(event) => setCompanyProfile({ ...companyProfile, email: event.target.value })} placeholder="sales@example.com" />
          </div>
          <div className="field">
            <label className="field-label">WhatsApp</label>
            <input className="input" value={companyProfile.whatsapp} onChange={(event) => setCompanyProfile({ ...companyProfile, whatsapp: event.target.value })} placeholder="+00 000 000 0000" />
          </div>
          <div className="field">
            <label className="field-label">官网</label>
            <input className="input" value={companyProfile.website} onChange={(event) => setCompanyProfile({ ...companyProfile, website: event.target.value })} placeholder="www.example.com" />
          </div>
        </div>
        <div className="company-profile-actions">
          <div className="field-hint">修改后只影响新生成的内容，不会改写历史记录。</div>
          <button className="btn btn-primary btn-sm" onClick={saveCompany} disabled={companyBusy}>
            {companyBusy ? "保存中…" : "保存公司资料"}
          </button>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-title"><span>数据存放位置</span></div>
        <p className="setup-tip" style={{ marginTop: 0 }}>
          所有数据都在 <b>{config.data_dir || "data/"}</b> 文件夹里，备份 = 复制整个文件夹，换电脑 = 整个文件夹拷过去即可。
        </p>
        <table className="table" style={{ maxWidth: 720 }}>
          <tbody>
            <tr>
              <td style={{ width: 150, color: "var(--ink-3)" }}>客户名单</td>
              <td className="cell-email">{config.data_files?.customers || "data/customers.json"}</td>
            </tr>
            <tr>
              <td style={{ color: "var(--ink-3)" }}>产品资料</td>
              <td className="cell-email">{config.data_files?.products || "data/products.json"}</td>
            </tr>
            <tr>
              <td style={{ color: "var(--ink-3)" }}>话术模板</td>
              <td className="cell-email">{config.data_files?.templates || "data/emails.json"}</td>
            </tr>
            <tr>
              <td style={{ color: "var(--ink-3)" }}>生成记录</td>
              <td className="cell-email">{config.data_files?.memory || "data/tradewind_memory.db"}</td>
            </tr>
            <tr>
              <td style={{ color: "var(--ink-3)" }}>爬虫导出 CSV</td>
              <td className="cell-email">{config.data_files?.crawler_csv || "data/maps_hk.csv"}（命令行爬虫输出；网页版 CSV 走浏览器下载）</td>
            </tr>
            <tr>
              <td style={{ color: "var(--ink-3)" }}>API Key</td>
              <td className="cell-email">data/config.json（本机保存，不随程序分发）</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-title"><span>视觉识别（爬虫照片分析）</span></div>
        <p className="setup-tip" style={{ marginTop: 0 }}>
          爬虫抓商家照片 → 视觉识别仪器品牌 → 对比产品库做缺品推荐，也可在照片库中人工筛选。只影响视觉分析，写开发信仍走上方服务商。
        </p>
        <div className="row" style={{ marginTop: 12 }}>
          {Object.entries(config.vision?.providers || {
            glm: { name: "智谱 GLM" }, qwen: { name: "阿里云 Qwen" }, volc: { name: "火山豆包" },
          }).map(([pid, pv]) => (
            <button
              key={pid}
              className="btn vision-provider-button"
              style={{
                flex: 1,
                border: visionProvider === pid ? "1px solid var(--amber)" : "1px solid var(--line)",
                color: visionProvider === pid ? "var(--amber)" : "var(--ink-2)",
                background: visionProvider === pid ? "color-mix(in srgb, var(--amber) 12%, transparent)" : "var(--surface)",
              }}
              onClick={() => selectVisionProvider(pid)}
            >
              <span>{pv?.name || pid}</span>
              <small className="vision-provider-state">
                {config.vision?.provider === pid ? "使用中" : pv?.has_key ? "已配置" : "未配置"}
              </small>
            </button>
          ))}
        </div>
        <div className="row" style={{ marginTop: 12 }}>
          <div className="field" style={{ flex: 2 }}>
            <label className="field-label">{VISION_KEY_LABELS[visionProvider] || "视觉 API Key"}</label>
            <input
              className="input"
              type="password"
              placeholder={selectedVision?.has_key
                ? "已配置，留空保持"
                : VISION_KEY_HINTS[visionProvider] || "在服务商控制台创建"}
              value={visionKey}
              onChange={(e) => setVisionKey(e.target.value)}
            />
          </div>
          <div className="field">
            <label className="field-label">模型</label>
            <VisionModelCombo
              value={visionModel}
              onChange={setVisionModel}
              models={config.vision?.providers?.[visionProvider]?.models || VISION_MODEL_FALLBACKS[visionProvider] || {}}
              placeholder="选择或输入 Model ID"
            />
          </div>
        </div>
        {visionProvider === "volc" && (
          <div className="field-hint vision-provider-hint">
            火山需先到 <a href="https://console.volcengine.com/ark/region:ark+cn-beijing/apikey" target="_blank" rel="noreferrer">方舟 API Key 页面</a> 创建专用 API Key，再回填 Key + Model ID。不要填写 AK/SK、Bearer 前缀或 ep-xxx；Model ID 直调，无需创建推理接入点。
          </div>
        )}
        <div style={{ marginTop: 12 }}>
          <button className="btn btn-primary" onClick={saveVision} disabled={visionBusy}>
            {visionBusy
              ? "保存中…"
              : selectedVision?.has_key && !visionActive && !visionKey
                ? "切换并启用"
                : "保存并启用"}
          </button>
          <span className="field-hint" style={{ marginLeft: 12 }}>
            {selectedVision?.has_key && visionActive
              ? `使用中 · ${selectedVision.model || visionModel}`
              : selectedVision?.has_key
                ? `已保存 · ${selectedVision.model || visionModel}`
              : `${VISION_PROVIDER_NAMES[visionProvider] || visionProvider}：未配置 Key`}
          </span>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-title">
          <span>本地诊断与售后</span>
          <span className="badge badge-neutral">不会后台上传</span>
        </div>
        <p className="setup-tip" style={{ marginTop: 0 }}>
          遇到启动、导入或模型调用问题时，可主动导出脱敏诊断包。包内只有版本、运行环境、模型名称、数据数量和错误类型统计。
        </p>
        <div className="diagnostic-actions">
          <button className="btn btn-ghost" onClick={exportDiagnostics} disabled={diagnosticBusy}>
            <DownloadIcon size={16} />
            {diagnosticBusy ? "正在生成…" : "导出诊断包"}
          </button>
          <span className="field-hint">不包含 Key、客户资料、邮件正文、搜索词、网址、照片、文件路径或原始日志。</span>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-title"><span>关于</span></div>
        <p className="setup-tip">
          Tradewind · 信风 v0.2.0<br />
          外贸开发信 Agent：医美设备出口商的获客助手。<br />
          生成 → 规则自检 → 质量评分 → 历史复盘，全链路本地运行。
        </p>
      </div>
      {toast.el}
    </>
  );
}
