import { useEffect, useState } from "react";
import { api, backupDownloadUrl } from "../api";
import { getDataSafetyState, subscribeDataSafety } from "../dataEpoch";
import type { BackupCounts, BackupState, OperationView, RestorePreview } from "../types";
import ConfirmDialog from "./ConfirmDialog";

// Page-lifetime only. Settings may unmount; an upload/controller/request identity
// must not. Never put ephemeral operation IDs in localStorage or bootstrap state.
const session: {
  state: BackupState | null; operation: OperationView | null; preview: RestorePreview | null;
  pending: { previewId: string; epoch: string; requestId: string } | null;
  upload: AbortController | null; uploaded: number; uploadTotal: number;
  busy: boolean; error: string; notice: string; downloads: Set<string>;
} = { state: null, operation: null, preview: null, pending: null, upload: null,
  uploaded: 0, uploadTotal: 0, busy: false, error: "", notice: "", downloads: new Set() };
const listeners = new Set<() => void>();
const publish = () => listeners.forEach(listener => listener());
const safeError = "操作未确认成功。请刷新状态或重试；救援备份不会自动删除。";
let operationTimer: number | null = null;

async function refresh(): Promise<void> {
  const next = await api.backup.getState();
  // Live durable authority wins over an acknowledged/uncertain old operation.
  // Same-epoch prepared -> ready confirms cancellation, including a lost ack.
  if (session.state?.prepared && next.mode === "ready" && next.epoch === session.state.epoch) {
    session.pending = null; session.preview = null;
    if (session.operation?.kind === "prepare") session.operation = null;
    session.notice = "已确认恢复准备解除，继续使用当前资料；救援备份仍保留。";
  }
  session.state = next;
  publish();
}

function track(operation: OperationView): void {
  session.operation = operation;
  if (operation.preview) session.preview = operation.preview;
  if (operation.status === "error") session.error = safeError;
  publish();
  if (operationTimer !== null) window.clearTimeout(operationTimer);
  operationTimer = null;
  if (operation.status === "running") {
    operationTimer = window.setTimeout(async () => {
      operationTimer = null;
      try {
        const next = await api.backup.getOperation(operation.id);
        if (session.operation?.id === operation.id) track(next);
        await refresh();
      } catch {
        session.error = safeError; publish();
        if (session.operation?.id === operation.id) track(session.operation);
      }
    }, 1000);
  } else { void refresh().catch(() => { session.error = safeError; publish(); }); }
}

async function act(action: () => Promise<void>): Promise<void> {
  if (session.busy) return;
  session.busy = true; session.error = ""; session.notice = ""; publish();
  try { await action(); } catch { session.error = safeError; }
  finally { session.busy = false; publish(); }
}

async function prepare(): Promise<void> {
  const pending = session.pending;
  if (!pending) return;
  await act(async () => {
    // On a lost acknowledgement, never create a new request or new rescue.
    const operation = await api.backup.prepareRestore(pending.previewId, pending.epoch, pending.requestId);
    track(operation);
    await refresh();
  });
}

const labels: Record<keyof BackupCounts, string> = {
  customers: "客户", products: "产品", templates: "话术模板", crawler_seen: "已发现线索",
  emails: "历史邮件", outreach_jobs: "外联任务", suppressions: "禁止联系规则",
  followup_records: "沟通记录", followup_deleted_requests: "已删除沟通请求",
  followup_plans: "跟进安排", active_plans: "有效跟进安排", photos: "照片",
};
const bytes = (value: number) => `${(Math.max(0, value) / 1024 / 1024).toFixed(1)} MiB`;
const phases: Record<string, string> = { capture: "采集快照", compress: "压缩备份", validate: "校验备份", inspect: "检查资料", rescue: "制作救援备份", prepare: "准备恢复", done: "服务端阶段完成" };

