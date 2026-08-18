import { useCallback, useEffect, useRef, useState } from "react";
import { api, copyText, downloadText } from "../api";
import { ShimmerButton, SpotlightCard, useToast, Elapsed } from "../components/effects";
import ProductCombo, { CustomerCombo, TemplateCombo } from "../components/Combo";
import { CopyIcon, DownloadIcon, SparkIcon, XIcon } from "../components/Icons";
import { resumeTask, watchTask } from "../tasks";
import type { Customer, EmailResult, Lang, OutFormat, Product, TaskState, Template } from "../types";
import { formatLocation } from "../location";

export default function ComposeTab() {
  const [customer, setCustomer] = useState("");
  const [country, setCountry] = useState("中国香港");
  const [product, setProduct] = useState("");
  const [extra, setExtra] = useState("");
  const [judge, setJudge] = useState(false);
  const [lang, setLang] = useState<Lang>("zh-hant");
  const [format, setFormat] = useState<OutFormat>("email");
  const [products, setProducts] = useState<Product[]>([]);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [templateId, setTemplateId] = useState("");
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [result, setResult] = useState<EmailResult | null>(null);
  const [stream, setStream] = useState("");
  const [busy, setBusy] = useState(false);
  const [taskId, setTaskId] = useState("");
  const customerRefreshRef = useRef<Promise<void> | null>(null);
  const toast = useToast();

  const refreshCustomers = useCallback(() => {
    if (customerRefreshRef.current) return customerRefreshRef.current;
    const request = api.customers.list()
      .then(setCustomers)
      .catch(() => {})
      .finally(() => {
        if (customerRefreshRef.current === request) customerRefreshRef.current = null;
      });
    customerRefreshRef.current = request;
    return request;
  }, []);

  // 任务状态处理（流式 + 结束判定），全局轮询回调
  const handleTask = useCallback((t: TaskState) => {
    if (t.stream) setStream(t.stream);
    if (t.status === "running") setBusy(true);
    else if (t.status === "done") {
      setBusy(false);
      setResult(t.result as EmailResult | null);
      const ok = !t.result?.issues?.length;
      toast.push(ok ? "生成完成，自检通过" : "生成完成，有自检提示", ok ? "ok" : "info");
    } else if (t.status === "cancelled") {
      setBusy(false);
      toast.push("已停止生成", "info");
    } else if (t.status === "error") {
      setBusy(false);
      toast.push(t.error || "生成失败", "error");
    }
  }, [toast.push]);

  // 挂载时恢复：生成中途切走再切回，继续显示流式与结果
  useEffect(() => {
    const unsub = resumeTask("compose", handleTask);
    return () => {
      unsub?.();
    };
  }, [handleTask]);

  useEffect(() => {
    api.products.list().then(setProducts).catch(() => {});
    api.templates.list().then(setTemplates).catch(() => {});
    void refreshCustomers();
  }, [refreshCustomers]);

  const generate = useCallback(async () => {
    if (!customer.trim()) {
      toast.push("先填客户名称", "error");
      return;
    }
    setBusy(true);
    setResult(null);
    setStream("");
    try {
      const { task_id, error } = await api.startEmail({
        customer: customer.trim(),
        country: country.trim(),
        product: product.trim(),
        extra: extra.trim(),
        judge,
        language: lang,
        format,
        template_id: templateId,
      });
      if (!task_id) throw new Error(error || "启动失败");
      setTaskId(task_id);
      watchTask("compose", task_id, handleTask);
    } catch (e) {
      setBusy(false);
      toast.push(e instanceof Error ? e.message : "启动失败", "error");
    }
  }, [customer, country, product, extra, judge, lang, format, templateId, handleTask, toast.push]);

  const stop = useCallback(async () => {
    if (!taskId) return;
    try {
      const r = await api.cancelTask(taskId);
      if (!r.ok) toast.push(r.error || "停止失败", "error");
    } catch { /* ignore */ }
  }, [taskId, toast.push]);

  const copy = useCallback(async () => {
    if (!result) return;
    (await copyText(result.email)) ? toast.push("已复制到剪贴板", "ok") : toast.push("复制失败", "error");
  }, [result, toast.push]);

  const download = useCallback(() => {
    if (!result) return;
    const safe = result.customer.replace(/[\\/:*?"<>|]/g, "_").slice(0, 30);
    downloadText(`${safe}.txt`, result.email);
  }, [result]);

  return (
    <>
      <div className="page-head">
        <div className="page-title">
          开发信 <span className="seal">写一封</span>
        </div>
        <div className="page-desc">填入目标客户，基于产品资料与话术模板生成个性化开发信 / WhatsApp 消息</div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 5fr) minmax(0, 7fr)", gap: 16, alignItems: "start" }}>
        <div className="card" style={{ position: "sticky", top: 20 }}>
          <div className="card-title">客户信息</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <div className="field">
              <label className="field-label">客户名称 *</label>
              <CustomerCombo
                value={customer}
                onChange={setCustomer}
                customers={customers}
                onOpen={refreshCustomers}
                placeholder="输入客户名称，或从客户名单选择"
                onSelect={(selected) => {
                  setCountry(formatLocation(selected.country, selected.city) || country);
                  if (selected.notes) setExtra(selected.notes);
                }}
              />
            </div>
            <div className="field">
              <label className="field-label">国家 / 地区</label>
              <input className="input" placeholder="中国香港（默认），可改其他地区" value={country} onChange={(e) => setCountry(e.target.value)} />
            </div>
            <div className="field">
              <label className="field-label">推荐产品</label>
              <ProductCombo value={product} onChange={setProduct} products={products} placeholder="输入设备名（如 DemoMed Vision-100），或留空自动挑" hint="可输入或从产品资料选择，留空自动挑最相关" />
            </div>
            <div className="field">
              <label className="field-label">话术模板</label>
              <TemplateCombo value={templateId} onChange={setTemplateId} templates={templates} />
            </div>
            <div className="field">
              <label className="field-label">语言 / 形态</label>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                {([["zh-hant", "繁体中文"], ["en", "English"]] as [Lang, string][]).map(([v, label]) => (
                  <button key={v} type="button" className={`badge ${lang === v ? "badge-ok" : "badge-neutral"}`}
                    style={{ cursor: "pointer" }} onClick={() => setLang(v)}>{label}</button>
                ))}
                <span style={{ width: 1, background: "var(--line)", margin: "0 4px" }} />
                {([["email", "开发信"], ["whatsapp", "WhatsApp 消息"]] as [OutFormat, string][]).map(([v, label]) => (
                  <button key={v} type="button" className={`badge ${format === v ? "badge-ok" : "badge-neutral"}`}
                    style={{ cursor: "pointer" }} onClick={() => setFormat(v)}>{label}</button>
                ))}
              </div>
              <div className="field-hint">
                {format === "whatsapp"
                  ? "手机短消息（150 字内口语化），配合名单里的 wa.me 链接直接发"
                  : lang === "zh-hant" ? "香港繁体正式开发信" : "English cold email"}
              </div>
            </div>
            <div className="field">
              <label className="field-label">客户背景（可选，补充业务重点或需求）</label>
              <textarea className="textarea" placeholder="例：主打激光脱毛、有 3 间分店、想引入 DemoMed Vision-100 检测项目；爬虫抓到的店铺仪器（激光/皮秒/IPL）也可以填进来…" value={extra} onChange={(e) => setExtra(e.target.value)} />
              <div className="field-hint">填得越细，AI 越能针对这家店用的仪器做利弊分析、写出独有的卖点</div>
            </div>
            <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--ink-2)", cursor: "pointer" }}>
              <input type="checkbox" checked={judge} onChange={(e) => setJudge(e.target.checked)} style={{ width: 15, height: 15, accentColor: "var(--amber)" }} />
              生成后 LLM 质量打分（慢几秒，花少量 token）
            </label>
            <div style={{ display: "flex", gap: 10 }}>
              <ShimmerButton onClick={generate} disabled={busy} className="" style={{ flex: 1 }}>
                <SparkIcon size={16} />
                {busy ? "生成中…" : "生成开发信"}
              </ShimmerButton>
              {busy && (
                <button
                  className="btn btn-ghost"
                  onClick={stop}
                  style={{ borderColor: "var(--red)", color: "var(--red)" }}
                >
                  <XIcon size={15} /> 停止
                </button>
              )}
            </div>
          </div>
        </div>

        <div>
          {!result && !busy && (
            <div className="card">
              <div className="empty">
                <MailOutline />
                <div className="empty-title">还没有生成结果</div>
                <div className="empty-desc">左侧填好客户信息，点击生成。邮件会在这里预览，可直接复制发送</div>
              </div>
            </div>
          )}
          {busy && (
            <div className="card">
              <div className="card-title">
                <span>正在生成…</span>
                <span className="badge badge-neutral"><Elapsed active={busy} /> 已用时</span>
              </div>
              {stream ? (
                <div className="email-body typing">{stream}</div>
              ) : (
                <div className="empty" style={{ padding: 26 }}>
                  <div style={{ display: "inline-flex", gap: 6 }}>
                    {[0, 1, 2].map((i) => (
                      <span key={i} style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--amber)", opacity: 0.4, animation: `pulse 0.9s ease-in-out ${i * 0.18}s infinite` }} />
                    ))}
                  </div>
                  <div className="empty-title">检索产品卖点与话术模板…</div>
                </div>
              )}
            </div>
          )}
          {result && (
            <SpotlightCard>
              <div className="card-title">
                <span style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  {result.customer}
                  <span className={`badge ${result.issues.length === 0 ? "badge-ok" : "badge-warn"}`}>
                    {result.issues.length === 0 ? "自检通过" : `${result.issues.length} 项提示`}
                  </span>
                  {result.scores?.overall ? (
                    <span className="badge badge-neutral">评分 {result.scores.overall}/5</span>
                  ) : null}
                </span>
                <span style={{ display: "flex", gap: 6 }}>
                  <button className="icon-btn" title="复制" onClick={copy}><CopyIcon size={15} /></button>
                  <button className="icon-btn" title="下载" onClick={download}><DownloadIcon size={15} /></button>
                </span>
              </div>
              <div className="email-body">{result.email}</div>
              {result.issues.length > 0 && (
                <ul style={{ marginTop: 12, paddingLeft: 18, fontSize: 12.5, color: "var(--red)" }}>
                  {result.issues.map((i) => (
                    <li key={i}>{i}</li>
                  ))}
                </ul>
              )}
              <div className="field-hint" style={{ marginTop: 10, display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
                <span>{result.country || "地区未填"} · {result.product}</span>
                <span>耗时 {result.time_s}s</span>
                {result.templates_used && result.templates_used.length > 0 && (
                  <span style={{ color: "var(--ink-3)" }}>参考话术：{result.templates_used.join(" / ")}</span>
                )}
                {result.scores?.suggestions ? <span style={{ color: "var(--amber-deep)" }}>建议：{result.scores.suggestions}</span> : null}
              </div>
            </SpotlightCard>
          )}
        </div>
      </div>
      {toast.el}
    </>
  );
}

function MailOutline() {
  return (
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="m3 7 9 6 9-6" />
    </svg>
  );
}
