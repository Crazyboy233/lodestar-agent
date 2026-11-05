import {
  useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState,
  type KeyboardEvent as KbEvent,
  type ReactNode,
  type RefObject,
} from 'react';
import { createPortal } from 'react-dom';

/**
 * 自绘下拉 / 候选列表。
 *
 * 不用原生 <select> 和 <input list=datalist>：这两块的弹出层由浏览器画，
 * 尺寸、配色、行高、以及在弹窗里的层级都改不了，和这套界面对不上。
 * 这里自己画：触发按钮 + fixed 定位的浮层（portal 到 body，避免被
 * 弹窗的 overflow 裁掉），键盘操作按 listbox 的习惯来。
 */

export interface Option {
  value: string;
  label: string;
  /** 右侧灰字：来源显示接口地址 / 命令名 */
  hint?: string;
}

interface Pos {
  left: number;
  top?: number;
  bottom?: number;
  width: number;
}

/** 浮层位置：优先往下开，下面不够就翻到上面，左右夹在视口里 */
function panelPos(el: HTMLElement | null, minWidth: number): Pos | null {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const width = Math.max(r.width, minWidth);
  const below = window.innerHeight - r.bottom - 10;
  const up = below < 190 && r.top > below;
  return {
    left: Math.max(8, Math.min(r.left, window.innerWidth - width - 8)),
    top: up ? undefined : r.bottom + 4,
    bottom: up ? window.innerHeight - r.top + 4 : undefined,
    width,
  };
}

function usePanelPos(open: boolean, ref: RefObject<HTMLElement | null>, minWidth: number) {
  const [pos, setPos] = useState<Pos | null>(null);
  useLayoutEffect(() => {
    setPos(open ? panelPos(ref.current, minWidth) : null);
  }, [open, minWidth, ref]);
  return pos;
}

/** 点外面、按 Esc、页面滚动 / 缩放：一律收起
 *  Esc 在这里拦下来（capture + stopPropagation），否则设置弹窗会跟着一起关掉 */
