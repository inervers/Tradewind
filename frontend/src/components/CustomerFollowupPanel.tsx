import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import { acquireBodyScrollLock } from "../scrollLock";
import { CHANNEL_LABEL, OUTCOME_LABEL, contactTime, eventLabel, localDateTime } from "../followup";
import type { Customer, FollowupChannel, FollowupOutcome, FollowupPlan, FollowupTimelineItem } from "../types";
import ConfirmDialog from "./ConfirmDialog";
import { XIcon } from "./Icons";

interface Props {
  customer: Customer; onClose: () => void; onChanged: () => void;
  onDirtyChange: (dirty: boolean) => void; onBusyChange?: (busy: boolean) => void;
  missing?: boolean;
}
const emptyRecord = () => ({ contacted_at: localDateTime(), channel: "phone" as FollowupChannel, outcome: "awaiting_reply" as FollowupOutcome, content: "" });
const errorText = (error: unknown) => error instanceof Error ? error.message : "请求失败，请重试";

export default function CustomerFollowupPanel({ customer, onClose, onChanged, onDirtyChange, onBusyChange, missing = false }: Props) {
  const [record, setRecord] = useState(emptyRecord);
  const [recordBaseline, setRecordBaseline] = useState(() => JSON.stringify(record));
  const [plan, setPlan] = useState<FollowupPlan | null>(null);
  const [planForm, setPlanForm] = useState({ due_date: "", action: "" });
  const [ready, setReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [items, setItems] = useState<FollowupTimelineItem[]>([]);
  const [nextOffset, setNextOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmation, setConfirmation] = useState<"close" | "reload" | "cancel-plan" | number | null>(null);
  const panelRef = useRef<HTMLElement>(null);
  const alive = useRef(true);
  const mutationBusy = useRef(false);
  const historyPending = useRef(false);
  const historyRevision = useRef(0);
  const historyErrorMode = useRef<"initial" | "more">("initial");
  const loadRevision = useRef(0);
  const requestTokens = useRef<Record<string, { payload: string; id: string }>>({});
  const planDirty = planForm.due_date !== (plan?.due_date || "") || planForm.action !== (plan?.action || "");
  const dirty = JSON.stringify(record) !== recordBaseline || planDirty;
  const callbacks = useRef({ onClose, onChanged, onDirtyChange, onBusyChange, dirty, busy, loading });
  callbacks.current = { onClose, onChanged, onDirtyChange, onBusyChange, dirty, busy, loading };

  useEffect(() => {
    onDirtyChange(dirty);
    onBusyChange?.(busy);
  }, [dirty, busy, onDirtyChange, onBusyChange]);

  const token = (kind: string, payload: unknown) => {
    const serialized = JSON.stringify(payload);
    if (requestTokens.current[kind]?.payload !== serialized) {
      requestTokens.current[kind] = { payload: serialized, id: crypto.randomUUID() };
    }
    return requestTokens.current[kind].id;
  };

  const updateTimeline = useCallback(async () => {
    const revision = ++historyRevision.current;
    try {
      const page = await api.followups.timeline(customer.id);
      if (!alive.current || revision !== historyRevision.current) return;
      setItems(page.items); setHasMore(page.has_more); setNextOffset(page.next_offset); setHistoryError("");
    } catch (e) {
      if (alive.current && revision === historyRevision.current) { historyErrorMode.current = "initial"; setHistoryError(errorText(e)); }
    }
  }, [customer.id]);

  const load = useCallback(async () => {
    const revision = ++loadRevision.current;
    setLoading(true); setError("");
    try {
      const detail = await api.followups.get(customer.id);
      if (!alive.current || revision !== loadRevision.current) return;
      setPlan(detail.plan);
      setPlanForm({ due_date: detail.plan?.due_date || "", action: detail.plan?.action || "" });
      setReady(true);
      await updateTimeline();
    } catch (e) { if (alive.current && revision === loadRevision.current) setError(errorText(e)); }
    finally { if (alive.current && revision === loadRevision.current) setLoading(false); }
  }, [customer.id, updateTimeline]);

  useEffect(() => { alive.current = true; void load(); return () => { alive.current = false; historyRevision.current++; loadRevision.current++; }; }, [load]);

  const close = useCallback(() => {
    if (mutationBusy.current) return;
    if (callbacks.current.dirty) setConfirmation("close");
    else callbacks.current.onClose();
  }, []);

  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    const releaseScroll = acquireBodyScrollLock();
    panelRef.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (document.querySelector(".confirm-card")) return;
      if (event.key === "Escape") { event.preventDefault(); close(); }
      if (event.key === "Tab") {
        const targets = Array.from(panelRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex='0']") || []).filter(el => el.getClientRects().length);
        const first = targets[0], last = targets[targets.length - 1];
        if (event.shiftKey && (document.activeElement === first || document.activeElement === panelRef.current || !panelRef.current?.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panelRef.current || !panelRef.current?.contains(document.activeElement))) { event.preventDefault(); first?.focus(); }
      }
    };
    const unload = (event: BeforeUnloadEvent) => {
      if (callbacks.current.dirty || mutationBusy.current) { event.preventDefault(); event.returnValue = ""; }
    };
    document.addEventListener("keydown", keydown);
    window.addEventListener("beforeunload", unload);
    return () => {
      releaseScroll();
      document.removeEventListener("keydown", keydown);
      window.removeEventListener("beforeunload", unload);
      callbacks.current.onDirtyChange(false); callbacks.current.onBusyChange?.(false);
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [close]);

  const mutate = async (operation: () => Promise<void>) => {
    if (mutationBusy.current || loading || missing) return;
    mutationBusy.current = true; setBusy(true); setError(""); setNotice("");
    callbacks.current.onBusyChange?.(true);
    try { await operation(); if (alive.current) { callbacks.current.onChanged(); await updateTimeline(); } }
    catch (e) { if (alive.current) setError(errorText(e)); }
    finally { mutationBusy.current = false; if (alive.current) { setBusy(false); callbacks.current.onBusyChange?.(false); } }
  };

  const saveRecord = (event: FormEvent) => {
    event.preventDefault();
    const contacted = new Date(record.contacted_at);
    if (!Number.isFinite(contacted.getTime()) || contacted.getTime() > Date.now() + 300_000) {
      setError("沟通时间无效或晚于当前时间，请检查后重试"); return;
    }
    if (!record.content.trim()) { setError("请填写沟通内容"); return; }
    const payload = { ...record, content: record.content.trim(), contacted_at: contacted.toISOString() };
    void mutate(async () => {
      await api.followups.addRecord(customer.id, { ...payload, request_id: token("record", payload) });
      if (!alive.current) return;
      delete requestTokens.current.record;
      const next = emptyRecord(); setRecord(next); setRecordBaseline(JSON.stringify(next)); setNotice("沟通记录已保存");
    });
  };

  const savePlan = (event: FormEvent) => {
    event.preventDefault();
    if (!planForm.action.trim()) { setError("请填写下次跟进事项"); return; }
    const payload = { ...planForm, action: planForm.action.trim(), expected_plan_id: plan?.id || null };
    void mutate(async () => {
      const result = await api.followups.setPlan(customer.id, { ...payload, request_id: token("plan", payload) });
      const current = result.item.status === "active" ? result.item : (await api.followups.get(customer.id)).plan;
      if (!alive.current) return;
      delete requestTokens.current.plan;
      setPlan(current); setPlanForm({ due_date: current?.due_date || "", action: current?.action || "" });
      setNotice(current?.id === result.item.id ? "跟进安排已保存" : "原请求已处理，当前安排已同步");
    });
  };

  const closePlan = (cancel = false) => void mutate(async () => {
    if (!plan) return;
    await (cancel ? api.followups.cancelPlan(customer.id, plan.id) : api.followups.completePlan(customer.id, plan.id));
    const current = (await api.followups.get(customer.id)).plan;
    if (!alive.current) return;
    setPlan(current); setPlanForm({ due_date: current?.due_date || "", action: current?.action || "" });
    const completedNotice = cancel ? "安排已取消" : "安排已完成；沟通结果请单独记录";
    setNotice(current ? `${completedNotice}；已同步新的有效安排` : completedNotice);
  });

  const more = async () => {
    if (historyPending.current || busy) return;
    historyPending.current = true; setHistoryBusy(true); setHistoryError("");
    const revision = historyRevision.current;
    try {
      const page = await api.followups.timeline(customer.id, nextOffset);
      if (!alive.current || revision !== historyRevision.current) return;
      setItems(previous => [...new Map([...previous, ...page.items].map(item => [item.id, item])).values()]);
      setHasMore(page.has_more); setNextOffset(page.next_offset);
    } catch (e) { if (alive.current) { historyErrorMode.current = "more"; setHistoryError(errorText(e)); } }
    finally { historyPending.current = false; if (alive.current) setHistoryBusy(false); }
  };

  const confirm = () => {
    const action = confirmation; setConfirmation(null);
    if (action === "close") onClose();
    else if (action === "reload") {
      const next = emptyRecord(); setRecord(next); setRecordBaseline(JSON.stringify(next)); void load();
    } else if (action === "cancel-plan") closePlan(true);
    else if (typeof action === "number") void mutate(async () => {
      await api.followups.deleteRecord(customer.id, action); if (alive.current) setNotice("沟通记录已删除");
    });
  };

  return createPortal(<>
    <div className="followup-backdrop" onClick={close} aria-hidden="true" />
    <section className="followup-panel" role="dialog" aria-modal="true" aria-labelledby="followup-title" tabIndex={-1} ref={panelRef}>
      <header className="followup-header">
        <div><div className="followup-eyebrow">客户跟进 · 仅保存在本机</div><h2 id="followup-title" title={customer.name}>{customer.name}</h2></div>
        <button className="icon-btn" aria-label="关闭客户跟进" onClick={close} disabled={busy}><XIcon size={18} /></button>
      </header>
      <div className="followup-scroll">
        <p className="followup-hint">记录实际沟通，安排下一步。不会自动发信，也不会改变客户原有的已发 / 已回复状态。</p>
        {missing && <p className="followup-error" role="alert">客户已从名单中删除，无法再保存跟进。未保存的输入仍保留在此，请先复制需要的内容再关闭。</p>}
        {loading && <p role="status">正在读取跟进记录…</p>}
        {error && <div className="followup-error" role="alert">{error}<button className="btn btn-ghost" disabled={busy || loading} onClick={() => dirty ? setConfirmation("reload") : void load()}>重新读取</button></div>}
        {notice && <p className="followup-notice" role="status">{notice}</p>}
        {plan && <div className="followup-current-plan"><span>当前安排 · {plan.due_date}</span><p>{plan.action}</p></div>}
        {ready && <>
          <form className="followup-section" onSubmit={saveRecord}>
            <h3>记一次沟通</h3>
            <fieldset disabled={busy || loading}>
              <div className="followup-grid">
                <div className="field"><label htmlFor="followup-contact-time" className="field-label">沟通时间</label><input id="followup-contact-time" className="input" type="datetime-local" required value={record.contacted_at} onChange={e => setRecord({ ...record, contacted_at: e.target.value })} /></div>
                <div className="field"><label htmlFor="followup-channel" className="field-label">沟通渠道</label><select id="followup-channel" className="select" value={record.channel} onChange={e => setRecord({ ...record, channel: e.target.value as FollowupChannel })}>{Object.entries(CHANNEL_LABEL).map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></div>
              </div>
              <div className="field"><label htmlFor="followup-outcome" className="field-label">沟通结果</label><select id="followup-outcome" className="select" value={record.outcome} onChange={e => setRecord({ ...record, outcome: e.target.value as FollowupOutcome })}>{Object.entries(OUTCOME_LABEL).map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></div>
              <div className="field"><label htmlFor="followup-content" className="field-label">沟通内容</label><textarea id="followup-content" className="textarea" rows={3} maxLength={4000} required placeholder="例如：客户想先了解设备参数，下周补发资料。" value={record.content} onChange={e => setRecord({ ...record, content: e.target.value })} /><span className="field-hint">{record.content.length}/4000 · 不会混入开发信的仪器备注</span></div>
              <button className="btn btn-primary" type="submit" disabled={missing}>{busy ? "处理中…" : "保存沟通记录"}</button>
            </fieldset>
          </form>
          <form className="followup-section" onSubmit={savePlan}>
            <h3>下次跟进 {plan && <span className="badge badge-neutral">已安排</span>}</h3>
            <fieldset disabled={busy || loading}>
              <div className="field"><label htmlFor="followup-due-date" className="field-label">跟进日期</label><input id="followup-due-date" className="input" type="date" required value={planForm.due_date} onChange={e => setPlanForm({ ...planForm, due_date: e.target.value })} /></div>
              <div className="field"><label htmlFor="followup-action" className="field-label">跟进事项</label><textarea id="followup-action" className="textarea" rows={2} maxLength={500} required placeholder="下一次要做什么？" value={planForm.action} onChange={e => setPlanForm({ ...planForm, action: e.target.value })} /><span className="field-hint">{planForm.action.length}/500 · 每位客户只保留一个有效安排</span></div>
              <div className="followup-actions"><button className="btn btn-navy" type="submit" disabled={missing || (!!plan && !planDirty)}>{plan ? "保存调整" : "保存跟进安排"}</button>
                {plan && <><button className="btn btn-ghost" type="button" disabled={planDirty || missing} onClick={() => closePlan()}>标记完成</button><button className="btn btn-ghost" type="button" disabled={planDirty || missing} onClick={() => setConfirmation("cancel-plan")}>取消安排</button></>}
                {planDirty && <button className="btn btn-ghost" type="button" onClick={() => setPlanForm({ due_date: plan?.due_date || "", action: plan?.action || "" })}>撤销编辑</button>}
              </div>
              <p className="followup-hint" style={{ marginTop: 10, marginBottom: 0 }}>“标记完成”仅表示待办已处理，不代表已发送或收到回复；沟通结果请单独记录。</p>
            </fieldset>
          </form>
          <section className="followup-section"><h3>联系时间线</h3><p className="followup-hint">外联队列只展示已知时间和当前状态，不推测实际送达或自动检测回复。</p>
            {!items.length && !historyError && <p className="followup-empty">还没有记录。记下第一次沟通，让下次联系有据可查。</p>}
            <ol className="followup-timeline">{items.map(item => <li key={item.id}>
              <div className="followup-event-head"><strong>{eventLabel(item)}</strong><time dateTime={new Date(item.occurred_at * 1000).toISOString()}>{contactTime(item.occurred_at)}</time></div>
              {item.due_date && <div className="followup-event-date">{item.previous_due_date ? `${item.previous_due_date} → ` : "跟进日期："}{item.due_date}</div>}
              {item.content && <p>{item.content}</p>}
              {item.job_id && <div className="field-hint">{CHANNEL_LABEL[item.channel]} · 外联任务 #{item.job_id}</div>}
              {item.record_id && <button className="btn btn-ghost" disabled={busy || loading || missing} onClick={() => setConfirmation(item.record_id!)} aria-label={`删除沟通记录 ${item.record_id}`}>删除记录</button>}
            </li>)}</ol>
            {historyError && <div className="followup-error" role="alert">时间线读取失败：{historyError}<button className="btn btn-ghost" disabled={busy || historyBusy} onClick={() => historyErrorMode.current === "more" ? void more() : void updateTimeline()}>重试时间线</button></div>}
            {hasMore && <button className="btn btn-ghost" disabled={busy || historyBusy} onClick={() => void more()}>{historyBusy ? "读取中…" : "加载更多"}</button>}
          </section>
        </>}
      </div>
      <footer className="followup-footer">{busy ? "正在保存，请稍候…" : dirty ? "有未保存的内容" : "本机保存 · 不自动发送"}</footer>
    </section>
    {confirmation !== null && <ConfirmDialog title={confirmation === "close" ? "放弃未保存的跟进内容？" : confirmation === "reload" ? "重新读取并放弃未保存的内容？" : confirmation === "cancel-plan" ? "取消本次跟进安排？" : "删除这条沟通记录？"} desc={confirmation === "cancel-plan" ? "取消动作会保留在时间线中，不影响沟通记录。" : "已保存的记录不会受影响。"} confirmText={confirmation === "close" ? "放弃并关闭" : confirmation === "reload" ? "放弃并重新读取" : confirmation === "cancel-plan" ? "确认取消安排" : "删除记录"} onCancel={() => setConfirmation(null)} onConfirm={confirm} />}
  </>, document.body);
}
