import { useCallback, useEffect, useState } from "react";
import { api, copyText, downloadText } from "../api";
import { Elapsed, NumberTicker, SpotlightCard, useToast } from "../components/effects";
import { BatchIcon, CopyIcon, DownloadIcon, XIcon } from "../components/Icons";
import ProductCombo, { TemplateCombo } from "../components/Combo";
import { currentTaskId, forgetTask, resumeTask, watchTask } from "../tasks";
import type { Customer, Lang, OutFormat, Product, TaskState, Template } from "../types";
import { formatLocation } from "../location";

export default function BatchTab() {
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [product, setProduct] = useState("");
  const [lang, setLang] = useState<Lang>("zh-hant");
  const [format, setFormat] = useState<OutFormat>("email");
  const [products, setProducts] = useState<Product[]>([]);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [templateId, setTemplateId] = useState("");
  const [taskId, setTaskId] = useState("");
  const [task, setTask] = useState<TaskState | null>(null);
  const [running, setRunning] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const toast = useToast();

  const loadCustomers = useCallback(() => {
    api.customers.list().then(setCustomers).catch(() => {});
  }, []);

  useEffect(() => {
    loadCustomers();
  }, [loadCustomers]);

  useEffect(() => {
    api.products.list().then(setProducts).catch(() => {});
    api.templates.list().then(setTemplates).catch(() => {});
  }, []);

  // 任务状态处理（进度 + 流式 + 结束），全局轮询回调
  const handleTask = useCallback((t: TaskState) => {
    setTask(t);
    if (t.status === "running") setRunning(true);
    else if (t.status === "done") {
      setRunning(false);
      toast.push(`批量完成：${t.results.length} 封`, "ok");
      loadCustomers();
    } else if (t.status === "cancelled") {
      setRunning(false);
      toast.push(`已停止：完成 ${t.results.length} 封，已取消剩余`, "info");
    } else if (t.status === "error") {
      setRunning(false);
      toast.push(t.error || "批量失败", "error");
    }
  }, [loadCustomers, toast.push]);

  // 挂载时恢复：批量生成中途切走再切回，进度不丢
  useEffect(() => {
    setTaskId(currentTaskId("batch"));
    const unsub = resumeTask("batch", handleTask);
    return () => {
      unsub?.();
    };
  }, [handleTask]);

  const start = useCallback(async () => {
    const rows = customers
      .filter((c) => selected.has(c.id))
      .map((c) => ({ name: c.name, country: c.country, notes: c.notes }));
    if (rows.length === 0) {
      toast.push("先从客户名单勾选客户", "error");
      return;
    }
    setRunning(true);
    setTask(null);
    setExpanded(new Set());
    try {
      const { task_id, error } = await api.startBatch(rows, product.trim() || "医美设备", false, lang, format, templateId);
      if (!task_id) throw new Error(error || "启动失败");
      setTaskId(task_id);
      setTask({ status: "running", total: rows.length, done: 0, current: "", results: [] });
      watchTask("batch", task_id, handleTask);
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "启动失败", "error");
      setRunning(false);
    }
  }, [customers, selected, product, handleTask, toast.push, lang, format, templateId]);

  const stop = useCallback(async () => {
    if (!taskId || !running) return;
    try {
      const r = await api.cancelTask(taskId);
      if (!r.ok) toast.push(r.error || "停止失败", "error");
      else toast.push("正在停止，当前这一封写完即停…", "info");
    } catch { /* ignore */ }
  }, [taskId, running, toast.push]);

  const toggleSelect = useCallback((id: string) => {
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }, []);

  const toggleAll = useCallback(() => {
    setSelected((s) => (s.size === customers.length ? new Set() : new Set(customers.map((c) => c.id))));
  }, [customers]);

  const toggleExpand = useCallback((i: number) => {
    setExpanded((s) => {
      const n = new Set(s);
      if (n.has(i)) n.delete(i);
      else n.add(i);
      return n;
    });
  }, []);

  const copyOne = useCallback(async (text: string) => {
    (await copyText(text)) ? toast.push("已复制", "ok") : toast.push("复制失败", "error");
  }, [toast.push]);

  const clearCurrent = useCallback(() => {
    forgetTask("batch", taskId);
    setTask(null);
    setTaskId("");
    setExpanded(new Set());
    toast.push("已清空本批结果，历史记录仍保留", "info");
  }, [taskId, toast.push]);

  const progress = task ? (task.total ? Math.round((task.done / task.total) * 100) : 0) : 0;

  return (
    <>
      <div className="page-head">
        <div className="page-title">
          批量生成 <span className="seal">一次出 N 封</span>
        </div>
        <div className="page-desc">从客户名单勾选，批量生成开发信，逐封检查与复制</div>
      </div>

      <div className="card">
        <div className="card-title">
          <span>1 · 选择客户与产品</span>
          {customers.length > 0 && (
            <button className="btn btn-ghost btn-sm" onClick={toggleAll}>
              {selected.size === customers.length ? "取消全选" : `全选（${customers.length}）`}
            </button>
          )}
        </div>
        <div className="batch-controls">
          <div className="field batch-product-field">
            <label className="field-label">产品</label>
            <ProductCombo value={product} onChange={setProduct} products={products} placeholder="激光脱毛仪 / 皮秒 / 水光" hint={`可输入或从产品资料选择（${products.length} 款）`} />
          </div>
          <div className="field batch-template-field">
            <label className="field-label">话术模板</label>
            <TemplateCombo value={templateId} onChange={setTemplateId} templates={templates} />
          </div>
          <div className="field batch-format-field">
            <label className="field-label">语言 / 形态</label>
            <div className="batch-format-options">
              {([["zh-hant", "繁体"], ["en", "English"]] as [Lang, string][]).map(([v, label]) => (
                <button key={v} type="button" className={`badge ${lang === v ? "badge-ok" : "badge-neutral"}`}
                  style={{ cursor: "pointer" }} onClick={() => setLang(v)}>{label}</button>
              ))}
              <span style={{ width: 1, background: "var(--line)", margin: "0 4px" }} />
              {([["email", "开发信"], ["whatsapp", "WA 短消息"]] as [OutFormat, string][]).map(([v, label]) => (
                <button key={v} type="button" className={`badge ${format === v ? "badge-ok" : "badge-neutral"}`}
                  style={{ cursor: "pointer" }} onClick={() => setFormat(v)}>{label}</button>
              ))}
            </div>
            <div className="field-hint">邮件用开发信；聊天用 WA 短消息（150 字内）</div>
          </div>
          <div className="field batch-selected-field">
            <label className="field-label">已选</label>
            <div style={{ display: "flex", alignItems: "center", height: 38 }}>
              <NumberTicker value={selected.size} /> <span style={{ marginLeft: 6, color: "var(--ink-3)", fontSize: 13 }}>/ {customers.length} 位客户</span>
            </div>
          </div>
        </div>
        {customers.length === 0 ? (
          <div className="empty" style={{ padding: 24 }}>
            <div className="empty-desc">名单为空，先去「客户名单」导入 CSV 或添加客户</div>
          </div>
        ) : (
          <div style={{ maxHeight: 260, overflowY: "auto", border: "1px solid var(--line)", borderRadius: "var(--r-sm)" }}>
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 36 }} />
                  <th>客户</th>
                  <th>国家</th>
                  <th>邮箱</th>
                </tr>
              </thead>
              <tbody>
                {customers.map((c) => (
                  <tr
                    key={c.id}
                    className={selected.has(c.id) ? "is-selected" : undefined}
                    onClick={() => toggleSelect(c.id)}
                    style={{ cursor: "pointer" }}
                  >
                    <td className="select-cell">
                      <input
                        className="selection-checkbox"
                        type="checkbox"
                        checked={selected.has(c.id)}
                        onChange={() => toggleSelect(c.id)}
                        onClick={(event) => event.stopPropagation()}
                        aria-label={`选择客户 ${c.name}`}
                      />
                    </td>
                    <td className="cell-name">{c.name}</td>
                    <td className="cell-muted">{formatLocation(c.country, c.city)}</td>
                    <td className="cell-email">{c.email || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div style={{ marginTop: 16, display: "flex", gap: 10 }}>
          <button className="btn btn-navy" onClick={start} disabled={running || selected.size === 0}>
            <BatchIcon size={16} />
            {running ? "批量生成中…" : `开始批量生成（${selected.size} 封）`}
          </button>
          {running && (
            <button
              className="btn btn-ghost"
              onClick={stop}
              style={{ borderColor: "var(--red)", color: "var(--red)" }}
            >
              <XIcon size={15} /> 停止批量
            </button>
          )}
        </div>
      </div>

      {task && task.status === "running" && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-title">
            <span>2 · 生成进度</span>
            <span className="badge badge-neutral">
              {task.done}/{task.total} · <Elapsed active={task.status === "running"} /> · {task.current ? `正在写：${task.current}` : "排队中"}
            </span>
          </div>
          <div className="progress">
            <div style={{ width: `${progress}%` }} />
          </div>
          {task.stream && (
            <div style={{ marginTop: 12 }}>
              <div className="email-body typing" style={{ fontSize: 13, padding: "12px 16px" }}>{task.stream}</div>
            </div>
          )}
        </div>
      )}

      {task?.results && task.results.length > 0 && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-title">
            <span>3 · 结果（{task.results.length} 封）</span>
            <span style={{ display: "flex", gap: 8, fontSize: 12, alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
              <span className="badge badge-ok">{task.results.filter((r) => r.ok && r.issues.length === 0).length} 自检通过</span>
              <span className="badge badge-warn">{task.results.filter((r) => r.ok && r.issues.length > 0).length} 有提示</span>
              <span className="badge badge-neutral">{task.results.filter((r) => !r.ok).length} 失败</span>
              {!running && (
                <button className="btn btn-ghost btn-sm" onClick={clearCurrent} title="只清空当前页面，历史记录不会删除">
                  <XIcon size={14} /> 清空本批
                </button>
              )}
            </span>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {task.results.map((r, i) => {
              const open = expanded.has(i);
              return (
                <SpotlightCard key={i} className="" style={{ padding: "14px 18px" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                    <button className="btn btn-ghost btn-sm" onClick={() => toggleExpand(i)} style={{ fontFamily: "var(--font-serif)", fontWeight: 600 }}>
                      {open ? "收起" : "展开"} · {r.name}
                    </button>
                    <span className="cell-muted">{r.country}</span>
                    {r.ok ? (
                      r.issues.length === 0 ? <span className="badge badge-ok">通过</span> : <span className="badge badge-warn">{r.issues.length} 项提示</span>
                    ) : (
                      <span className="badge badge-warn">失败</span>
                    )}
                    {r.scores?.overall ? <span className="badge badge-neutral">评分 {r.scores.overall}/5</span> : null}
                    {r.templates_used?.length ? <span className="badge badge-neutral">参考话术：{r.templates_used.join(" / ")}</span> : null}
                    <span className="spacer" />
                    {r.ok && (
                      <>
                        <button className="icon-btn" title="复制" onClick={() => copyOne(r.email)}><CopyIcon size={15} /></button>
                        <button className="icon-btn" title="下载" onClick={() => downloadText(`${r.name.replace(/[\\/:*?"<>|]/g, "_")}.txt`, r.email)}><DownloadIcon size={15} /></button>
                      </>
                    )}
                  </div>
                  {open && (
                    <div className="batch-result-detail">
                      {r.ok ? (
                        <>
                          <div className="email-body">{r.email}</div>
                          {r.issues.length > 0 && (
                            <div className="batch-issues" role="note">
                              <div className="batch-issues-title">自检提示</div>
                              <ul>
                                {r.issues.map((issue, issueIndex) => (
                                  <li key={`${issue}-${issueIndex}`}>{issue}</li>
                                ))}
                              </ul>
                            </div>
                          )}
                          {r.scores?.suggestions && (
                            <div className="batch-suggestion">
                              <span>改进建议</span>
                              {r.scores.suggestions}
                            </div>
                          )}
                        </>
                      ) : (
                        <div className="field-hint" style={{ color: "var(--red)" }}>{r.error}</div>
                      )}
                    </div>
                  )}
                </SpotlightCard>
              );
            })}
          </div>
          {task.status === "done" && task.results.length > 0 && (
            <div className="field-hint" style={{ marginTop: 14, textAlign: "center" }}>
              生成内容已存入历史记录，可去「历史记录」统一回顾 · 单封耗时约 15-25 秒
            </div>
          )}
        </div>
      )}
      {toast.el}
    </>
  );
}