function useDismiss(
  open: boolean,
  onClose: () => void,
  keep: RefObject<HTMLElement | null>[],
  onEscape?: () => void,
) {
  const cb = useRef(onClose);
  const esc = useRef(onEscape ?? onClose);
  const keepRef = useRef(keep);
  useEffect(() => {
    cb.current = onClose;
    esc.current = onEscape ?? onClose;
    keepRef.current = keep;
  });

  useEffect(() => {
    if (!open) return;
    const inside = (t: EventTarget | null) =>
      t instanceof Node && keepRef.current.some((r) => r.current?.contains(t));
    const onDown = (e: PointerEvent) => {
      if (!inside(e.target)) cb.current();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      esc.current();
    };
    // 页面滚动 → 浮层跟着错位，收起；但浮层自己滚（候选多的时候）不算，
    // 否则鼠标滚轮一滚就把整个面板收掉了，根本挑不到后面的项。
    const onScroll = (e: Event) => {
      if (inside(e.target)) return;
      cb.current();
    };
    document.addEventListener('pointerdown', onDown, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', onScroll);
    window.addEventListener('scroll', onScroll, true);
    return () => {
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('resize', onScroll);
      window.removeEventListener('scroll', onScroll, true);
    };
  }, [open]);
}

function Caret() {
  return (
    <span className="dd-caret" aria-hidden="true">
      <svg viewBox="0 0 10 6" width="9" height="6">
        <path d="M1 1.2 5 5l4-3.8" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </span>
  );
}

/**
 * 把高亮项滚进面板。
 * 不用 scrollIntoView：它会连带滚动祖先，而祖先一滚就被判成「页面滚动」把面板收掉。
 */
function revealActive(panel: HTMLDivElement | null) {
  if (!panel) return;
  const el = panel.querySelector<HTMLElement>('.dd-opt.on');
  if (!el) return;
  const top = el.offsetTop;
  const bottom = top + el.offsetHeight;
  if (top < panel.scrollTop) panel.scrollTop = top - 4;
  else if (bottom > panel.scrollTop + panel.clientHeight) {
    panel.scrollTop = bottom - panel.clientHeight + 4;
  }
}

function Panel({
  id, pos, panelRef, onKeyDown, children,
}: {
  id?: string;
  pos: Pos;
  panelRef: RefObject<HTMLDivElement | null>;
  onKeyDown: (e: KbEvent) => void;
  children: ReactNode;
}) {
  return createPortal(
    <div
      id={id}
      className="dd-panel"
      ref={panelRef}
      role="listbox"
      tabIndex={-1}
      style={{
        left: pos.left,
        top: pos.top,
        bottom: pos.bottom,
        width: pos.width,
      }}
      onKeyDown={onKeyDown}
    >
      {children}
    </div>,
    document.body,
  );
}

interface DropdownProps {
  value: string;
  options: Option[];
  onChange: (value: string) => void;
  disabled?: boolean;
  className?: string;
  title?: string;
  ariaLabel?: string;
  /** 浮层最小宽度：触发器窄、选项带副标题时用得上 */
  minWidth?: number;
  /** 没有选中项时显示什么 */
  placeholder?: string;
}

/** 单选下拉：按钮 + 浮层，键盘 ↑↓ 移动、Enter 选中、Esc 收起 */
export function Dropdown({
  value, options, onChange, disabled, className, title, ariaLabel,
  minWidth = 140, placeholder = '—',
}: DropdownProps) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const pos = usePanelPos(open, triggerRef, minWidth);

  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, [triggerRef, panelRef], () => {
    setOpen(false);
    triggerRef.current?.focus();
  });

  const pick = useCallback((v: string) => {
    onChange(v);
    setOpen(false);
    triggerRef.current?.focus();
  }, [onChange]);

  const show = useCallback(() => {
    const i = options.findIndex((o) => o.value === value);
    setActive(i < 0 ? 0 : i);
    setOpen(true);
  }, [options, value]);

  useEffect(() => {
    if (open) panelRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    revealActive(panelRef.current);
  }, [active, open]);

  const move = (delta: number) => {
    const n = options.length;
    if (!n) return;
    setActive((i) => (i + delta + n) % n);
  };

  const onPanelKey = (e: KbEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      const o = options[active];
      if (o) pick(o.value);
    } else if (e.key === 'Tab') close();
    else if (e.key === 'Escape') {
      e.preventDefault();
      close();
      triggerRef.current?.focus();
    }
  };

  const current = options.find((o) => o.value === value);

  return (
    <span className="dd">
      <button
        type="button"
        ref={triggerRef}
        className={`dd-trigger${className ? ` ${className}` : ''}`}
        disabled={disabled}
        title={title}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        onClick={() => (open ? close() : show())}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            show();
          } else if ((e.key === 'Enter' || e.key === ' ') && !open) {
            e.preventDefault();
            show();
          }
        }}
      >
        <span className={`dd-value${current ? '' : ' empty'}`}>
          {current?.label ?? (value || placeholder)}
        </span>
        <Caret />
      </button>

      {open && pos && (
        <Panel pos={pos} panelRef={panelRef} onKeyDown={onPanelKey}>
          {options.length === 0 && <div className="dd-empty">没有可选项</div>}
          {options.map((o, i) => (
            <button
              key={o.value || '∅'}
              type="button"
              role="option"
              aria-selected={o.value === value}
              title={o.hint}
              className={
                'dd-opt'
                + (i === active ? ' on' : '')
                + (o.value === value ? ' picked' : '')
              }
              onPointerEnter={() => setActive(i)}
              onClick={() => pick(o.value)}
            >
              <span className="dd-opt-main">{o.label}</span>
              {o.hint && <span className="dd-opt-hint">{o.hint}</span>}
            </button>
          ))}
        </Panel>
      )}
    </span>
  );
}

