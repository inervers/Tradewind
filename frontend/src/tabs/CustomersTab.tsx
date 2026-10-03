import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import CustomerFollowupPanel from "../components/CustomerFollowupPanel";
import { FadeIn, NumberTicker, useToast } from "../components/effects";
import { MailIcon, PenIcon, PlusIcon, RouteIcon, TrashIcon, UploadIcon, XIcon } from "../components/Icons";
import type { ComposeSeed, Customer, CustomerStatus, FollowupSummary } from "../types";
import { DUE_LABEL, contactTime } from "../followup";
import { formatLocation } from "../location";
import { internationalPhone, waLink } from "../outreach";

const STATUS_LABEL: Record<CustomerStatus, string> = { new: "未发", sent: "已发", replied: "已回复" };

const EMPTY_FORM = { name: "", country: "", city: "", email: "", website: "", phone: "", notes: "" };

interface Props {
  onStartOutreach: (seed: ComposeSeed) => void;
  onFollowupDirtyChange?: (dirty: boolean) => void;
  onFollowupBusyChange?: (busy: boolean) => void;
}

export default function CustomersTab({ onStartOutreach, onFollowupDirtyChange, onFollowupBusyChange }: Props) {
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [showAdd, setShowAdd] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [editId, setEditId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState(EMPTY_FORM);
  const [csvText, setCsvText] = useState("");
  const [importing, setImporting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const [del, setDel] = useState<Customer | null>(null);
  const [delBusy, setDelBusy] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);
  const [batchDeleteBusy, setBatchDeleteBusy] = useState(false);
  const [expandedNotes, setExpandedNotes] = useState<Set<string>>(new Set());
  const [srcFilter, setSrcFilter] = useState<"all" | "manual" | "maps" | "webs">("all");
  const [dueFilter, setDueFilter] = useState<"all" | "today" | "overdue" | "unplanned">("all");
  const [summaries, setSummaries] = useState<Record<string, FollowupSummary>>({});
  const [summaryError, setSummaryError] = useState("");
  const [summaryReady, setSummaryReady] = useState(false);
  const [listError, setListError] = useState("");
  const [followupCustomer, setFollowupCustomer] = useState<Customer | null>(null);
  const [pendingFollowup, setPendingFollowup] = useState<Customer | null>(null);
  const [cleanupRetry, setCleanupRetry] = useState<string[]>([]);
  const [cleanupError, setCleanupError] = useState("");
  const followupDirty = useRef(false), followupBusy = useRef(false);
  const mounted = useRef(true), summaryRequest = useRef(0), listRequest = useRef(0);
  const toast = useToast();

  const load = useCallback(() => {
    const request = ++listRequest.current;
    api.customers.list().then(items => { if (mounted.current && request === listRequest.current) { setCustomers(items); setListError(""); } }).catch(e => { if (mounted.current && request === listRequest.current) setListError(e instanceof Error ? e.message : "名单读取失败"); });
  }, []);
  const loadSummary = useCallback(() => {
    const request = ++summaryRequest.current;
    api.followups.summary().then(result => {
      if (!mounted.current || request !== summaryRequest.current) return;
      setSummaries(Object.fromEntries(result.items.map(item => [item.customer_id, item]))); setSummaryError(""); setSummaryReady(true);
    }).catch(e => { if (mounted.current && request === summaryRequest.current) { setSummaryError(e instanceof Error ? e.message : "跟进摘要读取失败"); setSummaryReady(false); } });
  }, []);
  const changed = useCallback(() => { loadSummary(); }, [loadSummary]);
  const dirtyChanged = useCallback((dirty: boolean) => { followupDirty.current = dirty; onFollowupDirtyChange?.(dirty); }, [onFollowupDirtyChange]);
  const busyChanged = useCallback((busy: boolean) => { followupBusy.current = busy; onFollowupBusyChange?.(busy); }, [onFollowupBusyChange]);
  const openFollowup = (customer: Customer) => {
    if (followupBusy.current || customer.id === followupCustomer?.id) return;
    if (followupDirty.current) setPendingFollowup(customer); else setFollowupCustomer(customer);
  };

  useEffect(() => {
    mounted.current = true;
    load();
    const focus = () => { load(); loadSummary(); };
    window.addEventListener("focus", focus);
    const interval = window.setInterval(loadSummary, 60_000);
    return () => { mounted.current = false; window.removeEventListener("focus", focus); window.clearInterval(interval); };
  }, [load, loadSummary]);
  const customerIdsKey = customers.map(c => c.id).join("\0");
  useEffect(() => { loadSummary(); }, [customerIdsKey, loadSummary]);

  const sourceCustomers = srcFilter === "all" ? customers : customers.filter((c) => (c.source || "manual") === srcFilter);
  const filtered = dueFilter === "all" ? sourceCustomers : sourceCustomers.filter(c => summaries[c.id]?.due_status === dueFilter);
  const filteredIds = filtered.map((c) => c.id);
  const visibleIdsKey = filteredIds.join("\0");
  useEffect(() => {
    setSelected(previous => {
      const visible = new Set(visibleIdsKey.split("\0"));
      const next = new Set([...previous].filter(id => visible.has(id)));
      return next.size === previous.size ? previous : next;
    });
  }, [visibleIdsKey]);
  const allFilteredSelected = filteredIds.length > 0 && filteredIds.every((id) => selected.has(id));

  const stats = {
    total: filtered.length,
    withEmail: filtered.filter((c) => c.email).length,
    sent: filtered.filter((c) => c.status === "sent" || c.status === "replied").length,
    replied: filtered.filter((c) => c.status === "replied").length,
  };

  const add = useCallback(async () => {
    if (!form.name.trim()) {
      toast.push("客户名称必填", "error");
      return;
    }
    try {
      const r = await api.customers.add(form);
      if (!r.ok) {
        toast.push(r.duplicate ? "该客户已在名单中，没有重复添加" : (r.error || "添加失败"), r.duplicate ? "info" : "error");
        return;
      }
      setForm(EMPTY_FORM);
      setShowAdd(false);
      toast.push("已添加", "ok");
      if (r.item) setCustomers((items) => [...items, r.item as Customer]);
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "添加失败", "error");
    }
  }, [form, toast.push]);

  const startEdit = useCallback((c: Customer) => {
    setEditId(c.id);
    setEditForm({ name: c.name, country: c.country, city: c.city, email: c.email, website: c.website, phone: c.phone || "", notes: c.notes });
  }, []);

  const saveEdit = useCallback(async () => {
    if (!editId) return;
    try {
      const r = await api.customers.update(editId, editForm);
      setEditId(null);
      toast.push("已保存", "ok");
      if (r.item) setCustomers((items) => items.map((item) => item.id === editId ? r.item as Customer : item));
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "保存失败", "error");
    }
  }, [editId, editForm, toast.push]);

  const confirmRemove = useCallback(async () => {
    if (!del) return;
    setDelBusy(true);
    try {
      const result = await api.customers.remove(del.id);
      if (!result.ok && !result.customer_deleted) throw new Error(result.error || "删除失败");
      if (!result.ok) { setCleanupRetry([del.id]); setCleanupError(result.error || "客户已删除，但跟进记录清理失败"); }
      else toast.push("已删除", "ok");
      setCustomers((items) => items.filter((item) => item.id !== del.id));
      setSelected((ids) => {
        const next = new Set(ids);
        next.delete(del.id);
        return next;
      });
      setDel(null);
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "删除失败", "error");
    } finally {
      setDelBusy(false);
    }
  }, [del, toast.push]);

  const toggleSelected = useCallback((id: string) => {
    setSelected((ids) => {
      const next = new Set(ids);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleNotes = useCallback((id: string) => {
    setExpandedNotes((expanded) => {
      const next = new Set(expanded);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleAllFiltered = useCallback(() => {
    setSelected((ids) => {
      const next = new Set(ids);
      if (filteredIds.length > 0 && filteredIds.every((id) => next.has(id))) {
        filteredIds.forEach((id) => next.delete(id));
      } else {
        filteredIds.forEach((id) => next.add(id));
      }
      return next;
    });
  }, [filteredIds]);

  const confirmBatchRemove = useCallback(async () => {
    const visible = new Set(filteredIds);
    const ids = [...selected].filter(id => visible.has(id));
    if (!ids.length) return;
    setBatchDeleteBusy(true);
    try {
      let removed = 0;
      let cleanupPending = false;
      try {
        const r = await api.customers.removeBatch(ids);
        if (!r.ok && !r.customer_deleted) throw new Error(r.error || "批量删除接口不可用");
        if (!r.ok) { cleanupPending = true; setCleanupRetry(ids); setCleanupError(r.error || "客户已删除，但跟进记录清理失败"); }
        removed = r.removed || 0;
      } catch (error) {
        throw new Error(
          error instanceof Error && error.message.includes("请求失败")
            ? "批量删除接口尚未加载，请重启后端后重试"
            : (error instanceof Error ? error.message : "批量删除失败"),
        );
      }
      const removedIds = new Set(ids);
      setCustomers((items) => items.filter((item) => !removedIds.has(item.id)));
      setSelected(new Set());
      setBatchDeleteOpen(false);
      toast.push(cleanupPending ? `已删除 ${removed} 位，跟进清理需要重试` : `已删除 ${removed} 位客户`, cleanupPending ? "error" : "ok");
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "批量删除失败", "error");
    } finally {
      setBatchDeleteBusy(false);
    }
  }, [selected, filteredIds, toast.push]);

  const setStatus = useCallback(async (c: Customer, status: CustomerStatus) => {
    try {
      await api.customers.update(c.id, { ...c, status });
      load();
    } catch { /* ignore */ }
  }, [load]);

  const doImport = useCallback(async (text: string) => {
    if (!text.trim()) return;
    setImporting(true);
    try {
      const r = await api.customers.importCsv(text);
      if (r.ok) {
        const added = r.added || 0;
        const duplicates = r.duplicates || 0;
        if (r.items?.length) {
          setCustomers((current) => {
            const currentIds = new Set(current.map((item) => item.id));
            return [...current, ...r.items!.filter((item) => !currentIds.has(item.id))];
          });
        }
        if (added === 0 && duplicates > 0) {
          toast.push(`没有重复导入：${duplicates} 位客户已在名单中`, "info");
        } else if (duplicates > 0) {
          toast.push(`导入完成：新增 ${added} 位，跳过重复 ${duplicates} 位`, "ok");
        } else {
          toast.push(`导入完成：新增 ${added} 位，共 ${r.total} 位`, "ok");
        }
        setCsvText("");
        setShowImport(false);
      } else {
        toast.push(r.error || "导入失败", "error");
      }
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "导入失败", "error");
    } finally {
      setImporting(false);
    }
  }, [toast.push]);

  const onFile = useCallback((f: File | undefined) => {
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => doImport(String(reader.result || ""));
    reader.readAsText(f, "utf-8");
  }, [doImport]);

  return (
    <>
      <div className="page-head">
        <div className="page-title">
          客户名单 <span className="seal">线索池</span>
        </div>
        <div className="page-desc">管理目标美容院，记录实际沟通、安排下次联系，不遗漏该跟进的客户</div>
      </div>

      {(listError || summaryError) && <div className="followup-list-error" role="alert">{listError || `跟进摘要读取失败：${summaryError}`}<button className="btn btn-ghost" onClick={() => { load(); loadSummary(); }}>重试读取</button></div>}
      {cleanupError && <div className="followup-list-error" role="alert">{cleanupError}<button className="btn btn-ghost" onClick={async () => {
        try { const result = await api.customers.removeBatch(cleanupRetry); if (!result.ok) throw new Error(result.error || "清理失败"); setCleanupRetry([]); setCleanupError(""); loadSummary(); }
        catch (e) { setCleanupError(e instanceof Error ? e.message : "清理失败"); }
      }}>重试跟进清理</button></div>}

      <div className="followup-filters" aria-label="跟进日期筛选">
        {([["all", "全部跟进"], ["today", "今日跟进"], ["overdue", "逾期跟进"], ["unplanned", "未安排跟进"]] as const).map(([key, label]) => <button type="button" key={key} className={`btn btn-ghost${dueFilter === key ? " is-active" : ""}`} aria-pressed={dueFilter === key} disabled={key !== "all" && !summaryReady} onClick={() => { setDueFilter(key); setSelected(new Set()); }}>{label}<span>{key === "all" ? sourceCustomers.length : summaryReady ? sourceCustomers.filter(c => summaries[c.id]?.due_status === key).length : "—"}</span></button>)}
      </div>

      <div className="card" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 12, padding: "18px 24px" }}>
        {[
          { label: "客户总数", value: stats.total },
          { label: "有邮箱", value: stats.withEmail },
          { label: "已发送", value: stats.sent },
          { label: "已回复", value: stats.replied },
        ].map((m) => (
          <div key={m.label}>
            <div style={{ fontSize: 22, fontWeight: 600, color: "var(--amber)", fontVariantNumeric: "tabular-nums" }}>
              <NumberTicker value={m.value} />
            </div>
            <div style={{ fontSize: 12, color: "var(--ink-3)" }}>{m.label}</div>
          </div>
        ))}
      </div>

      <div className="toolbar" style={{ marginTop: 16 }}>
        <button className="btn btn-primary" onClick={() => { setShowAdd(!showAdd); setShowImport(false); }}>
          <PlusIcon size={15} /> 添加客户
        </button>
        <button className="btn btn-ghost" onClick={() => { setShowImport(!showImport); setShowAdd(false); }}>
          <UploadIcon size={15} /> 导入 CSV
        </button>
        <span className="spacer" />
        <span style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          {([["all", "全部"], ["maps", "Maps"], ["webs", "Webs"], ["manual", "手动"]] as const).map(([k, label]) => (
            <button
              key={k}
              className={`badge ${srcFilter === k ? (k === "maps" ? "badge-maps" : k === "webs" ? "badge-webs" : "badge-ok") : "badge-neutral"}`}
              style={{ cursor: "pointer" }}
              onClick={() => { setSrcFilter(k); setSelected(new Set()); }}
            >
              {(k === "maps" || k === "webs") && <span className="source-dot" aria-hidden="true" />}
              {label}
            </button>
          ))}
        </span>
        <span className="cell-muted" style={{ marginLeft: 10 }}>{customers.length} 位 · 名单存于本地 data/customers.json，不上传</span>
      </div>

      {showAdd && (
        <FadeIn>
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-title"><span>添加客户</span><button className="icon-btn" onClick={() => setShowAdd(false)}><XIcon size={15} /></button></div>
            <div className="row">
              <div className="field"><label className="field-label">名称 *</label><input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
              <div className="field"><label className="field-label">国家</label><input className="input" value={form.country} onChange={(e) => setForm({ ...form, country: e.target.value })} /></div>
              <div className="field"><label className="field-label">城市</label><input className="input" value={form.city} onChange={(e) => setForm({ ...form, city: e.target.value })} /></div>
            </div>
            <div className="row" style={{ marginTop: 12 }}>
              <div className="field"><label className="field-label">邮箱</label><input className="input" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></div>
              <div className="field"><label className="field-label">官网</label><input className="input" value={form.website} onChange={(e) => setForm({ ...form, website: e.target.value })} /></div>
            </div>
            <div className="row" style={{ marginTop: 12 }}>
              <div className="field"><label className="field-label">电话（WhatsApp 跟进用）</label><input className="input" placeholder="+852 6xxx xxxx" value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} /></div>
              <div className="field"><label className="field-label">国家</label><input className="input" placeholder="香港" value={form.country} onChange={(e) => setForm({ ...form, country: e.target.value })} /></div>
            </div>
            <div className="field" style={{ marginTop: 12 }}>
              <label className="field-label">备注（客户画像，会影响邮件内容）</label>
              <textarea className="textarea" value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
            </div>
            <div style={{ marginTop: 14 }}><button className="btn btn-primary" onClick={add}>保存</button></div>
          </div>
        </FadeIn>
      )}

      {showImport && (
        <FadeIn>
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-title">
              <span>导入 CSV</span>
              <button className="icon-btn" onClick={() => setShowImport(false)}><XIcon size={15} /></button>
            </div>
            <div className="field-hint" style={{ marginBottom: 10 }}>
              列：name,country,city,email,website,phone,notes,instruments（爬虫导出的 instrument/instruments 列自动并入备注；首行表头，自动去重；香港电话自动生成 wa.me 链接）
            </div>
            <div className="row">
              <div className="field" style={{ flex: 3 }}>
                <textarea className="textarea" style={{ minHeight: 110, fontFamily: "var(--font-mono)", fontSize: 12.5 }} placeholder={"name,country,city,email,website,notes\nGlow Skin Clinic,Spain,Madrid,,glowskin.es,激光脱毛为主"} value={csvText} onChange={(e) => setCsvText(e.target.value)} />
              </div>
              <div className="field" style={{ flex: 1 }}>
                <button className="btn btn-navy" style={{ width: "100%" }} disabled={importing || !csvText.trim()} onClick={() => doImport(csvText)}>
                  {importing ? "导入中…" : "开始导入"}
                </button>
                <button className="btn btn-ghost" style={{ width: "100%" }} onClick={() => fileRef.current?.click()}>
                  选择 CSV 文件
                </button>
                <input ref={fileRef} type="file" accept=".csv,.txt" style={{ display: "none" }} onChange={(e) => { onFile(e.target.files?.[0]); e.target.value = ""; }} />
              </div>
            </div>
          </div>
        </FadeIn>
      )}

      <div className="card" style={{ padding: "8px 14px" }}>
        {filtered.length === 0 ? (
          <div className="empty">
            <div className="empty-title">{customers.length ? "这个筛选下没有客户" : "名单还是空的"}</div>
            <div className="empty-desc">{customers.length ? "可以切换来源或跟进日期筛选查看其他客户" : "添加客户，或从爬虫产出的 CSV 一键导入"}</div>
          </div>
        ) : (
          <>
            <div className={`selection-bar${selected.size ? " is-active" : ""}`}>
              <button className="btn btn-ghost btn-sm" onClick={toggleAllFiltered}>
                {allFilteredSelected ? "取消全选" : `全选当前 ${filtered.length} 位`}
              </button>
              <span className="selection-count">
                {selected.size ? `已选择 ${selected.size} 位客户` : "勾选客户后可批量操作"}
              </span>
              {selected.size > 0 && (
                <button className="btn btn-danger btn-sm" onClick={() => setBatchDeleteOpen(true)}>
                  <TrashIcon size={14} /> 批量删除
                </button>
              )}
            </div>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th className="select-cell">
                      <input
                        className="selection-checkbox"
                        type="checkbox"
                        aria-label={allFilteredSelected ? "取消全选当前客户" : "全选当前客户"}
                        checked={allFilteredSelected}
                        onChange={toggleAllFiltered}
                      />
                    </th>
                    <th>客户</th><th>国家 / 城市</th><th>邮箱</th><th>电话 / WhatsApp</th><th>状态</th><th style={{ width: 220 }}>外联 / 操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((c) => (
                    <tr key={c.id} className={selected.has(c.id) ? "is-selected" : ""}>
                      <td className="select-cell">
                        <input
                          className="selection-checkbox"
                          type="checkbox"
                          aria-label={`选择客户 ${c.name}`}
                          checked={selected.has(c.id)}
                          onChange={() => toggleSelected(c.id)}
                        />
                      </td>
                    {editId === c.id ? (
                      <>
                        <td colSpan={6}>
                          <div className="row" style={{ alignItems: "center" }}>
                            <div className="field"><input className="input" value={editForm.name} onChange={(e) => setEditForm({ ...editForm, name: e.target.value })} /></div>
                            <div className="field"><input className="input" value={editForm.country} onChange={(e) => setEditForm({ ...editForm, country: e.target.value })} /></div>
                            <div className="field"><input className="input" value={editForm.city} onChange={(e) => setEditForm({ ...editForm, city: e.target.value })} /></div>
                            <div className="field"><input className="input" value={editForm.email} onChange={(e) => setEditForm({ ...editForm, email: e.target.value })} /></div>
                            <div className="field"><input className="input" placeholder="电话" value={editForm.phone || ""} onChange={(e) => setEditForm({ ...editForm, phone: e.target.value })} /></div>
                            <button className="btn btn-primary btn-sm" onClick={saveEdit}>保存</button>
                            <button className="btn btn-ghost btn-sm" onClick={() => setEditId(null)}>取消</button>
                          </div>
                        </td>
                      </>
                    ) : (
                      <>
                        <td>
                          <div className="cell-name">
                            {c.name}
                            {c.source && c.source !== "manual" && (
                              <span
                                className={`badge ${c.source === "maps" ? "badge-maps" : "badge-webs"}`}
                                style={{ marginLeft: 8, fontSize: 11 }}
                              >
                                <span className="source-dot" aria-hidden="true" />
                                {c.source === "maps" ? "Maps" : "Webs"}
                              </span>
                            )}
                          </div>
                          {summaries[c.id] && <div className={`customer-followup-summary due-${summaries[c.id].due_status}`}>
                            {summaries[c.id].plan ? <><span>{DUE_LABEL[summaries[c.id].due_status]} · {summaries[c.id].plan!.due_date}</span><span className="followup-action-preview" title={summaries[c.id].plan!.action}>{summaries[c.id].plan!.action}</span></> : <span>未安排下次跟进</span>}
                            {summaries[c.id].last_contact_at && <span className="followup-last-contact">最近联系：{contactTime(summaries[c.id].last_contact_at!)}</span>}
                          </div>}
                          {c.notes && (
                            <div className={`customer-notes${expandedNotes.has(c.id) ? " is-expanded" : ""}`}>
                              <div
                                id={`customer-notes-${c.id}`}
                                className="customer-notes-content"
                                title={expandedNotes.has(c.id) ? undefined : c.notes}
                              >
                                {c.notes}
                              </div>
                              <button
                                type="button"
                                className="notes-toggle"
                                aria-expanded={expandedNotes.has(c.id)}
                                aria-controls={`customer-notes-${c.id}`}
                                onClick={() => toggleNotes(c.id)}
                              >
                                {expandedNotes.has(c.id) ? "收起" : "展开详情"}
                              </button>
                            </div>
                          )}
                        </td>
                        <td className="cell-muted">{formatLocation(c.country, c.city)}</td>
                        <td className="cell-email">{c.email || "—"}</td>
                        <td>
                          {c.phone ? (
                            <span className="cell-muted" style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{internationalPhone(c.phone, c.country)}</span>
                          ) : (
                            <span className="cell-muted">—</span>
                          )}
                        </td>
                        <td>
                          <select
                            className="select"
                            style={{ padding: "3px 8px", fontSize: 12.5, width: "auto" }}
                            value={c.status}
                            onChange={(e) => setStatus(c, e.target.value as CustomerStatus)}
                          >
                            {(Object.keys(STATUS_LABEL) as CustomerStatus[]).map((s) => (
                              <option key={s} value={s}>{STATUS_LABEL[s]}</option>
                            ))}
                          </select>
                        </td>
                        <td>
                          <span className="customer-outreach-actions">
                            <button className="btn btn-ghost btn-sm followup-entry" aria-label={`跟进 ${c.name}`} onClick={() => openFollowup(c)}>跟进</button>
                            {c.email && (
                              <button
                                className="btn btn-ghost btn-sm"
                                title="生成并审核邮件"
                                onClick={() => onStartOutreach({
                                  customerId: c.id,
                                  name: c.name,
                                  country: formatLocation(c.country, c.city),
                                  notes: c.notes || "",
                                  channel: "email",
                                })}
                              >
                                <MailIcon size={13} /> 准备邮件
                              </button>
                            )}
                            {waLink(c.phone, c.country) && (
                              <button
                                className="btn btn-ghost btn-sm"
                                title="生成并审核 WhatsApp 消息"
                                onClick={() => onStartOutreach({
                                  customerId: c.id,
                                  name: c.name,
                                  country: formatLocation(c.country, c.city),
                                  notes: c.notes || "",
                                  channel: "whatsapp",
                                })}
                              >
                                <RouteIcon size={13} /> 准备 WhatsApp
                              </button>
                            )}
                            <button className="icon-btn" title="编辑" onClick={() => startEdit(c)}><PenIcon size={14} /></button>
                            <button className="icon-btn danger" title="删除" onClick={() => setDel(c)}><TrashIcon size={14} /></button>
                          </span>
                        </td>
                      </>
                    )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
      {followupCustomer && <CustomerFollowupPanel key={followupCustomer.id} customer={customers.find(c => c.id === followupCustomer.id) || followupCustomer} missing={!customers.some(c => c.id === followupCustomer.id)} onClose={() => setFollowupCustomer(null)} onChanged={changed} onDirtyChange={dirtyChanged} onBusyChange={busyChanged} />}
      {pendingFollowup && <ConfirmDialog title="切换客户并放弃未保存的跟进内容？" confirmText="放弃并切换" onCancel={() => setPendingFollowup(null)} onConfirm={() => { setFollowupCustomer(pendingFollowup); setPendingFollowup(null); }} />}
      {del && (
        <ConfirmDialog
          title={`删除客户「${del.name}」？`}
          desc="将一并删除该客户的沟通记录和跟进安排；已有外联历史及禁止联系规则不受影响。此操作不可撤销，请先备份重要信息。"
          busy={delBusy}
          onCancel={() => setDel(null)}
          onConfirm={confirmRemove}
        />
      )}
      {batchDeleteOpen && (
        <ConfirmDialog
          title={`删除选中的 ${selected.size} 位客户？`}
          desc="将一并删除这些客户的沟通记录和跟进安排；已有外联历史及禁止联系规则不受影响。此操作不可撤销，请先备份重要信息。"
          confirmText={`删除 ${selected.size} 位`}
          busy={batchDeleteBusy}
          onCancel={() => setBatchDeleteOpen(false)}
          onConfirm={confirmBatchRemove}
        />
      )}
      {toast.el}
    </>
  );
}
