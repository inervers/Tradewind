import { useCallback, useEffect, useRef, useState } from "react";
import { api, downloadText } from "../api";
import { useToast } from "../components/effects";
import { RadarIcon, SearchIcon } from "../components/Icons";
import { resumeTask, watchTask } from "../tasks";
import type { CrawlerResult, CrawlerState, CrawlerTarget } from "../types";

const TARGETS: { key: CrawlerTarget; label: string; hint: string }[] = [
  { key: "all", label: "全部", hint: "邮箱 + 电话/WhatsApp" },
  { key: "email", label: "只要邮箱", hint: "官网有邮箱的店" },
  { key: "whatsapp", label: "电话 / WhatsApp", hint: "无邮箱但有电话也能跟进" },
];

function resultKey(result: CrawlerResult, index: number) {
  return `${result.website || ""}|${result.phone || ""}|${result.name}|${index}`;
}

export default function CrawlerTab() {
  const [source, setSource] = useState<"maps" | "webs">("maps");
  const [engine, setEngine] = useState("auto");
  const [queries, setQueries] = useState("medspa,medical spa,醫學美容");
  const [country, setCountry] = useState("香港");
  const [targets, setTargets] = useState<CrawlerTarget[]>(["all"]);
  const [maxN, setMaxN] = useState(20);
  const [allowRecrawl, setAllowRecrawl] = useState(false);
  const [savePhotos, setSavePhotos] = useState(true);
  const [state, setState] = useState<CrawlerState | null>(null);
  const [busy, setBusy] = useState(false);
  const [importing, setImporting] = useState(false);
  const [selectedResults, setSelectedResults] = useState<Set<string>>(new Set());
  const [taskId, setTaskId] = useState("");
  const logRef = useRef<HTMLDivElement | null>(null);
  const toast = useToast();

  const switchSource = useCallback((s: "maps" | "webs") => {
    setSource(s);
    setQueries(
      s === "webs"
        ? "medical aesthetic clinic Hong Kong laser treatment,香港 醫學美容 診所,香港 美容中心 激光"
        : "medspa,medical spa,醫學美容"
    );
    setState(null);
  }, []);

  // 日志自动滚到底部
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [state?.log]);

  const handleTask = useCallback((t: CrawlerState) => {
    setState(t);
    if (t.status === "running") setBusy(true);
    else if (t.status === "done" || t.status === "cancelled") setBusy(false);
    else if (t.status === "error") {
      setBusy(false);
      toast.push(t.error || "爬虫失败", "error");
    }
  }, [toast.push]);

  useEffect(() => {
    if (state?.status === "done") {
      setSelectedResults(new Set(state.results.map(resultKey)));
    }
  }, [state?.status]);

  // 切回时恢复进行中的爬虫任务
  useEffect(() => {
    const unsub = resumeTask("crawler", handleTask, api.crawler.task);
    return () => {
      unsub?.();
    };
  }, [handleTask]);

  const start = useCallback(async () => {
    if (!queries.trim()) {
      toast.push("先填搜索词", "error");
      return;
    }
    setState(null);
    setSelectedResults(new Set());
    setBusy(true);
    try {
      const r = await api.crawler.start({
        queries: queries.trim(),
        country: country.trim() || "香港",
        targets,
        max: maxN,
        source,
        engine,
        region: source === "webs" ? "hk" : "any",
        allowRecrawl,
        savePhotos,
      });
      if (r.error) {
        setBusy(false);
        toast.push(r.error, "error");
        return;
      }
      setTaskId(r.task_id);
      watchTask("crawler", r.task_id, handleTask, api.crawler.task);
    } catch (e) {
      setBusy(false);
      toast.push(e instanceof Error ? e.message : "启动失败", "error");
    }
  }, [queries, country, targets, maxN, source, engine, allowRecrawl, savePhotos, handleTask, toast.push]);

  const stop = useCallback(async () => {
    if (!taskId || !busy) return;
    try {
      const r = await api.crawler.cancel(taskId);
      if (!r.ok) toast.push(r.error || "取消失败", "error");
      else toast.push("正在取消：当前这家处理完即停…", "info");
    } catch {
      /* ignore */
    }
  }, [taskId, busy, toast.push]);

  const importToCustomers = useCallback(async (results: CrawlerResult[]) => {
    if (!results.length) return;
    const header = "name,country,city,website,email,phone,wa_link,instrument,instruments,gap_recs";
    const lines = results.map((r) =>
      [r.name, r.country, r.city, r.website, r.email, r.phone, r.wa_link, r.instrument, r.instruments || "", r.gap_recs || ""]
        .map((v) => `"${String(v).replace(/"/g, '""')}"`)
        .join(","),
    );
    setImporting(true);
    try {
      const r = await api.customers.importCsv([header, ...lines].join("\n"), source);
      if (r.ok) {
        const added = r.added || 0;
        const duplicates = r.duplicates || 0;
        if (added === 0 && duplicates > 0) {
          toast.push(`这批客户已经导入过了，已自动跳过 ${duplicates} 家`, "info");
        } else if (duplicates > 0) {
          toast.push(`新增 ${added} 家，自动跳过重复 ${duplicates} 家（共 ${r.total}）`, "ok");
        } else {
          toast.push(`已导入 ${added} 家到客户名单（共 ${r.total}）`, "ok");
        }
      } else {
        toast.push(r.error || "导入失败", "error");
      }
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "导入失败", "error");
    } finally {
      setImporting(false);
    }
  }, [source, toast.push]);

  const download = useCallback(() => {
    if (!state?.results.length) return;
    if (source === "webs") {
      const header = "name,website,email,phone,whatsapp,facebook,instagram,instruments,gap_recs";
      const lines = state.results.map((r) =>
        [r.name, r.website, r.email, r.phone, r.whatsapp || "", r.facebook || "", r.instagram || "", r.instruments || "", r.gap_recs || ""]
          .map((v) => `"${String(v).replace(/"/g, '""')}"`)
          .join(","),
      );
      downloadText(`webs_${country || "hk"}_${Date.now()}.csv`, [header, ...lines].join("\n"));
      return;
    }
    const header = "name,country,city,website,email,phone,wa_link,instrument,instruments,gap_recs";
    const lines = state.results.map((r) =>
      [r.name, r.country, r.city, r.website, r.email, r.phone, r.wa_link, r.instrument, r.instruments || "", r.gap_recs || ""]
        .map((v) => `"${String(v).replace(/"/g, '""')}"`)
        .join(","),
    );
    downloadText(`maps_${country || "hk"}_${Date.now()}.csv`, [header, ...lines].join("\n"));
  }, [state, country, source]);

  const waCount = state?.results.filter((r) => r.wa_link || r.whatsapp).length ?? 0;
  const emailCount = state?.results.filter((r) => r.email && !r.email.includes("WhatsApp")).length ?? 0;
  const instrCount = state?.results.filter((r) => r.instrument === "yes" || (r.instruments || "").trim()).length ?? 0;
  const selectedCustomers = state?.results.filter((result, index) => selectedResults.has(resultKey(result, index))) ?? [];
  const allResultsSelected = Boolean(state?.results.length) && selectedCustomers.length === state?.results.length;

  const toggleResult = useCallback((key: string) => {
    setSelectedResults((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  return (
    <>
      <div className="page-head">
        <div className="page-title">
          爬虫 <span className="seal">找客户</span>
        </div>
        <div className="page-desc">Maps：Google Maps 搜美容院抓官网邮箱/电话/WhatsApp；Webs：网页搜索进官网深挖邮箱/电话/WhatsApp/社媒/仪器品牌 + 缺品分析（源码版可用，需代理）</div>
      </div>

      <div className="card">
        <div className="card-title">
          <span>搜索设置</span>
          <span style={{ display: "flex", gap: 8, marginLeft: 16 }}>
            {([["maps", "Google Maps"], ["webs", "Webs（官网深挖）"]] as const).map(([k, label]) => (
              <button
                key={k}
                className={`badge ${source === k ? (k === "maps" ? "badge-maps" : "badge-webs") : "badge-neutral"}`}
                style={{ cursor: "pointer", fontSize: 12 }}
                onClick={() => switchSource(k)}
              >
                <span className="source-dot" aria-hidden="true" />
                {label}
              </button>
            ))}
          </span>
        </div>
        <div className="row" style={{ marginBottom: 14 }}>
          <div className="field" style={{ flex: 2 }}>
            <label className="field-label">搜索词（逗号分隔，多个词自动去重合并）</label>
            <input className="input" value={queries} onChange={(e) => setQueries(e.target.value)} placeholder="醫學美容,醫美診所,美容中心 儀器" />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label className="field-label">国家 / 地区</label>
            <input className="input" value={country} onChange={(e) => setCountry(e.target.value)} placeholder="香港" />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label className="field-label">最多处理（家）</label>
            <input className="input" type="number" min={1} max={60} value={maxN} onChange={(e) => setMaxN(Number(e.target.value) || 20)} />
          </div>
        </div>
        <div className="field" style={{ marginBottom: 14 }}>
          <label className="field-label">目标类型</label>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {TARGETS.map((t) => (
              <button
                key={t.key}
                type="button"
                className={`badge ${targets.includes(t.key) ? (t.key === "whatsapp" ? "badge-whatsapp" : "badge-ok") : "badge-neutral"}`}
                style={{ cursor: "pointer" }}
                onClick={() => setTargets([t.key])}
                title={t.hint}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div className="field-hint">
            {source === "webs"
              ? "Webs 模式锁定 .hk 域名，出有官网的正规机构，联系方式+仪器清单+缺品推荐"
              : targets[0] === "all" ? "邮箱 + 电话/WhatsApp 都要，无联系方式的店自动跳过" :
                targets[0] === "email" ? "只保留官网挖到邮箱的店（成功率最低但最精准）" :
                "只保留有电话或 WhatsApp 的店（香港主力通道）"}
          </div>
        </div>
        <div className="row" style={{ marginBottom: 14 }}>
          {source === "webs" && (
            <div className="field" style={{ flex: 1 }}>
              <label className="field-label">搜索引擎</label>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {([["auto", "自动"], ["google", "Google"], ["bing", "Bing"], ["ddg", "DDG"]] as const).map(([k, label]) => (
                <button
                  key={k}
                  type="button"
                  className={`badge ${engine === k ? "badge-ok" : "badge-neutral"}`}
                  style={{ cursor: "pointer" }}
                  onClick={() => setEngine(k)}
                  title={k === "auto" ? "DDG 优先，候选不足时合并 Google 与 Bing" : k === "ddg" ? "轻量搜索，香港站效果较稳定" : ""}
                >
                  {label}
                </button>
              ))}
              </div>
            </div>
          )}
          <div className="field" style={{ flex: 1 }}>
            <label className="field-label">历史客户</label>
            <label className={`recrawl-toggle${allowRecrawl ? " is-active" : ""}`}>
              <input type="checkbox" checked={allowRecrawl} onChange={(event) => setAllowRecrawl(event.target.checked)} />
              <span>允许重新爬取旧客户</span>
            </label>
            <div className="field-hint">
              {allowRecrawl
                ? "复查模式：结果可能与之前重复"
                : source === "maps"
                  ? "默认按 Maps 地点、官网、电话和名称跳过旧客户"
                  : "默认按官网跳过旧客户，候选耗尽时不会用旧客户凑数"}
              </div>
          </div>
          {source === "maps" && (
            <div className="field" style={{ flex: 1 }}>
              <label className="field-label">视觉照片</label>
              <label className={`recrawl-toggle${savePhotos ? " is-active" : ""}`}>
                <input type="checkbox" checked={savePhotos} onChange={(event) => setSavePhotos(event.target.checked)} />
                <span>保存识别照片</span>
              </label>
              <div className="field-hint">
                {savePhotos
                  ? "识别后保存到 data/crawler_photos，方便人工抽查"
                  : "仅进行视觉识别，不在本地保存照片"}
              </div>
            </div>
          )}
        </div>
        <div className="crawler-primary-actions">
          <button className="btn btn-navy" onClick={start} disabled={busy}>
            <SearchIcon size={15} />
            {busy ? "爬取中…（约 30-60 秒/家）" : "开始爬取"}
          </button>
          {busy && (
            <button className="btn btn-ghost" onClick={stop} style={{ borderColor: "var(--red)", color: "var(--red)" }}>
              取消爬取
            </button>
          )}
          {state?.results.length ? <button className="btn btn-ghost" onClick={download} disabled={busy}>下载 CSV</button> : null}
        </div>
      </div>

      {state && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-title">
            <span>
              爬取日志
              {state.status === "running" && <span className="badge badge-warn" style={{ marginLeft: 8 }}>进行中…</span>}
              {state.status === "done" && <span className="badge badge-ok" style={{ marginLeft: 8 }}>完成</span>}
              {state.status === "cancelled" && <span className="badge badge-warn" style={{ marginLeft: 8 }}>已取消</span>}
              {state.status === "error" && <span className="badge badge-warn" style={{ marginLeft: 8 }}>失败</span>}
            </span>
          </div>
          <div
            className="crawler-log"
            ref={logRef}
            role="log"
            aria-label="爬取日志"
            aria-live="polite"
            tabIndex={0}
          >
            {state.log.map((ln, i) => (
              <div key={i} className={ln.includes("邮箱") ? "log-ok" : ln.includes("跳过") ? "log-muted" : ""}>{ln}</div>
            ))}
            {state.status === "running" && state.log.length === 0 && <div className="log-muted">启动浏览器…（需要代理 Verge，每家用 30-60 秒）</div>}
            {state.status === "error" && (
              <div style={{ color: "var(--red)", marginTop: 6 }}>✗ {state.error || "爬虫失败"}（完整日志见 data/crawler_errors.log）</div>
            )}
            {state.status === "done" && (
              <div className="log-ok" style={{ marginTop: 6 }}>
                爬取完成：命中 {state.results.length} 家
                {state.results.length === 0 && (!allowRecrawl
                  ? "（没有找到符合条件的新客户，可换搜索词或开启重新爬取）"
                  : "（香港店公开邮箱/电话稀缺，可换搜索词或切目标类型 WhatsApp 再试）")}
              </div>
            )}
          </div>
        </div>
      )}

      {state?.results.length ? (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-title">
            <span>结果（{state.results.length} 家）</span>
            <span style={{ display: "flex", gap: 8, fontSize: 12 }}>
              <span className="badge badge-email">邮箱 {emailCount}</span>
              <span className="badge badge-wa">WhatsApp {waCount}</span>
              <span className="badge badge-neutral">仪器项目 {instrCount}</span>
            </span>
          </div>
          {state.status === "done" && (
            <div className="crawler-import-bar" aria-label="客户导入设置">
              <span className="crawler-selection-count">已选择 {selectedCustomers.length} / {state.results.length} 家</span>
              <button className="btn btn-ghost btn-sm" onClick={() => setSelectedResults(allResultsSelected ? new Set() : new Set(state.results.map(resultKey)))}>
                {allResultsSelected ? "取消全选" : "全选商家"}
              </button>
              <button className="btn btn-primary btn-sm" disabled={importing || !selectedCustomers.length} onClick={() => importToCustomers(selectedCustomers)}>
                {importing ? "正在查重并导入…" : `导入所选（${selectedCustomers.length} 家）`}
              </button>
              <button className="btn btn-navy btn-sm" disabled={importing} onClick={() => importToCustomers(state.results)}>
                一键导入全部（{state.results.length} 家）
              </button>
              <span className="crawler-import-note">按当前结果顺序导入，重复客户会自动跳过</span>
            </div>
          )}
          <div style={{ maxHeight: 420, overflowY: "auto", border: "1px solid var(--line)", borderRadius: "var(--r-sm)" }}>
            <table className="table">
              <thead>
                <tr>
                  <th className="select-cell">
                    <input
                      className="selection-checkbox"
                      type="checkbox"
                      aria-label="选择全部商家"
                      checked={allResultsSelected}
                      onChange={() => setSelectedResults(allResultsSelected ? new Set() : new Set(state.results.map(resultKey)))}
                    />
                  </th>
                  <th>名称</th>
                  <th>邮箱</th>
                  <th>电话</th>
                  <th>WhatsApp</th>
                  <th>仪器</th>
                  {source !== "webs" && <th>照片识别</th>}
                  <th>缺品推荐</th>
                </tr>
              </thead>
              <tbody>
                {state.results.map((r, i) => {
                  const realEmail = r.email && !r.email.includes("WhatsApp") ? r.email : "";
                  return (
                    <tr key={i} className={selectedResults.has(resultKey(r, i)) ? "is-selected" : ""}>
                      <td className="select-cell">
                        <input
                          className="selection-checkbox"
                          type="checkbox"
                          aria-label={`选择 ${r.name}`}
                          checked={selectedResults.has(resultKey(r, i))}
                          onChange={() => toggleResult(resultKey(r, i))}
                        />
                      </td>
                      <td className="cell-name">{r.name}</td>
                      <td className="cell-email">{realEmail || "—"}</td>
                      <td className="cell-muted">{r.phone || "—"}</td>
                      <td>
                        {r.wa_link ? (
                          <a href={r.wa_link} target="_blank" rel="noreferrer" className="badge badge-wa" style={{ textDecoration: "none", cursor: "pointer" }}>
                            wa.me ↗
                          </a>
                        ) : (
                          <span className="cell-muted">—</span>
                        )}
                      </td>
                      <td className="cell-muted">
                        {r.instruments ? (
                          <span className="badge badge-ok">{r.instruments}</span>
                        ) : r.instrument === "yes" ? (
                          "仪器✓"
                        ) : r.instrument === "massage" ? (
                          "按摩✗"
                        ) : (
                          "未知"
                        )}
                      </td>
                      {source !== "webs" && (
                        <td className="cell-muted" title={r.photo_analysis || ""}>
                          {r.photo_analysis ? (
                            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", display: "block", maxWidth: 150 }}>{r.photo_analysis}</span>
                          ) : (
                            "—"
                          )}
                        </td>
                      )}
                      <td className="cell-muted">
                        {r.gap_recs ? (
                          <details className="crawler-gap-details">
                            <summary title={r.gap_recs}>
                              <span className="crawler-gap-preview">{r.gap_recs}</span>
                              <span className="crawler-gap-collapse">收起推荐</span>
                            </summary>
                            <div>{r.gap_recs}</div>
                          </details>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {state.status === "done" && (
            <div className="field-hint" style={{ marginTop: 12 }}>
              提示：结果已按目标类型过滤。可按数量导入或一键导入全部，再去「批量生成」勾选客户发开发信 / WA 短消息
            </div>
          )}
        </div>
      ) : null}

      {!state && !busy && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="empty">
            <RadarIcon size={30} />
            <div className="empty-title">还没开始爬</div>
            <div className="empty-desc">填好搜索词，点「开始爬取」。每家用 30-60 秒（详情页 + 官网挖邮箱），20 家约 15-20 分钟，可切到其他 Tab 干别的，日志会继续滚</div>
          </div>
        </div>
      )}
      {toast.el}
    </>
  );
}

// 占位注释：RadarIcon 已加到 Icons.tsx
