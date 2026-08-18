import { useEffect, useRef, useState } from "react";
import type { Customer, Product, Template } from "../types";
import { formatLocation } from "../location";

interface ProductComboProps {
  value: string;
  onChange: (v: string) => void;
  products: Product[];
  placeholder?: string;
  hint?: string;
}

/** 产品选择下拉：替代原生 datalist（原生下拉无法定制样式），
 *  自绘主题化面板（暖纸表面 + 琥珀 hover + 细边框）。 */
export default function ProductCombo({ value, onChange, products, placeholder, hint }: ProductComboProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, []);

  const kw = value.trim().toLowerCase();
  // 标题 > 标签 > 内容 打分匹配（输入中文类别/英文关键词都能命中）
  const filtered = products
    .map((p) => {
      const t = p.title.toLowerCase();
      const g = (p.tags || []).join(" ").toLowerCase();
      const c = p.content.toLowerCase();
      const score = t.includes(kw) ? 3 : g.includes(kw) ? 2 : c.includes(kw) ? 1 : 0;
      return { p, score };
    })
    .filter((x) => !kw || x.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, 20)
    .map((x) => x.p);

  return (
    <div className="combo" ref={ref}>
      <input
        className="input"
        value={value}
        placeholder={placeholder}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
      />
      {open && filtered.length > 0 && (
        <div className="combo-panel">
          {filtered.map((p) => (
            <button
              key={p.id}
              type="button"
              className="combo-item"
              onMouseDown={(e) => {
                e.preventDefault(); // 避免 input 先失焦关闭面板
                onChange(p.title);
                setOpen(false);
              }}
            >
              <span className="combo-title">{p.title}</span>
              <span className="combo-desc">{p.content.slice(0, 60)}</span>
            </button>
          ))}
        </div>
      )}
      {hint && <div className="field-hint">{hint}</div>}
    </div>
  );
}

interface CustomerComboProps {
  value: string;
  onChange: (v: string) => void;
  onSelect: (customer: Customer) => void;
  customers: Customer[];
  placeholder?: string;
  onOpen?: () => void | Promise<void>;
}

/** 客户选择下拉：与产品选择共用同一视觉结构，只展示精简联系摘要。 */
export function CustomerCombo({ value, onChange, onSelect, customers, placeholder, onOpen }: CustomerComboProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, []);

  const kw = value.trim().toLowerCase();
  const filtered = customers
    .map((customer) => {
      const name = customer.name.toLowerCase();
      const search = [customer.country, customer.city, customer.email, customer.notes].join(" ").toLowerCase();
      const score = name.includes(kw) ? 2 : search.includes(kw) ? 1 : 0;
      return { customer, score };
    })
    .filter((item) => !kw || item.score > 0)
    .sort((a, b) => b.score - a.score || a.customer.name.localeCompare(b.customer.name))
    .map((item) => item.customer);

  const showOptions = () => {
    setOpen(true);
    void onOpen?.();
  };

  return (
    <div className="combo" ref={ref}>
      <input
        className="input"
        value={value}
        placeholder={placeholder}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={showOptions}
        onClick={showOptions}
      />
      {open && filtered.length > 0 && (
        <div className="combo-panel" role="listbox" aria-label="客户名单">
          {filtered.map((customer) => {
            const description = [
              formatLocation(customer.country, customer.city),
              customer.email,
            ].filter(Boolean).join(" · ") || "客户名单";
            return (
              <button
                key={customer.id}
                type="button"
                className="combo-item"
                role="option"
                onMouseDown={(e) => {
                  e.preventDefault();
                  onChange(customer.name);
                  onSelect(customer);
                  setOpen(false);
                }}
              >
                <span className="combo-title">{customer.name}</span>
                <span className="combo-desc">{description}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

interface TemplateComboProps {
  value: string;
  onChange: (id: string) => void;
  templates: Template[];
}

/** 批量生成的话术选择：空值代表自动匹配，指定后固定使用该模板。 */
export function TemplateCombo({ value, onChange, templates }: TemplateComboProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const selected = templates.find((item) => item.id === value);

  useEffect(() => {
    const close = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    const esc = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, []);

  const choose = (id: string) => {
    onChange(id);
    setOpen(false);
  };

  return (
    <div className="combo" ref={ref}>
      <button
        type="button"
        className="input combo-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span>{selected?.title || "自动匹配"}</span>
        <span className="combo-chevron" aria-hidden="true">⌄</span>
      </button>
      {open && (
        <div className="combo-panel" role="listbox" aria-label="话术模板">
          <button
            type="button"
            className="combo-item"
            role="option"
            aria-selected={!value}
            onMouseDown={(event) => {
              event.preventDefault();
              choose("");
            }}
          >
            <span className="combo-title">自动匹配</span>
            <span className="combo-desc">根据当前产品自动选择相关话术</span>
          </button>
          {templates.map((template) => (
            <button
              key={template.id}
              type="button"
              className="combo-item"
              role="option"
              aria-selected={template.id === value}
              onMouseDown={(event) => {
                event.preventDefault();
                choose(template.id);
              }}
            >
              <span className="combo-title">{template.title}</span>
              <span className="combo-desc">{template.content.slice(0, 60)}</span>
            </button>
          ))}
        </div>
      )}
      <div className="field-hint">{selected ? "本批次固定参考这份话术" : `自动匹配现有 ${templates.length} 份话术`}</div>
    </div>
  );
}

interface VisionModelComboProps {
  value: string;
  onChange: (model: string) => void;
  models: Record<string, string>;
  placeholder?: string;
}

/** 视觉模型选择：沿用产品/客户下拉主题，同时保留手输 endpoint ID。 */
export function VisionModelCombo({ value, onChange, models, placeholder }: VisionModelComboProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const close = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    const esc = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, []);

  const options = Object.entries(models);

  return (
    <div className="combo" ref={ref}>
      <div className="combo-input-wrap">
        <input
          className="input combo-input-with-chevron"
          value={value}
          placeholder={placeholder}
          role="combobox"
          aria-expanded={open}
          aria-controls="vision-model-listbox"
          onChange={(event) => {
            onChange(event.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
        />
        <button
          type="button"
          className="combo-chevron-button"
          aria-label="展开视觉模型"
          tabIndex={-1}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => setOpen((current) => !current)}
        >
          <svg className="vision-combo-chevron" width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
            <path d="m3.5 5.25 3.5 3.5 3.5-3.5" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>
      {open && options.length > 0 && (
        <div id="vision-model-listbox" className="combo-panel vision-model-panel" role="listbox" aria-label="视觉模型">
          {options.map(([id, label]) => (
            <button
              key={id}
              type="button"
              className="combo-item"
              role="option"
              aria-selected={id === value}
              onMouseDown={(event) => {
                event.preventDefault();
                onChange(id);
                setOpen(false);
              }}
            >
              <span className="combo-title">{label}</span>
              <span className="combo-desc">{id}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
