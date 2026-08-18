import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { Elapsed, FadeIn, SpotlightCard, useToast } from "../components/effects";
import { MailOpenIcon, PenIcon, PlusIcon, TrashIcon, UploadIcon, XIcon } from "../components/Icons";
import { resumeTask, watchTask } from "../tasks";
import type { ExtractTask, Template } from "../types";

const EMPTY = { title: "", content: "", tags: "" };

interface DocItem {
  title: string;
  content: string;
  tags?: string[];
}

export default function TemplatesTab() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [showAdd, setShowAdd] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [editId, setEditId] = useState<string | null>(null);
  const [docItems, setDocItems] = useState<DocItem[] | null>(null);
  const [docBusy, setDocBusy] = useState(false);
  const [docTaskId, setDocTaskId] = useState<string | null>(null);
  const [docSel, setDocSel] = useState<Set<number>>(new Set());
  const [del, setDel] = useState<Template | null>(null);
  const [delBusy, setDelBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const toast = useToast();

  const load = useCallback(() => {
    api.templates.list().then(setTemplates).catch(() => {});
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const save = useCallback(async () => {
    if (!form.title.trim() || !form.content.trim()) {
      toast.push("标题和内容必填", "error");
      return;
    }
    const payload = {
      title: form.title.trim(),
      content: form.content.trim(),
      tags: form.tags.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
    };
    try {
      if (editId) {
        const r = await api.templates.update(editId, payload);
        if (!r.ok) {
          toast.push(r.error || "保存失败", "error");
          return;
        }
        toast.push("已保存修改", "ok");
      } else {
        await api.templates.add(payload);
        toast.push("已保存，后续生成会参考它的语气与结构", "ok");
      }
      setForm(EMPTY);
      setEditId(null);
      setShowAdd(false);
      load();
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "保存失败", "error");
    }
  }, [form, editId, load, toast.push]);

  const startEdit = useCallback((t: Template) => {
    setForm({ title: t.title, content: t.content, tags: (t.tags || []).join(", ") });
    setEditId(t.id);
    setShowAdd(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  const confirmRemove = useCallback(async () => {
    if (!del) return;
    setDelBusy(true);
    try {
      await api.templates.remove(del.id);
      toast.push("已删除", "ok");
      setDel(null);
      load();
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "删除失败", "error");
    } finally {
      setDelBusy(false);
    }
  }, [del, load, toast.push]);

  // 识别任务回调（任务制：切 Tab 不中断，回来 resumeTask 恢复）
  const handleExtract = useCallback((t: ExtractTask) => {
    setDocBusy(t.status === "running");
    if (t.status === "done") {
      const items = t.items || [];
      setDocItems(items);
      setDocSel(new Set(items.map((_, i) => i)));
      toast.push(
        `已提取 ${items.length} 份原文${t.ocr ? "（图片型文档，OCR）" : ""}，正文未经 AI 改写`,
        "ok",
      );
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
    const unsub = resumeTask("extract-tpl", handleExtract, api.extractTask);
    return () => unsub?.();
  }, [handleExtract]);

  // 文档导入：历史邮件/聊天记录 → 原文提取，不经 LLM 改写
  const pickDoc = useCallback(() => fileRef.current?.click(), []);

  const onDocFile = useCallback(async (file: File | undefined) => {
    if (!file) return;
    setDocBusy(true);
    setDocItems(null);
    try {
      const r = await api.extractStart(file, "templates");
      if (!r.ok || !r.task_id) {
        toast.push(r.error || "识别启动失败", "error");
        setDocBusy(false);
        return;
      }
      setDocTaskId(r.task_id);
      watchTask("extract-tpl", r.task_id, handleExtract, api.extractTask);
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
        await api.templates.add({
          title: it.title,
          content: it.content,
          tags: it.tags && it.tags.length ? it.tags : ["邮件", "模板"],
        });
        n += 1;
      }
      toast.push(`已导入 ${n} 个话术模板`, "ok");
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
          话术模板 <span className="seal">喂样本</span>
        </div>
        <div className="page-desc">保存过去发过、客户有回复的开发信 / WhatsApp 消息；文档导入会保留原文，生成时可指定使用</div>
      </div>

      <div className="toolbar">
        <button className="btn btn-primary" onClick={() => { setShowAdd(!showAdd); if (showAdd) setEditId(null); }}>
          <PlusIcon size={15} /> {editId ? "编辑模板" : "新增模板"}
        </button>
        <button className="btn btn-ghost" onClick={pickDoc} disabled={docBusy}>
          <UploadIcon size={15} /> {docBusy ? "识别中…" : "从文档导入"}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".pdf,.docx,.xlsx,.pptx,.md,.txt,.html,.png,.jpg"
          style={{ display: "none" }}
          onChange={(e) => {
            onDocFile(e.target.files?.[0]);
            e.target.value = "";
          }}
        />
        <span className="spacer" />
        <span className="cell-muted">{templates.length} 份样本 · 生成时按相关性自动参考</span>
      </div>

      {docBusy && (
        <FadeIn>
          <div className="ocr-busy" style={{ marginBottom: 16 }}>
            <span className="ocr-spinner" />
            <div className="ocr-busy-text">
              <div className="ocr-busy-line">正在识别文档… <Elapsed active={docBusy} /></div>
              <div className="ocr-busy-sub">图片型文档自动切 OCR · 首次加载模型约 5s，之后每页约 3-5s</div>
            </div>
            <button className="btn btn-danger btn-sm" onClick={cancelExtract} style={{ marginLeft: 14 }}>取消识别</button>
          </div>
        </FadeIn>
      )}

      {docItems && (
        <FadeIn>
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-title">
              <span>识别结果（勾选要导入的话术）</span>
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

      {showAdd && (
        <FadeIn>
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-title">
              <span>{editId ? "编辑话术模板" : "新增话术样本"}</span>
              <button className="icon-btn" onClick={() => { setShowAdd(false); setEditId(null); setForm(EMPTY); }}><XIcon size={15} /></button>
            </div>
            <div className="field" style={{ marginBottom: 12 }}>
              <label className="field-label">标题（场景：开业客户 / 激光脱毛询价 / 香港美容院跟进）</label>
              <input className="input" placeholder="示例：老客户续购 皮秒升级款" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
            </div>
            <div className="field" style={{ marginBottom: 12 }}>
              <label className="field-label">邮件 / 消息全文</label>
              <textarea className="textarea" style={{ minHeight: 160 }} placeholder="把完整邮件粘贴进来（收件人姓名可用 XX 代替）…" value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })} />
            </div>
            <div className="field" style={{ marginBottom: 14 }}>
              <label className="field-label">标签（逗号分隔，建议包含 邮件/开发信/WhatsApp 等，便于检索）</label>
              <input className="input" placeholder="邮件，开发信，香港" value={form.tags} onChange={(e) => setForm({ ...form, tags: e.target.value })} />
            </div>
            <button className="btn btn-primary" onClick={save}>{editId ? "保存修改" : "保存"}</button>
          </div>
        </FadeIn>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {templates.map((t, i) => (
          <FadeIn key={t.id} delay={Math.min(i * 40, 240)}>
            <SpotlightCard style={{ padding: "16px 20px" }}>
              <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
                <div style={{ color: "var(--amber)", flexShrink: 0, marginTop: 2 }}><MailOpenIcon size={18} /></div>
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                    <span style={{ fontFamily: "var(--font-serif)", fontSize: 15, fontWeight: 600, color: "var(--ink)" }}>{t.title}</span>
                    <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      {(t.tags || []).slice(0, 4).map((tag) => (
                        <span key={tag} className="badge badge-neutral">{tag}</span>
                      ))}
                    </span>
                    <span className="spacer" />
                    <button className="icon-btn" title="编辑" onClick={() => startEdit(t)}><PenIcon size={14} /></button>
                    <button className="icon-btn danger" title="删除" onClick={() => setDel(t)}><TrashIcon size={14} /></button>
                  </div>
                  <div style={{ fontSize: 13, color: "var(--ink-2)", lineHeight: 1.7, marginTop: 8, whiteSpace: "pre-wrap" }}>
                    {t.content.slice(0, 400)}{t.content.length > 400 ? "…" : ""}
                  </div>
                </div>
              </div>
            </SpotlightCard>
          </FadeIn>
        ))}
      </div>

      {templates.length === 0 && (
        <div className="card">
          <div className="empty">
            <MailOpenIcon size={30} />
            <div className="empty-title">还没有话术样本</div>
            <div className="empty-desc">把已脱敏的历史邮件或你满意的开发信贴进来，新邮件会更贴近实际业务表达</div>
          </div>
        </div>
      )}

      {del && (
        <ConfirmDialog
          title={`删除模板「${del.title}」？`}
          desc="删除后生成新邮件将不再参考这份样本"
          busy={delBusy}
          onCancel={() => setDel(null)}
          onConfirm={confirmRemove}
        />
      )}
      {toast.el}
    </>
  );
}