export default function BackupSettingsCard(): JSX.Element {
  const [, rerender] = useState(0);
  const [photos, setPhotos] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [word, setWord] = useState("");
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [safety, setSafety] = useState(getDataSafetyState());
  useEffect(() => {
    const update = () => rerender(value => value + 1);
    listeners.add(update);
    const unsubscribe = subscribeDataSafety(setSafety);
    const read = () => { void refresh().catch(() => { session.error = safeError; publish(); }); };
    read();
    const timer = window.setInterval(read, 2000);
    return () => { listeners.delete(update); unsubscribe(); window.clearInterval(timer); };
  }, []);

  const { state, operation, preview, pending, upload, busy, error, notice } = session;
  const prepared = state?.prepared;
  const running = operation?.status === "running";
  const ready = state?.mode === "ready" && safety.mode === "ready";
  const locked = busy || !!upload || !!running || !!pending || !ready;

  const uploadFile = (file: File) => {
    if (locked) return;
    session.preview = null; session.operation = null; session.error = "";
    session.uploaded = 0; session.uploadTotal = file.size;
    const controller = new AbortController();
    session.upload = controller; publish();
    void api.backup.startPreview(file, controller.signal, (loaded, total) => {
      session.uploaded = loaded; session.uploadTotal = total; publish();
    }).then(track).catch(error => {
      session.error = error instanceof DOMException && error.name === "AbortError" ? "上传已取消，资料未恢复。请刷新状态后再选择文件。" : safeError;
    }).finally(() => { session.upload = null; publish(); void refresh().catch(() => {}); });
  };

  const download = (id: string) => {
    session.downloads.add(id);
    session.notice = "已发起下载，请在浏览器下载列表确认成功或失败。此页面无法确认下载完成；下载中不能删除材料。";
    publish();
  };

  return <section className="card backup-settings-card" data-backup-controls aria-label="业务备份与恢复">
    <div className="card-title"><span>业务备份与恢复</span><span className="badge badge-neutral">仅在本机处理</span></div>
    <p className="backup-warning">业务备份含客户资料，是未加密的私密 ZIP，请勿公开分享。</p>
    <p>模型/API Key、公司资料与签名、SMTP/邮箱设置及 Windows 凭据不在备份内。换电脑后需重新配置；未审核草稿、未保存表单不在备份内，请先复制保存。</p>
    <p>恢复不会自动停止或重启程序，也不会自动发送邮件或 WhatsApp。</p>
    {state?.report.status === "restored" && <p role="status">重启后的恢复已生效。请核查邮箱发件箱，重新配置设置后再人工确认发送。</p>}
    {state?.report.status === "rolled_back" && <p role="status">恢复未完成，程序已回滚。请保留救援备份并检查资料。</p>}
    {error && <p className="backup-error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <div className="backup-actions">
      <button className="btn btn-ghost" disabled={busy} onClick={() => void act(refresh)}>刷新备份状态</button>
    </div>

    {!prepared && <>
      <h3>导出业务备份</h3>
      <label className="backup-photo-option"><input type="checkbox" checked={photos} disabled={locked} onChange={event => setPhotos(event.target.checked)} />包含照片</label>
      <p>照片数量尚未测量；缓存参考值可能过时（{state && state.photo_count > 0 ? `${state.photo_count} 张` : "未知"}）。业务文件估算 {state ? bytes(state.estimated_bytes) : "未知"}，不含照片，不代表 ZIP 大小或可用容量。实际数量以严格预览为准。</p>
      <div className="backup-actions">
        <button className="btn btn-primary" disabled={locked} onClick={() => void act(async () => { session.preview = null; track(await api.backup.startExport(photos)); })}>导出业务备份</button>
        {operation?.kind === "export" && operation.status === "done" && <a className="btn btn-ghost" href={backupDownloadUrl(operation.id, false)} download target="_blank" rel="noopener" onClick={() => download(operation.id)}>下载业务备份</a>}
      </div>
      <h3>从 ZIP 预览恢复</h3>
      <label className="backup-file-label">选择业务备份 ZIP<input className="input" aria-label="选择业务备份 ZIP" type="file" accept=".zip,application/zip" disabled={locked} onChange={event => {
        const file = event.target.files?.[0]; event.target.value = "";
        if (file) uploadFile(file);
      }} /></label>
      <p>上限 2 GiB。上传和预览不会恢复资料；预览有效期 30 分钟。</p>
    </>}

    {upload && <div role="status">
      <p>上传 {bytes(session.uploaded)} / {bytes(session.uploadTotal)}{session.uploadTotal > 0 ? `（${Math.floor(session.uploaded / session.uploadTotal * 100)}%）` : ""}，仅表示已传输字节；随后由服务端校验。</p>
      <progress aria-label="文件上传进度" value={session.uploaded} max={Math.max(1, session.uploadTotal)} />
      <button className="btn btn-ghost" onClick={() => session.upload?.abort()}>取消上传</button>
    </div>}
    {running && <div role="status">
      <p>服务端阶段：{phases[operation.phase] || "处理备份"} · {operation.completed} / {operation.total || "总量未报告"}</p>
      {operation.total > 0 && <progress aria-label="服务端阶段进度" value={Math.min(operation.completed, operation.total)} max={operation.total} />}
      {operation.kind === "prepare" ? <p>完整救援准备期间不能中断；准备完成后可取消恢复，资料仍未应用。</p> : <button className="btn btn-ghost" disabled={busy} onClick={() => void act(async () => {
        await api.backup.cancelOperation(operation.id); track(await api.backup.getOperation(operation.id));
        session.notice = "已请求取消，请等待服务端释放材料后再操作。";
      })}>取消当前操作</button>}
    </div>}
    {operation?.status === "cancelled" && <p role="status">服务端操作已取消，资料未恢复。</p>}

    {preview && !prepared && <div className="backup-preview">
      <h3>严格预览 · 数量对照</h3>
      <table><thead><tr><th>业务项目</th><th>备份</th><th>当前</th></tr></thead><tbody>
        {(Object.keys(labels) as (keyof BackupCounts)[]).map(key => <tr key={key}><th scope="row">{labels[key]}</th><td>{preview.backup.counts[key]}</td><td>{preview.current.counts[key]}</td></tr>)}
      </tbody></table>
      <p>{preview.backup.includes_photos ? "包含照片：替换当前照片与照片扫描结果。" : "不含照片：保留当前照片与照片扫描结果。"}</p>
      <button className="btn btn-danger" disabled={!ready || busy || !!pending || !!running || !!upload} onClick={() => { setWord(""); setConfirm(true); }}>准备恢复</button>
    </div>}
    {pending && !busy && !running && (operation?.kind !== "prepare" || operation.status === "error") && <button className="btn btn-ghost" disabled={safety.mode === "stale"} onClick={() => void prepare()}>重试准备恢复</button>}
    {prepared && <div className="backup-prepared" role="status">
      <h3>恢复已准备，尚未应用</h3>
      <p>重启程序后才会生效。先复制未保存草稿并下载救援备份，再手动关闭服务端程序并重新打开。仅关闭或刷新浏览器不会重启服务端；此页面不会自动关闭任何程序。</p>
      <p>重启前可以取消恢复，继续使用当前资料。救援备份仍会保留。</p>
      <div className="backup-actions">
        <a className="btn btn-ghost" href={backupDownloadUrl(prepared.rescue_id, true)} download target="_blank" rel="noopener" onClick={() => download(prepared.rescue_id)}>下载救援备份</a>
        <button className="btn btn-ghost" disabled={busy || safety.mode === "stale"} onClick={() => void act(async () => {
          await api.backup.cancelRestore(prepared.operation_id);
          session.pending = null; session.operation = null; session.preview = null;
          await refresh(); session.notice = "恢复已取消，继续使用原有资料。救援备份仍保留。";
        })}>取消恢复</button>
      </div>
    </div>}

    {!!state?.rescues.length && <div className="backup-rescues"><h3>救援备份 · 整组管理</h3>
      {state.rescues.map(rescue => <div className="backup-rescue" key={rescue.id}>
        <p>{rescue.created_at} · {bytes(rescue.size_bytes)} · {rescue.active ? "恢复使用中，不可删除" : "已完成救援材料"}</p>
        <div className="backup-actions">
          {rescue.id !== prepared?.rescue_id && <a className="btn btn-ghost" href={backupDownloadUrl(rescue.id, true)} download target="_blank" rel="noopener" onClick={() => download(rescue.id)}>下载救援备份</a>}
          <button className="btn btn-danger" disabled={busy || rescue.active || session.downloads.has(rescue.id) || safety.mode === "stale"} onClick={() => setDeleteId(rescue.id)}>删除整组救援备份</button>
          {session.downloads.has(rescue.id) && <button className="btn btn-ghost" onClick={() => {
            session.downloads.delete(rescue.id); session.notice = "已按你的确认解除本页下载保护，服务端仍会拒绝删除正在下载的材料。"; publish();
          }}>我已核实浏览器下载结束</button>}
        </div>
      </div>)}
    </div>}

    {confirm && preview && <ConfirmDialog backupControls title="确认准备恢复业务资料？" confirmText="确认恢复" confirmDisabled={word !== "恢复"} onCancel={() => setConfirm(false)} onConfirm={() => {
      if (word !== "恢复") return;
      session.pending = { previewId: preview.id, epoch: preview.epoch, requestId: crypto.randomUUID() };
      setConfirm(false); void prepare();
    }} content={<div className="backup-confirm-content">
      <p>恢复将替换白名单业务资料；先成功生成完整救援备份，再准备恢复。</p>
      <p>{preview.backup.includes_photos ? "照片与照片扫描结果将被替换。" : "不含照片，保留当前照片与照片扫描结果。"}</p>
      <p>禁止联系规则取并集，避免恢复后再次联系禁联客户。</p>
      <p>邮件提交可能无法确定，请先检查发件箱再决定是否发送。WhatsApp 必须人工确认，不会自动续跑。</p>
      <p>模型、Key、公司签名及 SMTP 设置不会恢复。未审核草稿请先复制。准备后尚未应用，手动重启服务端才生效；程序不会自动停止。</p>
      <label>确认词<input className="input" aria-label="确认词" value={word} onChange={event => setWord(event.target.value)} placeholder="请输入：恢复" /></label>
    </div>} />}
    {deleteId && <ConfirmDialog backupControls title="删除整组救援备份？" desc="将删除这一组救援快照和 ZIP，不能撤销。恢复使用中或下载中的材料不能删除。" confirmText="确认删除整组" busy={busy} onCancel={() => setDeleteId(null)} onConfirm={() => void act(async () => {
      await api.backup.deleteRescue(deleteId); setDeleteId(null); await refresh(); session.notice = "整组救援备份已删除，无法恢复。";
    })} />}
  </section>;
}
