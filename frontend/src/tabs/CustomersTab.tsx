import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { FadeIn, NumberTicker, useToast } from "../components/effects";
import { PenIcon, PlusIcon, TrashIcon, UploadIcon, XIcon } from "../components/Icons";
import type { Customer, CustomerStatus } from "../types";
import { formatLocation } from "../location";

const STATUS_LABEL: Record<CustomerStatus, string> = { new: "未发", sent: "已发", replied: "已回复" };

const EMPTY_FORM = { name: "", country: "", city: "", email: "", website: "", phone: "", notes: "" };

function internationalPhone(phone?: string, country?: string): string {
  if (!phone) return "";
  const raw = phone.trim();
  const digits = raw.replace(/\D/g, "");
  let normalized = raw;
  if (raw.startsWith("+")) normalized = `+${digits}`;
  else if (raw.startsWith("00")) normalized = `+${digits.slice(2)}`;

  const place = (country || "").toLowerCase();
  const isHongKong = /香港|hong\s*kong|\bhk\b/.test(place);
  const isMainland = /中国大陆|中國大陸|大陆|大陸|mainland|\bchina\b/.test(place);
  if (!normalized.startsWith("+") && (isHongKong || digits.startsWith("852")) && (digits.length === 8 || digits.length === 11)) {
    normalized = `+${digits.startsWith("852") ? digits : `852${digits}`}`;
  }
  if (!normalized.startsWith("+") && isMainland && /^1\d{10}$/.test(digits)) normalized = `+86${digits}`;
  if (!normalized.startsWith("+") && digits.startsWith("86") && digits.length === 13) normalized = `+${digits}`;

  if (/^\+852\d{8}$/.test(normalized)) return `+852 ${normalized.slice(4)}`;
  if (/^\+86\d{11}$/.test(normalized)) return `+86 ${normalized.slice(3)}`;
  return normalized;
}

function waLink(phone?: string, country?: string): string {
  if (!phone) return "";
  const normalized = internationalPhone(phone, country);
  const digits = normalized.replace(/\D/g, "");
  return normalized.startsWith("+") && digits.length >= 10 ? `https://wa.me/${digits}` : "";
}

export default function CustomersTab() {
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
  const toast = useToast();

  const load = useCallback(() => {
    api.customers.list().then(setCustomers).catch(() => {});
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = srcFilter === "all" ? customers : customers.filter((c) => (c.source || "manual") === srcFilter);
  const filteredIds = filtered.map((c) => c.id);
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
      await api.customers.remove(del.id);
      toast.push("已删除", "ok");
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
    const ids = [...selected];
    if (!ids.length) return;
    setBatchDeleteBusy(true);
    try {
      let removed = 0;
      try {
        const r = await api.customers.removeBatch(ids);
        if (!r.ok) throw new Error(r.error || "批量删除接口不可用");
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
      toast.push(`已删除 ${removed} 位客户`, "ok");
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "批量删除失败", "error");
    } finally {
      setBatchDeleteBusy(false);
    }
  }, [selected, toast.push]);

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
        <div className="page-desc">管理目标美容院：爬虫产出 / CSV 导入 / 手动添加，标记跟进状态</div>
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
            <div className="empty-title">名单还是空的</div>
            <div className="empty-desc">添加客户，或从爬虫产出的 CSV 一键导入</div>
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
                    <th>客户</th><th>国家 / 城市</th><th>邮箱</th><th>电话 / WhatsApp</th><th>状态</th><th style={{ width: 84 }} />
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
                            <>
                              <span className="cell-muted" style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{internationalPhone(c.phone, c.country)}</span>
                              {waLink(c.phone, c.country) && (
                                <a className="badge badge-wa" href={waLink(c.phone, c.country)} target="_blank" rel="noreferrer" title="在 WhatsApp 中打开">
                                  WhatsApp
                                </a>
                              )}
                            </>
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
                          <span style={{ display: "inline-flex", gap: 2 }}>
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
      {del && (
        <ConfirmDialog
          title={`删除客户「${del.name}」？`}
          desc="删除后名单里将不再保留该客户"
          busy={delBusy}
          onCancel={() => setDel(null)}
          onConfirm={confirmRemove}
        />
      )}
      {batchDeleteOpen && (
        <ConfirmDialog
          title={`删除选中的 ${selected.size} 位客户？`}
          desc="将从本地客户名单中一次性删除这些客户，此操作不可撤销"
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
