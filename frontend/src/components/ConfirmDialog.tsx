import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { acquireBodyScrollLock } from "../scrollLock";

interface Props {
  title: string;
  desc?: string;
  confirmText?: string;
  busy?: boolean;
  content?: ReactNode;
  confirmDisabled?: boolean;
  /** Only backup-owned controls bypass the business wrapper, never other dialogs. */
  backupControls?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

/** 主题化确认弹窗：替换原生 window.confirm（暖纸卡片 + 遮罩模糊）。 */
export default function ConfirmDialog({ title, desc, confirmText = "删除", busy = false, content, confirmDisabled = false, backupControls = false, onCancel, onConfirm }: Props) {
  const card = useRef<HTMLDivElement>(null);
  const latest = useRef({ busy, onCancel });
  latest.current = { busy, onCancel };
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    const releaseScroll = acquireBodyScrollLock();
    const selector = 'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]';
    const firstInput = card.current?.querySelector<HTMLElement>("input:not(:disabled), textarea:not(:disabled)");
    (firstInput || card.current?.querySelector<HTMLElement>(selector) || card.current)?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault(); event.stopImmediatePropagation();
        if (!latest.current.busy) latest.current.onCancel();
      }
      if (event.key === "Tab") {
        const buttons = card.current?.querySelectorAll<HTMLElement>(selector);
        const first = buttons?.[0], last = buttons?.[buttons.length - 1];
        if (event.shiftKey && (document.activeElement === first || !card.current?.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || !card.current?.contains(document.activeElement))) { event.preventDefault(); first?.focus(); }
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      releaseScroll();
      document.removeEventListener("keydown", onKeyDown, true);
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, []);

  return createPortal(
    <div className="confirm-mask" data-backup-controls={backupControls ? "" : undefined} onClick={() => !busy && onCancel()}>
      <div ref={card} className="confirm-card" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} onClick={(e) => e.stopPropagation()}>
        <div className="confirm-title">{title}</div>
        {desc ? <div className="confirm-desc">{desc}</div> : null}
        {content}
        <div className="confirm-actions">
          <button className="btn btn-ghost" onClick={onCancel} disabled={busy}>取消</button>
          <button className="btn btn-danger" onClick={onConfirm} disabled={busy || confirmDisabled}>
            {busy ? "处理中…" : confirmText}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
