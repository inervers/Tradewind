import { useEffect } from "react";
import { createPortal } from "react-dom";

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
  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [busy, onCancel]);

  return createPortal(
    <div className="confirm-mask" onClick={() => !busy && onCancel()}>
      <div className="confirm-card" role="dialog" aria-modal="true" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <div className="confirm-title">{title}</div>
        {desc ? <div className="confirm-desc">{desc}</div> : null}
        <div className="confirm-actions">
          <button className="btn btn-ghost" onClick={onCancel} disabled={busy} autoFocus>取消</button>
          <button className="btn btn-danger" onClick={onConfirm} disabled={busy}>
            {busy ? "处理中…" : confirmText}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
