// 特效组件（Aceternity 系 · 手写 · 克制版）
import {
  useCallback, useEffect, useRef, useState,
  type CSSProperties, type MouseEvent, type ReactNode,
} from "react";

/** 暖色极光背景：三个大光斑缓慢漂移（纯 CSS，transform/opacity） */
export function AuroraBackground() {
  return (
    <div className="aurora" aria-hidden="true">
      <div className="aurora-blob b1" />
      <div className="aurora-blob b2" />
      <div className="aurora-blob b3" />
    </div>
  );
}

/** Spotlight 卡片：鼠标位置光晕跟随（--mx/--my 由 onMouseMove 设置） */
export function SpotlightCard({ children, className = "", style }: { children: ReactNode; className?: string; style?: CSSProperties }) {
  const ref = useRef<HTMLDivElement>(null);

  const onMove = useCallback((e: MouseEvent<HTMLDivElement>) => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    el.style.setProperty("--mx", `${e.clientX - r.left}px`);
    el.style.setProperty("--my", `${e.clientY - r.top}px`);
  }, []);

  return (
    <div ref={ref} onMouseMove={onMove} className={`spot-card ${className}`} style={style}>
      {children}
    </div>
  );
}

/** Shimmer 按钮：微流光主 CTA */
export function ShimmerButton({
  children, onClick, disabled, type = "button", className = "", style,
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  type?: "button" | "submit";
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <button type={type} className={`btn btn-shimmer ${className}`} onClick={onClick} disabled={disabled} style={style}>
      {children}
    </button>
  );
}

/** 数字滚动：从 0 滚到目标值（rAF，1s ease-out） */
export function NumberTicker({ value, suffix = "", duration = 900 }: { value: number; suffix?: string; duration?: number }) {
  const [display, setDisplay] = useState(0);
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) {
      setDisplay(value);
      return;
    }
    const t0 = performance.now();
    let raf = 0;
    const tick = (t: number) => {
      const p = Math.min(1, (t - t0) / duration);
      const eased = 1 - Math.pow(1 - p, 3);
      setDisplay(Math.round(value * eased));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, duration]);

  return <span ref={ref} className="num">{display.toLocaleString()}{suffix}</span>;
}

/** Bento 网格：非等宽卡片布局（历史记录/统计区） */
export function BentoGrid({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={className}
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
        gap: 14,
      }}
    >
      {children}
    </div>
  );
}

/** 浮入动画（列表/卡片入场，opacity+translateY，短时长） */
export function FadeIn({ children, delay = 0, className = "" }: { children: ReactNode; delay?: number; className?: string }) {
  return (
    <div className={`fade-in ${className}`} style={{ animationDelay: `${delay}ms`, animationDuration: "420ms" }}>
      {children}
    </div>
  );
}

/** Toast 轻提示（2.6s 自动消失） */
export function useToast() {
  const [toasts, setToasts] = useState<{ id: number; msg: string; kind: "ok" | "error" | "info" }[]>([]);

  const push = useCallback((msg: string, kind: "ok" | "error" | "info" = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, msg, kind }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 2600);
  }, []);

  const el = (
    <div className="toast-wrap" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind !== "info" ? t.kind : ""}`}>{t.msg}</div>
      ))}
    </div>
  );

  return { push, el };
}

/** 读秒计时：active 时每秒 +1（识别/生成中的等宽字体计时，防“没反应”焦虑），停时归零。 */
export function Elapsed({ active, className }: { active: boolean; className?: string }) {
  const [s, setS] = useState(0);
  useEffect(() => {
    if (!active) {
      setS(0);
      return;
    }
    const t = window.setInterval(() => setS((x) => x + 1), 1000);
    return () => window.clearInterval(t);
  }, [active]);
  return <span className={className || "ocr-clock"}>{s}s</span>;
}
