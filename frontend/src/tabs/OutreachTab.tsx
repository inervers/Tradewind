import { useCallback, useEffect, useMemo, useState } from "react";
import { api, copyText, fmtTime } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { CheckIcon, CopyIcon, MailIcon, RouteIcon } from "../components/Icons";
import { useToast } from "../components/effects";
import type {
  ConfigState,
  EmailOutreachSettings,
  OutreachJob,
  OutreachStatus,
  OutreachSuppression,
} from "../types";

type Filter = "all" | "whatsapp" | "email" | "needs_action" | "complete";

const NEEDS_ACTION: OutreachStatus[] = ["pending_review", "opened", "failed", "unknown"];
const COMPLETE: OutreachStatus[] = ["sent_manual", "accepted", "replied", "invalid", "skipped", "suppressed"];

const STATUS_LABEL: Record<OutreachStatus, string> = {
  pending_review: "待审核",
  opened: "已打开，待确认",
  sent_manual: "已确认发送",
  sending: "提交中",
  accepted: "服务器已接受",
  unknown: "状态待核查",
  failed: "发送失败",
  replied: "客户已回复",
  invalid: "联系方式无效",
  skipped: "已跳过",
  suppressed: "禁止联系",
};

const EMPTY_SETTINGS: EmailOutreachSettings = {
  provider: "qq_smtp",
  address: "",
  transport: "ssl",
  host: "smtp.qq.com",
  port: 465,
  has_credential: false,
};

function maskedRecipient(job: OutreachJob): string {
  if (job.channel === "email") {
    const [local, domain] = job.recipient.split("@");
    return domain ? `${local.slice(0, 1)}***@${domain}` : "***";
  }
  return job.recipient.length > 5
    ? `${job.recipient.slice(0, 3)}****${job.recipient.slice(-2)}`
    : "*****";
}

function statusClass(status: OutreachStatus): string {
  if (status === "unknown") return "status-unknown";
  if (status === "failed" || status === "invalid") return "status-failed";
  if (status === "accepted" || status === "sent_manual" || status === "replied") return "status-accepted";
  return "badge-neutral";
}

