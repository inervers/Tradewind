import { useCallback, useEffect, useState } from "react";
import { api, copyText, downloadText, fmtTime } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { FadeIn, SpotlightCard, useToast } from "../components/effects";
import { CopyIcon, DownloadIcon, SearchIcon, SparkIcon, TrashIcon } from "../components/Icons";
import type { HistoryItem } from "../types";

export default function HistoryTab() {
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [q, setQ] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [del, setDel] = useState<HistoryItem | null>(null);
  const [delBusy, setDelBusy] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);
  const [batchDeleteBusy, setBatchDeleteBusy] = useState(false);
  const [promoteTarget, setPromoteTarget] = useState<HistoryItem | null>(null);
  const [promoteBusy, setPromoteBusy] = useState(false);
  const [promoted, setPromoted] = useState<Set<number>>(new Set());
  const toast = useToast();

  const load = useCallback(() => {
    api.history.list(100).then(setItems).catch(() => {});
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = items.filter((h) => {
    if (!q.trim()) return true;
    const s = q.toLowerCase();
    return [h.customer, h.country, h.product].some((v) => v.toLowerCase().includes(s));
  });
  const filteredIds = filtered.map((item) => item.id);
  const allFilteredSelected = filteredIds.length > 0 && filteredIds.every((id) => selected.has(id));

  const toggle = useCallback((id: number) => {
    setExpanded((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }, []);

  const copy = useCallback(async (text: string) => {
    (await copyText(text)) ? toast.push("已复制", "ok") : toast.push("复制失败", "error");
  }, [toast.push]);

  const toggleSelected = useCallback((id: number) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleAllFiltered = useCallback(() => {
    setSelected((current) => {
      const next = new Set(current);
      if (filteredIds.length > 0 && filteredIds.every((id) => next.has(id))) {
        filteredIds.forEach((id) => next.delete(id));
      } else {
        filteredIds.forEach((id) => next.add(id));
      }
      return next;
    });
  }, [filteredIds]);

  const downloadSelected = useCallback(() => {
    const chosen = items.filter((item) => selected.has(item.id));
    if (!chosen.length) return;
    const content = chosen.map((item, index) => [
      `${index + 1}. ${item.customer}`,
      `地区：${item.country || "未填写"}`,
      `产品：${item.product || "未填写"}`,
      `类型：${item.format === "whatsapp" ? "WhatsApp" : "开发信"}`,
      `时间：${fmtTime(item.created_at)}`,
      "",
      item.email,
    ].join("\n")).join(`\n\n${"=".repeat(56)}\n\n`);
    const day = new Date().toISOString().slice(0, 10);
    downloadText(`Tradewind_历史记录_${day}_${chosen.length}条.txt`, content);
    toast.push(`已下载 ${chosen.length} 条历史记录`, "ok");
  }, [items, selected, toast.push]);

  const confirmRemove = useCallback(async () => {
    if (!del) return;
    setDelBusy(true);
    try {
      await api.history.remove(del.id);
      toast.push("已删除", "ok");
      setItems((current) => current.filter((item) => item.id !== del.id));
      setSelected((current) => {
        const next = new Set(current);
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

  const confirmBatchRemove = useCallback(async () => {
    const ids = [...selected];
    if (!ids.length) return;
    setBatchDeleteBusy(true);
    try {
      const result = await api.history.removeBatch(ids);
      if (!result.ok) {
        toast.push(result.error || "批量删除失败", "error");
        return;
      }
      const removedIds = new Set(ids);
      setItems((current) => current.filter((item) => !removedIds.has(item.id)));
      setExpanded((current) => new Set([...current].filter((id) => !removedIds.has(id))));
      setSelected(new Set());
      setBatchDeleteOpen(false);
      toast.push(`已删除 ${result.removed || 0} 条历史记录`, "ok");
    } catch (error) {
      toast.push(error instanceof Error ? error.message : "批量删除失败", "error");
    } finally {
      setBatchDeleteBusy(false);
    }
  }, [selected, toast.push]);

  const confirmPromote = useCallback(async () => {
    if (!promoteTarget) return;
    setPromoteBusy(true);
    try {
      const result = await api.templates.add({
        title: `复盘｜${promoteTarget.customer}｜${promoteTarget.product || "通用话术"}`,
        content: promoteTarget.email,
        tags: ["邮件", "模板", "人工复盘"],
      });
      if (!result.ok) {
        toast.push(result.error || "沉淀失败", "error");
        return;
      }
      setPromoted((current) => new Set(current).add(promoteTarget.id));
      toast.push(result.duplicate ? "话术库中已有相同内容" : "已沉淀到话术模板，可在生成时显式选择", "ok");
      setPromoteTarget(null);
    } catch (error) {
      toast.push(error instanceof Error ? error.message : "沉淀失败", "error");
    } finally {
      setPromoteBusy(false);
    }
  }, [promoteTarget, toast.push]);

  return (
    <>
      <div className="page-head">
        <div className="page-title">
          历史记录 <span className="seal">复盘</span>
        </div>
        <div className="page-desc">生成历史只用于复盘；人工认可并确认脱敏后，可沉淀到话术模板供后续显式复用</div>
      </div>

      <div className="toolbar">
        <div className="field" style={{ maxWidth: 320, position: "relative" }}>
          <input
            className="input"
            placeholder="搜索客户 / 国家 / 产品"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            style={{ paddingLeft: 34 }}
          />
          <span style={{ position: "absolute", left: 10, top: 9, color: "var(--ink-3)", pointerEvents: "none" }}>
            <SearchIcon size={16} />
          </span>
        </div>
        <span className="spacer" />
        <span className="cell-muted">{filtered.length} 条</span>
      </div>

      {items.length > 0 && (
        <div className={`selection-bar history-selection-bar${selected.size ? " is-active" : ""}`}>
          <button className="btn btn-ghost btn-sm" onClick={toggleAllFiltered} disabled={filtered.length === 0}>
            {allFilteredSelected ? "取消全选" : `全选当前 ${filtered.length} 条`}
          </button>
          <span className="selection-count">
            {selected.size ? `已选择 ${selected.size} 条记录` : "勾选记录后可批量下载或删除"}
          </span>
          {selected.size > 0 && (
            <>
              <button className="btn btn-ghost btn-sm" onClick={downloadSelected}>
                <DownloadIcon size={14} /> 批量下载
              </button>
              <button className="btn btn-danger btn-sm" onClick={() => setBatchDeleteOpen(true)}>
                <TrashIcon size={14} /> 批量删除
              </button>
            </>
          )}
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {filtered.map((h, i) => {
          const open = expanded.has(h.id);
          const dt = new Date(h.created_at * 1000);
          return (
            <FadeIn key={h.id} delay={Math.min(i * 30, 300)}>
              <SpotlightCard className={`history-card${selected.has(h.id) ? " is-selected" : ""}`} style={{ padding: "16px 20px" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                  <input
                    className="selection-checkbox"
                    type="checkbox"
                    aria-label={`选择 ${h.customer} 的历史记录`}
                    checked={selected.has(h.id)}
                    onChange={() => toggleSelected(h.id)}
                  />
                  <button className="btn btn-ghost btn-sm" onClick={() => toggle(h.id)} style={{ fontFamily: "var(--font-serif)", fontWeight: 600 }}>
                    {open ? "收起" : "展开"} · {h.customer}
                  </button>
                  <span className="cell-muted">{h.country}</span>
                  <span className="badge badge-neutral">{h.product}</span>
                  {h.format === "whatsapp" ? (
                    <span className="badge badge-wa" title="WhatsApp 短消息">WA</span>
                  ) : (
                    <span className="badge badge-email" title="开发信">✉ 开发信</span>
                  )}
                  {h.language === "en" && <span className="badge badge-neutral">EN</span>}
                  {h.score > 0 && <span className="badge badge-ok">评分 {h.score}/5</span>}
                  <span className="spacer" />
                  <span className="cell-muted" style={{ fontSize: 12 }}>{fmtTime(h.created_at)}</span>
                  <button
                    className="icon-btn"
                    title={promoted.has(h.id) ? "已沉淀到话术模板" : "人工审核后沉淀为话术模板"}
                    disabled={promoted.has(h.id)}
                    onClick={() => setPromoteTarget(h)}
                  ><SparkIcon size={15} /></button>
                  <button className="icon-btn" title="复制" onClick={() => copy(h.email)}><CopyIcon size={15} /></button>
                  <button className="icon-btn" title="下载" onClick={() => downloadText(`${h.customer.replace(/[\\/:*?"<>|]/g, "_")}.txt`, h.email)}><DownloadIcon size={15} /></button>
                  <button className="icon-btn danger" title="删除" onClick={() => setDel(h)}><TrashIcon size={15} /></button>
                </div>
                {open && (
                  <div style={{ marginTop: 12 }}>
                    <div className="email-body">{h.email}</div>
                    <div className="field-hint" style={{ marginTop: 8, textAlign: "right" }}>
                      {dt.toLocaleDateString("zh-CN")} {dt.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}
                    </div>
                  </div>
                )}
              </SpotlightCard>
            </FadeIn>
          );
        })}
      </div>

      {items.length === 0 && (
        <div className="card">
          <div className="empty">
            <div className="empty-title">还没有生成记录</div>
            <div className="empty-desc">去「开发信」或「批量生成」写第一封</div>
          </div>
        </div>
      )}
      {items.length > 0 && filtered.length === 0 && (
        <div className="card">
          <div className="empty">
            <div className="empty-desc">没有匹配「{q}」的记录</div>
          </div>
        </div>
      )}
      {del && (
        <ConfirmDialog
          title={`删除「${del.customer}」的历史记录？`}
          desc="删除后不可恢复"
          busy={delBusy}
          onCancel={() => setDel(null)}
          onConfirm={confirmRemove}
        />
      )}
      {batchDeleteOpen && (
        <ConfirmDialog
          title={`删除选中的 ${selected.size} 条历史记录？`}
          desc="这些开发信将从本地历史记录中永久删除"
          confirmText={`删除 ${selected.size} 条`}
          busy={batchDeleteBusy}
          onCancel={() => setBatchDeleteOpen(false)}
          onConfirm={confirmBatchRemove}
        />
      )}
      {promoteTarget && (
        <ConfirmDialog
          title={`将「${promoteTarget.customer}」沉淀为话术模板？`}
          desc="正文会原样保存在本机话术库，并可能进入后续生成 Prompt。请先确认其中没有真实客户姓名、邮箱或其他敏感信息。"
          confirmText="确认已脱敏并沉淀"
          busy={promoteBusy}
          onCancel={() => setPromoteTarget(null)}
          onConfirm={confirmPromote}
        />
      )}
      {toast.el}
    </>
  );
}