interface ComboProps {
  value: string;
  options: Option[];
  onChange: (value: string) => void;
  disabled?: boolean;
  className?: string;
  placeholder?: string;
  title?: string;
  ariaLabel?: string;
  minWidth?: number;
}

/**
 * 可输入 + 候选：替代 input + datalist。
 *
 * 型号不能只给候选——接口那边的型号列不全，供应商随时上新；
 * 所以输入框保持自由填写，候选只是提示，按输入内容过滤。
 */
export function ComboInput({
  value, options, onChange, disabled, className, placeholder, title, ariaLabel,
  minWidth = 190,
}: ComboProps) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  /** 刚点开 / 刚敲字时，Enter 该保留输入；用方向键挑过才算「选中了候选」 */
  const [armed, setArmed] = useState(false);
  /** null = 刚点开，显示全部候选；输入过就按内容过滤 */
  const [query, setQuery] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const pos = usePanelPos(open, inputRef, minWidth);

  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, [inputRef, panelRef]);

  const list = useMemo(() => {
    if (query === null) return options;
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter(
      (o) => o.value.toLowerCase().includes(q) || o.label.toLowerCase().includes(q),
    );
  }, [options, query]);

  const show = useCallback((q: string | null) => {
    setQuery(q);
    setActive(0);
    setArmed(false);
    setOpen(true);
  }, []);

  const pick = useCallback((v: string) => {
    onChange(v);
    setOpen(false);
    setQuery(null);
    inputRef.current?.focus();
  }, [onChange]);

  useEffect(() => {
    if (!open) return;
    revealActive(panelRef.current);
  }, [active, open]);

  const move = (delta: number) => {
    const n = list.length;
    if (!n) return;
    setArmed(true);
    setActive((i) => {
      const next = i + delta;
      return next < 0 ? n - 1 : next >= n ? 0 : next;
    });
  };

  const onPanelKey = (e: KbEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const o = armed ? list[active] : undefined;
      if (o) pick(o.value);
      else close(); // 没挑候选 = 保留手输的型号
    } else if (e.key === 'Tab' || e.key === 'Escape') close();
  };

  return (
    <span className="dd combo">
      <input
        ref={inputRef}
        type="text"
        autoComplete="off"
        className={`dd-input${className ? ` ${className}` : ''}`}
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        title={title}
        aria-label={ariaLabel}
        aria-expanded={open}
        onFocus={() => show(null)}
        onChange={(e) => {
          onChange(e.target.value);
          show(e.target.value);
        }}
        onClick={() => { if (!open) show(null); }}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (!open) show(null); else move(1);
          } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (!open) show(null); else move(-1);
          } else if (e.key === 'Enter') {
            const o = open && armed ? list[active] : undefined;
            if (o) {
              e.preventDefault();
              pick(o.value);
            } else {
              close();
            }
          } else if (e.key === 'Escape') {
            close();
          }
        }}
      />

      {open && pos && (
        <Panel pos={pos} panelRef={panelRef} onKeyDown={onPanelKey}>
          {list.length === 0 && (
            <div className="dd-empty">{value.trim() ? `「${value.trim()}」不在候选里，直接用` : '没有候选，直接填'}</div>
          )}
          {list.map((o, i) => (
            <button
              key={o.value || '∅'}
              type="button"
              role="option"
              aria-selected={o.value === value}
              title={o.hint}
              className={
                'dd-opt'
                + (i === active ? ' on' : '')
                + (o.value === value ? ' picked' : '')
              }
              onPointerEnter={() => { setActive(i); setArmed(true); }}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(o.value)}
            >
              <span className="dd-opt-main">{o.label}</span>
              {o.hint && <span className="dd-opt-hint">{o.hint}</span>}
            </button>
          ))}
        </Panel>
      )}
    </span>
  );
}