export default function OutreachTab() {
  const [jobs, setJobs] = useState<OutreachJob[]>([]);
  const [suppressions, setSuppressions] = useState<OutreachSuppression[]>([]);
  const [settings, setSettings] = useState<EmailOutreachSettings>(EMPTY_SETTINGS);
  const [appConfig, setAppConfig] = useState<ConfigState>({ has_key: false });
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [sendConfirm, setSendConfirm] = useState<OutreachJob | null>(null);
  const [rebindConfirm, setRebindConfirm] = useState<OutreachJob | null>(null);
  const [suppressConfirm, setSuppressConfirm] = useState<OutreachJob | null>(null);
  const [unsuppressConfirm, setUnsuppressConfirm] = useState<OutreachSuppression | null>(null);
  const [suppressionReason, setSuppressionReason] = useState("");
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      const [nextJobs, nextSuppressions, nextSettings, nextConfig] = await Promise.all([
        api.outreach.list(),
        api.outreach.listSuppressions(),
        api.outreach.getEmailSettings(),
        api.getConfig(),
      ]);
      setJobs(nextJobs);
      setSuppressions(nextSuppressions);
      setSettings(nextSettings);
      setAppConfig(nextConfig);
      setSelectedId((current) => {
        if (current && nextJobs.some((item) => item.id === current)) return current;
        return nextJobs[0]?.id ?? null;
      });
    } catch (error) {
      toast.push(error instanceof Error ? error.message : "外联队列加载失败", "error");
    } finally {
      setLoading(false);
    }
  }, [toast.push]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!jobs.some((item) => item.status === "sending")) return;
    const timer = window.setInterval(() => void load(), 800);
    return () => window.clearInterval(timer);
  }, [jobs, load]);

  const selected = jobs.find((item) => item.id === selectedId) || null;

  useEffect(() => {
    if (!selected) return;
    setSubject(selected.subject);
    setBody(selected.body);
    setSuppressionReason("");
  }, [selected?.id, selected?.updated_at]);

  const filtered = useMemo(() => jobs.filter((item) => {
    if (filter === "whatsapp" || filter === "email") return item.channel === filter;
    if (filter === "needs_action") return NEEDS_ACTION.includes(item.status) || item.status === "sending";
    if (filter === "complete") return COMPLETE.includes(item.status);
    return true;
  }), [filter, jobs]);

  const counts = useMemo(() => ({
    review: jobs.filter((item) => item.status === "pending_review").length,
    action: jobs.filter((item) => NEEDS_ACTION.includes(item.status) || item.status === "sending").length,
    accepted: jobs.filter((item) => item.status === "accepted" || item.status === "sent_manual").length,
    replied: jobs.filter((item) => item.status === "replied").length,
    suppressed: suppressions.length,
  }), [jobs, suppressions]);

  const run = useCallback(async (action: () => Promise<{ ok: boolean; error?: string }>, success: string) => {
    setBusy(true);
    try {
      const result = await action();
      if (!result.ok) {
        toast.push(result.error || "操作失败", "error");
        return false;
      }
      toast.push(success, "ok");
      await load();
      return true;
    } catch (error) {
      toast.push(error instanceof Error ? error.message : "操作失败", "error");
      return false;
    } finally {
      setBusy(false);
    }
  }, [load, toast.push]);

  const save = useCallback(async () => {
    if (!selected) return;
    await run(
      () => api.outreach.update(selected.id, { subject, body }),
      "修改已保存，任务回到待审核状态",
    );
  }, [body, run, selected, subject]);

  const openWhatsApp = useCallback(async () => {
    if (!selected) return;
    const target = window.open("about:blank", "_blank");
    if (!target) {
      toast.push("浏览器阻止了新窗口，请允许弹窗后重试", "error");
      return;
    }
    target.opener = null;
    setBusy(true);
    try {
      const result = await api.outreach.openWhatsApp(selected.id);
      if (!result.ok || !result.url) {
        target.close();
        toast.push(result.error || "无法打开 WhatsApp", "error");
        return;
      }
      target.location.replace(result.url);
      toast.push("已打开预填页面；返回后请确认是否真的发送", "info");
      await load();
    } catch (error) {
      target.close();
      toast.push(error instanceof Error ? error.message : "无法打开 WhatsApp", "error");
    } finally {
      setBusy(false);
    }
  }, [load, selected, toast.push]);

  const transition = useCallback((status: OutreachStatus, reason = "") => {
    if (!selected) return Promise.resolve(false);
    return run(() => api.outreach.transition(selected.id, status, reason), "状态已更新");
  }, [run, selected]);

  const sendEmail = useCallback(async () => {
    if (!sendConfirm) return;
    const id = sendConfirm.id;
    setSendConfirm(null);
    await run(() => api.outreach.sendEmail(id), "邮件已提交，正在等待服务器结果");
  }, [run, sendConfirm]);

  const rebindSender = useCallback(async () => {
    if (!rebindConfirm) return;
    const id = rebindConfirm.id;
    setRebindConfirm(null);
    await run(
      () => api.outreach.rebindSender(id),
      "已改用当前邮箱，发送身份需要重新审核",
    );
  }, [rebindConfirm, run]);

  const suppress = useCallback(async () => {
    if (!suppressConfirm || !suppressionReason.trim()) return;
    const job = suppressConfirm;
    setSuppressConfirm(null);
    await run(
      () => api.outreach.suppress({
        channel: job.channel,
        recipient: job.recipient,
        reason: suppressionReason.trim(),
      }),
      "已加入禁止联系名单",
    );
  }, [run, suppressConfirm, suppressionReason]);

  const unsuppress = useCallback(async () => {
    if (!unsuppressConfirm) return;
    const id = unsuppressConfirm.id;
    setUnsuppressConfirm(null);
    await run(() => api.outreach.unsuppress(id), "已解除禁止联系");
  }, [run, unsuppressConfirm]);

  const profile = appConfig.company_profile;
  const foldedBody = body.toLocaleLowerCase();
  const validationIssues = selected?.channel === "email" ? [
    !subject.trim() ? "缺少邮件主题" : "",
    !body.trim() ? "缺少正文" : "",
    !profile?.sender_name || !profile?.company_name
      || !foldedBody.includes(profile.sender_name.toLocaleLowerCase())
      || !foldedBody.includes(profile.company_name.toLocaleLowerCase())
      ? "缺少发件身份" : "",
    !(/not to receive further messages|stop future contact/i.test(body) || /停止发送|不希望再收到联系/.test(body))
      ? "缺少停止联系说明" : "",
  ].filter(Boolean) : [];
  const senderMatches = selected?.channel === "email"
    && selected.provider === settings.provider
    && selected.sender_account_snapshot.toLocaleLowerCase() === settings.address.toLocaleLowerCase();
  const draftDirty = Boolean(selected && (selected.subject !== subject || selected.body !== body));
  const canSend = selected?.channel === "email"
    && ["pending_review", "failed"].includes(selected.status)
    && settings.has_credential
    && senderMatches
    && !draftDirty
    && validationIssues.length === 0;
  const editable = selected ? ["pending_review", "opened", "failed"].includes(selected.status) : false;

  return (
    <>
      <div className="page-head outreach-head">
        <div>
          <div className="page-title">外联队列 <span className="seal">逐条审核</span></div>
          <div className="page-desc">WhatsApp 只打开预填页面；QQ / Gmail 每封确认后才提交。</div>
        </div>
        <div className="outreach-summary" aria-label="外联状态汇总">
          <span><b>{counts.review}</b> 待审核</span>
          <span><b>{counts.action}</b> 需处理</span>
          <span><b>{counts.accepted}</b> 已提交</span>
          <span><b>{counts.replied}</b> 已回复</span>
          <span><b>{counts.suppressed}</b> 禁止联系</span>
        </div>
      </div>

      <div className="outreach-filters" role="group" aria-label="筛选外联任务">
        {([
          ["all", "全部"], ["whatsapp", "WhatsApp"], ["email", "邮件"],
          ["needs_action", "需处理"], ["complete", "已完成"],
        ] as [Filter, string][]).map(([value, label]) => (
          <button key={value} className={`badge ${filter === value ? "badge-ok" : "badge-neutral"}`} onClick={() => setFilter(value)} aria-pressed={filter === value}>
            {label}
          </button>
        ))}
      </div>

      <div className="outreach-grid">
        <section className="card outreach-list" aria-label="外联任务列表">
          {loading ? <div className="empty"><div className="empty-title">正在读取队列…</div></div> : null}
          {!loading && filtered.length === 0 ? (
            <div className="empty">
              <RouteIcon size={34} />
              <div className="empty-title">这个筛选下还没有任务</div>
              <div className="empty-desc">从客户名单选择“准备邮件”或“准备 WhatsApp”，生成后加入队列。</div>
            </div>
          ) : null}
          {filtered.map((job) => (
            <button key={job.id} className={`outreach-list-item ${selectedId === job.id ? "is-active" : ""}`} onClick={() => setSelectedId(job.id)}>
              <span className="outreach-channel-icon">{job.channel === "email" ? <MailIcon size={16} /> : <RouteIcon size={16} />}</span>
              <span className="outreach-list-copy">
                <strong>{job.customer_name_snapshot}</strong>
                <small>{maskedRecipient(job)} · {fmtTime(job.updated_at)}</small>
              </span>
              <span className={`badge ${statusClass(job.status)}`}>{STATUS_LABEL[job.status]}</span>
            </button>
          ))}
        </section>

        <section className="card outreach-editor" aria-label="外联审核区">
          {!selected ? (
            <div className="empty"><div className="empty-title">选择一条任务开始审核</div></div>
          ) : (
            <>
              <div className="card-title outreach-editor-title">
                <span>{selected.customer_name_snapshot}</span>
                <span className={`badge ${statusClass(selected.status)}`}>{STATUS_LABEL[selected.status]}</span>
              </div>
              <div className="outreach-meta">
                <span>渠道：{selected.channel === "email" ? "邮件" : "WhatsApp"}</span>
                <span>目标：{selected.recipient}</span>
                {selected.channel === "email" && <span>发件：{selected.sender_account_snapshot || "未配置"}</span>}
              </div>

              {selected.channel === "email" && (
                <div className="field">
                  <label className="field-label" htmlFor="outreach-subject">邮件主题</label>
                  <input id="outreach-subject" className="input" value={subject} disabled={!editable || busy} onChange={(event) => setSubject(event.target.value)} />
                </div>
              )}
              <div className="field outreach-body-field">
                <label className="field-label" htmlFor="outreach-body">{selected.channel === "email" ? "邮件正文" : "消息正文"}</label>
                <textarea id="outreach-body" className="textarea outreach-body" value={body} disabled={!editable || busy} onChange={(event) => setBody(event.target.value)} />
              </div>

              {editable && (
                <div className="outreach-action-row">
                  <button className="btn btn-ghost" onClick={save} disabled={busy || !body.trim()}>保存修改</button>
                  <button className="btn btn-ghost" onClick={async () => {
                    const ok = await copyText(body);
                    toast.push(ok ? "正文已复制" : "复制失败", ok ? "ok" : "error");
                  }}><CopyIcon size={14} />复制正文</button>
                </div>
              )}

              {selected.status === "accepted" && <div className="outreach-notice status-accepted">邮箱服务器已接受，不代表客户已收到或阅读。</div>}
              {selected.status === "opened" && <div className="outreach-notice status-unknown">页面已打开不等于已发送，请根据实际操作确认。</div>}
              {selected.status === "unknown" && <div className="outreach-notice status-unknown">请先登录发件箱核查；确认未发送后才可重试。</div>}
              {selected.status === "failed" && selected.error_summary && <div className="outreach-notice status-failed">{selected.error_summary}</div>}
              {selected.channel === "email" && validationIssues.length > 0 && ["pending_review", "failed"].includes(selected.status) && (
                <div className="outreach-notice status-failed">发送前检查：{validationIssues.join("、")}</div>
              )}
              {selected.channel === "email" && !senderMatches && ["pending_review", "failed"].includes(selected.status) && (
                <div className="outreach-notice status-unknown outreach-rebind-notice">
                  <span>当前邮箱账号与草稿快照不同，不能直接发送。</span>
                  <button
                    className="btn btn-ghost btn-sm"
                    disabled={!settings.has_credential || busy || draftDirty}
                    onClick={() => setRebindConfirm(selected)}
                  >
                    改用当前账号并重新审核
                  </button>
                </div>
              )}
              {draftDirty && <div className="outreach-notice status-unknown">内容有未保存修改，请先保存再执行打开或发送。</div>}

              <div className="outreach-primary-actions">
                {selected.channel === "whatsapp" && selected.status === "pending_review" && (
                  <button className="btn btn-primary" onClick={openWhatsApp} disabled={busy || draftDirty}>打开 WhatsApp</button>
                )}
                {selected.channel === "whatsapp" && selected.status === "opened" && (
                  <>
                    <button className="btn btn-primary" onClick={() => transition("sent_manual")} disabled={busy}><CheckIcon size={14} />确认已发送</button>
                    <button className="btn btn-ghost" onClick={() => transition("pending_review")} disabled={busy}>暂未发送</button>
                    <button className="btn btn-ghost" onClick={() => transition("invalid")} disabled={busy}>号码无效</button>
                  </>
                )}
                {selected.channel === "email" && ["pending_review", "failed"].includes(selected.status) && (
                  <button className="btn btn-primary" onClick={() => setSendConfirm(selected)} disabled={busy || !canSend}>
                    {selected.status === "failed" ? "审核后重试发送" : "确认发送邮件"}
                  </button>
                )}
                {selected.status === "sending" && <button className="btn btn-primary" disabled>正在提交…</button>}
                {selected.status === "unknown" && (
                  <>
                    <button className="btn btn-primary" onClick={() => transition("accepted", "已由用户在发件箱确认发送")} disabled={busy}>已在发件箱确认发送</button>
                    <button className="btn btn-ghost" onClick={() => transition("failed", "已由用户确认未发送")} disabled={busy}>已确认未发送</button>
                  </>
                )}
                {(selected.status === "accepted" || selected.status === "sent_manual") && (
                  <button className="btn btn-primary" onClick={() => transition("replied")} disabled={busy}>标记已回复</button>
                )}
                {NEEDS_ACTION.includes(selected.status) && selected.status !== "unknown" && (
                  <button className="btn btn-ghost" onClick={() => transition("skipped")} disabled={busy}>跳过</button>
                )}
              </div>

              {!COMPLETE.includes(selected.status) && selected.status !== "sending" && (
                <div className="outreach-suppress-box">
                  <div className="field">
                    <label className="field-label" htmlFor="suppression-reason">禁止再联系原因</label>
                    <input id="suppression-reason" className="input" value={suppressionReason} onChange={(event) => setSuppressionReason(event.target.value)} placeholder="例如：客户明确要求停止联系" />
                  </div>
                  <button className="btn btn-danger" disabled={!suppressionReason.trim() || busy} onClick={() => setSuppressConfirm(selected)}>禁止再联系</button>
                </div>
              )}
            </>
          )}
        </section>
      </div>

      <section className="card outreach-suppressions">
        <div className="card-title"><span>禁止联系名单</span><span className="badge badge-neutral">{suppressions.length} 条</span></div>
        {suppressions.length === 0 ? <div className="field-hint">尚无禁止联系记录。</div> : (
          <div className="outreach-suppression-list">
            {suppressions.map((item) => (
              <div key={item.id} className="outreach-suppression-item">
                <span className="badge badge-neutral">{item.channel === "email" ? "邮件" : "WhatsApp"}</span>
                <span className="cell-email">{item.masked_recipient}</span>
                <span className="outreach-suppression-reason">{item.reason}</span>
                <button className="btn btn-ghost btn-sm" onClick={() => setUnsuppressConfirm(item)}>解除禁止</button>
              </div>
            ))}
          </div>
        )}
      </section>

      {sendConfirm && (
        <ConfirmDialog
          title="确认提交这封邮件？"
          desc={`发件账号：${sendConfirm.sender_account_snapshot}\n收件地址：${sendConfirm.recipient}\n主题：${sendConfirm.subject}\n\n提交后无法撤回；服务器接受不代表送达或已读。`}
          confirmText="确认提交"
          busy={busy}
          onCancel={() => setSendConfirm(null)}
          onConfirm={sendEmail}
        />
      )}
      {rebindConfirm && (
        <ConfirmDialog
          title="改用当前邮箱账号？"
          desc={`原发件账号：${rebindConfirm.sender_account_snapshot}\n当前发件账号：${settings.address}\n\n草稿会回到待审核，旧 Message-ID 会清除，不会自动发送。`}
          confirmText="改用并重新审核"
          busy={busy}
          onCancel={() => setRebindConfirm(null)}
          onConfirm={rebindSender}
        />
      )}
      {suppressConfirm && (
        <ConfirmDialog
          title={`禁止再联系「${suppressConfirm.customer_name_snapshot}」？`}
          desc={`目标：${suppressConfirm.recipient}\n原因：${suppressionReason}\n后续新建和发送都会被阻止。`}
          confirmText="加入禁止名单"
          busy={busy}
          onCancel={() => setSuppressConfirm(null)}
          onConfirm={suppress}
        />
      )}
      {unsuppressConfirm && (
        <ConfirmDialog
          title="解除这条禁止联系记录？"
          desc={`${unsuppressConfirm.masked_recipient}\n解除后仍不会自动发送任何消息。`}
          confirmText="确认解除"
          busy={busy}
          onCancel={() => setUnsuppressConfirm(null)}
          onConfirm={unsuppress}
        />
      )}
      {toast.el}
    </>
  );
}
