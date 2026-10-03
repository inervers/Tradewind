import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { acquireBodyScrollLock } from "../scrollLock";

interface Props {
  title: string;
  desc?: string;
  confirmText?: string;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

/** 主题化确认弹窗：替换原生 window.confirm（暖纸卡片 + 遮罩模糊）。 */
export default function ConfirmDialog({ title, desc, confirmText = "删除", busy = false, onCancel, onConfirm }: Props) {
  const card = useRef<HTMLDivElement>(null);
  const latest = useRef({ busy, onCancel });
  latest.current = { busy, onCancel };
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    const releaseScroll = acquireBodyScrollLock();
    const firstButton = card.current?.querySelector<HTMLButtonElement>("button:not(:disabled)");
    (firstButton || card.current)?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault(); event.stopImmediatePropagation();
        if (!latest.current.busy) latest.current.onCancel();
      }
      if (event.key === "Tab") {
        const buttons = card.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)");
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
    <div className="confirm-mask" onClick={() => !busy && onCancel()}>
      <div ref={card} className="confirm-card" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} onClick={(e) => e.stopPropagation()}>
        <div className="confirm-title">{title}</div>
        {desc ? <div className="confirm-desc">{desc}</div> : null}
        <div className="confirm-actions">
          <button className="btn btn-ghost" onClick={onCancel} disabled={busy}>取消</button>
          <button className="btn btn-danger" onClick={onConfirm} disabled={busy}>
            {busy ? "处理中…" : confirmText}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
