import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { BentoGrid, Elapsed, FadeIn, SpotlightCard, useToast } from "../components/effects";
import { BoxIcon, PenIcon, PlusIcon, TrashIcon, UploadIcon, XIcon } from "../components/Icons";
import { resumeTask, watchTask } from "../tasks";
import type { ExtractTask, Product } from "../types";

const EMPTY = { title: "", content: "", tags: "" };

interface DocItem {
  title: string;
  content: string;
  tags?: string[];
}

export default function ProductsTab() {
  const [products, setProducts] = useState<Product[]>([]);
  const [showAdd, setShowAdd] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [editId, setEditId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState(EMPTY);
  const [docItems, setDocItems] = useState<DocItem[] | null>(null);
  const [docBusy, setDocBusy] = useState(false);
  const [docTaskId, setDocTaskId] = useState<string | null>(null);
  const [docSel, setDocSel] = useState<Set<number>>(new Set());
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [del, setDel] = useState<Product | null>(null);
  const [delBusy, setDelBusy] = useState(false);
  const toast = useToast();

  // 识别任务回调（watchTask 轮询驱动，切 Tab 不中断；回来 resumeTask 恢复）
  const handleExtract = useCallback((t: ExtractTask) => {
    setDocBusy(t.status === "running");
    if (t.status === "done") {
      const items = t.items || [];
      setDocItems(items);
      setDocSel(new Set(items.map((_, i) => i)));
      if (t.mode === "summary") {
        toast.push(`已用 AI 总结成 ${items.length} 条结构化资料${t.ocr ? "（图片型文档，OCR）" : ""}`, "ok");
      } else {
        toast.push(t.ocr
          ? `识别出 ${items.length} 个原始块（未配置模型无法总结，可直接勾选或手动编辑）`
          : `识别出 ${items.length} 个条目，勾选后导入`, "ok");
      }
      setDocTaskId(null);
    } else if (t.status === "cancelled") {
      toast.push("已取消识别", "info");
      setDocTaskId(null);
    } else if (t.status === "error") {
      toast.push(t.error || "识别失败", "error");
      setDocTaskId(null);
    }
  }, [toast.push]);

  // 切回时恢复进行中的识别任务
  useEffect(() => {
    const unsub = resumeTask("extract-prod", handleExtract, api.extractTask);
    return () => unsub?.();
  }, [handleExtract]);

  const load = useCallback(() => {
    api.products.list().then(setProducts).catch(() => {});
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const add = useCallback(async () => {
    if (!form.title.trim() || !form.content.trim()) {
      toast.push("标题和内容必填", "error");
      return;
    }
    try {
      await api.products.add({
        title: form.title.trim(),
        content: form.content.trim(),
        tags: form.tags.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
      });
      setForm(EMPTY);
      setShowAdd(false);
      toast.push("产品已添加，生成开发信时自动选用", "ok");
      load();
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "添加失败", "error");
    }
  }, [form, load, toast.push]);

  const startEdit = useCallback((p: Product) => {
    setEditId(p.id);
    setEditForm({ title: p.title, content: p.content, tags: (p.tags || []).join("，") });
  }, []);

  const saveEdit = useCallback(async () => {
    if (!editId) return;
    try {
      await api.products.update(editId, {
        title: editForm.title.trim(),
        content: editForm.content.trim(),
        tags: editForm.tags.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
      });
      setEditId(null);
      toast.push("已保存", "ok");
      load();
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "保存失败", "error");
    }
  }, [editId, editForm, load, toast.push]);

  const confirmRemove = useCallback(async () => {
    if (!del) return;
    setDelBusy(true);
    try {
      await api.products.remove(del.id);
      toast.push("已删除", "ok");
      setDel(null);
      load();
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "删除失败", "error");
    } finally {
      setDelBusy(false);
    }
  }, [del, load, toast.push]);

  // 文档识别导入：选文件 → 后端任务化识别（可取消）→ 预览勾选 → 逐个入库
  const pickDoc = useCallback(() => fileRef.current?.click(), []);

  const onDocFile = useCallback(async (file: File | undefined) => {
    if (!file) return;
    setDocBusy(true);
    setDocItems(null);
    try {
      const r = await api.extractStart(file, "products");
      if (!r.ok || !r.task_id) {
        toast.push(r.error || "识别启动失败", "error");
        setDocBusy(false);
        return;
      }
      setDocTaskId(r.task_id);
      watchTask("extract-prod", r.task_id, handleExtract, api.extractTask);
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "识别失败", "error");
      setDocBusy(false);
    }
  }, [handleExtract, toast.push]);

  const cancelExtract = useCallback(async () => {
    if (!docTaskId || !docBusy) return;
    try {
      const r = await api.extractCancel(docTaskId);
      if (!r.ok) toast.push(r.error || "取消失败", "error");
      else toast.push("正在取消：当前这一页识别完即停…", "info");
    } catch {
      /* ignore */
    }
  }, [docTaskId, docBusy, toast.push]);

  const importDocItems = useCallback(async () => {
    if (!docItems) return;
    const picked = docItems.filter((_, i) => docSel.has(i));
    if (!picked.length) {
      toast.push("先勾选要导入的条目", "error");
      return;
    }
    try {
      let n = 0;
      for (const it of picked) {
        await api.products.add({
          title: it.title,
          content: it.content,
          tags: it.tags && it.tags.length ? it.tags : ["文档导入"],
        });
        n += 1;
      }
      toast.push(`已导入 ${n} 个产品`, "ok");
      setDocItems(null);
      load();
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "导入失败", "error");
    }
  }, [docItems, docSel, load, toast.push]);

  return (
    <>
      <div className="page-head">
        <div className="page-title">
          产品资料 <span className="seal">卖点库</span>
        </div>
        <div className="page-desc">设备资料是开发信卖点的来源，写清楚参数与认证，邮件才有力</div>
      </div>

      <div className="toolbar">
        <button className="btn btn-primary" onClick={() => { setShowAdd(!showAdd); setEditId(null); }}>
          <PlusIcon size={15} /> 新增产品
        </button>
        <button className="btn btn-ghost" onClick={pickDoc} disabled={docBusy}>
          <UploadIcon size={15} /> {docBusy ? "识别中…" : "从文档导入"}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".pdf,.docx,.xlsx,.pptx,.md,.txt,.html,.csv"
          style={{ display: "none" }}
          onChange={(e) => {
            onDocFile(e.target.files?.[0]);
            e.target.value = ""; // 允许重复选同一文件
          }}
        />
        <span className="spacer" />
        <span className="cell-muted">{products.length} 台设备 · 生成时按相关性自动挑选 · 支持 PDF/Word/Excel 识别导入</span>
      </div>

      {showAdd && (
        <FadeIn>
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-title"><span>新增产品</span><button className="icon-btn" onClick={() => setShowAdd(false)}><XIcon size={15} /></button></div>
            <div className="field" style={{ marginBottom: 12 }}>
              <label className="field-label">名称（含型号）</label>
              <input className="input" placeholder="半导体激光脱毛仪 Diode-808" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
            </div>
            <div className="field" style={{ marginBottom: 12 }}>
              <label className="field-label">卖点描述</label>
              <textarea className="textarea" placeholder="参数、认证、适用场景、差异化优势…" value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })} />
            </div>
            <div className="field" style={{ marginBottom: 14 }}>
              <label className="field-label">标签（逗号分隔）</label>
              <input className="input" placeholder="激光脱毛，808nm，CE 认证" value={form.tags} onChange={(e) => setForm({ ...form, tags: e.target.value })} />
            </div>
            <button className="btn btn-primary" onClick={add}>保存</button>
          </div>
        </FadeIn>
      )}

      {docBusy && (
        <FadeIn>
          <div className="ocr-busy" style={{ marginBottom: 16 }}>
            <span className="ocr-spinner" />
            <div className="ocr-busy-text">
              <div className="ocr-busy-line">
                正在识别 <Elapsed active={docBusy} />
              </div>
              <div className="ocr-busy-sub">先尝试文字解析，图片型 PDF 自动切 OCR · 首次加载模型约 5s，之后每页约 3-5s</div>
            </div>
            <button className="btn btn-danger btn-sm" onClick={cancelExtract} style={{ marginLeft: 14 }}>取消识别</button>
          </div>
        </FadeIn>
      )}

      {docItems && (
        <FadeIn>
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-title">
              <span>文档识别结果（勾选要导入的条目）</span>
              <button className="icon-btn" onClick={() => setDocItems(null)}><XIcon size={15} /></button>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 320, overflowY: "auto", marginBottom: 14 }}>
              {docItems.map((it, i) => (
                <label key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start", cursor: "pointer", padding: "8px 10px", border: "1px solid var(--line)", borderRadius: "var(--r-sm)" }}>
                  <input
                    type="checkbox"
                    style={{ marginTop: 3, width: 15, height: 15, accentColor: "var(--amber)" }}
                    checked={docSel.has(i)}
                    onChange={() =>
                      setDocSel((s) => {
                        const n = new Set(s);
                        if (n.has(i)) n.delete(i);
                        else n.add(i);
                        return n;
                      })
                    }
                  />
                  <div style={{ flex: 1 }}>
                    <div style={{ fontWeight: 600, fontSize: 13.5, fontFamily: "var(--font-serif)" }}>{it.title}</div>
                    <div style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{it.content.slice(0, 200)}{it.content.length > 200 ? "…" : ""}</div>
                  </div>
                </label>
              ))}
            </div>
            <div style={{ display: "flex", gap: 10 }}>
              <button className="btn btn-primary btn-sm" onClick={importDocItems}>导入所选（{docSel.size}）</button>
              <button className="btn btn-ghost btn-sm" onClick={() => setDocSel(new Set(docItems.map((_, i) => i)))}>全选</button>
            </div>
          </div>
        </FadeIn>
      )}

      <BentoGrid>
        {products.map((p, i) => (
          <FadeIn key={p.id} delay={i * 40}>
            <SpotlightCard style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 170 }}>
              {editId === p.id ? (
                <>
                  <div className="field" style={{ marginBottom: 10 }}><input className="input" value={editForm.title} onChange={(e) => setEditForm({ ...editForm, title: e.target.value })} /></div>
                  <div className="field" style={{ flex: 1, marginBottom: 10 }}><textarea className="textarea" style={{ minHeight: 80 }} value={editForm.content} onChange={(e) => setEditForm({ ...editForm, content: e.target.value })} /></div>
                  <div className="field" style={{ marginBottom: 10 }}><input className="input" value={editForm.tags} onChange={(e) => setEditForm({ ...editForm, tags: e.target.value })} /></div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <button className="btn btn-primary btn-sm" onClick={saveEdit}>保存</button>
                    <button className="btn btn-ghost btn-sm" onClick={() => setEditId(null)}>取消</button>
                  </div>
                </>
              ) : (
                <>
                  <div style={{ display: "flex", alignItems: "flex-start", gap: 10, marginBottom: 8 }}>
                    <div style={{ color: "var(--amber)", flexShrink: 0, marginTop: 2 }}><BoxIcon size={18} /></div>
                    <div style={{ fontFamily: "var(--font-serif)", fontSize: 15.5, fontWeight: 600, color: "var(--ink)", lineHeight: 1.4 }}>{p.title}</div>
                  </div>
                  <div style={{ fontSize: 13, color: "var(--ink-2)", lineHeight: 1.65, flex: 1 }}>{p.content}</div>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 12 }}>
                    <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      {(p.tags || []).slice(0, 4).map((t) => (
                        <span key={t} className="badge badge-neutral">{t}</span>
                      ))}
                    </span>
                    <span style={{ display: "inline-flex", gap: 2 }}>
                      <button className="icon-btn" title="编辑" onClick={() => startEdit(p)}><PenIcon size={14} /></button>
                      <button className="icon-btn danger" title="删除" onClick={() => setDel(p)}><TrashIcon size={14} /></button>
                    </span>
                  </div>
                </>
              )}
            </SpotlightCard>
          </FadeIn>
        ))}
      </BentoGrid>

      {products.length === 0 && (
        <div className="card">
          <div className="empty">
            <div className="empty-title">还没有产品资料</div>
            <div className="empty-desc">新增设备资料，开发信生成时会作为卖点参考</div>
          </div>
        </div>
      )}
      {del && (
        <ConfirmDialog
          title={`删除产品「${del.title}」？`}
          desc="删除后生成开发信将不再推荐该设备"
          busy={delBusy}
          onCancel={() => setDel(null)}
          onConfirm={confirmRemove}
        />
      )}
      {toast.el}
    </>
  );
}
