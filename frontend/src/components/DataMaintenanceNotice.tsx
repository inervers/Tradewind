import type { ReactNode } from "react";
import type { DataSafetyState } from "../types";

interface Props {
  state: DataSafetyState;
  /** Task8 supplies the shared backup control host here, not SettingsTab. */
  controls?: ReactNode;
}

export default function DataMaintenanceNotice({ state, controls }: Props) {
  if (state.mode === "ready") return null;
  const needsControls = state.mode === "prepared" || state.mode === "blocked";
  return (
    <section className="card data-maintenance-notice" aria-label="本机资料状态"
      style={{ marginBottom: 20, borderColor: "var(--amber)", background: "var(--surface)", overflowWrap: "anywhere" }}>
      <div role="status" aria-live="polite" style={{ color: "var(--navy)", lineHeight: 1.7 }}>{state.message}</div>
      {state.mode === "stale" && <p className="field-hint" style={{ marginTop: 8 }}>重新打开前，请先复制未保存的内容。页面不会自动刷新。</p>}
      {needsControls && <div id="data-maintenance-controls" role="region" aria-label="备份与恢复"
        style={{ marginTop: 16, borderTop: "1px solid var(--line)", paddingTop: 16 }}>
        {controls ?? <p style={{ color: "var(--ink-2)", lineHeight: 1.7 }}>备份与恢复 · 请保留现有内容，按程序重启后的恢复结果继续操作。</p>}
      </div>}
      {state.mode === "blocked" && <button type="button" className="btn btn-ghost" style={{ minHeight: 44, marginTop: 12 }} onClick={() => window.location.reload()}>重新打开页面</button>}
    </section>
  );
}
